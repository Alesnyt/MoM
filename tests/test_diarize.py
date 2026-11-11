import sys
import wave
from types import ModuleType, SimpleNamespace

import pytest

from backend import config, diarize, worker


@pytest.fixture
def fake_diarization(tmp_path, monkeypatch):
    diarize.unload()
    audio_path = tmp_path / "recording.wav"
    frames = bytes(range(32))
    with wave.open(str(audio_path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(frames)
    state = SimpleNamespace(loads=[], calls=[], reads=[], fail=False)

    class Audio:
        def __call__(self, path):
            state.reads.append(path)
            return frames, 16000

    class Pipeline:
        @classmethod
        def from_pretrained(cls, model, *, token):
            state.loads.append((model, token))
            return cls()

        def to(self, _device):
            pass

        def __call__(self, source, **kwargs):
            state.calls.append((source, kwargs))
            if state.fail:
                raise RuntimeError("inference failed")
            if isinstance(source, dict):
                assert source["waveform"] == frames
            else:
                with wave.open(source) as audio:
                    assert audio.readframes(audio.getnframes()) == frames
            span = SimpleNamespace(start=0, end=1)
            return SimpleNamespace(itertracks=lambda **_: [(span, None, "SPEAKER_00")])

    package = ModuleType("pyannote")
    audio = ModuleType("pyannote.audio")
    audio.Audio, audio.Pipeline = Audio, Pipeline
    package.audio = audio
    monkeypatch.setitem(sys.modules, "pyannote", package)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(device=lambda device: device))
    monkeypatch.setattr(diarize, "configure_torch_threads", lambda: None)
    monkeypatch.setattr(config, "HF_TOKEN", "local-fixture")
    monkeypatch.setattr(config, "DIARIZE_MODEL", "fixture-model")
    monkeypatch.setattr(config, "DIARIZE_CACHE_MODEL", True)
    monkeypatch.setattr(config, "DIARIZE_PRELOAD_MAX_BYTES", len(frames) * 2)
    yield audio_path, state
    diarize.unload()


def test_preload_keeps_the_waveform_speaker_bounds_and_result(fake_diarization, monkeypatch):
    path, state = fake_diarization
    in_memory = diarize.diarize_turns(path, prepared=path)
    assert state.reads == [str(path)]
    assert state.calls[0][0]["sample_rate"] == 16000
    monkeypatch.setattr(config, "DIARIZE_PRELOAD_MAX_BYTES", 63)
    from_file = diarize.diarize_turns(path, prepared=path)
    assert from_file == in_memory
    assert state.reads == [str(path)]
    assert state.calls[1][0] == str(path)
    assert all(kwargs == {"min_speakers": 1, "max_speakers": config.DIARIZE_MAX_SPEAKERS} for _, kwargs in state.calls)
    assert path.exists()


def test_preload_error_uses_file_path(fake_diarization, monkeypatch):
    path, state = fake_diarization

    def unavailable(_self, _path):
        raise RuntimeError("decoder error")

    monkeypatch.setattr(sys.modules["pyannote.audio"].Audio, "__call__", unavailable)
    assert diarize.diarize_turns(path, prepared=path)
    assert state.calls[0][0] == str(path)


def test_cache_reuses_pipeline_and_reloads_for_new_configuration(fake_diarization, monkeypatch):
    path, state = fake_diarization
    for _ in range(2):
        diarize.diarize_turns(path, prepared=path)
    assert state.loads == [("fixture-model", "local-fixture")]
    monkeypatch.setattr(config, "DIARIZE_MODEL", "changed-model")
    diarize.diarize_turns(path, prepared=path)
    monkeypatch.setattr(config, "HF_TOKEN", "changed-token")
    diarize.diarize_turns(path, prepared=path)
    assert state.loads == [("fixture-model", "local-fixture"), ("changed-model", "local-fixture"),
                           ("changed-model", "changed-token")]


def test_cache_is_evicted_after_inference_error_or_idle_queue(fake_diarization, monkeypatch):
    path, state = fake_diarization
    state.fail = True
    with pytest.raises(RuntimeError, match="inference failed"):
        diarize.diarize_turns(path, prepared=path)
    state.fail = False
    diarize.diarize_turns(path, prepared=path)
    monkeypatch.setattr(worker.local_asr, "unload", lambda: None)
    monkeypatch.setattr(worker.gigaam_asr, "unload", lambda: None)
    counts = {"active": 1, "waiting": 0}
    monkeypatch.setattr(worker.store, "queue_stats", lambda: counts)
    worker._unload_idle_asr()
    diarize.diarize_turns(path, prepared=path)
    assert len(state.loads) == 2
    counts["active"] = 0
    worker._unload_idle_asr()
    diarize.diarize_turns(path, prepared=path)
    assert len(state.loads) == 3


def test_cache_disabled_loads_pipeline_for_every_recording(fake_diarization, monkeypatch):
    path, state = fake_diarization
    monkeypatch.setattr(config, "DIARIZE_CACHE_MODEL", False)
    for _ in range(2):
        diarize.diarize_turns(path, prepared=path)
    assert len(state.loads) == 2


def test_wespeaker_shares_only_identical_waveforms_and_keeps_each_mask(monkeypatch):
    torch = pytest.importorskip("torch")

    class WeSpeakerResNet34:
        hparams = SimpleNamespace(dither=0.0)
        training = False
        fbank_only = False

        def __init__(self):
            self.batch_sizes = []

        def forward_frames(self, waveforms):
            self.batch_sizes.append(len(waveforms))
            return waveforms[:, 0] * 2

        def forward_embedding(self, frames, weights=None):
            return (frames * weights).sum(dim=1)

        def forward(self, waveforms, weights=None):
            return self.forward_embedding(self.forward_frames(waveforms), weights)

    module = ModuleType("pyannote.audio.models.embedding.wespeaker")
    module.WeSpeakerResNet34 = WeSpeakerResNet34
    monkeypatch.setitem(sys.modules, module.__name__, module)

    model = WeSpeakerResNet34()
    waveforms = torch.tensor([[[1., 2.]], [[1., 2.]], [[3., 4.]], [[3., 4.]], [[1., 2.]]])
    weights = torch.tensor([[1., 0.], [0., 1.], [1., 0.], [0., 0.], [0., 1.]])
    baseline = model.forward(waveforms, weights)
    diarize._reuse_cpu_speaker_frames(SimpleNamespace(_embedding=SimpleNamespace(model_=model)))
    assert torch.equal(model.forward(waveforms, weights), baseline)
    assert model.batch_sizes == [5, 3]

    distinct = waveforms[[0, 2]]
    assert torch.equal(model.forward(distinct, weights[:2]), torch.tensor([2., 8.]))
    assert model.batch_sizes[-1] == 2

    model.training = True
    assert torch.equal(model.forward(waveforms, weights), baseline)
    assert model.batch_sizes[-1] == 5
