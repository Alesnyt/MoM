from __future__ import annotations

from . import store

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


def bind_job(meeting_id: str, task: object) -> None:
    _jobs[meeting_id] = task


def cancel_job(meeting_id: str) -> bool:
    """Request cooperative cancellation without orphaning asyncio.to_thread work.

    Cancelling the asyncio task does not stop ffmpeg/ASR already executing in
    a thread. Keep the worker slot occupied until the old work actually exits,
    so a restarted job cannot race against its predecessor.
    """
    task = _jobs.get(meeting_id)
    return task is not None and not getattr(task, "done", lambda: True)()


def release(meeting_id: str) -> None:
    _jobs.pop(meeting_id, None)
    _sealed.discard(meeting_id)
    _stop.discard(meeting_id)
    _restart.discard(meeting_id)
    _applied.discard(meeting_id)


def finish(meeting_id: str) -> str:
    """Apply a requested stop once. Returns 'stop', 'restart', or ''."""
    if meeting_id in _applied:
        return ""
    restart = meeting_id in _restart
    stopped = meeting_id in _stop or meeting_id in _sealed
    if not stopped and not restart:
        return ""
    _applied.add(meeting_id)
    _stop.discard(meeting_id)
    _restart.discard(meeting_id)
    if restart:
        store.update_meeting(
            meeting_id,
            status="queued",
            status_message="Повторная обработка в очереди",
            error=None,
            result=None,
            transcript=None,
            transcript_segments=None,
            language=None,
            progress=0,
            queued_at=store.utc_now(),
        )
        store.refresh_queue_messages()
        # Queued jobs have no running task to release their cancellation flags.
        # Active jobs are cleaned up in worker._run_job.finally.
        if meeting_id not in _jobs:
            release(meeting_id)
        return "restart"
    meeting = store.get_meeting(meeting_id)
    if meeting and meeting.get("status") != "done":
        store.update_meeting(
            meeting_id,
            status="error",
            status_message="Остановлено",
            error="Обработка остановлена",
            progress=0,
        )
    if meeting_id not in _jobs:
        release(meeting_id)
    return "stop"
