from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import config

_recordings_cache: dict = {"at": 0.0, "bytes": 0}
_cache_lock = threading.Lock()
_scan_lock = threading.Lock()
_generation = 0


def invalidate_recordings() -> None:
    """Invalidate without waiting for a scan of a large archive to finish."""
    global _generation
    with _cache_lock:
        _generation += 1
        _recordings_cache.clear()


class DiskError(RuntimeError):
    pass


def _usage(path: Path):
    return shutil.disk_usage(path)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def _recordings_bytes() -> int:
    paths = (config.UPLOAD_DIR, config.AUDIO_DIR)

    def cached():
        if (_recordings_cache.get("paths") == paths
                and time.monotonic() - _recordings_cache.get("at", 0) < 30):
            return int(_recordings_cache["bytes"])
        return None

    with _cache_lock:
        value = cached()
    if value is not None:
        return value
    # Concurrent monitoring requests share one directory scan.
    with _scan_lock:
        with _cache_lock:
            value = cached()
            if value is not None:
                return value
            generation = _generation
        total = sum(_dir_size(path) for path in paths)
        with _cache_lock:
            if generation == _generation:
                _recordings_cache.update(at=time.monotonic(), bytes=total, paths=paths)
        return total


def format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    number = float(size)
    unit = units[0]
    for unit in units:
        if number < 1024 or unit == units[-1]:
            break
        number /= 1024
    if unit == "Б" or number >= 10:
        return f"{int(number)} {unit}"
    text = f"{number:.1f}".replace(".0", "")
    return f"{text} {unit}"


def free_bytes() -> int:
    path = config.DATA_DIR if config.DATA_DIR.exists() else config.DATA_DIR.parent
    try:
        return int(_usage(path).free)
    except FileNotFoundError:
        config.ensure_dirs()
        return int(_usage(config.DATA_DIR).free)


def has_room() -> bool:
    return free_bytes() >= config.MIN_FREE_BYTES


def snapshot() -> dict:
    config.ensure_dirs()
    usage = _usage(config.DATA_DIR)
    free = int(usage.free)
    minimum = config.MIN_FREE_BYTES
    return {
        "free_bytes": free,
        "total_bytes": int(usage.total),
        "recordings_bytes": _recordings_bytes(),
        "min_free_bytes": minimum,
        "ok": free >= minimum,
    }


def ensure_space(extra: int = 0) -> None:
    free = free_bytes()
    incoming = max(0, int(extra))
    minimum = config.MIN_FREE_BYTES
    need = minimum + incoming
    if free >= need:
        return
    if incoming:
        raise DiskError(
            f"Файл не помещается: свободно {format_bytes(free)}, "
            f"для записи и запаса нужно {format_bytes(need)}."
        )
    raise DiskError(
        f"На диске свободно {format_bytes(free)}. "
        f"Нужно хотя бы {format_bytes(minimum)}, чтобы расшифровка не оборвалась."
    )
