from __future__ import annotations

import threading

from . import store

# Writers acquire this before opening a meeting transaction. Publishers already
# holding SQLite's write lock read pending() without acquiring this lock.
command_lock = threading.RLock()
_stop: set[str] = set()
_restart: set[str] = set()
_sealed: set[str] = set()
_applied: set[str] = set()
_jobs: dict[str, object] = {}


class ProcessingStopped(Exception):
    """The user stopped this meeting while a step was still running."""


def request_stop(meeting_id: str, *, restart: bool) -> None:
    _stop.add(meeting_id)
    _sealed.add(meeting_id)
    if restart:
        _restart.add(meeting_id)
    else:
        _restart.discard(meeting_id)


def is_stopped(meeting_id: str) -> bool:
    return meeting_id in _stop or meeting_id in _sealed


def pending(connection, meeting_id: str) -> bool:
    return is_stopped(meeting_id) or connection.execute(
        "SELECT 1 FROM processing_requests WHERE meeting_id=?", (meeting_id,)
    ).fetchone() is not None


def bind_job(meeting_id: str, task: object) -> None:
    _jobs[meeting_id] = task


def job_running(meeting_id: str) -> bool:
    task = _jobs.get(meeting_id)
    return task is not None and not getattr(task, "done", lambda: True)()


def cancel_job(meeting_id: str) -> bool:
    """Request cooperative cancellation without orphaning asyncio.to_thread work.

    Cancelling the asyncio task does not stop ffmpeg/ASR already executing in
    a thread. Keep the worker slot occupied until the old work actually exits,
    so a restarted job cannot race against its predecessor.
    """
    return job_running(meeting_id)


def release(meeting_id: str) -> None:
    with command_lock:
        _jobs.pop(meeting_id, None)
        _sealed.discard(meeting_id)
        _stop.discard(meeting_id)
        _restart.discard(meeting_id)
        _applied.discard(meeting_id)


def finish(meeting_id: str) -> str:
    """Apply a requested stop once. Returns 'stop', 'restart', or ''."""
    with command_lock:
        return _finish_locked(meeting_id)


def _finish_locked(meeting_id: str) -> str:
    if meeting_id in _applied:
        return ""
    restart = meeting_id in _restart
    stopped = meeting_id in _stop or meeting_id in _sealed
    if not stopped and not restart:
        return ""
    action = "restart" if restart else "stop"
    with store.edit_meeting(meeting_id) as (conn, meeting):
        if meeting:
            saved = conn.execute("SELECT action FROM processing_requests WHERE meeting_id=?", (meeting_id,)).fetchone()
            if saved:
                action = saved[0]
            if saved or action == "restart" or meeting["status"] != "done":
                store.apply_processing_request(conn, meeting, action)
    _applied.add(meeting_id)
    _stop.discard(meeting_id)
    _restart.discard(meeting_id)
    if meeting_id not in _jobs:
        release(meeting_id)
    if action == "restart":
        store.refresh_queue_messages()
    return action
