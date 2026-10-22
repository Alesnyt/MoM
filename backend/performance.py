"""Stage timings and CPU settings shared by local speech models."""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator

from . import config

log = logging.getLogger("mom.performance")


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
        log.info("timing stage=%s seconds=%.3f outcome=%s", stage, time.perf_counter() - started, outcome)


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
