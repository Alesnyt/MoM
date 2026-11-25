"""Deletion remains durable across filesystem failures and upload races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend import auth, config, control, keys, main, retention, store, transcript_import


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("DATA_DIR", "UPLOAD_DIR", "AUDIO_DIR", "WHISPER_DIR"):
        monkeypatch.setattr(config, name, tmp_path if name == "DATA_DIR" else tmp_path / name.lower())
    path = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(main, "UPLOAD_DIR", config.UPLOAD_DIR)
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(main, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(main, "looks_like_audio", lambda path: True)
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True})
    store.init_db()
    user = store.create_user("owner@example.com", "unused", 1)
    return path, user


def recording(user, meeting_id="deleted", status="done"):
    store.create_meeting(meeting_id, "Meeting", "recording.webm", user["id"])
    store.update_meeting(meeting_id, status=status)
    source = config.UPLOAD_DIR / f"{meeting_id}.webm"
    source.write_bytes(b"recording")
    return source


def cleanup_row(meeting_id="deleted"):
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM recording_cleanup WHERE meeting_id = ?", (meeting_id,)).fetchone()
    return dict(row) if row else None


def test_deletion_and_cleanup_record_commit_or_rollback_together(db):
    _, user = db
    source = recording(user)
    with pytest.raises(RuntimeError, match="rollback"):
        with store.edit_meeting("deleted") as (conn, _):
            assert store.delete_meeting("deleted", connection=conn)
            raise RuntimeError("rollback")
    assert store.get_meeting("deleted") is not None
    assert cleanup_row() is None
    assert source.exists()
    assert store.delete_meeting("deleted")
    assert store.get_meeting("deleted") is None
    assert cleanup_row()["attempts"] == 0
    # Reinitializing an existing database does not lose pending deletions.
    store.init_db()
    assert store.pending_recording_cleanup() == ["deleted"]


def test_failed_file_deletion_is_retried_after_restart_and_backoff(db, monkeypatch):
    _, user = db
    source = recording(user)
    store.delete_meeting("deleted")
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("temporarily denied")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        assert retention.cleanup_recording("deleted") is False
    row = cleanup_row()
    assert row["attempts"] == 1 and "denied" in row["last_error"]
    assert source.exists() and store.pending_recording_cleanup() == []
    store.init_db()
    monkeypatch.setattr(store.time, "time", lambda: row["due_at"] + 1)
    retention.cleanup_pending_recordings()
    assert not source.exists() and cleanup_row() is None


def test_chunk_directory_failure_is_not_silently_acknowledged(db, monkeypatch):
    _, user = db
    source = recording(user)
    chunks = config.AUDIO_DIR / "deleted_chunks"
    chunks.mkdir()
    (chunks / "part.wav").write_bytes(b"private audio")
    store.delete_meeting("deleted")
    with monkeypatch.context() as patch:
        def denied(path, **kwargs):
            raise PermissionError("chunks denied")
        patch.setattr(retention.shutil, "rmtree", denied)
        assert retention.cleanup_recording("deleted") is False
    assert not source.exists() and chunks.exists() and cleanup_row() is not None
    assert retention.cleanup_recording("deleted") is True
    assert not chunks.exists() and cleanup_row() is None


def test_upload_directory_listing_failure_keeps_cleanup_pending(db, monkeypatch):
    _, user = db
    source = recording(user)
    store.delete_meeting("deleted")
    with monkeypatch.context() as patch:
        def denied(_):
            raise PermissionError("listing denied")
        patch.setattr(retention.os, "scandir", denied)
        assert retention.cleanup_recording("deleted") is False
    assert source.exists() and cleanup_row()["attempts"] == 1
    assert retention.cleanup_recording("deleted") is True
    assert not source.exists() and cleanup_row() is None


def test_chunk_stat_failure_keeps_cleanup_pending(db, monkeypatch):
    _, user = db
    recording(user)
    chunks = config.AUDIO_DIR / "deleted_chunks"
    chunks.mkdir()
    store.delete_meeting("deleted")
    original = Path.lstat
    with monkeypatch.context() as patch:
        def denied(path):
            if path == chunks:
                raise PermissionError("stat denied")
            return original(path)
        patch.setattr(Path, "lstat", denied)
        assert retention.cleanup_recording("deleted") is False
    assert chunks.exists() and cleanup_row()["attempts"] == 1
    assert retention.cleanup_recording("deleted") is True
    assert not chunks.exists() and cleanup_row() is None


@pytest.mark.parametrize("missing_root", ["uploads", "audio", "both"])
def test_missing_directories_are_safe_after_partial_cleanup(db, missing_root):
    _, user = db
    source = recording(user)
    wav = config.AUDIO_DIR / "deleted.wav"
    wav.write_bytes(b"audio")
    store.delete_meeting("deleted")
    if missing_root in {"uploads", "both"}:
        source.unlink()
        config.UPLOAD_DIR.rmdir()
    if missing_root in {"audio", "both"}:
        wav.unlink()
        config.AUDIO_DIR.rmdir()
    assert retention.cleanup_recording("deleted") is True
    assert not source.exists() and not wav.exists() and cleanup_row() is None


def test_cleanup_can_repeat_after_crash_before_acknowledgement(db, monkeypatch):
    _, user = db
    source = recording(user)
    store.delete_meeting("deleted")
    with monkeypatch.context() as patch:
        def interrupted(_):
            raise RuntimeError("interrupted after file removal")
        patch.setattr(store, "complete_recording_cleanup", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            retention.cleanup_recording("deleted")
    assert not source.exists() and cleanup_row() is not None
    store.init_db()
    retention.cleanup_pending_recordings()
    assert cleanup_row() is None


def test_pruning_and_profile_deletion_queue_every_removed_meeting(db):
    _, user = db
    first = recording(user, "old")
    second = recording(user, "recent")
    removed = store.prune_user_meetings(user["id"])
    assert [item["id"] for item in removed] == ["old"]
    assert store.pending_recording_cleanup() == ["old"]
    assert store.delete_user(user["id"]) == ["recent"]
    assert set(store.pending_recording_cleanup()) == {"old", "recent"}
    retention.cleanup_pending_recordings()
    assert not first.exists() and not second.exists()
    assert store.pending_recording_cleanup() == []


def test_cleanup_waits_for_terminal_worker_to_release_files(db):
    _, user = db
    source = recording(user)
    control.bind_job("deleted", SimpleNamespace(done=lambda: False))
    try:
        store.delete_meeting("deleted")
        assert retention.cleanup_recording("deleted") is False
        assert source.exists() and cleanup_row() is not None
    finally:
        control.release("deleted")
    retention.cleanup_pending_recordings()
    assert not source.exists() and cleanup_row() is None


def test_stale_cleanup_entry_never_deletes_a_live_meeting(db):
    _, user = db
    source = recording(user)
    with store.connect() as conn:
        conn.execute("INSERT INTO recording_cleanup (meeting_id, created_at) VALUES ('deleted', ?)", (store.utc_now(),))
    assert retention.cleanup_recording("deleted") is False
    assert source.exists() and store.get_meeting("deleted") is not None


@pytest.mark.parametrize("kind", ["audio", "text"])
def test_deleted_owner_cannot_create_a_meeting(db, kind):
    _, user = db
    store.delete_user(user["id"])
    with pytest.raises(store.MeetingOwnerMissing):
        if kind == "audio":
            store.create_meeting("orphan", "Meeting", "recording.webm", user["id"])
        else:
            store.create_text_meeting("orphan", "Meeting", "transcript.txt", user["id"], {"segments": []})
    assert store.get_meeting("orphan") is None
    assert store.claim_next_meeting(1) is None


@pytest.mark.parametrize("kind", ["audio", "text"])
def test_owner_deleted_just_after_insert_is_reported_consistently(db, monkeypatch, kind):
    _, user = db
    original = store.get_meeting

    def delete_then_read(meeting_id):
        store.delete_user(user["id"])
        return original(meeting_id)

    monkeypatch.setattr(store, "get_meeting", delete_then_read)
    with pytest.raises(store.MeetingOwnerMissing):
        if kind == "audio":
            store.create_meeting("orphan", "Meeting", "recording.webm", user["id"])
        else:
            store.create_text_meeting("orphan", "Meeting", "transcript.txt", user["id"], {"segments": []})
    assert store.get_meeting("orphan") is None and cleanup_row("orphan") is not None


@pytest.mark.parametrize("kind", ["audio", "text"])
def test_owner_check_and_insert_hold_same_write_transaction(db, monkeypatch, kind):
    path, user = db
    checked, release = threading.Event(), threading.Event()
    original = store._check_meeting_owner

    def paused(conn, user_id):
        original(conn, user_id)
        checked.set()
        assert release.wait(5)

    monkeypatch.setattr(store, "_check_meeting_owner", paused)
    create = store.create_meeting if kind == "audio" else store.create_text_meeting
    args = ("race", "Meeting", "recording.webm", user["id"])
    if kind == "text":
        args += ({"segments": []},)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create, *args)
        try:
            assert checked.wait(5)
            with sqlite3.connect(path, timeout=0) as competing:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competing.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        finally:
            release.set()
        assert future.result(timeout=5)["user_id"] == user["id"]


def authorized_client(user):
    client = TestClient(main.app)
    store.put_session("upload-race", "user", user_id=user["id"], email=user["email"], expires=time.time() + 3600)
    client.cookies.set(auth.USER_COOKIE, "upload-race")
    return client


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_profile_deleted_after_upload_is_rejected_and_files_tracked(db, monkeypatch, cleanup_fails):
    _, user = db
    client = authorized_client(user)

    def delete_owner_after_write(path):
        assert path.exists()
        store.delete_user(user["id"])
        return True

    monkeypatch.setattr(main, "looks_like_audio", delete_owner_after_write)
    if cleanup_fails:
        def denied(_):
            raise PermissionError("deletion denied")
        monkeypatch.setattr(retention, "purge_files", denied)
    response = client.post("/api/meetings", files={"file": ("recording.webm", b"audio", "video/webm")})
    assert response.status_code == 401, response.text
    assert store.list_meetings() == [] and store.claim_next_meeting(1) is None
    files = list(config.UPLOAD_DIR.iterdir())
    if cleanup_fails:
        assert len(files) == 1 and cleanup_row(files[0].stem)["attempts"] == 1
    else:
        assert files == [] and store.pending_recording_cleanup() == []


def test_profile_deleted_during_transcript_import_is_rejected(db, monkeypatch):
    _, user = db
    client = authorized_client(user)
    original = transcript_import.parse_transcript

    def parse_and_delete(text, suffix):
        document = original(text, suffix)
        store.delete_user(user["id"])
        return document

    monkeypatch.setattr(transcript_import, "parse_transcript", parse_and_delete)
    response = client.post("/api/meetings/transcript", data={"text": "Обсудили задачи на следующую неделю."})
    assert response.status_code == 401, response.text
    assert store.list_meetings() == []


def test_delete_endpoint_succeeds_with_pending_file_retry(db, monkeypatch):
    _, user = db
    source = recording(user)

    def denied(_):
        raise PermissionError("retry required")

    monkeypatch.setattr(retention, "purge_files", denied)
    assert main.delete_meeting("deleted", user={"user_id": user["id"], "email": user["email"]}) == {"ok": True}
    assert store.get_meeting("deleted") is None and source.exists()
    assert cleanup_row()["attempts"] == 1


def test_cleanup_service_processes_pending_work_after_start(db, monkeypatch):
    _, user = db
    source = recording(user)
    store.delete_meeting("deleted")

    async def pause_then_stop(stop, seconds):
        assert not source.exists()
        stop.set()

    monkeypatch.setattr(retention.background, "pause", pause_then_stop)
    asyncio.run(retention.run_file_cleanup(asyncio.Event()))
    assert cleanup_row() is None


def test_failed_meeting_insert_never_leaves_untracked_audio(db, monkeypatch):
    _, user = db

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(store, "create_meeting", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        authorized_client(user).post("/api/meetings", files={"file": ("new.wav", b"recording", "audio/wav")})
    assert list(config.UPLOAD_DIR.iterdir()) == []
    assert store.pending_recording_cleanup() == []


def test_upload_cleanup_ticket_survives_crash_and_protects_active_files(db):
    _, user = db
    mid = "new-audio"
    path = config.UPLOAD_DIR / f"{mid}.wav"
    with retention.incoming_recording(mid):
        path.write_bytes(b"active")
        retention.cleanup_pending_recordings()
        assert path.exists() and cleanup_row(mid)
        store.create_meeting(mid, "Live", "live.wav", user["id"])
    assert path.exists() and not cleanup_row(mid)

    # An uncommitted upload on disk at process startup has no in-memory owner.
    abandoned = "crashed-upload"
    store.enqueue_recording_cleanup(abandoned)
    partial = config.UPLOAD_DIR / f"{abandoned}.uploading"
    partial.write_bytes(b"partial")
    retention.cleanup_pending_recordings()
    assert not partial.exists() and not cleanup_row(abandoned)
    assert path.exists()


def test_legacy_upload_reconciliation_only_queues_unowned_mom_files(db):
    _, user = db
    owned = recording(user, "a" * 32)
    missing = config.UPLOAD_DIR / ("b" * 32 + ".webm")
    unrelated = config.UPLOAD_DIR / "internal-test.wav"
    missing.write_bytes(b"orphan")
    unrelated.write_bytes(b"fixture")
    retention.reconcile_legacy_uploads(main.ALLOWED_SUFFIXES)
    assert store.pending_recording_cleanup() == [missing.stem]
    retention.cleanup_pending_recordings()
    assert owned.exists() and unrelated.exists() and not missing.exists()
