"""Shared lifecycle and bounded recovery for the single-process services."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("mom.background")
RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 30.0


async def pause(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


class Service:
    def __init__(self, name: str):
        self.name = name
        self.state = "starting"
        self.updated = time.monotonic()
        self.failures = 0

    def ready(self):
        self.state = "running"
        self.updated = time.monotonic()
        self.failures = 0

    def failed(self) -> float:
        self.state = "retrying"
        self.updated = time.monotonic()
        self.failures += 1
        return min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(self.failures - 1, 5))

    def snapshot(self, *, heartbeat: bool = False) -> dict:
        stale = heartbeat and time.monotonic() - self.updated > 90
        state = "unresponsive" if stale and self.state == "running" else self.state
        return {"name": self.name, "state": state, "ok": state == "running"}

    async def supervise(self, run: Callable[[asyncio.Event], Awaitable[None]], stop: asyncio.Event):
        try:
            while not stop.is_set():
                self.state = "running"
                self.updated = time.monotonic()
                try:
                    await run(stop)
                    if stop.is_set():
                        return
                    raise RuntimeError(f"Service {self.name} exited unexpectedly")
                except Exception:
                    log.exception("Сбой фоновой службы %s; повторный запуск", self.name)
                    await pause(stop, self.failed())
        finally:
            self.state = "stopped"
