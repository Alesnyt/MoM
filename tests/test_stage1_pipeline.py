"""Stage 1 regression tests for cancellation at model boundaries.

Use fake model responses and real SQLite state transitions; no cloud or model
downloads. They protect the installed meeting archive during upgrades.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from backend import auth, config, control, pipeline, store


@pytest.fixture
def recording_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(store, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(pipeline, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(pipeline, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(config, "get_api_key", lambda: "unit-test-key")
    monkeypatch.setattr(pipeline, "make_client", lambda key: object())
    monkeypatch.setattr(pipeline, "notify_meeting_done", lambda meeting: None)
    store.init_db()
    user = store.create_user("stability@example.com", auth.hash_password("secret-pass"))
    return user["id"]


def _result() -> dict:
    return {
        "title": "Тестовая встреча",
        "language": "ru",
        "participants": [],
        "date_hint": None,
        "summary": "Обсудили тесты.",
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "mom": {"agenda": [], "next_meeting": None},
    }


def test_stop_during_llm_cannot_publish_completed_protocol(
    recording_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "stop-llm-1"
    store.create_meeting(meeting_id, "До обработки", "test.webm", recording_db)
    store.update_meeting(meeting_id, status="analyzing", transcript="[00:00] Спикер 1: привет")

    async def scenario() -> None:
        entered = asyncio.Event()
        complete = asyncio.Event()

        async def fake_analysis(*args, **kwargs):
            entered.set()
            await complete.wait()
            return _result()

        monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", fake_analysis)
        task = asyncio.create_task(pipeline.process_meeting(meeting_id))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            control.request_stop(meeting_id, restart=False)
            complete.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            complete.set()
            control.release(meeting_id)

    asyncio.run(scenario())
    stopped = store.get_meeting(meeting_id)
    assert stopped is not None
    assert stopped["status"] == "error"
    assert stopped["result"] is None
    assert stopped["error"] == "Обработка остановлена"


def test_stop_during_gigaam_cannot_publish_review(
    recording_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "stop-gigaam-1"
    store.create_meeting(meeting_id, "До обработки", "test.webm", recording_db)
    (config.UPLOAD_DIR / f"{meeting_id}.webm").write_bytes(b"test audio input")
    monkeypatch.setattr(config, "is_gigaam_asr", lambda model=None: True)
    monkeypatch.setattr(pipeline, "diarization_available", lambda: True)

    async def fake_extract(src, dst):
        dst.write_bytes(b"mp3")
        return 10.0

    def fake_wav(src, dst):
        dst.write_bytes(b"wav")
        return 10.0

    monkeypatch.setattr(pipeline, "extract_audio_async", fake_extract)
    monkeypatch.setattr(pipeline, "extract_wav", fake_wav)
    monkeypatch.setattr(
        pipeline,
        "diarize_turns",
        lambda audio_path, prepared=None: [{"start": 0, "end": 10, "speaker": "original"}],
    )
    entered = threading.Event()
    complete = threading.Event()

    def fake_spans(src, spans, language, callback):
        entered.set()
        if not complete.wait(timeout=5):
            raise AssertionError("Test release signal not sent")
        return {
            "language": "ru",
            "text": "текст записи",
            "segments": [{"start": 0, "end": 10, "text": "текст записи"}],
        }

    monkeypatch.setattr(pipeline, "transcribe_spans", fake_spans)

    async def scenario() -> None:
        task = asyncio.create_task(pipeline.process_meeting(meeting_id))
        try:
            assert await asyncio.to_thread(entered.wait, 4), "ASR did not start"
            control.request_stop(meeting_id, restart=False)
            complete.set()
            await asyncio.wait_for(task, timeout=6)
        finally:
            complete.set()
            control.release(meeting_id)

    asyncio.run(scenario())
    stopped = store.get_meeting(meeting_id)
    assert stopped is not None
    assert stopped["status"] == "error"
    assert stopped["transcript"] is None
    assert stopped["error"] == "Обработка остановлена"


def test_protocol_still_completes_when_not_cancelled(
    recording_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "success-llm-1"
    store.create_meeting(meeting_id, "Тест", "test.webm", recording_db)
    store.update_meeting(meeting_id, status="analyzing", transcript="[00:00] Спикер 1: привет")

    async def fake_analysis(*args, **kwargs):
        return _result()

    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", fake_analysis)
    asyncio.run(pipeline.process_meeting(meeting_id))
    doc = store.get_meeting(meeting_id)
    assert doc is not None
    assert doc["status"] == "done"
    assert doc["result"] == _result()


def test_restart_during_llm_queues_fresh_processing_without_stale_result(
    recording_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "restart-llm-1"
    store.create_meeting(meeting_id, "До обработки", "test.webm", recording_db)
    store.update_meeting(meeting_id, status="analyzing", transcript="[00:00] Спикер 1: привет")

    async def scenario() -> None:
        entered = asyncio.Event()
        complete = asyncio.Event()

        async def fake_analysis(*args, **kwargs):
            entered.set()
            await complete.wait()
            return _result()

        monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", fake_analysis)
        task = asyncio.create_task(pipeline.process_meeting(meeting_id))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            control.request_stop(meeting_id, restart=True)
            complete.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            complete.set()
            control.release(meeting_id)

    asyncio.run(scenario())
    meeting = store.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting["status"] == "queued"
    assert meeting["result"] is None
    assert meeting["transcript"] is None
    assert store.claim_next_meeting(1) == meeting_id


def test_parallel_gigaam_uses_a_single_model_slot(
    recording_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "is_gigaam_asr", lambda model=None: True)
    monkeypatch.setattr(pipeline, "diarization_available", lambda: True)

    async def fake_extract(src, dst):
        dst.write_bytes(b"mp3")
        return 10.0

    def fake_wav(src, dst):
        dst.write_bytes(b"wav")
        return 10.0

    monkeypatch.setattr(pipeline, "extract_audio_async", fake_extract)
    monkeypatch.setattr(pipeline, "extract_wav", fake_wav)
    monkeypatch.setattr(
        pipeline, "diarize_turns",
        lambda audio_path, prepared=None: [{"start": 0, "end": 10, "speaker": "original"}],
    )
    running = 0
    peak = 0
    lock = threading.Lock()

    def fake_spans(src, spans, language, callback):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            time.sleep(0.15)
            return {
                "language": "ru",
                "text": "привет",
                "segments": [{"start": 0, "end": 10, "text": "привет"}],
            }
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(pipeline, "transcribe_spans", fake_spans)
    ids = ["parallel-1", "parallel-2"]
    for meeting_id in ids:
        store.create_meeting(meeting_id, "Параллельная", "test.webm", recording_db)
        (config.UPLOAD_DIR / f"{meeting_id}.webm").write_bytes(b"test audio input")

    async def scenario() -> None:
        await asyncio.gather(*(pipeline.process_meeting(mid) for mid in ids))

    asyncio.run(scenario())
    assert peak == 1, "Two simultaneous GigaAM calls share the same model"
    assert [store.get_meeting(mid)["status"] for mid in ids] == ["review", "review"]
