import math
import shutil
import struct
import subprocess
import time
import wave

import pytest
from fastapi.testclient import TestClient

from backend import auth, config, main, store


@pytest.fixture
def player(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "meetings.sqlite3")
    store.init_db()
    config.UPLOAD_DIR.mkdir(exist_ok=True)
    for token in ("owner", "other"):
        user = store.create_user(token + "@example.org", "unused")
        store.put_session(token, "user", user_id=user["id"], email=user["email"], expires=time.time() + 3600)
        if token == "owner":
            store.create_meeting("playback", "Запись", "совещание.WAV", user["id"])
            store.update_meeting("playback", status="review", transcript="Разметка")
            store.create_text_meeting("imported", "Субтитры", "meeting.srt", user["id"],
                                      {"source_type": "text", "speakers": [], "segments": []})
    client = TestClient(main.app)
    client.cookies.set(auth.USER_COOKIE, "owner")
    return client, "/api/meetings/playback/turn-audio", config.UPLOAD_DIR / "playback.wav"


def test_audio_is_owner_only_and_validated_before_decoding(player, monkeypatch):
    client, path, source = player
    calls = []
    monkeypatch.setattr(main, "playback_clip", lambda *args: calls.append(args) or b"audio")
    interval = {"start_ms": 1500, "end_ms": 2750}
    assert client.get(path, params=interval).status_code == 404
    source.write_bytes(b"recording")
    client.cookies.clear()
    assert client.get(path, params=interval).status_code == 401
    client.cookies.set(auth.USER_COOKIE, "other")
    assert client.get(path, params=interval).status_code == 404
    client.cookies.set(auth.USER_COOKIE, "owner")
    assert client.get("/api/meetings/imported/turn-audio", params=interval).status_code == 404
    for start, end in [(-1, 1000), (6000, 6000), (0, 60001), ("nan", 1000)]:
        assert client.get(path, params={"start_ms": start, "end_ms": end}).status_code == 422
    assert client.get("/api/meetings/playback").is_success and not calls
    result = client.get(path, params=interval)
    assert result.content == b"audio" and result.headers["content-type"] == "audio/mpeg"
    assert "no-store" in result.headers["cache-control"] and calls == [(source, 1.5, 1.25)]
    store.update_meeting("playback", status="transcribing")
    assert client.get(path, params=interval).status_code == 409


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
def test_listening_extracts_only_the_requested_part_of_the_original(player):
    client, path, source = player
    rate = 16000
    # Two seconds of silence followed by a tone, allowing the seek to be verified by ear-independent measurement.
    tone = b"".join(struct.pack("<h", int(16000 * math.sin(i * 2 * math.pi * 440 / rate))) for i in range(rate))
    with wave.open(str(source), "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(bytes(2 * rate * 2) + tone)
    files = list(source.parent.iterdir())
    for start, audible in [(0, False), (2000, True)]:
        response = client.get(path, params={"start_ms": start, "end_ms": start + 500})
        assert response.status_code == 200
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                             input=response.content, capture_output=True, check=True).stdout
        assert 0.48 <= len(pcm) / (2 * rate) <= 0.62  # MP3 frame padding.
        assert (max(abs(sample) for sample, in struct.iter_unpack("<h", pcm)) > 1000) == audible
    assert list(source.parent.iterdir()) == files
    assert store.get_meeting("playback")["status"] == "review"
