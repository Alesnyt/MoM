from __future__ import annotations

import asyncio
import logging
import time

from . import config, diarize, gigaam_asr, local_asr, mail, retention, store
from . import background, control, corporate_context, glossary, term_discovery
from .logctx import meeting_scope
from .performance import record_outcome
from .pipeline import process_meeting

log = logging.getLogger("mom.queue")

_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
_services: dict[str, background.Service] = {}


def snapshot() -> dict:
    services = [service.snapshot(heartbeat=name == "queue") for name, service in list(_services.items())]
    return {"ok": bool(services) and all(item["ok"] for item in services), "services": services}


def notify_work() -> None:
    # API handlers also call this from Starlette's worker threads.
    loop, wake = _loop, _wake
    if loop is not None and wake is not None and not loop.is_closed():
        loop.call_soon_threadsafe(wake.set)


def _unload_idle_asr() -> None:
    stats = store.queue_stats()
    if stats["active"] or stats["waiting"]:
        return
    local_asr.unload()
    gigaam_asr.unload()
    diarize.unload()
    log.info("Локальный ASR выгружен после простоя очереди")


async def run_worker(stop: asyncio.Event) -> None:
    global _wake, _loop, _services
    _wake = asyncio.Event()
    _loop = asyncio.get_running_loop()
    jobs = {"mail": _run_delivery, "directory": corporate_context.run_sync,
            "glossary": glossary.run_imports, "term_discovery": term_discovery.run}
    _services = {name: background.Service(name) for name in ("queue", *jobs)}
    queue = _services["queue"]
    services = [asyncio.create_task(_services[name].supervise(run, stop), name=f"mom-{name}")
                for name, run in jobs.items()]
    running: set[asyncio.Task[None]] = set()
    idle_since: float | None = None
    try:
        while not stop.is_set():
            try:
                for task in list(running):
                    if task.done():
                        running.remove(task)
                        # Always observe exceptions; never leave a silently dead task.
                        if not task.cancelled() and task.exception() is not None:
                            log.error("Задача очереди завершилась с ошибкой", exc_info=task.exception())
                limit = config.get_max_jobs()
                while len(running) < limit and not stop.is_set():
                    meeting_id = await asyncio.to_thread(store.claim_next_meeting, limit)
                    if not meeting_id:
                        break
                    idle_since = None
                    # Register ownership immediately after claim. No fallible DB
                    # operation may strand an extracting meeting between these steps.
                    task = asyncio.create_task(_run_job(meeting_id), name=f"mom-job-{meeting_id[:8]}")
                    control.bind_job(meeting_id, task)
                    running.add(task)
                await asyncio.to_thread(store.refresh_queue_messages)
                queue.ready()
                if running:
                    idle_since = None
                    await asyncio.wait(running, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                    continue
                now = time.monotonic()
                if idle_since is None:
                    idle_since = now
                elif now - idle_since >= float(config.ASR_IDLE_UNLOAD_SECONDS):
                    await asyncio.to_thread(_unload_idle_asr)
                    idle_since = now
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=1.5)
                except TimeoutError:
                    pass
                finally:
                    _wake.clear()
            except Exception:
                log.exception("Временный сбой очереди; повтор без отмены работающих встреч")
                await background.pause(stop, queue.failed())
    finally:
        queue.state = "stopped"
        for task in services:
            task.cancel()
        await asyncio.gather(*services, return_exceptions=True)
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        _wake = None
        _loop = None


async def _run_delivery(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await asyncio.to_thread(mail.deliver_pending):
                continue
        except Exception:
            log.exception("Ошибка очереди писем")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            continue


async def _run_job(meeting_id: str) -> None:
    try:
        with meeting_scope(meeting_id):
            await _run_job_scoped(meeting_id)
    except asyncio.CancelledError:
        control.finish(meeting_id)
        raise
    except Exception:
        log.exception("Сбой завершения встречи %s", meeting_id)
        # DB may be temporarily unavailable even when recording the failure.
        # Keep ownership until the terminal state is durable; never rerun ASR.
        delay = background.RETRY_SECONDS
        while True:
            try:
                await asyncio.to_thread(_finish_failed_job, meeting_id)
                break
            except Exception:
                log.exception("Не удалось записать результат встречи %s; повтор", meeting_id)
                await asyncio.sleep(delay)
                delay = min(background.MAX_RETRY_SECONDS, delay * 2)
    finally:
        control.release(meeting_id)
        notify_work()


def _finish_failed_job(meeting_id: str) -> None:
    if control.is_stopped(meeting_id):
        control.finish(meeting_id)
    meeting = store.get_meeting(meeting_id)
    if meeting and meeting["status"] in {"extracting", "transcribing", "analyzing"}:
        store.update_meeting(meeting_id, status="error", status_message="Обработка не удалась",
                             error="Временный сбой очереди. Повторите обработку встречи.")


async def _run_job_scoped(meeting_id: str) -> None:
    try:
        await process_meeting(meeting_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Обработка прервалась")
        meeting = store.get_meeting(meeting_id)
        if meeting and meeting.get("status") not in {"done", "error"}:
            store.update_meeting(
                meeting_id,
                status="error",
                status_message="Обработка не удалась",
                error="Внутренняя ошибка очереди",
            )
    finally:
        meeting = store.get_meeting(meeting_id)
        if meeting and meeting["status"] == "error":
            if meeting["status_message"] != "Остановлено":
                record_outcome("error")
            if meeting.get("user_id"):
                await asyncio.to_thread(retention.trim_archive, meeting["user_id"])
        notify_work()
