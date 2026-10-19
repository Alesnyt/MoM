"""SQLite upgrade compatibility: old archives remain readable without a data reset."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend import config, store


def test_existing_legacy_database_survives_stage1(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "mom.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")

    old_result = {
        "title": "Исторический протокол",
        "summary": "Архивная запись не должна исчезнуть.",
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "participants": [],
        "mom": {"agenda": [], "next_meeting": None},
    }
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
                status TEXT NOT NULL, status_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, duration_seconds REAL, language TEXT,
                transcript TEXT, result_json TEXT, error TEXT,
                progress INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, archive_limit INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO meetings(id,title,filename,status,created_at,transcript,result_json) "
            "VALUES (?,?,?,?,?,?,?)",
            ("legacy-1", "Старый протокол", "recording.webm", "done",
             "2026-08-01T10:00:00+00:00", "Спикер 1: старый текст",
             json.dumps(old_result, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO users(id,email,password_hash,created_at) VALUES (?,?,?,?)",
            ("legacy-user", "legacy@example.com", "old-hash", "2026-08-01T10:00:00+00:00"),
        )

    store.init_db()
    item = store.get_meeting("legacy-1")
    assert item is not None
    assert item["title"] == "Старый протокол"
    assert item["transcript"] == "Спикер 1: старый текст"
    assert item["result"] == old_result
    assert item["status"] == "done"
    assert item["user_id"] is None
    users = store.list_users()
    assert len(users) == 1
    assert users[0]["email"] == "legacy@example.com"
    assert users[0]["auth_mode"] == "local"
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(meetings)")}
        assert {"user_id", "queued_at", "segments_json"} <= columns
