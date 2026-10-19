"""In-flight cancellation must not free the concurrency slot before a thread exits."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from backend import auth, config, control, store, worker


def test_worker_holds_slot_until_inflight_thread_returns(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(store, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(config, "get_max_jobs", lambda: 1)
    store.init_db()
    user = store.create_user("worker@example.com", auth.hash_password("test-password"))
    store.create_meeting("first", "Первая", "one.webm", user["id"])
    store.create_meeting("second", "Вторая", "two.webm", user["id"])

    first_entered = threading.Event()
    second_entered = threading.Event()
    first_release = threading.Event()
    second_release = threading.Event()

    def fake_work(meeting_id):
        if meeting_id == "first":
            first_entered.set()
            assert first_release.wait(timeout=8)
        elif meeting_id == "second":
            second_entered.set()
            assert second_release.wait(timeout=8)

    async def fake_scoped(meeting_id):
        await asyncio.to_thread(fake_work, meeting_id)
        if control.is_stopped(meeting_id):
            control.finish(meeting_id)

    monkeypatch.setattr(worker, "_run_job_scoped", fake_scoped)

    async def scenario():
        stop = asyncio.Event()
        service = asyncio.create_task(worker.run_worker(stop))
        try:
            assert await asyncio.to_thread(first_entered.wait, 5)
            control.request_stop("first", restart=False)
            assert control.cancel_job("first")
            await asyncio.sleep(0.3)
            assert not second_entered.is_set(), "New job started before old ASR thread stopped"
            first_release.set()
            assert await asyncio.to_thread(second_entered.wait, 5)
        finally:
            first_release.set()
            second_release.set()
            stop.set()
            worker.notify_work()
            await asyncio.wait_for(service, timeout=7)

    asyncio.run(scenario())
    first = store.get_meeting("first")
    assert first is not None and first["status"] == "error"
    assert store.get_meeting("second") is not None
