from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import audio, auth, config, keys, mail, store, worker
from backend.logctx import meeting_scope
from backend.main import app


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", tmp_path / "whisper")
    monkeypatch.setattr(store, "DB_PATH", path)
    store.init_db()
    return path


def test_audit_keeps_short_text_newest_first(db: Path) -> None:
    store.record_audit("boss", "user.create", "a@example.com", "первая\nстрока")
    store.record_audit("boss", "ldap.save", "ldap", "x" * 800)
    rows = store.list_audit()
    assert [row["action"] for row in rows] == ["ldap.save", "user.create"]
    assert "\n" not in rows[1]["detail"]
    assert rows[1]["detail"] == "первая строка"
    assert len(rows[0]["detail"]) == 500
    assert store.record_audit("", "") is None


def test_meeting_id_on_log_and_in_thread() -> None:
    logger = logging.getLogger("mom.asr.test")
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Grab()
    logger.addHandler(handler)
    formatter = logging.Formatter("%(levelname)s %(name)s [%(meeting)s] %(message)s")
    try:
        with meeting_scope("meet42"):
            logger.info("шаг: расшифровка model=gigaam")

            def inside() -> None:
                logger.info("whisper size=small file=meet42.mp3")

            asyncio.run(_in_thread(inside))
        logger.info("вне встречи")
    finally:
        logger.removeHandler(handler)

    line = formatter.format(records[0])
    assert "[meet42]" in line
    assert "шаг: расшифровка" in line
    threaded = formatter.format(records[1])
    assert "[meet42]" in threaded
    assert formatter.format(records[2]).endswith("[-] вне встречи")


async def _in_thread(fn) -> None:
    with meeting_scope("meet42"):
        await asyncio.to_thread(fn)


def test_ffmpeg_log_names_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = "1.5"
        stderr = ""

    monkeypatch.setattr(audio.subprocess, "run", lambda *args, **kwargs: _Result())
    logger = logging.getLogger("mom.audio")
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Grab()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(meeting)s] %(message)s")
    try:
        with meeting_scope("abc123"):
            audio._run(["ffmpeg", "-i", "in.webm", "/data/audio/abc123.mp3"])
    finally:
        logger.removeHandler(handler)
    line = formatter.format(records[-1])
    assert line == "[abc123] ffmpeg → abc123.mp3"


def test_admin_journal_hides_secrets(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.main as main

    monkeypatch.setattr(main, "UPLOAD_DIR", config.UPLOAD_DIR)
    monkeypatch.setattr(main, "AUDIO_DIR", config.AUDIO_DIR)
    monkeypatch.setattr(config, "migrate_token_plan_asr", lambda: None)

    async def _no_key(*args, **kwargs):
        return None

    monkeypatch.setattr(keys, "verify_key", _no_key)

    async def _idle(stop: asyncio.Event) -> None:
        await stop.wait()

    monkeypatch.setattr(worker, "run_worker", _idle)
    monkeypatch.setattr(config, "set_ldap", lambda **kwargs: None)
    monkeypatch.setattr(main, "open_or_save_eml", lambda meeting: {"mode": "saved"})

    with TestClient(app) as client:
        assert client.get("/api/admin/audit").status_code == 401
        admin = "admin-token"
        store.put_session(admin, "admin", username="boss", expires=time.time() + 3600)
        client.cookies.set(auth.COOKIE_NAME, admin)

        created = client.post(
            "/api/admin/users",
            json={"email": "a@example.com", "archive_limit": 5, "auth_mode": "local"},
        )
        assert created.status_code == 200, created.text
        password = created.json()["password"]
        assert password

        saved = client.post(
            "/api/settings/ldap",
            json={
                "enabled": True,
                "url": "ldaps://ldap.example.com",
                "bind_dn": "cn=svc,dc=example,dc=com",
                "bind_password": "super-secret-bind",
                "base_dn": "ou=people,dc=example,dc=com",
                "user_filter": "(mail={username})",
                "starttls": False,
                "tls_verify": True,
                "email_attr": "mail",
            },
        )
        assert saved.status_code == 200, saved.text

        user_id = created.json()["user"]["id"]
        token = "user-token"
        store.put_session(token, "user", user_id=user_id, email="a@example.com", expires=time.time() + 3600)
        store.create_meeting("meet1", "Планёрка", "plan.webm", user_id)
        store.update_meeting(
            "meet1",
            status="done",
            transcript="[00:00] Спикер 1: привет",
            transcript_segments={
                "speakers": [{"id": "S1", "name": "Спикер 1"}, {"id": "S2", "name": "Спикер 2"}],
                "segments": [
                    {"start": 0, "end": 1, "text": "привет", "speaker": "S1"},
                    {"start": 1, "end": 2, "text": "да", "speaker": "S2"},
                ],
            },
            result={"title": "Планёрка", "summary": "ок"},
        )
        client.cookies.set(auth.USER_COOKIE, token)
        renamed = client.patch("/api/meetings/meet1/speakers/S1", json={"name": "Анна"})
        assert renamed.status_code == 200, renamed.text
        mailed = client.post("/api/meetings/meet1/email")
        assert mailed.status_code == 200, mailed.text
        moved = client.patch(
            "/api/meetings/meet1/segments",
            json={"speaker_id": "S2", "segment_indexes": [0]},
        )
        assert moved.status_code == 200, moved.text
        store.update_meeting("meet1", status="done")
        deleted = client.delete("/api/meetings/meet1")
        assert deleted.status_code == 200, deleted.text

        reset = client.post(f"/api/admin/users/{user_id}/reset-password")
        assert reset.status_code == 200, reset.text
        fresh = reset.json()["password"]

        journal = client.get("/api/admin/audit").json()

    blob = " ".join(f"{row['actor']} {row['action']} {row['target']} {row['detail']}" for row in journal)
    assert password not in blob
    assert fresh not in blob
    assert "super-secret-bind" not in blob
    actions = [row["action"] for row in journal]
    assert actions[0] == "user.password"
    assert "user.create" in actions
    assert "ldap.save" in actions
    assert "speaker.rename" in actions
    assert "speaker.reassign" in actions
    assert "email.open" in actions
    assert "meeting.delete" in actions
    renamed_row = next(row for row in journal if row["action"] == "speaker.rename")
    assert renamed_row["detail"] == "Спикер 1 → Анна"
    assert renamed_row["target"] == "meet1"
    ldap_row = next(row for row in journal if row["action"] == "ldap.save")
    assert "ldaps://ldap.example.com" in ldap_row["detail"]
    assert ldap_row["actor"] == "boss"


def test_auto_mail_is_audited(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = store.create_user("a@example.com", auth.hash_password("secret-pass"), 5)
    store.create_meeting("meet1", "Планёрка", "plan.webm", user["id"])
    store.update_meeting("meet1", status="done", result={"title": "Планёрка", "summary": "ок"})
    monkeypatch.setattr(config, "smtp_configured", lambda: True)
    monkeypatch.setattr(config, "get_public_base_url", lambda: "")
    monkeypatch.setattr(mail, "send_mail", lambda *args, **kwargs: None)
    meeting = store.get_meeting("meet1")
    assert meeting
    mail.notify_meeting_done(meeting)
    rows = store.list_audit()
    assert rows[0]["action"] == "email.send"
    assert rows[0]["actor"] == "a@example.com"
    assert rows[0]["detail"] == "автоматически"
    assert "ок" not in rows[0]["detail"]
