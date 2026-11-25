import asyncio
from contextlib import contextmanager
import logging
import os
import re
from pathlib import Path
import shutil
import sqlite3
import threading

from . import background, config, control, disk, store

log = logging.getLogger("mom.retention")
_cleanup_lock = threading.Lock()
_active_uploads: set[str] = set()


def purge_files(meeting_id: str) -> None:
    try:
        # Path.glob may suppress directory access errors. A successful empty
        # listing must mean no files exist, not that we lacked permission.
        try:
            with os.scandir(config.UPLOAD_DIR) as entries:
                uploads = [Path(entry.path) for entry in entries if entry.name.startswith(f"{meeting_id}.")]
        except FileNotFoundError:
            uploads = []
        for path in uploads:
            path.unlink(missing_ok=True)
        (config.AUDIO_DIR / f"{meeting_id}.mp3").unlink(missing_ok=True)
        (config.AUDIO_DIR / f"{meeting_id}.wav").unlink(missing_ok=True)
        chunks = config.AUDIO_DIR / f"{meeting_id}_chunks"
        try:
            chunks.lstat()
        except FileNotFoundError:
            pass
        else:
            shutil.rmtree(chunks)
    finally:
        disk.invalidate_recordings()


def trim_archive(user_id: str, actor: str = "система") -> None:
    remove_pruned(store.prune_user_meetings(user_id), actor)


def remove_pruned(meetings: list[dict[str, str]], actor: str = "система") -> None:
    for meeting in meetings:
        meeting_id = meeting["id"]
        store.record_audit(actor, "meeting.delete", meeting_id, f"{meeting['title']} · вытеснено из архива")
        cleanup_recording(meeting_id)


def cleanup_recording(meeting_id: str) -> bool:
    """Attempt queued deletion; acknowledge only after every file is gone.

    Immediate API cleanup and the periodic service share a lock in the
    supported single-process deployment. Repeating a deletion after a crash
    is safe because purge_files tolerates files already removed.
    """
    with _cleanup_lock:
        if not store.recording_cleanup_pending(meeting_id):
            return True
        # A terminal DB status can precede the worker's file/client cleanup.
        # Leave the task pending until that worker releases its ownership.
        if meeting_id in _active_uploads or control.job_running(meeting_id):
            return False
        # Never turn a corrupt/stale cleanup entry into deletion of live data.
        if store.get_meeting(meeting_id) is not None:
            store.retry_recording_cleanup(meeting_id, "Встреча ещё существует")
            log.error("Очистка файлов отложена: встреча %s ещё существует", meeting_id)
            return False
        try:
            purge_files(meeting_id)
        except OSError as exc:
            store.retry_recording_cleanup(meeting_id, str(exc))
            log.exception("Не удалось очистить файлы встречи %s; удаление будет повторено", meeting_id)
            return False
        store.complete_recording_cleanup(meeting_id)
        return True


@contextmanager
def incoming_recording(meeting_id: str):
    """Create a durable cleanup ticket before writing any recording bytes."""
    with _cleanup_lock:
        store.enqueue_recording_cleanup(meeting_id)
        _active_uploads.add(meeting_id)
    try:
        yield
    finally:
        with _cleanup_lock:
            _active_uploads.discard(meeting_id)
        # create_meeting clears the ticket in the INSERT transaction. If that
        # transaction failed or this process stopped, the cleanup service owns it.
        try:
            cleanup_recording(meeting_id)
        except (sqlite3.Error, OSError):
            log.exception("Очистка незавершённой загрузки отложена до восстановления хранилища")
        disk.invalidate_recordings()


def reconcile_legacy_uploads(suffixes: set[str]) -> None:
    """Queue unregistered originals from releases that predate upload tickets, once at startup."""
    if store.get_app_setting("recording_uploads_reconciled"):
        return
    with os.scandir(config.UPLOAD_DIR) as entries:
        candidates = {Path(entry.name).stem for entry in entries
                      if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix in suffixes
                      and re.fullmatch(r"[0-9a-f]{32}", Path(entry.name).stem)}
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        registered = {row[0] for row in conn.execute("SELECT id FROM meetings")}
        missing = candidates - registered
        conn.executemany("INSERT OR IGNORE INTO recording_cleanup (meeting_id, created_at) VALUES (?, ?)",
                         [(mid, store.utc_now()) for mid in missing])
        conn.execute("INSERT OR REPLACE INTO app_settings(key,payload) VALUES ('recording_uploads_reconciled','true')")
    if missing:
        log.info("Поставлено на очистку незарегистрированных записей: %d", len(missing))


def cleanup_pending_recordings() -> None:
    for meeting_id in store.pending_recording_cleanup():
        cleanup_recording(meeting_id)


async def run_file_cleanup(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await background.run_sync(cleanup_pending_recordings)
        await background.pause(stop, 30)


async def run_session_cleanup(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.to_thread(store.purge_expired_sessions)
        await background.pause(stop, 60)
