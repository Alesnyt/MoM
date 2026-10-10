from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import config

_recordings_cache: dict[str, float | int] = {"at": 0.0, "bytes": 0}


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
    now = time.monotonic()
    cached_at = float(_recordings_cache["at"])
    if now - cached_at < 30:
        return int(_recordings_cache["bytes"])
    total = _dir_size(config.UPLOAD_DIR) + _dir_size(config.AUDIO_DIR)
    _recordings_cache["at"] = now
    _recordings_cache["bytes"] = total
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
