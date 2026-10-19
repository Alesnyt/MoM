"""Regression checks for the complete authenticated upload/API flow.

No external LLM, models, ffmpeg, or corporate services are contacted.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from backend import auth, config, keys, store, worker
import backend.main as main


def test_upload_route_is_bound_to_upload_handler() -> None:
    matches = [
        route for route in main.app.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/meetings"
        and "POST" in route.methods
    ]
    assert len(matches) == 1
    assert matches[0].endpoint is main.create_meeting


def test_authenticated_upload_and_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    for name in ("uploads", "audio", "whisper"):
        (data / name).mkdir(parents=True)
    db = data / "mom.db"
    for module in (config, store):
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(main, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(main, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(main, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(main, "looks_like_audio", lambda path: True)
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    monkeypatch.setattr(config, "migrate_token_plan_asr", lambda: None)
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True, "message": ""})
    monkeypatch.setattr(keys, "reset_status", lambda *args, **kwargs: None)

    async def idle(stop: asyncio.Event) -> None:
        await stop.wait()

    monkeypatch.setattr(worker, "run_worker", idle)

    with TestClient(main.app) as client:
        user = store.create_user("one@example.com", auth.hash_password("test-pass123"))
        token = "upload-test-session"
        store.put_session(
            token, "user", user_id=user["id"],
            email=user["email"], expires=time.time() + 3600,
        )
        client.cookies.set(auth.USER_COOKIE, token)
        created = client.post(
            "/api/meetings",
            data={"title": "Регрессия загрузки"},
            files={"file": ("meeting.webm", b"test recording content", "video/webm")},
        )
        assert created.status_code == 200, created.text
        doc = created.json()
        assert doc["status"] == "queued"
        assert doc["title"] == "Регрессия загрузки"
        assert (data / "uploads" / (doc["id"] + ".webm")).exists()

        archive = client.get("/api/meetings")
        assert archive.status_code == 200, archive.text
        assert [item["id"] for item in archive.json()] == [doc["id"]]
        assert client.get("/api/meetings/" + doc["id"]).status_code == 200

        other = store.create_user("two@example.com", auth.hash_password("test-pass123"))
        other_token = "second-user"
        store.put_session(
            other_token, "user", user_id=other["id"],
            email=other["email"], expires=time.time() + 3600,
        )
        client.cookies.set(auth.USER_COOKIE, other_token)
        assert client.get("/api/meetings/" + doc["id"]).status_code == 404
        assert client.get("/api/meetings").json() == []
