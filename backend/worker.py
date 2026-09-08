from __future__ import annotations

import asyncio
import logging

from . import config, store
from .pipeline import process_meeting

log = logging.getLogger("mom.queue")

_wake: asyncio.Event | None = None


def notify_work() -> None:
    if _wake is not None:
        _wake.set()


async def run_worker(stop: asyncio.Event) -> None:
    global _wake
    _wake = asyncio.Event()
    running: set[asyncio.Task[None]] = set()
    try:
        while not stop.is_set():
            running = {task for task in running if not task.done()}
            limit = config.get_max_jobs()
            while len(running) < limit and not stop.is_set():
                meeting_id = await asyncio.to_thread(store.claim_next_meeting, limit)
                if not meeting_id:
                    break
                await asyncio.to_thread(store.refresh_queue_messages)
                running.add(asyncio.create_task(_run_job(meeting_id), name=f"mom-job-{meeting_id[:8]}"))
            if running:
                _done, running = await asyncio.wait(
                    running,
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await asyncio.to_thread(store.refresh_queue_messages)
                continue
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
