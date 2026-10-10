from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Any, Callable

from . import config
from .audio import extract_wav
from .speakers import assign_speakers, expand_coarse_segments, render_transcript

log = logging.getLogger("mom.diarize")


class DiarizeUnavailable(RuntimeError):
    pass


def apply_diarization(
    audio_path: Path,
    payload: dict[str, Any],
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    segments = list(payload.get("segments") or [])
    if not segments and (payload.get("text") or "").strip():
        segments = [{"start": 0.0, "end": 0.0, "text": payload["text"]}]
    note = None
    turns: list[dict[str, Any]] = []
    if on_progress:
        on_progress("Размечаю спикеров")
    unavailable = _unavailable_reason()
    if unavailable:
        note = unavailable
        log.info("Спикеры не размечены: %s", unavailable)
    else:
        try:
            _unload_asr()
            turns = diarize_turns(audio_path)
        except DiarizeUnavailable as exc:
            note = str(exc)
            log.info("Спикеры не размечены: %s", exc)
        except Exception:
            log.exception("Разметка спикеров не удалась")
            note = "Не удалось разметить спикеров. Реплики собраны в одного, имя можно задать вручную."
    if turns and config.is_gigaam_asr():
        before = len(segments)
        segments = expand_coarse_segments(
            segments,
            turns,
            lambda start, end: _transcribe_voice_slice(audio_path, start, end),
        )
        if len(segments) != before:
            if on_progress:
                on_progress("Режу реплики по голосам")
            log.info("gigaam реплик стало %s вместо %s", len(segments), before)
    document = assign_speakers(segments, turns)
    document["note"] = note
    document["text"] = render_transcript(document)
    return document


def _transcribe_voice_slice(audio_path: Path, start: float, end: float) -> str:
    from .gigaam_asr import transcribe_span

    log.info("gigaam граница голоса %.2f–%.2f", start, end)
    return transcribe_span(audio_path, start, end)


def available() -> bool:
    return _unavailable_reason() is None


def _unavailable_reason() -> str | None:
    if not config.get_hf_token():
        return "Нет HF_TOKEN — все реплики собраны в одного спикера"
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return "Модель спикеров не установлена. Выполните: pip install -r requirements-diarize.txt"
    return None


def diarize_turns(audio_path: Path, *, prepared: Path | None = None) -> list[dict[str, Any]]:
    token = config.get_hf_token()
    if not token:
        raise DiarizeUnavailable("Нет HF_TOKEN — все реплики собраны в одного спикера")
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise DiarizeUnavailable(
            "Модель спикеров не установлена. Выполните: pip install -r requirements-diarize.txt"
        ) from exc
    wav = prepared or audio_path.with_name(f"{audio_path.stem}.diarize.wav")
    pipeline = None
    try:
        if prepared is None:
            extract_wav(audio_path, wav)
        pipeline = _load_pipeline(Pipeline, token)
        output = _run_pipeline(pipeline, wav)
        return _read_turns(output)
    finally:
        if prepared is None:
            wav.unlink(missing_ok=True)
        del pipeline
        gc.collect()


def _unload_asr() -> None:
    try:
        from . import gigaam_asr, local_asr

        local_asr.unload()
        gigaam_asr.unload()
    except Exception:
        log.debug("Не удалось выгрузить ASR перед разметкой спикеров", exc_info=True)
    gc.collect()


def _load_pipeline(pipeline_cls: Any, token: str) -> Any:
    model = config.DIARIZE_MODEL
    try:
        pipeline = pipeline_cls.from_pretrained(model, token=token)
    except TypeError:
        pipeline = pipeline_cls.from_pretrained(model, use_auth_token=token)
    if pipeline is None:
        raise DiarizeUnavailable(f"Не удалось загрузить {model}. Примите условия модели на Hugging Face.")
    try:
        import torch

        pipeline.to(torch.device("cpu"))
    except Exception:
        log.debug("Модель спикеров остаётся на устройстве по умолчанию", exc_info=True)
    return pipeline


def _run_pipeline(pipeline: Any, wav: Path) -> Any:
    limit = int(config.DIARIZE_MAX_SPEAKERS)
    try:
        return pipeline(str(wav), min_speakers=1, max_speakers=limit)
    except TypeError:
        return pipeline(str(wav))


def _read_turns(output: Any) -> list[dict[str, Any]]:
    annotation = getattr(output, "speaker_diarization", output)
    turns: list[dict[str, Any]] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)})
    return turns
