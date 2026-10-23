from __future__ import annotations

import gc
import hashlib
import importlib
import json
import logging
import math
import sys
import types
from pathlib import Path
from typing import Any, Callable

from . import config
from .audio import open_pcm_audio, read_pcm_span
from .performance import configure_torch_threads, stage_timer

log = logging.getLogger("mom.gigaam")

ProgressFn = Callable[[int, str], None]

_model = None
_revision: str | None = None


def unload() -> None:
    global _model, _revision
    _model = None
    _revision = None
    gc.collect()


def _allow_gigaam_import() -> None:
    # transformers scans the GigaAM modeling file and demands `pyannote`
    # even though we never call transcribe_longform (we process short PCM spans).
    try:
        importlib.import_module("pyannote")
    except ImportError:
        log.warning("pyannote не установлен — подставляю заглушку только для импорта GigaAM")
        sys.modules["pyannote"] = types.ModuleType("pyannote")


def vendor_code_path() -> Path:
    return Path(__file__).resolve().parent / "vendor" / "gigaam" / "modeling_gigaam.py"


def vendor_code_sha256() -> str:
    return hashlib.sha256(vendor_code_path().read_bytes()).hexdigest()


_vendor_checked = False


def assert_vendor_code() -> None:
    global _vendor_checked
    if _vendor_checked:
        return
    digest = vendor_code_sha256()
    if digest != config.GIGAAM_CODE_SHA256:
        raise RuntimeError(
            "Код GigaAM в репозитории не совпадает с зафиксированным хешем. "
            "Файл modeling_gigaam.py менять нельзя."
        )
    _vendor_checked = True


def gigaam_pin(revision: str | None = None) -> dict[str, Any]:
    name = revision or config.gigaam_revision()
    pin = config.GIGAAM_PINS.get(name)
    if not pin:
        raise RuntimeError(f"Неизвестная ревизия GigaAM: {name}")
    return pin


def _load(on_progress: ProgressFn | None = None):
    global _model, _revision
    configure_torch_threads()
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
        with stage_timer("gigaam_model_load"):
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
    texts: list[str] = []
    timed: list[dict[str, Any]] = []
    with open_pcm_audio(path) as audio:
        # Use actual PCM duration so the final partial chunk is never lost.
        total = audio.getnframes() / audio.getframerate()
        chunk_seconds = config.GIGAAM_CHUNK_SECONDS
        count = max(1, math.ceil(total / chunk_seconds))
        for index in range(count):
            if on_progress:
                on_progress(min(99, int(index / count * 100)), f"GigaAM: фрагмент {index + 1} из {count}")
            start = float(index * chunk_seconds)
            end = min(total, start + chunk_seconds)
            if end <= start:
                continue
            text = _transcribe_pcm(model, read_pcm_span(audio, start, end))
            if text:
                texts.append(text)
                timed.append({"start": start, "end": end, "text": text})
    if on_progress:
        on_progress(100, "Расшифровка завершена")
    log.info("gigaam готово file=%s segments=%s", path.name, len(timed))
    return {"language": language, "text": " ".join(texts).strip(), "segments": timed}


def transcribe_span(src: Path, start: float, end: float) -> str:
    """Legacy single-span entry point, without a temporary file per utterance."""
    if end - start < 0.3:
        return ""
    model = _load()
    with open_pcm_audio(src) as audio:
        return _transcribe_pcm(model, read_pcm_span(audio, start, end))


def transcribe_spans(
    src: Path,
    spans: list[dict[str, float]],
    language: str | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Read bounded voice spans from one open WAV and infer directly on PCM."""
    model = _load(on_progress)
    texts: list[str] = []
    timed: list[dict[str, Any]] = []
    count = max(1, len(spans))
    with open_pcm_audio(src) as audio:
        total = audio.getnframes() / audio.getframerate()
        for index, span in enumerate(spans):
            if on_progress:
                on_progress(min(99, int(index / count * 100)), f"GigaAM: реплика {index + 1} из {count}")
            start = float(span["start"])
            end = float(span["end"])
            try:
                frames = read_pcm_span(audio, start, end)
                text = _transcribe_pcm(model, frames)
            except Exception as exc:
                log.exception("gigaam реплика %.2f–%.2f", start, end)
                raise RuntimeError(f"GigaAM не смог расшифровать реплику {index + 1}: {exc}") from exc
            if not text:
                continue
            texts.append(text)
            timed.append({"start": max(0.0, start), "end": min(total, end), "text": text})
    if on_progress:
        on_progress(100, "Расшифровка завершена")
    log.info("gigaam готово реплик %s", len(timed))
    return {"language": language, "text": " ".join(texts).strip(), "segments": timed}


def _transcribe_pcm(model: Any, frames: bytes) -> str:
    """Same inference path as the pinned GigaAMASR.transcribe, minus ffmpeg.

    The vendor file stays untouched and hash-checked. Inputs match its
    load_audio: mono PCM16 at 16 kHz, normalized by 32768, shape [1, samples].
    Only one short span is materialized, even for recordings lasting hours.
    """
    if not frames:
        return ""
    if len(frames) % 2 or len(frames) // 2 > 25 * 16000:
        raise ValueError("GigaAM ожидает PCM16-фрагмент не длиннее 25 секунд")
    import torch

    inner = model.model
    with torch.inference_mode():
        # Writable buffer avoids PyTorch's warning about immutable bytes.
        pcm = torch.frombuffer(bytearray(frames), dtype=torch.int16).float() / 32768.0
        wav = pcm.to(device=inner._device, dtype=inner._dtype).unsqueeze(0)
        length = torch.full([1], wav.shape[-1], device=inner._device, dtype=torch.long)
        encoded, encoded_len = inner.forward(wav, length)
        text, _ = inner._decode(encoded, encoded_len, length, False)[0]
        return str(text).strip()
