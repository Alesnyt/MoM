"""Meeting eligibility checks stay valid until their writes commit."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest
from fastapi import HTTPException

from backend import config, keys, main, retention, store, worker


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True})
    monkeypatch.setattr(worker, "notify_work", lambda: None)
    monkeypatch.setattr(retention, "purge_files", lambda meeting_id: None)
    store.init_db()
    user = store.create_user("owner@example.com", "unused", 10)
    store.create_meeting("race", "Meeting", "meeting.webm", user["id"])
    return path, {"user_id": user["id"], "email": user["email"]}


@pytest.mark.parametrize("operation, initial_status, expected_status", [
    (main.retry_meeting, "error", "queued"),
    (main.delete_meeting, "review", None),
    (main.stop_meeting, "queued", "error"),
    (main.restart_meeting, "queued", "queued"),
])
def test_worker_cannot_change_eligibility_after_endpoint_read(db, monkeypatch, operation, initial_status, expected_status):
    """Reproduce the old check/write gap using a separate SQLite writer.

    The competing writer must be unable to claim/requeue a job after the API
    has accepted the old status but before the API writes its own transition.
    """
    path, user = db
    store.update_meeting("race", status=initial_status)
    read, release = threading.Event(), threading.Event()
    original_owned = main._owned

    def pause_after_read(row, owner):
        meeting = original_owned(row, owner)
        read.set()
        assert release.wait(5), "Endpoint was not released"
        return meeting

    monkeypatch.setattr(main, "_owned", pause_after_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(operation, "race", user=user)
        try:
            assert read.wait(5), "Endpoint never checked the meeting"
            # Fail immediately instead of waiting on the endpoint we paused.
            with sqlite3.connect(path, timeout=0) as competing:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute("UPDATE meetings SET status='extracting' WHERE id='race'")
        finally:
            release.set()
        response = request.result(timeout=5)
    if expected_status is None:
        assert response == {"ok": True}
        assert store.get_meeting("race") is None
    else:
        assert response["status"] == expected_status
        assert store.claim_next_meeting(2) == ("race" if expected_status == "queued" else None)
        assert store.claim_next_meeting(2) is None


def test_simultaneous_retries_enqueue_and_claim_once(db, monkeypatch):
    _, user = db
    store.update_meeting("race", status="error", transcript="Saved transcript")
    gate = threading.Barrier(8)
    claims = []

    def wake_worker():
        claims.append(store.claim_next_meeting(8))

    monkeypatch.setattr(worker, "notify_work", wake_worker)

    def retry():
        gate.wait(timeout=5)
        try:
            return main.retry_meeting("race", user=user)
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: retry(), range(8)))
    assert sum(isinstance(response, dict) for response in responses) == 1
    assert responses.count(409) == 7
    assert claims == ["race"]
    assert store.get_meeting("race")["status"] == "extracting"
    assert store.get_meeting("race")["transcript"] == "Saved transcript"
    assert store.claim_next_meeting(8) is None


def test_delete_rechecks_status_after_waiting_for_protocol_transaction(db, monkeypatch):
    path, user = db
    store.update_meeting("race", status="review", transcript="Transcript")
    started = threading.Event()
    original_edit = store.edit_meeting

    def announced_edit(meeting_id):
        started.set()
        return original_edit(meeting_id)

    monkeypatch.setattr(store, "edit_meeting", announced_edit)
    purged = []
    monkeypatch.setattr(retention, "purge_files", purged.append)

    # Protocol wins the write lock while a delete request is already in flight.
    # Its uncommitted state remains invisible to other SQLite readers.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with sqlite3.connect(path) as protocol:
            protocol.execute("BEGIN IMMEDIATE")
            protocol.execute("UPDATE meetings SET status='queued' WHERE id='race'")
            deletion = pool.submit(main.delete_meeting, "race", user=user)
            assert started.wait(5), "Delete did not attempt its transaction"
        with pytest.raises(HTTPException) as error:
            deletion.result(timeout=5)
    assert error.value.status_code == 409
    assert store.get_meeting("race")["status"] == "queued"
    assert store.claim_next_meeting(1) == "race"
    assert purged == []


@pytest.mark.parametrize("operation", [main.retry_meeting, main.delete_meeting])
def test_rejected_owner_cannot_change_meeting(db, operation):
    store.update_meeting("race", status="error")
    with pytest.raises(HTTPException) as error:
        operation("race", user={"user_id": "someone-else"})
    assert error.value.status_code == 404
    assert store.get_meeting("race")["status"] == "error"


@pytest.mark.parametrize("restart", [False, True])
def test_halt_between_worker_claim_and_task_registration_keeps_ownership(db, restart):
    """A claimed job belongs to the worker even before bind_job is called."""
    from backend import control
    from backend.pipeline import _Progress

    _, user = db
    assert store.claim_next_meeting(2) == "race"
    operation = main.restart_meeting if restart else main.stop_meeting
    try:
        response = operation("race", user=user)
        assert response["status"] == "extracting"
        assert control.is_stopped("race")
        assert store.claim_next_meeting(2) is None
        # The late-starting worker observes the stop before changing progress.
        with pytest.raises(control.ProcessingStopped):
            _Progress("race").set(10, "Working", status="transcribing")
        assert store.get_meeting("race")["status"] == "extracting"
        # Only the owner, after its work exits, may acknowledge and requeue.
        assert control.finish("race") == ("restart" if restart else "stop")
        assert store.get_meeting("race")["status"] == ("queued" if restart else "error")
        assert not control.is_stopped("race")
        assert store.claim_next_meeting(2) == ("race" if restart else None)
    finally:
        control.release("race")


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("source_type", ["audio", "text"])
def test_halt_queued_job_applies_atomically_without_cancellation_flags(db, restart, source_type):
    from backend import control

    _, user = db
    store.update_meeting(
        "race", transcript="Saved transcript", language="ru",
        transcript_segments={"source_type": source_type},
    )
    operation = main.restart_meeting if restart else main.stop_meeting
    response = operation("race", user=user)
    assert response["status"] == ("queued" if restart else "error")
    assert not control.is_stopped("race")
    if restart:
        assert response["transcript"] == ("Saved transcript" if source_type == "text" else None)
        assert store.claim_next_meeting(2) == "race"
        assert store.claim_next_meeting(2) is None
    else:
        assert store.claim_next_meeting(2) is None
        # A subsequent retry must not inherit the previous stop request.
        assert main.retry_meeting("race", user=user)["status"] == "queued"
        assert not control.is_stopped("race")
        assert store.claim_next_meeting(2) == "race"


@pytest.mark.parametrize("terminal_status", ["done", "review", "error"])
def test_worker_keeps_meeting_ownership_until_terminal_job_cleanup_exits(db, monkeypatch, terminal_status):
    """Other meetings can run, but a retry/protocol waits for its own old job."""
    import asyncio
    from backend import control

    _, user = db
    monkeypatch.setattr(config, "get_max_jobs", lambda: 2)

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(worker, "_run_delivery", idle)
    monkeypatch.setattr(worker.corporate_context, "run_sync", idle)
    monkeypatch.setattr(worker.glossary, "run_imports", idle)
    monkeypatch.setattr(worker.term_discovery, "run", idle)
    monkeypatch.setattr(worker.calendar_service, "run_cleanup", idle)
    monkeypatch.setattr(retention, "run_session_cleanup", idle)

    async def scenario():
        loop = asyncio.get_running_loop()
        cleaning, release, restarted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        other_started, skipped = asyncio.Event(), asyncio.Event()
        calls = []
        cleanup_complete = False
        requeued = False
        original_claim = store.claim_next_meeting

        def observed_claim(limit, **kwargs):
            claimed = original_claim(limit, **kwargs)
            if requeued and claimed is None and store.get_meeting("race")["status"] == "queued":
                loop.call_soon_threadsafe(skipped.set)
            return claimed

        monkeypatch.setattr(store, "claim_next_meeting", observed_claim)

        async def process(meeting_id):
            nonlocal cleanup_complete
            calls.append(meeting_id)
            if meeting_id == "other":
                store.update_meeting(meeting_id, status="done")
                other_started.set()
            elif calls.count("race") == 1:
                store.update_meeting("race", status=terminal_status, transcript="Transcript")
                cleaning.set()
                await release.wait()
                cleanup_complete = True
            else:
                assert cleanup_complete, "Same meeting restarted before its old cleanup finished"
                store.update_meeting("race", status="done")
                restarted.set()

        monkeypatch.setattr(worker, "process_meeting", process)
        stop = asyncio.Event()
        service = asyncio.create_task(worker.run_worker(stop))
        try:
            await asyncio.wait_for(cleaning.wait(), 5)
            owner = control._jobs["race"]
            if terminal_status == "review":
                await asyncio.to_thread(main.start_protocol, "race", user=user)
            else:
                await asyncio.to_thread(main.retry_meeting, "race", user=user)
            requeued = True
            store.create_meeting("other", "Other", "other.webm", user["user_id"])
            await asyncio.wait_for(other_started.wait(), 5)
            await asyncio.wait_for(skipped.wait(), 5)
            assert calls.count("race") == 1
            assert control._jobs["race"] is owner
            assert not restarted.is_set()
            release.set()
            await asyncio.wait_for(restarted.wait(), 5)
            assert calls.count("race") == 2
        finally:
            release.set()
            stop.set()
            await asyncio.wait_for(service, 7)
            control.release("race")
            control.release("other")

    asyncio.run(scenario())
