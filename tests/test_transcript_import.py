"""Text import uses real HTTP/SQLite; models and corporate services stay offline."""
import asyncio
import time
from io import BytesIO
from urllib.parse import unquote

import pytest
from docx import Document
from fastapi.testclient import TestClient

from backend import auth, config, control, keys, pipeline, store, worker
from backend import main, transcript_import as ti
from backend.speakers import render_transcript


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for module in (config, store):
        monkeypatch.setattr(module, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(config, "DATA_DIR", data)
    for module in (config, main, pipeline):
        monkeypatch.setattr(module, "UPLOAD_DIR", data / "uploads")
        monkeypatch.setattr(module, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    monkeypatch.setattr(config, "migrate_token_plan_asr", lambda: None)
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": False, "message": "Нет ключа"})
    monkeypatch.setattr(keys, "reset_status", lambda *a, **kw: None)

    def forbidden(*a, **kw):
        raise AssertionError("Text import must not call audio/model services")

    for name in ("ffmpeg_available", "looks_like_audio"):
        monkeypatch.setattr(main, name, forbidden)
    for name in ("extract_audio_async", "extract_wav", "_transcribe", "diarize_turns"):
        monkeypatch.setattr(pipeline, name, forbidden)

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(worker, "run_worker", idle)
    with TestClient(main.app) as http:
        login(http, "owner@example.com")
        yield http


def login(client, email):
    user = store.create_user(email, "unused", archive_limit=100)
    token = email
    store.put_session(token, "user", user_id=user["id"], email=email, expires=time.time() + 3600)
    client.cookies.set(auth.USER_COOKIE, token)


def create(client, text="Анна: Привет\nИван: Сделаю завтра"):
    response = client.post("/api/meetings/transcript", data={"text": text, "title": "Совещание"})
    assert response.status_code == 200, response.text
    return response.json()


def test_names_timestamps_and_multiline():
    doc = ti.parse_transcript("\ufeff[01:02:03,250] **Анна Иванова:** Привет\r\nпродолжение\r\nанна   Иванова: Далее\n00:05 Иван: Ответ")
    assert doc["speakers"] == [{"id": "S1", "name": "Анна Иванова"}, {"id": "S2", "name": "Иван"}]
    assert [s["speaker"] for s in doc["segments"]] == ["S1", "S1", "S2"]
    assert doc["segments"][0]["start"] == 3723.25
    assert doc["segments"][1]["start"] is None
    assert doc["segments"][0]["text"] == "Привет\nпродолжение"
    assert "Анна Иванова: Далее" in render_transcript(doc)
    assert "[00:00]" not in render_transcript(doc)


@pytest.mark.parametrize("suffix,text,start,end", [
    (".srt", "1\n00:00:01,500 --> 00:00:03,000\nАнна: Привет", 1.5, 3),
    (".vtt", "WEBVTT\n\ncue-id\n00:01.500 --> 00:03.000 align:start\n<v Анна>Привет</v>", 1.5, 3),
    (".md", "**Анна**: Привет", None, None),
])
def test_file_formats(client, suffix, text, start, end):
    response = client.post("/api/meetings/transcript", files={"file": ("../../meeting" + suffix, text.encode("utf-8"))})
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["filename"] == "meeting" + suffix
    assert doc["speakers"][0]["name"] == "Анна"
    assert doc["segments"][0]["start"] == start
    assert doc["segments"][0]["end"] == end
    assert not list(config.UPLOAD_DIR.iterdir())


def test_review_persistence_rename_reassign_and_protocol(client, monkeypatch):
    doc = create(client)
    mid = doc["id"]
    assert doc["status"] == "review" and doc["source_type"] == "text"
    assert store.claim_next_meeting(1) is None
    store.init_db()  # Existing schema/reopen preserves source and names.
    assert store.get_meeting(mid)["segments"] == doc["segments"]
    assert client.post(f"/api/meetings/{mid}/protocol").status_code == 400
    renamed = client.patch(f"/api/meetings/{mid}/speakers/S1", json={"name": "Анна Петрова"}).json()
    assert renamed["source_type"] == "text"
    moved = client.patch(f"/api/meetings/{mid}/segments", json={"speaker_id": "S1", "segment_indexes": [1]}).json()
    assert moved["status"] == "review"
    assert moved["source_type"] == "text"
    assert "Анна Петрова: Сделаю завтра" in moved["transcript"]
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True})
    assert client.post(f"/api/meetings/{mid}/protocol").json()["status"] == "queued"
    monkeypatch.setattr(config, "get_api_key", lambda: "fake")
    monkeypatch.setattr(pipeline, "make_client", lambda key: object())

    async def analyze(client, transcript, *args):
        assert "Анна Петрова: Сделаю завтра" in transcript
        return {"summary": "Готово", "participants": ["Анна Петрова"], "action_items": [], "mom": {}}

    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", analyze)
    asyncio.run(pipeline.process_meeting(mid))
    assert store.get_meeting(mid)["status"] == "done"
    retried = client.post(f"/api/meetings/{mid}/retry").json()
    assert retried["transcript"] == moved["transcript"]
    restarted = client.post(f"/api/meetings/{mid}/restart").json()
    assert restarted["segments"] == moved["segments"]
    assert restarted["source_type"] == "text"
    assert client.post(f"/api/meetings/{mid}/stop").json()["status"] == "error"
    assert client.post(f"/api/meetings/{mid}/retry").json()["transcript"] == moved["transcript"]
    control.release(mid)


def test_auth_and_owner_isolation(client):
    doc = create(client)
    path = f'/api/meetings/{doc["id"]}'
    login(client, "other@example.com")
    assert client.get("/api/meetings").json() == []
    for method, suffix, kwargs in [
        ("get", "", {}), ("delete", "", {}), ("get", "/export.md", {}), ("get", "/export.docx", {}),
        ("post", "/protocol", {}), ("post", "/retry", {}), ("post", "/restart", {}),
        ("patch", "/speakers/S1", {"json": {"name": "Changed"}}),
        ("patch", "/segments", {"json": {"speaker_id": "S1", "segment_indexes": [0]}}),
    ]:
        assert getattr(client, method)(path + suffix, **kwargs).status_code == 404
    client.cookies.clear()
    assert client.post("/api/meetings/transcript", data={"text": "А: Б"}).status_code == 401
    assert client.get(path + "/export.docx").status_code == 401


def test_word_export_contains_the_protocol_and_preserves_russian_text(client):
    meeting = create(client, text="Анна: Закрытый транскрипт")
    meeting_id = meeting["id"]
    path = f"/api/meetings/{meeting_id}/export.docx"
    assert client.get(path).status_code == 409
    result = {
        "title": "Запуск «Пилота» / ноябрь", "summary": "Уточнили план.\n\nСверили ограничения.",
        "participants": ["Алёна Ёлкина"], "date_hint": "9 ноября",
        "key_points": ["• ПИЛОТ УТВЕРЖДЁН", "Бюджет согласуют отдельно.", "Согласовать смету"],
        "decisions": ["Пилот утверждён.", "Пилот утверждён"],
        "action_items": [
            {"task": "Согласовать смету", "assignee": "Алёна Ёлкина", "due": "к пятнице", "priority": "high"},
            {"task": "Согласовать смету", "assignee": "Алёна Ёлкина", "due": "к пятнице", "priority": "high"},
            {"task": "Согласовать смету", "assignee": "Анна", "due": "в понедельник", "priority": "medium"},
            {"task": "Проверить подпись \x0c и формулу A < B"},
        ],
        "mom": {"agenda": [{"topic": "Бюджет", "discussion": "Обсудили два сценария.", "outcome": "Оценки передадут на проверку."}],
                "next_meeting": "На следующей неделе"},
    }
    store.update_meeting(meeting_id, status="done", result=result)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].endswith("wordprocessingml.document")
    assert "Запуск Пилота ноябрь.docx" in unquote(response.headers["content-disposition"])
    assert response.headers["cache-control"] == "no-store"
    document = Document(BytesIO(response.content))
    assert document.paragraphs[0].style.name == "Title"
    paragraphs = [p.text for p in document.paragraphs]
    assert paragraphs.count("Пилот утверждён.") == 1
    assert "Бюджет согласуют отдельно." in paragraphs and "Сверили ограничения." in paragraphs
    assert "На следующей неделе" in paragraphs and "Закрытый транскрипт" not in "\n".join(paragraphs)
    table = [[cell.text for cell in row.cells] for row in document.tables[0].rows]
    assert table == [
        ["Поручение", "Исполнитель", "Срок", "Приоритет"],
        ["Согласовать смету", "Алёна Ёлкина", "к пятнице", "Высокий"],
        ["Согласовать смету", "Анна", "в понедельник", "Средний"],
        ["Проверить подпись  и формулу A < B", "Не назначен", "Не указан", "Средний"],
    ]


@pytest.mark.parametrize("kwargs,status", [
    ({"data": {"text": " "}}, 400),
    ({"data": {"text": "Анна: \x00"}}, 400),
    ({"data": {"text": "Анна: Текст", "title": "x" * 201}}, 400),
    ({"files": {"file": ("x.exe", b"Name: Text")}}, 400),
    ({"files": {"file": ("x.txt", b"\xff\xfe")}}, 400),
    ({"files": {"file": ("x.txt", b"a" * (ti.MAX_TEXT_BYTES + 1))}}, 413),
    ({"files": {"file": ("x.txt", b"A: B")}, "data": {"text": "A: B"}}, 400),
    ({"data": {"text": "Я" * ti.MAX_TEXT_BYTES}}, 413),
    ({"files": {"text": (None, "x" * (ti.MAX_TEXT_BYTES + 1))}}, 413),
    ({"files": [("text", (None, "A: B")), ("text", (None, "C: D"))]}, 400),
])
def test_invalid_input_leaves_no_meetings_or_files(client, kwargs, status):
    response = client.post("/api/meetings/transcript", **kwargs)
    assert response.status_code == status, response.text
    assert client.get("/api/meetings").json() == []
    assert not list(config.UPLOAD_DIR.iterdir())


@pytest.mark.parametrize("text,suffix", [
    ("[00:99] Анна: текст", ".txt"),
    ("1\n00:02 --> 00:01\nАнна: текст", ".srt"),
    ("Not subtitles", ".vtt"),
    ("x" * 81 + ": text", ".txt"),
    ("\n".join(f"Person {i}: text" for i in range(201)), ".txt"),
    ("A: text\n" * 10001, ".txt"),
])
def test_parser_limits(text, suffix):
    with pytest.raises(ti.TranscriptError):
        ti.parse_transcript(text, suffix)


def test_html_is_plain_text_and_unnamed_text_is_preserved(client):
    doc = create(client, "<script>alert(1)</script>\nАнна: <img src=x onerror=alert(1)>")
    assert doc["speakers"][0] == {"id": "unknown", "name": "Неясно"}
    assert doc["segments"][0]["text"] == "<script>alert(1)</script>"
    assert "<img" in doc["transcript"]


def test_chunked_body_guard_does_not_trust_content_length(monkeypatch):
    monkeypatch.setattr(ti, "MAX_REQUEST_BYTES", 16)
    called, sent = [], []

    async def app(*args):
        called.append(True)

    messages = iter([
        {"type": "http.request", "body": b"x" * 10, "more_body": True},
        {"type": "http.request", "body": b"x" * 10, "more_body": False},
    ])

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(ti.TranscriptBodyLimit(app)({"type": "http", "method": "POST", "path": "/api/meetings/transcript", "headers": [(b"content-length", b"1")]}, receive, send))
    assert not called
    assert sent[0]["status"] == 413


def test_archive_limit_only_prunes_owners_completed_items(client):
    old = create(client)
    with store.connect() as conn:
        conn.execute("UPDATE users SET archive_limit = 1 WHERE email = 'owner@example.com'")
    latest = create(client, "Анна: Новая встреча")
    assert [m["id"] for m in client.get("/api/meetings").json()] == [latest["id"], old["id"]]
    store.update_meeting(old["id"], status="done")
    middle = create(client)
    store.update_meeting(middle["id"], status="error")
    main.retention.trim_archive(old["user_id"])
    assert [m["id"] for m in client.get("/api/meetings").json()] == [middle["id"], latest["id"]]
    assert store.get_meeting(old["id"]) is None


def test_low_disk_rejects_before_persistence(client, monkeypatch):
    from backend import disk

    def full(*args):
        raise disk.DiskError("Нет места")

    monkeypatch.setattr(disk, "ensure_space", full)
    assert client.post("/api/meetings/transcript", data={"text": "Анна: Привет"}).status_code == 507
    assert client.get("/api/meetings").json() == []


def test_active_restart_keeps_names_and_discards_late_llm(client, monkeypatch):
    doc = create(client)
    mid = doc["id"]
    monkeypatch.setattr(config, "get_api_key", lambda: "fake")
    monkeypatch.setattr(pipeline, "make_client", lambda key: object())

    async def analyze(*args):
        control.request_stop(mid, restart=True)
        return {"summary": "Late result"}

    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", analyze)
    asyncio.run(pipeline.process_meeting(mid))
    updated = store.get_meeting(mid)
    assert updated["status"] == "queued"
    assert updated["result"] is None
    assert updated["transcript"] == doc["transcript"]
    assert updated["speakers"] == doc["speakers"]
    control.release(mid)
