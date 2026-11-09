from __future__ import annotations

import threading
import time
from typing import TypedDict

import psutil


class Resources(TypedDict):
    cpu_percent: float | None
    rss_bytes: int | None
    process_count: int | None


def _process_totals() -> tuple[float, int, int]:
    root = psutil.Process()
    cpu_seconds = 0.0
    rss_bytes = 0
    process_count = 0
    for process in (root, *root.children(recursive=True)):
        try:
            with process.oneshot():
                cpu = process.cpu_times()
                memory = process.memory_info().rss
        except (psutil.Error, OSError):
            if process is root:
                raise
            continue  # A short-lived child such as ffmpeg may exit during sampling.
        # On Linux the children counters retain the CPU time of children already reaped.
        cpu_seconds += cpu.user + cpu.system + cpu.children_user + cpu.children_system
        rss_bytes += memory
        process_count += 1
    return cpu_seconds, rss_bytes, process_count


class ResourceSampler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._previous: tuple[float, float] | None = None
        self._last: Resources = {"cpu_percent": None, "rss_bytes": None, "process_count": None}

    def snapshot(self) -> Resources:
        with self._lock:
            now = time.monotonic()
            previous = self._previous
            if previous is not None and now - previous[0] < 1:
                return self._last
            try:
                cpu_seconds, rss_bytes, process_count = _process_totals()
            except (psutil.Error, OSError):
                self._previous = None
                self._last = {"cpu_percent": None, "rss_bytes": None, "process_count": None}
                return self._last

            cpu_percent = None
            if previous is not None:
                elapsed = now - previous[0]
                delta = cpu_seconds - previous[1]
                # A long gap between visits or a reset of process counters needs a new baseline.
                if elapsed <= 20 and delta >= 0:
                    cpu_percent = round(100 * delta / elapsed, 1)
            self._previous = (now, cpu_seconds)
            self._last = {"cpu_percent": cpu_percent, "rss_bytes": rss_bytes, "process_count": process_count}
            return self._last


resources = ResourceSampler()
