from __future__ import annotations

import asyncio
import logging
import struct
import sys
import types
import wave
from pathlib import Path

import pytest

from backend import analyze, audio, config, gigaam_asr, performance
from backend.logctx import meeting_scope


def write_wav(path: Path, frames: bytes, *, rate: int = 16000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(frames)


def test_pcm_spans_keep_sample_boundaries_and_do_not_convert(tmp_path, monkeypatch):
    src = tmp_path / "shared.wav"
    frames = struct.pack("<4h", -32768, -123, 123, 32767) * 4000
    write_wav(src, frames)
    monkeypatch.setattr(audio, "extract_wav", lambda *a: pytest.fail("Prepared WAV was re-encoded"))
    with audio.open_pcm_audio(src) as reader:
        assert audio.read_pcm_span(reader, 0.25, 0.5) == frames[8000:16000]
        assert audio.read_pcm_span(reader, -1, 0.25) == frames[:8000]
        assert audio.read_pcm_span(reader, 0.75, 2) == frames[24000:]
        assert audio.read_pcm_span(reader, 2, 3) == b""
        for start, end in ((1, 0), (float("nan"), 1), (0, float("inf"))):
            with pytest.raises(audio.AudioError, match="границы"):
                audio.read_pcm_span(reader, start, end)
    assert reader._file is None
    assert list(tmp_path.iterdir()) == [src]


def test_pcm_truncation_is_reported(tmp_path):
    src = tmp_path / "truncated.wav"
    write_wav(src, b"\x00\x00" * 16000)
    src.write_bytes(src.read_bytes()[:-200])
    with audio.open_pcm_audio(src) as reader:
        with pytest.raises(audio.AudioError, match="обрезан"):
            audio.read_pcm_span(reader, 0, 1)


def test_non_prepared_audio_converts_once_and_cleans_up_on_error(tmp_path, monkeypatch):
    src = tmp_path / "stereo.wav"
    write_wav(src, b"\x00\x00" * 32000, rate=8000, channels=2)
    calls = []

    def convert(source, target):
        calls.append((source, target))
        write_wav(target, b"\x01\x00" * 16000)

    monkeypatch.setattr(audio, "extract_wav", convert)
    with pytest.raises(RuntimeError, match="decoder"):
        with audio.open_pcm_audio(src) as reader:
            assert len(audio.read_pcm_span(reader, 0, 0.5)) == 16000
            assert len(audio.read_pcm_span(reader, 0.5, 1)) == 16000
            raise RuntimeError("decoder")
    assert len(calls) == 1
    assert calls[0][0] == src
    assert list(tmp_path.iterdir()) == [src]


@pytest.mark.parametrize("requested,available,expected", [(0, 32, 8), (16, 32, 16), (64, 12, 12), (0, 2, 2)])
def test_whisper_thread_override(requested, available, expected, monkeypatch):
    monkeypatch.setattr(config, "WHISPER_CPU_THREADS", requested)
    monkeypatch.setattr(performance, "available_cpus", lambda: available)
    assert performance.whisper_threads() == expected


def test_cpu_affinity_and_fallback(monkeypatch):
    monkeypatch.setattr(performance.os, "sched_getaffinity", lambda pid: {2, 3, 5}, raising=False)
    assert performance.available_cpus() == 3

    def unavailable(pid):
        raise OSError("not supported")

    monkeypatch.setattr(performance.os, "sched_getaffinity", unavailable)
    monkeypatch.setattr(performance.os, "process_cpu_count", lambda: None, raising=False)
    assert performance.available_cpus() == 2


def test_torch_threads_preserve_defaults_and_apply_override(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(get_num_threads=lambda: 4, set_num_threads=calls.append)
    monkeypatch.setitem(sys.modules, "torch", fake)
    monkeypatch.setattr(performance, "available_cpus", lambda: 16)
    monkeypatch.setattr(config, "TORCH_CPU_THREADS", 0)
    performance.configure_torch_threads()
    assert calls == []
    monkeypatch.setattr(config, "TORCH_CPU_THREADS", 12)
    performance.configure_torch_threads()
    assert calls == [12]
    monkeypatch.setattr(config, "TORCH_CPU_THREADS", 64)
    performance.configure_torch_threads()
    assert calls == [12, 16]


def test_timing_includes_meeting_id_and_failure(caplog, monkeypatch):
    clock = iter([10.0, 12.5, 20.0, 21.0])
    monkeypatch.setattr(performance.time, "perf_counter", lambda: next(clock))
    with caplog.at_level(logging.INFO), meeting_scope("timed-meeting"):
        with performance.stage_timer("audio_prepare"):
            pass
        with pytest.raises(ValueError):
            with performance.stage_timer("asr"):
                raise ValueError("failed")
    assert "stage=audio_prepare seconds=2.500 outcome=ok" in caplog.records[0].message
    assert "stage=asr seconds=1.000 outcome=interrupted" in caplog.records[1].message
    assert all(r.meeting == "timed-meeting" for r in caplog.records)


def test_cancelled_slot_wait_does_not_release_another_job(monkeypatch):
    async def scenario():
        lock = asyncio.Lock()
        monkeypatch.setattr(analyze, "_local_asr_lock", lock)
        entered = asyncio.Event()

        async def wait():
            entered.set()
            async with analyze.local_asr_slot():
                pytest.fail("Cancelled waiter acquired slot")

        async with analyze.local_asr_slot():
            task = asyncio.create_task(wait())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lock.locked()
        assert not lock.locked()

    asyncio.run(scenario())


class RecordingModel:
    """Small inference boundary double; numerical conversion uses real PyTorch."""
    def __init__(self, torch):
        self.torch = torch
        self.model = self
        self._device = torch.device("cpu")
        self._dtype = torch.float32
        self.inputs = []

    def forward(self, wav, length):
        assert self.torch.is_inference_mode_enabled()
        assert wav.shape == (1, length.item())
        assert wav.dtype == self._dtype
        self.inputs.append(wav.clone())
        return wav, length

    def _decode(self, encoded, encoded_len, length, word_timestamps):
        assert self.torch.is_inference_mode_enabled()
        assert not word_timestamps
        assert encoded_len.item() == length.item()
        return [(f"реплика {len(self.inputs)}", None)]


def test_gigaam_memory_spans_preserve_pcm_and_timestamps(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    src = tmp_path / "shared.wav"
    write_wav(src, struct.pack("<4h", -32768, -16384, 0, 32767) * 8000)
    model = RecordingModel(torch)
    monkeypatch.setattr(gigaam_asr, "_load", lambda *a: model)
    monkeypatch.setattr(audio, "extract_wav", lambda *a: pytest.fail("Prepared WAV was re-encoded"))
    progress = []
    result = gigaam_asr.transcribe_spans(
        src, [{"start": 0.25, "end": 0.75}, {"start": 1, "end": 3}],
        "ru", lambda pct, msg: progress.append(pct),
    )
    assert result["language"] == "ru"
    assert [(s["start"], s["end"]) for s in result["segments"]] == [(0.25, 0.75), (1, 2)]
    assert result["text"] == "реплика 1 реплика 2"
    assert [t.numel() for t in model.inputs] == [8000, 16000]
    torch.testing.assert_close(model.inputs[0][0, :4], torch.tensor([-1, -0.5, 0, 32767 / 32768]))
    assert progress[-1] == 100
    assert list(tmp_path.iterdir()) == [src]
    with pytest.raises(ValueError, match="25 секунд"):
        gigaam_asr._transcribe_pcm(model, b"\x00\x00" * (25 * 16000 + 1))
    assert gigaam_asr._transcribe_pcm(model, b"") == ""


def test_gigaam_full_recording_uses_actual_duration_and_keeps_tail(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    src = tmp_path / "shared.wav"
    write_wav(src, b"\x01\x00" * (49 * 16000))
    model = RecordingModel(torch)
    monkeypatch.setattr(gigaam_asr, "_load", lambda *a: model)
    monkeypatch.setattr(config, "GIGAAM_CHUNK_SECONDS", 24)
    result = gigaam_asr.transcribe_gigaam_sync(src, "ru", duration=1)
    assert [(s["start"], s["end"]) for s in result["segments"]] == [(0, 24), (24, 48), (48, 49)]
    assert [t.numel() for t in model.inputs] == [24 * 16000, 24 * 16000, 16000]
    assert list(tmp_path.iterdir()) == [src]
