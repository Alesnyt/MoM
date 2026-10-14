from backend.pipeline import has_saved_transcript


def test_resume_only_when_transcript_exists() -> None:
    assert has_saved_transcript({"transcript": "Спикер 1: текст"})
    assert has_saved_transcript({"transcript": "  "}) is False
    assert has_saved_transcript({}) is False


def test_review_keeps_speaker_edits_until_protocol(tmp_path, monkeypatch) -> None:
    import time

    from fastapi.testclient import TestClient

    import backend.main as main
    from backend import auth, config, keys, store, worker

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", tmp_path / "whisper")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    monkeypatch.setattr(config, "migrate_token_plan_asr", lambda: None)
    monkeypatch.setattr(keys, "reset_status", lambda message="": None)
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True, "message": ""})

    async def _idle(stop) -> None:
        await stop.wait()

    monkeypatch.setattr(worker, "run_worker", _idle)

    with TestClient(main.app) as client:
        user = store.create_user("a@example.com", auth.hash_password("secret-pass"), 5)
        store.create_meeting("meet1", "Планёрка", "plan.webm", user["id"])
        store.update_meeting(
            "meet1",
            status="review",
            transcript="[00:00] Спикер 1: привет",
            transcript_segments={
                "speakers": [{"id": "S1", "name": "Спикер 1"}],
                "segments": [{"start": 0, "end": 1, "text": "привет", "speaker": "S1"}],
            },
        )
        token = "user-review"
        store.put_session(token, "user", user_id=user["id"], email="a@example.com", expires=time.time() + 3600)
        client.cookies.set(auth.USER_COOKIE, token)

        moved = client.patch(
            "/api/meetings/meet1/segments",
            json={"speaker_id": "new", "segment_indexes": [0]},
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["status"] == "review"
        assert moved.json()["segments"][0]["speaker"] == "S2"

        started = client.post("/api/meetings/meet1/protocol")
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["status"] == "queued"
        assert "привет" in body["transcript"]
        assert store.claim_next_meeting(1) == "meet1"
