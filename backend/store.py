from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import stat
import time

from .config import DB_PATH, ensure_dirs, get_max_jobs, restrict_path

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
    restrict_path(DB_PATH, stat.S_IRUSR | stat.S_IWUSR)


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
        if "queued_at" not in columns:
            conn.execute("ALTER TABLE meetings ADD COLUMN queued_at TEXT")
            conn.execute("UPDATE meetings SET queued_at = created_at WHERE queued_at IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_queue ON meetings(status, queued_at)")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                username TEXT,
                user_id TEXT,
                email TEXT,
                expires REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires)")



def create_meeting(meeting_id: str, title: str, filename: str, user_id: str) -> dict[str, Any]:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO meetings (
                id, title, filename, status, status_message, created_at, queued_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                title,
                filename,
                "queued",
                "Файл принят, обработка в очереди",
                now,
                now,
                user_id,
            ),
        )
    refresh_queue_messages()
    meeting = get_meeting(meeting_id)
    if not meeting:
        raise RuntimeError("Не удалось сохранить встречу")
    return meeting


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
        "queued_at",
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
    if not row:
        return None
    return attach_queue_info([_serialize(row)])[0]


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
    return attach_queue_info([_serialize(row) for row in rows])


ACTIVE_STATUSES = {"queued", "extracting", "transcribing", "analyzing"}


def prune_user_meetings(user_id: str) -> list[str]:
    user = get_user(user_id)
    if not user:
        return []
    limit = max(1, int(user["archive_limit"]))
    meetings = list_meetings(user_id)
    over = len(meetings) - limit
    removed: list[str] = []
    for item in reversed(meetings):
        if over <= 0:
            break
        if item.get("status") in ACTIVE_STATUSES:
            continue
        if delete_meeting(item["id"]):
            removed.append(item["id"])
            over -= 1
    return removed


def queue_wait_message(ahead: int) -> str:
    if ahead <= 0:
        return "Следующая в очереди"
    return f"В очереди, впереди {ahead} {_meetings_word(ahead)}"


def _meetings_word(count: int) -> str:
    n = abs(count) % 100
    if 11 <= n <= 14:
        return "встреч"
    last = n % 10
    if last == 1:
        return "встреча"
    if 2 <= last <= 4:
        return "встречи"
    return "встреч"


def queue_stats() -> dict[str, int]:
    with connect() as conn:
        active = conn.execute(
            """
            SELECT COUNT(*) AS n FROM meetings
            WHERE status IN ('extracting', 'transcribing', 'analyzing')
            """
        ).fetchone()["n"]
        waiting = conn.execute(
            "SELECT COUNT(*) AS n FROM meetings WHERE status = 'queued'"
        ).fetchone()["n"]
    return {"active": int(active), "waiting": int(waiting), "limit": get_max_jobs()}


def attach_queue_info(meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not meetings:
        return meetings
    with connect() as conn:
        active = conn.execute(
            """
            SELECT COUNT(*) AS n FROM meetings
            WHERE status IN ('extracting', 'transcribing', 'analyzing')
            """
        ).fetchone()["n"]
        queued = conn.execute(
            """
            SELECT id FROM meetings
            WHERE status = 'queued'
            ORDER BY COALESCE(queued_at, created_at) ASC, id ASC
            """
        ).fetchall()
    order = {row["id"]: index for index, row in enumerate(queued)}
    active_n = int(active)
    for item in meetings:
        if item.get("status") == "queued" and item["id"] in order:
            item["queue_ahead"] = active_n + order[item["id"]]
        else:
            item["queue_ahead"] = 0
    return meetings


def refresh_queue_messages() -> None:
    with connect() as conn:
        active = conn.execute(
            """
            SELECT COUNT(*) AS n FROM meetings
            WHERE status IN ('extracting', 'transcribing', 'analyzing')
            """
        ).fetchone()["n"]
        rows = conn.execute(
            """
            SELECT id, status_message FROM meetings
            WHERE status = 'queued'
            ORDER BY COALESCE(queued_at, created_at) ASC, id ASC
            """
        ).fetchall()
        for index, row in enumerate(rows):
            message = queue_wait_message(int(active) + index)
            if row["status_message"] != message:
                conn.execute(
                    "UPDATE meetings SET status_message = ? WHERE id = ? AND status = 'queued'",
                    (message, row["id"]),
                )


def claim_next_meeting(max_jobs: int) -> str | None:
    limit = max(1, int(max_jobs))
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    chosen: str | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            """
            SELECT id, user_id FROM meetings
            WHERE status IN ('extracting', 'transcribing', 'analyzing')
            """
        ).fetchall()
        if len(running) >= limit:
            conn.execute("COMMIT")
            return None
        busy_users = {row["user_id"] for row in running if row["user_id"]}
        queued = conn.execute(
            """
            SELECT id, user_id FROM meetings
            WHERE status = 'queued'
            ORDER BY COALESCE(queued_at, created_at) ASC, id ASC
            """
        ).fetchall()
        for row in queued:
            if row["user_id"] and row["user_id"] in busy_users:
                continue
            chosen = row["id"]
            break
        if not chosen and queued:
            chosen = queued[0]["id"]
        if not chosen:
            conn.execute("COMMIT")
            return None
        cursor = conn.execute(
            """
            UPDATE meetings
            SET status = 'extracting',
                status_message = 'Начинаю обработку',
                progress = 1,
                error = NULL
            WHERE id = ? AND status = 'queued'
            """,
            (chosen,),
        )
        if cursor.rowcount != 1:
            conn.execute("COMMIT")
            return None
        conn.execute("COMMIT")
        return chosen
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
        restrict_path(DB_PATH, stat.S_IRUSR | stat.S_IWUSR)


def requeue_interrupted_meetings() -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE meetings
            SET status = 'queued',
                status_message = 'В очереди после перезапуска сервера',
                progress = 0,
                error = NULL
            WHERE status IN ('extracting', 'transcribing', 'analyzing')
            """
        )
        count = cursor.rowcount
    refresh_queue_messages()
    return count


def fail_interrupted_meetings() -> int:
    return requeue_interrupted_meetings()


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


def put_session(
    token: str,
    kind: str,
    *,
    expires: float,
    username: str | None = None,
    user_id: str | None = None,
    email: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions (token, kind, username, user_id, email, expires)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, kind, username, user_id, email, expires),
        )


def get_session_row(token: str) -> dict[str, Any] | None:
    now = time.time()
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    if float(row["expires"]) < now:
        drop_session_token(token)
        return None
    return dict(row)


def drop_session_token(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def drop_sessions_for_user(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
