from __future__ import annotations

import gc
import importlib
import shutil
import sys
import types
from pathlib import Path
from typing import Any, Callable

from . import config
from .audio import duration_seconds, extract_wav, split_wav

ProgressFn = Callable[[int, str], None]

_model = None
_revision: str | None = None


def unload() -> None:
    global _model, _revision
    _model = None
    _revision = None
    gc.collect()


def _text_of(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text.strip()
    if isinstance(result, dict):
        value = result.get("text") or result.get("transcription") or ""
        return str(value).strip()
    return str(result).strip()


def _allow_gigaam_import() -> None:
    # transformers scans the whole GigaAM modeling file and demands `pyannote`
    # even though we never call transcribe_longform (we cut audio with ffmpeg).
    try:
        importlib.import_module("pyannote")
    except ImportError:
        sys.modules["pyannote"] = types.ModuleType("pyannote")


def _load(on_progress: ProgressFn | None = None):
    global _model, _revision
    revision = config.gigaam_revision()
    if _model is not None and _revision == revision:
        return _model
    try:
        from . import local_asr

        local_asr.unload()
    except Exception:
        pass
    if on_progress:
        label = config.asr_engine_label()
        on_progress(0, f"Загружаю {label}")
    try:
        from transformers import AutoModel
    except ImportError as exc:
        raise RuntimeError(
            "Не установлен GigaAM. Выполните ./update.sh "
            "или: source .venv/bin/activate && pip install -r requirements.txt"
        ) from exc
    config.ensure_dirs()
    _allow_gigaam_import()
    try:
        _model = AutoModel.from_pretrained(
            config.GIGAAM_REPO,
            revision=revision,
            trust_remote_code=True,
            cache_dir=str(config.DATA_DIR / "hf"),
        )
    except Exception as exc:
        raise RuntimeError(
            "Не удалось загрузить Сбер GigaAM Multilingual. "
            "Проверьте интернет и выполните ./update.sh. "
            f"{exc}"
        ) from exc
    _revision = revision
    return _model


def transcribe_gigaam_sync(
    path: Path,
    language: str | None = None,
    duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    model = _load(on_progress)
    work = path.parent / f"{path.stem}_gigaam"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "full.wav"
    try:
        if on_progress:
            on_progress(2, "Готовлю WAV для GigaAM")
        extract_wav(path, wav)
        total = float(duration or duration_seconds(wav))
        chunk_seconds = config.GIGAAM_CHUNK_SECONDS
        if total <= chunk_seconds:
            if on_progress:
                on_progress(5, "GigaAM распознаёт запись")
            text = _text_of(model.transcribe(str(wav)))
            if on_progress:
                on_progress(100, "Расшифровка завершена")
            segments = [{"start": 0.0, "end": total, "text": text}] if text else []
            return {"language": language, "text": text, "segments": segments}

        chunks = split_wav(wav, work / "chunks", chunk_seconds)
        texts: list[str] = []
        timed: list[dict[str, Any]] = []
        count = max(1, len(chunks))
        for index, chunk in enumerate(chunks):
            if on_progress:
                pct = min(99, max(5, int(index / count * 100)))
                on_progress(pct, f"GigaAM: фрагмент {index + 1} из {count}")
            try:
                text = _text_of(model.transcribe(str(chunk)))
            except Exception as exc:
                raise RuntimeError(f"GigaAM не смог расшифровать фрагмент {index + 1}: {exc}") from exc
            if not text:
                continue
            texts.append(text)
            offset = float(index * chunk_seconds)
            timed.append(
                {
                    "start": offset,
                    "end": offset + float(duration_seconds(chunk)),
                    "text": text,
                }
            )
        if on_progress:
            on_progress(100, "Расшифровка завершена")
        return {
            "language": language,
            "text": " ".join(texts).strip(),
            "segments": timed,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
