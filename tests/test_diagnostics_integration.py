"""Real persistence/API boundaries, with no model, network or media subprocesses."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend import auth, config, control, diagnostics, keys, llm_metrics, main, pipeline, store, worker
from backend.performance import stage_timer


@pytest.fixture
def installation(tmp_path, monkeypatch):
    data = tmp_path / "private-installation"
    for name in ("uploads", "audio", "whisper"):
        (data / name).mkdir(parents=True)
    for module in (config, store):
        monkeypatch.setattr(module, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for module in (config, main, pipeline):
        monkeypatch.setattr(module, "UPLOAD_DIR", data / "uploads")
        monkeypatch.setattr(module, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(config, "get_api_key", lambda: "private-key")
    monkeypatch.setattr(config, "smtp_configured", lambda: False)
    monkeypatch.setattr(main, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(main, "looks_like_audio", lambda path: True)
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True, "message": ""})
    monkeypatch.setattr(diagnostics, "_active", {})
    monkeypatch.setattr(diagnostics, "_environment", lambda: {"logical_cpus": 12, "host_memory_total_bytes": 16 * 1024**3})
    monkeypatch.setattr(diagnostics, "_resource_values", lambda: ({"process_rss_bytes": 128 * 1024**2}, None, None))
    store.init_db()
    assert diagnostics.initialize()
    user = store.create_user("private-user@example.test", "unused")
    for token, kind, fields in (
        ("user-session", "user", {"user_id": user["id"], "email": user["email"]}),
        ("admin-session", "admin", {"username": "private-admin"}),
    ):
        store.put_session(token, kind, expires=time.time() + 3600, **fields)
    # No lifespan is started, so the real queue cannot consume test uploads.
    client = TestClient(main.app)
    yield SimpleNamespace(data=data, user=user, client=client)
    client.close()
    for meeting in store.list_meetings(user["id"]):
        control.release(meeting["id"])


def create_meeting(installation, *, transcript=""):
    meeting = store.create_meeting("private-meeting-id", "Private title", "private-recording.webm", installation.user["id"])
    if transcript:
        store.update_meeting(meeting["id"], transcript=transcript, status="analyzing")
    return meeting["id"]


def test_audio_then_protocol_persist_separate_private_runs(installation, monkeypatch):
    meeting_id = create_meeting(installation)
    (config.UPLOAD_DIR / f"{meeting_id}.webm").write_bytes(b"private media")
    monkeypatch.setattr(config, "is_local_asr", lambda model=None: True)
    monkeypatch.setattr(config, "is_gigaam_asr", lambda model=None: False)
    monkeypatch.setattr(pipeline, "diarization_available", lambda: False)
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Private output"))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    client = SimpleNamespace(close=AsyncMock(), chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
    monkeypatch.setattr(pipeline, "make_client", lambda key: client)

    def extract(src, dst):
        dst.write_bytes(b"fake WAV")
        return 60.0

    async def transcribe(*args, **kwargs):
        with stage_timer("asr"):
            return {"language": "ru", "text": "Private transcript", "segments": []}

    async def analyze(*args, **kwargs):
        await llm_metrics.completion(client, model="Private model", messages=[{"role": "user", "content": "Private transcript"}])
        return {"title": "Private title", "summary": "Private summary", "participants": [],
                "key_points": [], "decisions": [], "action_items": [], "mom": {"agenda": []}}

    monkeypatch.setattr(pipeline, "extract_wav", extract)
    monkeypatch.setattr(pipeline, "transcribe_file", transcribe)
    monkeypatch.setattr(pipeline, "apply_diarization", lambda *args, **kwargs: {"text": "Private transcript", "segments": [], "speakers": []})
    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", analyze)
    asyncio.run(pipeline.process_meeting(meeting_id))
    assert store.get_meeting(meeting_id)["status"] == "review"
    store.update_meeting(meeting_id, status="analyzing")
    asyncio.run(pipeline.process_meeting(meeting_id))
    assert store.get_meeting(meeting_id)["status"] == "done"
    assert client.close.await_count == 2
    assert not diagnostics._active
    assert diagnostics.initialize()  # final records remain durable after restart initialization
    report = diagnostics.export_report()
    by_phase = {run["phase"]: run for run in report["runs"]}
    assert set(by_phase) == {"audio", "protocol"}
    audio, protocol = by_phase["audio"], by_phase["protocol"]
    assert (audio["status"], protocol["status"]) == ("review", "done")
    assert audio["audio_seconds"] == protocol["audio_seconds"] == 60
    assert audio["meeting"] == protocol["meeting"]
    assert audio["stages"]["asr"]["count"] == 1
    assert audio["asr_rtf"] is not None
    assert protocol["stages"]["protocol"]["count"] == 1
    assert protocol["llm"][0]["prompt_tokens"] == 100
    assert all(run["resource_sample_count"] >= 1 for run in report["runs"])
    serialized = json.dumps(report)
    assert "private" not in serialized.lower()
    assert str(installation.data) not in serialized


@pytest.mark.parametrize("path", ["/api/admin/diagnostics", "/api/admin/diagnostics/export"])
def test_diagnostic_routes_require_admin_and_prevent_caching(installation, path):
    client = installation.client
    assert client.get(path).status_code == 401
    client.cookies.set(auth.USER_COOKIE, "user-session")
    assert client.get(path).status_code == 401
    client.cookies.set("mom_admin", "admin-session")
    result = client.get(path)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["available"] is True
    if path.endswith("export"):
        assert result.headers["content-disposition"] == 'attachment; filename="mom-performance-7d.json"'


@pytest.mark.parametrize("path", ["/api/admin/diagnostics", "/api/admin/diagnostics/export"])
@pytest.mark.parametrize("days", [0, 15, "invalid"])
def test_report_period_is_bounded(installation, path, days):
    installation.client.cookies.set("mom_admin", "admin-session")
    assert installation.client.get(path, params={"days": days}).status_code == 422


def test_unavailable_storage_returns_explicit_safe_error(installation, monkeypatch):
    # A directory cannot be opened as a SQLite database; application/auth DB is unaffected.
    monkeypatch.setattr(diagnostics, "_path", lambda: installation.data)
    installation.client.cookies.set("mom_admin", "admin-session")
    summary = installation.client.get("/api/admin/diagnostics")
    assert summary.status_code == 200 and summary.json()["available"] is False
    assert summary.json()["error"] == "storage_unavailable"
    result = installation.client.get("/api/admin/diagnostics/export")
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"
    assert "private" not in result.text and str(installation.data) not in result.text


@pytest.mark.parametrize("collector_fails", [False, True])
def test_upload_records_only_metrics_and_survives_collector_failure(installation, monkeypatch, collector_fails):
    if collector_fails:
        def fail(*args, **kwargs):
            raise RuntimeError("private collector failure")
        monkeypatch.setattr(diagnostics, "record_upload", fail)
    client = installation.client
    client.cookies.set(auth.USER_COOKIE, "user-session")
    content = b"private audio contents"
    response = client.post("/api/meetings", data={"title": "Private uploaded title"},
                           files={"file": ("private-filename.webm", content, "video/webm")})
    assert response.status_code == 200, response.text
    meeting = response.json()
    assert store.get_meeting(meeting["id"])["status"] == "queued"
    assert (config.UPLOAD_DIR / f'{meeting["id"]}.webm').read_bytes() == content
    report = diagnostics.export_report()
    if not collector_fails:
        assert report["exported_runs"] == 1
        run = report["runs"][0]
        assert (run["phase"], run["status"], run["size_bytes"]) == ("upload", "uploaded", len(content))
        assert set(run["stages"]) == {"upload_receive", "upload_save"}
    serialized = json.dumps(report)
    assert "private" not in serialized.lower()
    assert meeting["id"] not in serialized


@pytest.mark.parametrize("function", ["begin_run", "sample_run", "finish_run"])
def test_collector_exceptions_cannot_fail_successful_processing(installation, monkeypatch, function):
    meeting_id = create_meeting(installation)
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("private diagnostic failure")
    async def process(meeting_id):
        store.update_meeting(meeting_id, status="review", transcript="Private transcript")
    monkeypatch.setattr(diagnostics, function, fail)
    monkeypatch.setattr(pipeline, "_process_meeting", process)
    asyncio.run(worker._run_job_scoped(meeting_id))
    assert calls
    assert store.get_meeting(meeting_id)["status"] == "review"


def test_cancellation_during_begin_finalizes_created_run(installation, monkeypatch):
    meeting_id = create_meeting(installation)
    entered, release = threading.Event(), threading.Event()
    original = diagnostics.begin_run
    process = AsyncMock()
    def delayed_begin(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(diagnostics, "begin_run", delayed_begin)
    monkeypatch.setattr(pipeline, "_process_meeting", process)

    async def scenario():
        task = asyncio.create_task(pipeline.process_meeting(meeting_id))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
    asyncio.run(scenario())
    process.assert_not_awaited()
    assert not diagnostics._active
    assert diagnostics.export_report()["runs"][0]["status"] == "interrupted"


def test_processing_cancellation_finalizes_trace(installation, monkeypatch):
    meeting_id = create_meeting(installation)
    async def scenario():
        entered = asyncio.Event()
        async def process(meeting_id):
            entered.set()
            await asyncio.Future()
        monkeypatch.setattr(pipeline, "_process_meeting", process)
        task = asyncio.create_task(pipeline.process_meeting(meeting_id))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    asyncio.run(scenario())
    assert not diagnostics._active
    assert diagnostics.export_report()["runs"][0]["status"] == "interrupted"


def test_final_diagnostic_read_failure_does_not_turn_review_into_error(installation, monkeypatch):
    meeting_id = create_meeting(installation)
    original = store.get_meeting
    fail_next = [False]
    async def process(meeting_id):
        store.update_meeting(meeting_id, status="review", transcript="Private transcript")
        fail_next[0] = True
    def read(meeting_id):
        if fail_next[0]:
            fail_next[0] = False
            raise OSError("private temporary DB failure")
        return original(meeting_id)
    monkeypatch.setattr(pipeline, "_process_meeting", process)
    monkeypatch.setattr(store, "get_meeting", read)
    asyncio.run(worker._run_job_scoped(meeting_id))
    assert original(meeting_id)["status"] == "review"
    assert not diagnostics._active
    assert diagnostics.export_report()["runs"][0]["status"] == "unknown"
