import logging
import shutil

from . import config, store

log = logging.getLogger("mom.retention")


def purge_files(meeting_id: str) -> None:
    for path in config.UPLOAD_DIR.glob(f"{meeting_id}.*"):
        path.unlink(missing_ok=True)
    (config.AUDIO_DIR / f"{meeting_id}.mp3").unlink(missing_ok=True)
    (config.AUDIO_DIR / f"{meeting_id}.wav").unlink(missing_ok=True)
    chunks = config.AUDIO_DIR / f"{meeting_id}_chunks"
    if chunks.exists():
        shutil.rmtree(chunks, ignore_errors=True)


def trim_archive(user_id: str, actor: str = "система") -> None:
    remove_pruned(store.prune_user_meetings(user_id), actor)


def remove_pruned(meetings: list[dict[str, str]], actor: str = "система") -> None:
    for meeting in meetings:
        meeting_id = meeting["id"]
        store.record_audit(actor, "meeting.delete", meeting_id, f"{meeting['title']} · вытеснено из архива")
        try:
            purge_files(meeting_id)
        except OSError:
            log.exception("Не удалось очистить файлы встречи %s", meeting_id)
