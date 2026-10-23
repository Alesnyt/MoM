from __future__ import annotations

from pathlib import Path

import pytest

from backend import auth, config, store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    store.init_db()
    return path


def _user() -> str:
    user = store.create_user("a@example.com", auth.hash_password("secret-pass"), 5)
    return user["id"]


def test_stop_and_restart_apply_once(db: Path) -> None:
    from backend import control
    from backend.pipeline import _Progress

    user_id = _user()
    store.create_meeting("job1", "Запись", "a.webm", user_id)
    store.update_meeting("job1", status="transcribing", transcript="черновик", progress=40)
    control.request_stop("job1", restart=False)
    with pytest.raises(control.ProcessingStopped):
        _Progress("job1").set(50, "ещё идёт", status="transcribing")
    assert control.finish("job1") == "stop"
    assert control.finish("job1") == ""
    stopped = store.get_meeting("job1")
    assert stopped and stopped["status"] == "error"
    assert stopped["error"] == "Обработка остановлена"
    assert stopped["progress"] == 0

    control.release("job1")
    store.update_meeting("job1", status="transcribing", transcript="черновик", progress=20)
    control.request_stop("job1", restart=True)
    assert control.finish("job1") == "restart"
    again = store.get_meeting("job1")
    assert again and again["status"] == "queued"
    assert not (again.get("transcript") or "").strip()
    control.release("job1")


def test_review_is_not_claimed(db: Path) -> None:
    user_id = _user()
    store.create_meeting("rev1", "проверка", "a.webm", user_id)
    store.update_meeting("rev1", status="review", transcript="Спикер 1: привет")
    assert store.claim_next_meeting(1) is None
    meeting = store.get_meeting("rev1")
    assert meeting and meeting["status"] == "review"


def test_claim_fifo(db: Path) -> None:
    user_id = _user()
    store.create_meeting("aaa", "первая", "a.webm", user_id)
    store.create_meeting("bbb", "вторая", "b.webm", user_id)
    assert store.claim_next_meeting(1) == "aaa"
    assert store.claim_next_meeting(1) is None
    first = store.get_meeting("aaa")
    assert first and first["status"] == "extracting"
    waiting = store.get_meeting("bbb")
    assert waiting and waiting["status"] == "queued"


def test_claim_prefers_other_users(db: Path) -> None:
    first = store.create_user("a@example.com", auth.hash_password("secret-pass"), 5)["id"]
    second = store.create_user("b@example.com", auth.hash_password("secret-pass"), 5)["id"]
    store.create_meeting("aaa", "первая", "a.webm", first)
    store.create_meeting("bbb", "вторая", "b.webm", second)
    assert store.claim_next_meeting(2) == "aaa"
    assert store.claim_next_meeting(2) == "bbb"


def test_queue_position_counts_other_users_and_updates_without_worker_refresh(db: Path) -> None:
    first = _user()
    second = store.create_user("b@example.com", auth.hash_password("secret-pass"), 5)["id"]
    for index, (meeting_id, owner) in enumerate((("active", first), ("earlier", first), ("mine", second))):
        store.create_meeting(meeting_id, meeting_id, "recording.webm", owner)
        store.update_meeting(meeting_id, queued_at=f"2026-10-22T10:00:0{index}+00:00")
    store.update_meeting("active", status="transcribing")

    rows = store.list_meetings(second)
    assert [row["id"] for row in rows] == ["mine"]
    assert rows[0]["queue_ahead"] == 2
    assert rows[0]["status_message"] == "В очереди, впереди 2 встречи"

    # Responses stay current even before the background worker refreshes stored messages.
    store.update_meeting("active", status="review")
    meeting = store.get_meeting("mine")
    assert meeting["queue_ahead"] == 1
    assert meeting["status_message"] == "В очереди, впереди 1 встреча"
    store.update_meeting("earlier", status="error")
    meeting = store.get_meeting("mine")
    assert meeting["queue_ahead"] == 0
    assert meeting["status_message"] == "Следующая в очереди"
    assert store.claim_next_meeting(1) == "mine"
    meeting = store.get_meeting("mine")
    assert meeting["status"] == "extracting"
    assert meeting["queue_ahead"] == 0
    assert meeting["status_message"] == "Начинаю обработку"


def test_requeue_interrupted(db: Path) -> None:
    user_id = _user()
    store.create_meeting("aaa", "первая", "a.webm", user_id)
    store.update_meeting("aaa", status="transcribing", status_message="в работе")
    count = store.requeue_interrupted_meetings()
    assert count == 1
    meeting = store.get_meeting("aaa")
    assert meeting and meeting["status"] == "queued"


def test_list_meetings_omits_body(db: Path) -> None:
    user_id = _user()
    store.create_meeting("aaa", "первая", "a.webm", user_id)
    store.update_meeting("aaa", transcript="секретный текст", result={"title": "X", "summary": "y"})
    rows = store.list_meetings(user_id, body=False)
    assert rows[0]["transcript"] is None
    assert rows[0]["result"] is None
    full = store.get_meeting("aaa")
    assert full and full["transcript"] == "секретный текст"
    assert full["result"]["title"] == "X"


def test_rate_allow_persists(db: Path) -> None:
    assert store.rate_allow("login:1.1.1.1", window=60, limit=2)
    assert store.rate_allow("login:1.1.1.1", window=60, limit=2)
    assert store.rate_allow("login:1.1.1.1", window=60, limit=2) is False


def test_queued_stop_does_not_poison_future_restart(db: Path) -> None:
    from backend import control

    user_id = _user()
    store.create_meeting("queued1", "Очередь", "queued.webm", user_id)
    control.request_stop("queued1", restart=False)
    assert control.cancel_job("queued1") is False
    assert control.finish("queued1") == "stop"
    assert not control.is_stopped("queued1")
    store.update_meeting("queued1", status="queued")
    assert store.claim_next_meeting(1) == "queued1"
    control.release("queued1")


def test_queued_restart_clears_cancellation_flags(db: Path) -> None:
    from backend import control

    user_id = _user()
    store.create_meeting("queued2", "Очередь", "queued.webm", user_id)
    control.request_stop("queued2", restart=True)
    assert control.finish("queued2") == "restart"
    assert not control.is_stopped("queued2")
    assert store.claim_next_meeting(1) == "queued2"
    control.release("queued2")


def test_cancel_job_does_not_cancel_background_thread(db: Path) -> None:
    from backend import control

    class RunningTask:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    task = RunningTask()
    control.bind_job("inflight", task)
    try:
        control.request_stop("inflight", restart=False)
        assert control.cancel_job("inflight") is True
        assert task.cancelled is False
        assert control.is_stopped("inflight")
    finally:
        control.release("inflight")
