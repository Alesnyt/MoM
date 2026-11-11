from __future__ import annotations

import gc
import logging
import threading
import wave
from pathlib import Path
from types import MethodType
from typing import Any, Callable

from . import config
from .audio import extract_wav
from .speakers import assign_speakers, expand_coarse_segments, render_transcript
from .performance import configure_torch_threads, stage_timer

log = logging.getLogger("mom.diarize")
_pipeline_lock = threading.Lock()
_cached_pipeline: Any = None
_cached_key: tuple[str, str] | None = None


class DiarizeUnavailable(RuntimeError):
    pass


def apply_diarization(
    audio_path: Path,
    payload: dict[str, Any],
    on_progress: Callable[[str], None] | None = None,
    *,
    prepared: Path | None = None,
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
            turns = diarize_turns(audio_path, prepared=prepared)
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
    global _cached_pipeline, _cached_key
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
    with _pipeline_lock:
        configure_torch_threads()
        cache = config.DIARIZE_CACHE_MODEL
        key = (config.DIARIZE_MODEL, token)
        if not cache or key != _cached_key:
            _drop_cached_pipeline()
        pipeline = _cached_pipeline
        try:
            if prepared is None:
                extract_wav(audio_path, wav)
            if pipeline is None:
                with stage_timer("diarization_model_load"):
                    pipeline = _load_pipeline(Pipeline, *key)
                if cache:
                    _cached_pipeline, _cached_key = pipeline, key
            source = _audio_input(wav)
            with stage_timer("diarization_inference"):
                return _read_turns(_run_pipeline(pipeline, source, wav))
        except Exception:
            # A failed inference may have left state in the pipeline.
            pipeline = None
            _drop_cached_pipeline()
            raise
        finally:
            if prepared is None:
                wav.unlink(missing_ok=True)
            if not cache:
                pipeline = None
                gc.collect()


def unload() -> None:
    with _pipeline_lock:
        _drop_cached_pipeline()


def _drop_cached_pipeline() -> None:
    global _cached_pipeline, _cached_key
    if _cached_pipeline is not None:
        _cached_pipeline, _cached_key = None, None
        gc.collect()
        log.info("Модель спикеров выгружена")


def _audio_input(wav: Path) -> str | dict[str, Any]:
    path = str(wav)
    limit = config.DIARIZE_PRELOAD_MAX_BYTES
    if not limit:
        return path
    try:
        with wave.open(path, "rb") as reader:
            # Audio() normalizes PCM to float32. Bound the tensor, not the on-disk file.
            size = reader.getnframes() * reader.getnchannels() * 4
        if size <= limit:
            from pyannote.audio import Audio

            with stage_timer("diarization_audio_load"):
                waveform, rate = Audio()(path)
            return {"waveform": waveform, "sample_rate": rate, "uri": wav.stem}
    except (OSError, EOFError, wave.Error, ImportError, RuntimeError, ValueError, MemoryError):
        log.debug("Не удалось прочитать WAV в память, использую файл", exc_info=True)
    return path


def _unload_asr() -> None:
    try:
        from . import gigaam_asr, local_asr

        local_asr.unload()
        gigaam_asr.unload()
    except Exception:
        log.debug("Не удалось выгрузить ASR перед разметкой спикеров", exc_info=True)
    gc.collect()


def _load_pipeline(pipeline_cls: Any, model: str, token: str) -> Any:
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
    _reuse_cpu_speaker_frames(pipeline)
    return pipeline


def _reuse_cpu_speaker_frames(pipeline: Any) -> None:
    model = getattr(getattr(pipeline, "_embedding", None), "model_", None)
    if model is None:
        return
    try:
        from pyannote.audio.models.embedding.wespeaker import WeSpeakerResNet34
    except ImportError:
        return
    if type(model) is WeSpeakerResNet34 and model.hparams.dither == 0.0:
        model.forward = MethodType(_forward_with_shared_frames, model)


def _forward_with_shared_frames(model: Any, waveforms: Any, weights: Any = None) -> Any:
    import torch

    original = type(model).forward
    if (
        weights is None
        or weights.ndim != 2
        or model.training
        or model.fbank_only
        or waveforms.device.type != "cpu"
        or waveforms.shape[0] < 2
    ):
        return original(model, waveforms, weights=weights)

    # Pyannote repeats each waveform for its local speaker masks. WeSpeaker
    # applies the mask only after the shared frame encoder.
    changed = waveforms[1:].ne(waveforms[:-1]).flatten(1).any(dim=1)
    if changed.all():
        return original(model, waveforms, weights=weights)

    starts = torch.ones(waveforms.shape[0], dtype=torch.bool, device=waveforms.device)
    starts[1:] = changed
    rows = starts.long().cumsum(dim=0) - 1
    frames = model.forward_frames(waveforms[starts])
    return model.forward_embedding(frames.index_select(0, rows), weights=weights)


def _run_pipeline(pipeline: Any, source: str | dict[str, Any], wav: Path) -> Any:
    limit = int(config.DIARIZE_MAX_SPEAKERS)

    def run(audio: str | dict[str, Any]) -> Any:
        try:
            return pipeline(audio, min_speakers=1, max_speakers=limit)
        except TypeError:
            return pipeline(audio)

    try:
        return run(source)
    except TypeError:
        if isinstance(source, str):
            raise
        return run(str(wav))


def _read_turns(output: Any) -> list[dict[str, Any]]:
    annotation = getattr(output, "speaker_diarization", output)
    turns: list[dict[str, Any]] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)})
    return turns
