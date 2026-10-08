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
