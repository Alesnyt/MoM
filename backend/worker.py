from __future__ import annotations

import asyncio
import logging
import time

from . import config, gigaam_asr, local_asr, store
from .pipeline import process_meeting

log = logging.getLogger("mom.queue")

_wake: asyncio.Event | None = None


def notify_work() -> None:
    if _wake is not None:
        _wake.set()


def _unload_idle_asr() -> None:
    stats = store.queue_stats()
    if stats["active"] or stats["waiting"]:
        return
    local_asr.unload()
    gigaam_asr.unload()
    log.info("Локальный ASR выгружен после простоя очереди")


async def run_worker(stop: asyncio.Event) -> None:
    global _wake
    _wake = asyncio.Event()
    running: set[asyncio.Task[None]] = set()
    idle_since: float | None = None
    try:
        while not stop.is_set():
            running = {task for task in running if not task.done()}
            limit = config.get_max_jobs()
            while len(running) < limit and not stop.is_set():
                meeting_id = await asyncio.to_thread(store.claim_next_meeting, limit)
                if not meeting_id:
                    break
                idle_since = None
                await asyncio.to_thread(store.refresh_queue_messages)
                running.add(asyncio.create_task(_run_job(meeting_id), name=f"mom-job-{meeting_id[:8]}"))
            if running:
                idle_since = None
                _done, running = await asyncio.wait(
                    running,
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await asyncio.to_thread(store.refresh_queue_messages)
                continue
            now = time.monotonic()
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= float(config.ASR_IDLE_UNLOAD_SECONDS):
                await asyncio.to_thread(_unload_idle_asr)
                idle_since = now
            await asyncio.to_thread(store.refresh_queue_messages)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=1.5)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            finally:
                _wake.clear()
    finally:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        _wake = None


async def _run_job(meeting_id: str) -> None:
    try:
        await process_meeting(meeting_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Обработка встречи %s прервалась", meeting_id)
        meeting = store.get_meeting(meeting_id)
        if meeting and meeting.get("status") not in {"done", "error"}:
            store.update_meeting(
                meeting_id,
                status="error",
                status_message="Обработка не удалась",
                error="Внутренняя ошибка очереди",
            )
    finally:
        notify_work()
