from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Iterator

import stat
import time

from .config import DB_PATH, ensure_dirs, get_max_jobs, restrict_path
from .performance import record_timing
from .protocol_text import deduplicate

STATUSES = (
    "queued",
    "extracting",
    "transcribing",
    "analyzing",
    "review",
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
        if "calendar_json" not in columns:
            conn.execute("ALTER TABLE meetings ADD COLUMN calendar_json TEXT")
        if "segments_json" not in columns:
            conn.execute("ALTER TABLE meetings ADD COLUMN segments_json TEXT")
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
        conn.execute("""CREATE TABLE IF NOT EXISTS protocol_mail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            due_at REAL NOT NULL DEFAULT 0,
            UNIQUE(meeting_id, recipient)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_protocol_mail_due ON protocol_mail(state, due_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS protocol_mail_format (
            meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
            html TEXT NOT NULL,
            filename TEXT NOT NULL,
            docx BLOB NOT NULL
        )""")
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "auth_mode" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN auth_mode TEXT NOT NULL DEFAULT 'local'"
            )
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_hits (
                key TEXT NOT NULL,
                at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits ON rate_hits(key, at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_id ON audit_events(id)")
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS llm_settings (
            version INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            payload TEXT NOT NULL
        )""")

        from .glossary import init_schema
        init_schema(conn)
        from .term_discovery import init_schema as init_discovery
        init_discovery(conn)



def get_app_setting(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT payload FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["payload"]) if row else None


def set_app_setting(key: str, payload: dict) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO app_settings (key, payload) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                     (key, json.dumps(payload, ensure_ascii=False)))


def get_llm_settings() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT version, payload FROM llm_settings ORDER BY version DESC LIMIT 1").fetchone()
    return {**json.loads(row["payload"]), "version": row["version"]} if row else None


def save_llm_settings(payload: dict, actor: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO llm_settings (created_at, actor, payload) VALUES (?, ?, ?)",
            (utc_now(), actor, json.dumps(payload, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)


def create_meeting(meeting_id: str, title: str, filename: str, user_id: str, *, calendar_context: dict | None = None) -> dict[str, Any]:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO meetings (
                id, title, filename, status, status_message, created_at, queued_at, user_id, calendar_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(calendar_context, ensure_ascii=False) if calendar_context else None,
            ),
        )
    refresh_queue_messages()
    meeting = get_meeting(meeting_id)
    if not meeting:
        raise RuntimeError("Не удалось сохранить встречу")
    return meeting


def create_text_meeting(meeting_id: str, title: str, filename: str, user_id: str, document: dict, *, calendar_context: dict | None = None) -> dict:
    """Publish review + transcript atomically; never expose an ASR queue job."""
    from .speakers import render_transcript

    now = utc_now()
    with connect() as conn:
        conn.execute(
            """INSERT INTO meetings
               (id, title, filename, user_id, status, status_message, created_at,
                queued_at, progress, transcript, segments_json, calendar_json)
               VALUES (?, ?, ?, ?, 'review', 'Транскрипт импортирован. Проверьте спикеров', ?, ?, 85, ?, ?, ?)""",
            (meeting_id, title, filename, user_id, now, now,
             render_transcript(document), json.dumps(document, ensure_ascii=False),
             json.dumps(calendar_context, ensure_ascii=False) if calendar_context else None),
        )
    return get_meeting(meeting_id)


def update_meeting(meeting_id: str, *, connection: sqlite3.Connection | None = None, **fields: Any) -> None:
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
        "segments_json",
    }
    sets = []
    values: list[Any] = []
    for key, value in fields.items():
        if key == "result":
            key = "result_json"
            value = json.dumps(value, ensure_ascii=False) if value is not None else None
        if key == "transcript_segments":
            key = "segments_json"
            value = json.dumps(value, ensure_ascii=False) if value is not None else None
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        sets.append(f"{key} = ?")
        values.append(value)
    values.append(meeting_id)
    with (connect() if connection is None else nullcontext(connection)) as conn:
        conn.execute(f"UPDATE meetings SET {', '.join(sets)} WHERE id = ?", values)


def get_meeting(meeting_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(f"SELECT meetings.*, {_MAIL_PENDING} FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not row:
        return None
    return attach_queue_info([_serialize(row, body=True)])[0]


@contextmanager
def edit_meeting(meeting_id: str) -> Iterator[tuple[sqlite3.Connection, dict | None]]:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        yield conn, _serialize(row) if row else None


def queue_review_protocol(meeting_id: str, document: dict, *, connection: sqlite3.Connection | None = None) -> bool:
    """Save explicitly selected recipients and claim review in one transaction."""
    with (connect() if connection is None else nullcontext(connection)) as conn:
        cursor = conn.execute(
            """UPDATE meetings SET status='queued', status_message='Собираю протокол',
               error=NULL, result_json=NULL, progress=88, queued_at=?, segments_json=?
               WHERE id=? AND status='review'""",
            (utc_now(), json.dumps(document, ensure_ascii=False), meeting_id),
        )
        return cursor.rowcount == 1


_MAIL_PENDING = """CASE WHEN meetings.status = 'done' THEN EXISTS(SELECT 1 FROM protocol_mail
    WHERE meeting_id = meetings.id AND state IN ('pending', 'sending')) ELSE 0 END AS mail_pending"""


_LIST_COLUMNS = f"""
    id, title, filename, status, status_message, created_at,
    duration_seconds, language, error, progress, queued_at, user_id,
    NULL AS transcript, NULL AS result_json, {_MAIL_PENDING}
"""


def list_meetings(user_id: str | None = None, *, body: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        if body:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM meetings WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM meetings ORDER BY created_at DESC"
                ).fetchall()
        elif user_id:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM meetings WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM meetings ORDER BY created_at DESC"
            ).fetchall()
    return attach_queue_info([_serialize(row, body=body) for row in rows])


ACTIVE_STATUSES = {"queued", "extracting", "transcribing", "analyzing"}


def prune_user_meetings(user_id: str, *, connection: sqlite3.Connection | None = None) -> list[dict[str, str]]:
    with (connect() if connection is None else nullcontext(connection)) as conn:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT archive_limit FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return []
        expired = conn.execute(
            """SELECT id, title FROM (
                   SELECT id, title, status FROM meetings WHERE user_id = ? AND status IN ('done', 'error')
                   ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?
               ) AS expired WHERE status = 'error' OR NOT EXISTS (
                   SELECT 1 FROM protocol_mail WHERE meeting_id = expired.id AND state IN ('pending', 'sending')
               )""",
            (user_id, max(1, int(user["archive_limit"]))),
        ).fetchall()
        conn.executemany("DELETE FROM meetings WHERE id = ?", [(row["id"],) for row in expired])
        return [dict(row) for row in expired]


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


def queue_monitoring() -> dict[str, Any]:
    with connect() as conn:
        conn.execute("BEGIN")
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM meetings WHERE status IN ('queued', 'review') GROUP BY status"
        ).fetchall())
        oldest = conn.execute(
            "SELECT MIN(COALESCE(queued_at, created_at)) FROM meetings WHERE status = 'queued'",
        ).fetchone()[0]
        jobs = conn.execute(
            """SELECT id, status, progress FROM meetings
               WHERE status IN ('extracting', 'transcribing', 'analyzing')
               ORDER BY queued_at, id"""
        ).fetchall()
    return {
        "active": len(jobs),
        "waiting": counts.get("queued", 0),
        "review": counts.get("review", 0),
        "limit": get_max_jobs(),
        "oldest_wait_seconds": _seconds_since(oldest) if oldest else None,
        "jobs": [dict(job) for job in jobs],
    }


def _seconds_since(stamp: str) -> float:
    try:
        value = datetime.fromisoformat(stamp)
        return max(0, time.time() - value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
    except ValueError:
        return 0


def attach_queue_info(meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any(item.get("status") == "queued" for item in meetings):
        for item in meetings:
            item["queue_ahead"] = 0
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
            item["status_message"] = queue_wait_message(item["queue_ahead"])
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
            SELECT id, user_id, COALESCE(queued_at, created_at) AS entered FROM meetings
            WHERE status = 'queued'
            ORDER BY COALESCE(queued_at, created_at) ASC, id ASC
            """
        ).fetchall()
        for row in queued:
            if row["user_id"] and row["user_id"] in busy_users:
                continue
            selected = row
            chosen = selected["id"]
            break
        if not chosen and queued:
            selected = queued[0]
            chosen = selected["id"]
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
        record_timing("queue_wait", _seconds_since(selected["entered"]))
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


def rate_allow(key: str, window: float, limit: int) -> bool:
    now = time.time()
    cutoff = now - float(window)
    cap = max(1, int(limit))
    with connect() as conn:
        conn.execute("DELETE FROM rate_hits WHERE at < ?", (cutoff,))
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM rate_hits WHERE key = ? AND at >= ?",
            (key, cutoff),
        ).fetchone()["n"]
        if int(n) >= cap:
            return False
        conn.execute("INSERT INTO rate_hits (key, at) VALUES (?, ?)", (key, now))
    return True


def delete_user(user_id: str) -> list[str] | None:
    with connect() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return None
        rows = conn.execute(
            "SELECT id, status FROM meetings WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        if any(row["status"] in {"extracting", "transcribing", "analyzing"} for row in rows):
            raise ValueError("active")
        ids = [row["id"] for row in rows]
        conn.execute("DELETE FROM meetings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return ids


def delete_meeting(meeting_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        return cursor.rowcount > 0


def _serialize(row: sqlite3.Row, *, body: bool = True) -> dict[str, Any]:
    data = dict(row)
    raw = data.pop("result_json", None)
    raw_segments = data.pop("segments_json", None)
    raw_calendar = data.pop("calendar_json", None)
    if body:
        data["calendar_context"] = json.loads(raw_calendar) if raw_calendar else None
    data["progress"] = int(data.get("progress") or 0)
    if "mail_pending" in data:
        data["mail_pending"] = bool(data["mail_pending"])
    data["user_id"] = data.get("user_id")
    if body:
        result = json.loads(raw) if raw else None
        data["result"] = deduplicate(result) if isinstance(result, dict) else result
        parsed = json.loads(raw_segments) if raw_segments else {}
        data["speakers"] = parsed.get("speakers") or []
        data["segments"] = parsed.get("segments") or []
        data["diarization_note"] = parsed.get("note")
        data["name_review"] = parsed.get("name_review")
        data["corporate_context"] = parsed.get("corporate_context")
        data["source_type"] = parsed.get("source_type", "audio")
    else:
        data.pop("transcript", None)
        data["transcript"] = None
        data["result"] = None
        data["speakers"] = None
        data["segments"] = None
        data["diarization_note"] = None
    return data


def _serialize_user(row: sqlite3.Row, meeting_count: int | None = None) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "email": row["email"],
        "archive_limit": int(row["archive_limit"]),
        "auth_mode": (row["auth_mode"] if "auth_mode" in row.keys() else None) or "local",
        "created_at": row["created_at"],
    }
    if meeting_count is not None:
        data["meeting_count"] = meeting_count
    return data


def create_user(
    email: str,
    password_hash: str,
    archive_limit: int = 5,
    auth_mode: str = "local",
) -> dict[str, Any]:
    user_id = uuid.uuid4().hex
    created = utc_now()
    limit = max(1, min(100, int(archive_limit)))
    mode = auth_mode if auth_mode in {"local", "ldap"} else "local"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, archive_limit, created_at, auth_mode)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, email, password_hash, limit, created, mode),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _serialize_user(row, 0)


def search_user_emails(query: str, limit: int = 40) -> list[dict[str, str]]:
    """Bounded suggestions without loading accounts or counting their meetings."""
    needle = query.strip().lower()
    with connect() as conn:
        rows = conn.execute(
            "SELECT email FROM users WHERE instr(lower(email), ?) > 0 OR ? = '' "
            "ORDER BY CASE WHEN lower(email) = ? THEN 0 "
            "WHEN instr(lower(email), ?) = 1 THEN 1 ELSE 2 END, lower(email) LIMIT ?",
            (needle, needle, needle, needle, max(1, min(40, limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        counts = {
            item["user_id"]: item["n"]
            for item in conn.execute(
                "SELECT user_id, COUNT(*) AS n FROM meetings WHERE status IN ('done', 'error') GROUP BY user_id"
            )
        }
    return [_serialize_user(row, counts.get(row["id"], 0)) for row in rows]


def get_user(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM meetings WHERE user_id = ? AND status IN ('done', 'error')", (user_id,)
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
    return {
        "id": row["id"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "auth_mode": (row["auth_mode"] if "auth_mode" in row.keys() else None) or "local",
    }


def update_user(user_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"password_hash", "archive_limit", "auth_mode"}
    sets = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        if key == "archive_limit":
            value = max(1, min(100, int(value)))
        if key == "auth_mode" and value not in {"local", "ldap"}:
            raise ValueError("Unknown auth mode")
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return get_user(user_id)
    values.append(user_id)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values)
    return get_user(user_id)


def session_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
            (session_id(token), kind, username, user_id, email, expires),
        )


def get_session_row(token: str) -> dict[str, Any] | None:
    now = time.time()
    stored = session_id(token)
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (stored,)).fetchone()
        if row is None and token != stored:
            row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    if float(row["expires"]) < now:
        drop_session_token(token)
        return None
    return dict(row)


def drop_session_token(token: str) -> None:
    stored = session_id(token)
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ? OR token = ?", (stored, token))


def drop_sessions_for_user(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


AUDIT_KEEP = 2000


def _audit_text(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def record_audit(actor: str, action: str, target: str = "", detail: str = "") -> None:
    action_text = _audit_text(action, 80)
    if not action_text:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_events (at, actor, action, target, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                _audit_text(actor, 200) or "система",
                action_text,
                _audit_text(target, 200),
                _audit_text(detail, 500),
            ),
        )
        conn.execute(
            """
            DELETE FROM audit_events
            WHERE id NOT IN (
                SELECT id FROM audit_events ORDER BY id DESC LIMIT ?
            )
            """,
            (AUDIT_KEEP,),
        )


def list_audit(limit: int = 200) -> list[dict[str, Any]]:
    size = max(1, min(int(limit), AUDIT_KEEP))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, at, actor, action, target, detail
            FROM audit_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (size,),
        ).fetchall()
    return [dict(row) for row in rows]
