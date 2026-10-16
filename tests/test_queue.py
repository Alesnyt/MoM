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
