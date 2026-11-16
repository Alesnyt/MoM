import asyncio
import logging
import os
from pathlib import Path
import shutil
import threading

from . import background, config, control, disk, store

log = logging.getLogger("mom.retention")
_cleanup_lock = threading.Lock()


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
        if control.job_running(meeting_id):
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


def discard_upload(meeting_id: str) -> None:
    """An interrupted owner deletion must not leave an untracked upload."""
    store.enqueue_recording_cleanup(meeting_id)
    cleanup_recording(meeting_id)


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
