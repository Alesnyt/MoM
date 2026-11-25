import asyncio
import io
import json
import os
import wave
from email import policy
from email.parser import BytesParser
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import httpx2 as httpx
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from backend import config, main, pipeline, remote_asr, store


@pytest.fixture
def admin(tmp_path, monkeypatch):
    for key in ("WHISPER_MODEL", "ASR_API_BASE_URL", "ASR_API_KEY", "ASR_API_MODEL",
                "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    store.init_db()
    monkeypatch.setitem(main.app.dependency_overrides, main.require_admin, lambda: {"username": "admin"})
    with patch.dict(os.environ):
        yield TestClient(main.app)


DRAFT = {"base_url": "https://gpu.test/v1/audio/transcriptions", "api_key": "asr-test-key",
         "model": "gigaam-v3-local"}


def transport(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)))


def multipart(request):
    header = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    parts = BytesParser(policy=policy.default).parsebytes(header + request.content).iter_parts()
    return {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in parts}


def test_discovery_probe_save_and_pipeline(admin, monkeypatch, tmp_path):
    requests = []

    def handle(request):
        assert request.url.host == "gpu.test"
        assert request.headers["authorization"] == "Bearer asr-test-key"
        requests.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "minimax", "mode": "chat"},
                {"id": "gigaam-v3-local", "object": "model", "owned_by": "openai", "mode": "audio_transcription"},
                {"id": "unclassified"},
            ]})
        assert request.url.path == "/v1/audio/transcriptions"
        fields = multipart(request)
        assert fields["model"] == b"gigaam-v3-local"
        assert fields["response_format"] == b"verbose_json" and fields["timestamp_granularities[]"] == b"segment"
        with wave.open(io.BytesIO(fields["file"])) as wav:
            assert wav.getframerate() == 16000
        return httpx.Response(200, json={"text": "Реплика", "segments": [{"start": 0, "end": 1, "text": "Реплика"}]})

    transport(monkeypatch, handle)
    config.set_runtime("sk-ws-chat-key", config.QWEN_BASE_INTL, "qwen-plus", "local-whisper")
    assert admin.post("/api/settings/asr/models", json=DRAFT).json() == {"models": ["gigaam-v3-local"]}
    assert admin.post("/api/settings/asr/test", json=DRAFT).json() == {"ok": True}
    assert config.get_asr_model() == "local-whisper"
    saved = admin.post("/api/settings/asr", json={**DRAFT, "engine": "remote-api"}).json()
    assert saved["asr"] == {"engine": "remote-api", "base_url": "https://gpu.test/v1",
                            "model": DRAFT["model"], "key_set": True}
    assert config.get_chat_model() == "qwen-plus" and config.get_api_key() == "sk-ws-chat-key"
    assert DRAFT["api_key"] not in json.dumps(saved) + json.dumps(store.list_audit())
    public = admin.get("/api/health").json()
    assert "asr" not in public and "gpu.test" not in json.dumps(public)
    assert dotenv_values(config.ENV_PATH)["ASR_API_MODEL"] == DRAFT["model"]

    path = tmp_path / "meeting.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(bytes(32000))
    payload = asyncio.run(pipeline._transcribe(None, path, "test", 1, SimpleNamespace(set=lambda *args, **kwargs: None)))
    assert payload["segments"] == [{"start": 0, "end": 1, "text": "Реплика"}]
    assert requests == ["/v1/models", "/v1/audio/transcriptions", "/v1/audio/transcriptions"]


def test_key_reuse_atomicity_and_per_meeting_settings(admin):
    fields = {**DRAFT, "engine": "remote-api"}
    assert admin.post("/api/settings/asr", json=fields).is_success
    with config.model_settings_scope():
        assert admin.post("/api/settings/asr", json={**fields, "api_key": "", "model": "next"}).is_success
        assert config.get_asr_connection().model == DRAFT["model"]
    assert config.get_asr_connection().model == "next"
    switched = {**fields, "api_key": "", "base_url": "https://other.test/v1"}
    for route in ("", "/models", "/test"):
        assert admin.post("/api/settings/asr" + route, json=switched).status_code == 400
    assert config.get_asr_connection().key == DRAFT["api_key"]
    config.set_openai_api_key("sk-sp-other")
    config.migrate_token_plan_asr()
    assert config.get_asr_model() == config.REMOTE_ASR
    assert admin.post("/api/settings/models", json={"chat_model": "new-llm"}).is_success
    assert config.get_asr_model() == config.REMOTE_ASR
    assert admin.post("/api/settings/asr", json={"engine": "local-whisper-medium"}).is_success
    assert config.get_asr_model() == "local-whisper-medium" and config.get_chat_model() == "new-llm"


@pytest.mark.parametrize("status,body,editable", [
    (200, {"data": [{"id": "second"}, {"id": "first"}, {"id": "second"}]}, ["first", "second"]),
    (200, {"data": [{"id": "llm", "mode": "chat"}]}, []),
    (404, {}, []),
    (401, {"detail": "upstream-private-message"}, None),
    (307, {}, None),
])
def test_catalog_fallback_and_rejected_key(admin, monkeypatch, status, body, editable):
    hosts = []
    def handle(request):
        hosts.append(request.url.host)
        return httpx.Response(status, json=body, headers={"Location": "https://other.test/v1/models"})
    transport(monkeypatch, handle)
    res = admin.post("/api/settings/asr/models", json=DRAFT)
    assert hosts == ["gpu.test"]
    if editable is None:
        assert res.status_code == 502 and "upstream-private-message" not in res.text
    else:
        assert res.json() == {"models": editable}


def test_chunked_transcription_offsets_and_text_only(admin, monkeypatch, tmp_path):
    # A single client retains negotiated response format across chunks.
    requests = []
    def handle(request):
        fields = multipart(request)
        requests.append(fields["response_format"])
        if len(requests) == 1:
            return httpx.Response(422, json={"detail": "response_format: input should be json"})
        if len(requests) == 2:
            return httpx.Response(200, json={"text": "Первая"})
        return httpx.Response(200, json={"text": "Вторая", "segments": [{"start": 1, "end": 2, "text": "Вторая"}]})
    transport(monkeypatch, handle)
    monkeypatch.setattr(pipeline, "MAX_WHISPER_BYTES", 44 + 18 * 32000)
    monkeypatch.setattr(pipeline, "AUDIO_DIR", tmp_path)
    src = tmp_path / "recording.wav"
    with wave.open(str(src), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(bytes(30 * 32000))
    config.save_asr(config.REMOTE_ASR, DRAFT["base_url"], DRAFT["api_key"], DRAFT["model"])
    out = asyncio.run(pipeline._transcribe(None, src, "chunks", 30, SimpleNamespace(set=lambda *args, **kwargs: None)))
    assert requests == [b"verbose_json", b"json", b"json"]
    assert out["text"] == "Первая\nВторая"
    assert [(s["start"], s["end"]) for s in out["segments"]] == [(None, None), (19, 20)]


def test_asr_admin_only():
    anonymous = TestClient(main.app)
    for suffix in ("", "/models", "/test"):
        response = anonymous.post("/api/settings/asr" + suffix, json={**DRAFT, "engine": "remote-api"})
        assert response.status_code in (401, 403)


def test_wav_fragments_keep_samples_and_offsets(tmp_path):
    from backend import audio
    if not audio.ffmpeg_available():
        pytest.skip("ffmpeg required")
    src = tmp_path / "source.wav"
    pcm = bytes(range(256)) * 600
    with wave.open(str(src), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(pcm)
    chunks = audio.split_audio_timed(src, tmp_path / "parts", 2)
    restored = bytearray()
    for start, path in chunks:
        assert start == pytest.approx(len(restored) / 32000, abs=.000001)
        with wave.open(str(path), "rb") as wav:
            restored.extend(wav.readframes(wav.getnframes()))
    assert len(chunks) == 3 and restored == pcm


@pytest.mark.parametrize("status", [401, 403, 307, 500])
def test_upload_failures_keep_audio_and_key_on_selected_service(admin, monkeypatch, status):
    calls = []
    def handle(request):
        calls.append((request.url.host, request.url.path))
        return httpx.Response(status, json={"detail": "upstream-private-message"},
                              headers={"Location": "https://other.test/upload"})
    transport(monkeypatch, handle)
    response = admin.post("/api/settings/asr/test", json=DRAFT)
    assert calls == [("gpu.test", "/v1/audio/transcriptions")]
    assert response.status_code == 502
    assert "upstream-private-message" not in response.text and DRAFT["api_key"] not in response.text


def test_remote_recognition_recovers_checkpoint_and_model_change_restarts(admin, monkeypatch, tmp_path):
    from backend import keys
    user = store.create_user("asr@example.test", "unused")
    store.create_meeting("resume-asr", "Resume", "test.wav", user["id"])
    config.save_asr(config.REMOTE_ASR, DRAFT["base_url"], DRAFT["api_key"], DRAFT["model"])
    assert keys.snapshot()["connected"] is False and keys.public_snapshot()["asr_ready"] is True
    main._require_model_ready(protocol=False)
    with pytest.raises(main.HTTPException):
        main._require_model_ready(protocol=True)

    monkeypatch.setattr(pipeline, "CHUNK_SECONDS", 1)
    monkeypatch.setattr(pipeline, "AUDIO_DIR", tmp_path)
    path = tmp_path / "input.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        for value in range(10):
            wav.writeframes(bytes([value]) * 32000)
    calls = []
    fail = True

    async def transcribe(path):
        nonlocal fail
        with wave.open(str(path), "rb") as wav:
            part = wav.readframes(1)[0]
        assert list(path.parent.glob("*.wav")) == [path]
        calls.append(part)
        if fail and part == 8:
            fail = False
            raise RuntimeError("temporary")
        return {"text": str(part), "segments": [{"start": 0, "end": 1, "text": str(part)}]}

    async def run():
        speech = SimpleNamespace(transcribe=transcribe)
        return await pipeline._transcribe_remote(speech, config.get_asr_connection(), path, "resume-asr",
                                                 SimpleNamespace(set=lambda *a, **kw: None), True)
    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(run())
    assert calls == list(range(9))
    calls.clear()
    result = asyncio.run(run())
    assert calls == [8, 9]
    assert [s["start"] for s in result["segments"]] == list(range(10))
    assert not (tmp_path / "resume-asr_chunks").exists()

    config.save_asr(config.REMOTE_ASR, DRAFT["base_url"], "", "another-model")
    calls.clear()
    asyncio.run(run())
    assert calls == list(range(10))
    pipeline._publish_transcript("resume-asr", transcript="complete", status="review")
    with store.connect() as conn:
        assert not conn.execute("SELECT 1 FROM asr_checkpoints").fetchone()


def test_gpu_meeting_reaches_review_without_llm_key(admin, monkeypatch, tmp_path):
    user = store.create_user("pipeline@example.test", "unused")
    store.create_meeting("independent", "Только речь", "sample.wav", user["id"])
    config.save_asr(config.REMOTE_ASR, DRAFT["base_url"], DRAFT["api_key"], DRAFT["model"])
    monkeypatch.setattr(pipeline, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "AUDIO_DIR", tmp_path / "prepared")
    monkeypatch.setattr(pipeline, "make_client", lambda *a: pytest.fail("unexpected LLM client"))
    def copy_prepared(src, dst):
        dst.parent.mkdir()
        dst.write_bytes(src.read_bytes())
        return 1

    monkeypatch.setattr(pipeline, "extract_wav", copy_prepared)
    assert not config.get_api_key()
    src = tmp_path / "independent.wav"
    with wave.open(str(src), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(bytes(32000))
    transport(monkeypatch, lambda req: httpx.Response(200, json={"text": "Добрый день"}))
    asyncio.run(pipeline.process_meeting("independent"))
    item = store.get_meeting("independent")
    assert item["status"] == "review" and "Добрый день" in item["transcript"]
