from __future__ import annotations

import gc
import logging
import os
from pathlib import Path
from typing import Any, Callable

from . import config

log = logging.getLogger("mom.asr")

ProgressFn = Callable[[int, str], None]

_model = None
_model_size: str | None = None


def unload() -> None:
    global _model, _model_size
    _model = None
    _model_size = None
    gc.collect()


def _load(on_progress: ProgressFn | None = None):
    global _model, _model_size
    size = config.local_whisper_size()
    if _model is not None and _model_size == size:
        return _model
    try:
        from . import gigaam_asr

        gigaam_asr.unload()
    except Exception:
        pass
    if on_progress:
        on_progress(0, f"Загружаю модель Whisper {size}")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "Не установлен faster-whisper. Выполните: "
            "source .venv/bin/activate && pip install faster-whisper"
        ) from exc
    config.ensure_dirs()
    threads = max(1, min(8, os.cpu_count() or 2))
    _model = WhisperModel(
        size,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        num_workers=1,
        download_root=str(config.WHISPER_DIR),
    )
    _model_size = size
    return _model


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def transcribe_local_sync(
    path: Path,
    language: str | None = None,
    duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    log.info("whisper size=%s file=%s", config.local_whisper_size(), path.name)
    model = _load(on_progress)
    if on_progress:
        on_progress(1, "Whisper готов, начинаю распознавание")
    kwargs: dict[str, Any] = {"vad_filter": True}
    if language:
        kwargs["language"] = language
    segments, info = model.transcribe(str(path), **kwargs)
    total = float(duration or getattr(info, "duration", 0) or 0)
    texts: list[str] = []
    timed: list[dict[str, Any]] = []
    last_pct = -1
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        texts.append(text)
        end = float(segment.end)
        timed.append({"start": float(segment.start), "end": end, "text": text})
        if on_progress and total > 0:
            pct = min(99, max(1, int(end / total * 100)))
            if pct != last_pct:
                last_pct = pct
                on_progress(pct, f"Whisper: {_clock(end)} из {_clock(total)} · {pct}%")
    if on_progress:
        on_progress(100, "Расшифровка завершена")
    log.info("whisper готово file=%s segments=%s", path.name, len(timed))
    return {
        "language": getattr(info, "language", None) or language,
        "text": " ".join(texts).strip(),
        "segments": timed,
    }
