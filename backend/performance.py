"""Stage timings and CPU settings shared by local speech models."""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from statistics import median
from typing import Iterator

from . import config

log = logging.getLogger("mom.performance")
_lock = threading.Lock()
_timings: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=4096))
_outcomes: deque[tuple[float, str]] = deque(maxlen=4096)
WINDOW_SECONDS = 86400


def record_timing(stage: str, seconds: float) -> None:
    with _lock:
        _timings[stage].append((time.monotonic(), max(0.0, seconds)))


def record_outcome(status: str) -> None:
    with _lock:
        _outcomes.append((time.monotonic(), status))


def snapshot() -> dict:
    since = time.monotonic() - WINDOW_SECONDS
    with _lock:
        timings = {name: [seconds for at, seconds in samples if at >= since]
                   for name, samples in _timings.items()}
        outcomes = [status for at, status in _outcomes if at >= since]
    stages = {}
    for name, values in timings.items():
        if values:
            values.sort()
            stages[name] = {"samples": len(values), "median_seconds": round(median(values), 2),
                            "p95_seconds": round(values[math.ceil(len(values) * 0.95) - 1], 2)}
    return {"stages": stages, "finished": len(outcomes), "errors": outcomes.count("error")}


@contextmanager
def stage_timer(stage: str) -> Iterator[None]:
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "interrupted"
        raise
    finally:
        seconds = time.perf_counter() - started
        if outcome == "ok":
            record_timing(stage, seconds)
        log.info("timing stage=%s seconds=%.3f outcome=%s", stage, seconds, outcome)


def available_cpus() -> int:
    """Respect process affinity where supported; never return zero."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        process_count = getattr(os, "process_cpu_count", os.cpu_count)
        return max(1, process_count() or 2)


def whisper_threads() -> int:
    available = available_cpus()
    requested = config.WHISPER_CPU_THREADS
    return min(available, requested) if requested > 0 else min(8, available)


def configure_torch_threads() -> None:
    """Call within the shared local-model slot, before loading/running a model.

    PyTorch's setting is process-wide, so GigaAM and pyannote use one setting.
    Zero preserves PyTorch/environment defaults.
    """
    if config.TORCH_CPU_THREADS <= 0:
        return
    import torch

    threads = min(config.TORCH_CPU_THREADS, available_cpus())
    if torch.get_num_threads() != threads:
        torch.set_num_threads(threads)
        log.info("cpu engine=torch threads=%s", threads)
