from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DB_PATH, ensure_dirs

STATUSES = (
    "queued",
    "extracting",
    "transcribing",
    "analyzing",
    "done",
    "error",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                status_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                duration_seconds REAL,
                language TEXT,
                transcript TEXT,
                result_json TEXT,
                error TEXT,
                progress INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(meetings)").fetchall()}
        if "progress" not in columns:
            conn.execute("ALTER TABLE meetings ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
        if "user_id" not in columns:
            conn.execute("ALTER TABLE meetings ADD COLUMN user_id TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                archive_limit INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_user ON meetings(user_id, created_at)")



def create_meeting(meeting_id: str, title: str, filename: str, user_id: str) -> dict[str, Any]:
    row = {
        "id": meeting_id,
        "title": title,
        "filename": filename,
        "status": "queued",
        "status_message": "Файл принят, обработка в очереди",
        "created_at": utc_now(),
        "duration_seconds": None,
        "language": None,
        "transcript": None,
        "result": None,
        "error": None,
        "progress": 0,
        "user_id": user_id,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO meetings (
                id, title, filename, status, status_message, created_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["title"],
                row["filename"],
                row["status"],
                row["status_message"],
                row["created_at"],
                user_id,
            ),
        )
    return row


def update_meeting(meeting_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "title",
        "status",
        "status_message",
        "duration_seconds",
        "language",
        "transcript",
        "result_json",
        "error",
        "progress",
    }
    sets = []
    values: list[Any] = []
    for key, value in fields.items():
        if key == "result":
            key = "result_json"
            value = json.dumps(value, ensure_ascii=False) if value is not None else None
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        sets.append(f"{key} = ?")
        values.append(value)
    values.append(meeting_id)
    with connect() as conn:
        conn.execute(f"UPDATE meetings SET {', '.join(sets)} WHERE id = ?", values)


def get_meeting(meeting_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return _serialize(row) if row else None


def list_meetings(user_id: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM meetings WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM meetings ORDER BY created_at DESC"
            ).fetchall()
    return [_serialize(row) for row in rows]


def prune_user_meetings(user_id: str) -> list[str]:
    user = get_user(user_id)
    if not user:
        return []
    limit = max(1, int(user["archive_limit"]))
    meetings = list_meetings(user_id)
    extra = meetings[limit:]
    removed: list[str] = []
    for item in extra:
        if delete_meeting(item["id"]):
            removed.append(item["id"])
    return removed


def delete_meeting(meeting_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        return cursor.rowcount > 0


def _serialize(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    raw = data.pop("result_json")
    data["result"] = json.loads(raw) if raw else None
    data["progress"] = int(data.get("progress") or 0)
    data["user_id"] = data.get("user_id")
    return data


def _serialize_user(row: sqlite3.Row, meeting_count: int | None = None) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "email": row["email"],
        "archive_limit": int(row["archive_limit"]),
        "created_at": row["created_at"],
    }
    if meeting_count is not None:
        data["meeting_count"] = meeting_count
    return data


def create_user(email: str, password_hash: str, archive_limit: int = 5) -> dict[str, Any]:
    user_id = uuid.uuid4().hex
    created = utc_now()
    limit = max(1, min(100, int(archive_limit)))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, archive_limit, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, email, password_hash, limit, created),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _serialize_user(row, 0)


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        counts = {
            item["user_id"]: item["n"]
            for item in conn.execute(
                "SELECT user_id, COUNT(*) AS n FROM meetings WHERE user_id IS NOT NULL GROUP BY user_id"
            )
        }
    return [_serialize_user(row, counts.get(row["id"], 0)) for row in rows]


def get_user(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM meetings WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
    return _serialize_user(row, count)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return None
    return get_user(row["id"])


def get_user_auth(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "email": row["email"], "password_hash": row["password_hash"]}


def update_user(user_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"password_hash", "archive_limit"}
    sets = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        if key == "archive_limit":
            value = max(1, min(100, int(value)))
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return get_user(user_id)
    values.append(user_id)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values)
    return get_user(user_id)
