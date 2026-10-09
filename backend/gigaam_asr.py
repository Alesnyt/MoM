from __future__ import annotations

import gc
import hashlib
import importlib
import json
import logging
import shutil
import sys
import types
from pathlib import Path
from typing import Any, Callable

from . import config
from .audio import duration_seconds, extract_wav, split_wav

log = logging.getLogger("mom.gigaam")

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
    # transformers scans the GigaAM modeling file and demands `pyannote`
    # even though we never call transcribe_longform (we cut audio with ffmpeg).
    try:
        importlib.import_module("pyannote")
    except ImportError:
        log.warning("pyannote не установлен — подставляю заглушку только для импорта GigaAM")
        sys.modules["pyannote"] = types.ModuleType("pyannote")


def vendor_code_path() -> Path:
    return Path(__file__).resolve().parent / "vendor" / "gigaam" / "modeling_gigaam.py"


def vendor_code_sha256() -> str:
    return hashlib.sha256(vendor_code_path().read_bytes()).hexdigest()


def assert_vendor_code() -> None:
    digest = vendor_code_sha256()
    if digest != config.GIGAAM_CODE_SHA256:
        raise RuntimeError(
            "Код GigaAM в репозитории не совпадает с зафиксированным хешем. "
            "Файл modeling_gigaam.py менять нельзя."
        )


def gigaam_pin(revision: str | None = None) -> dict[str, Any]:
    name = revision or config.gigaam_revision()
    pin = config.GIGAAM_PINS.get(name)
    if not pin:
        raise RuntimeError(f"Неизвестная ревизия GigaAM: {name}")
    return pin


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
    config.ensure_dirs()
    try:
        _model = load_gigaam_model(revision)
    except Exception as exc:
        raise RuntimeError(
            "Не удалось загрузить Сбер GigaAM Multilingual. "
            "Проверьте интернет и выполните ./update.sh. "
            f"{exc}"
        ) from exc
    _revision = revision
    return _model


def load_gigaam_model(revision: str):
    """Веса — с зафиксированного коммита, код — только из репозитория MoM."""
    assert_vendor_code()
    pin = gigaam_pin(revision)
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError(
            "Не установлен GigaAM. Выполните ./update.sh "
            "или: source .venv/bin/activate && pip install -r requirements.txt"
        ) from exc
    cache = str(config.DATA_DIR / "hf")
    weight = Path(
        hf_hub_download(
            repo_id=config.GIGAAM_REPO,
            filename="pytorch_model.bin",
            revision=pin["commit"],
            cache_dir=cache,
        )
    )
    if weight.stat().st_size != pin["weights_size"]:
        raise RuntimeError(
            "Размер веса GigaAM не совпал с зафиксированным. "
            f"Ожидался коммит {pin['commit']}."
        )
    config_path = Path(
        hf_hub_download(
            repo_id=config.GIGAAM_REPO,
            filename="config.json",
            revision=pin["commit"],
            cache_dir=cache,
        )
    )
    config_bytes = config_path.read_bytes()
    digest = hashlib.sha256(config_bytes).hexdigest()
    if digest != pin["config_sha256"]:
        raise RuntimeError(
            "config.json GigaAM не совпал с зафиксированным хешем. "
            f"Ожидался коммит {pin['commit']}."
        )
    raw = json.loads(config_bytes)
    raw.pop("auto_map", None)
    _allow_gigaam_import()
    from .vendor.gigaam.modeling_gigaam import GigaAMConfig, GigaAMModel

    cfg = GigaAMConfig(cfg=raw.get("cfg"))
    return GigaAMModel.from_pretrained(
        config.GIGAAM_REPO,
        config=cfg,
        revision=pin["commit"],
        cache_dir=cache,
        trust_remote_code=False,
    )


def transcribe_gigaam_sync(
    path: Path,
    language: str | None = None,
    duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    log.info("gigaam revision=%s file=%s", config.gigaam_revision(), path.name)
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
            log.info("gigaam готово file=%s segments=%s", path.name, len(segments))
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
                log.exception("gigaam фрагмент %s из %s", index + 1, count)
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
        log.info("gigaam готово file=%s segments=%s", path.name, len(timed))
        return {
            "language": language,
            "text": " ".join(texts).strip(),
            "segments": timed,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def transcribe_span(src: Path, start: float, end: float) -> str:
    """Recognize one voice slice cut out of a longer GigaAM chunk."""
    if end - start < 0.3:
        return ""
    from .audio import cut_wav

    model = _load()
    dst = src.parent / f"{src.stem}_{start:.2f}_{end:.2f}.wav"
    try:
        cut_wav(src, dst, start, end)
        return _text_of(model.transcribe(str(dst)))
    finally:
        dst.unlink(missing_ok=True)
