import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from docx import Document
from fastapi.testclient import TestClient

from backend import auth, config, control, main, pipeline, protocol_editor as editor, store

PATH = "/api/meetings/ready"
ORIGINAL = {
    "title": "План", "language": "ru", "participants": ["Иван"], "date_hint": None,
    "summary": "Согласно решения комиссии.", "decisions": ["Срок подтверждён"], "key_points": [],
    "action_items": [{"task": "Подготовить отчёт", "priority": "high", "assignee": "Иван", "due": None,
                      "source_turns": [2]}],
    "decision_sources": [{"decision": "Срок подтверждён", "turns": [1]}],
    "mom": {"agenda": [], "next_meeting": None},
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    store.init_db()
    for token in ("owner", "other"):
        user = store.create_user(token + "@example.org", "unused")
        store.put_session(token, "user", user_id=user["id"], email=user["email"], expires=time.time() + 3600)
        if token == "owner":
            store.create_meeting("ready", "План", "audio.wav", user["id"])
    store.update_meeting("ready", status="done", transcript="Исходная реплика", result=ORIGINAL)
    client = TestClient(main.app)
    client.cookies.set(auth.USER_COOKIE, "owner")
    full = client.get(PATH).json()
    return client, {"revision": full["result_revision"], "result": full["result"]}


def test_edits_reach_exports_and_preserve_evidence_only_for_unchanged_facts(api):
    client, payload = api
    draft = payload["result"]
    draft.update(title="Новый план", summary="Согласно решению комиссии.", decisions=["Новый срок"],
                 key_points=["Новый срок"], decision_sources=[{"decision": "Новый срок", "turns": [99]}])
    draft["action_items"][0]["source_turns"] = [999]
    draft["action_items"].append({**draft["action_items"][0], "task": "Другая задача"})
    response = client.patch(PATH + "/protocol", json=payload)
    assert response.status_code == 200, response.text
    saved = client.get(PATH).json()
    assert saved["result_revision"] != payload["revision"]
    assert saved["title"] == draft["title"] and saved["transcript"] == "Исходная реплика"
    assert saved["result"]["key_points"] == saved["result"]["decisions"] == ["Новый срок"]
    assert saved["result"]["decision_sources"] == []
    assert [row["source_turns"] for row in saved["result"]["action_items"]] == [[2], []]
    assert draft["summary"] in client.get(PATH + "/export.md").text
    assert draft["summary"] in client.get(PATH + "/email.txt").text
    doc = Document(BytesIO(client.get(PATH + "/export.docx").content))
    assert draft["summary"] in [p.text for p in doc.paragraphs]
    assert client.get(PATH + "/email.eml").status_code == 200


def test_archive_body_version_tracks_detail_edits_without_returning_the_transcript(api):
    client, payload = api
    def archive_version():
        row, = client.get("/api/meetings").json()
        assert row["result"] is None and row["transcript"] is None
        assert row["body_version"] == client.get(PATH).json()["body_version"]
        return row["body_version"]

    old = archive_version()
    store.update_meeting("ready", progress=99)
    assert archive_version() == old
    payload["result"]["summary"] = "Новый текст"
    assert client.patch(PATH + "/protocol", json=payload).status_code == 200
    assert archive_version() == old + 1
    store.update_meeting("ready", transcript_segments={"segments": [], "speakers": []})
    assert archive_version() == old + 2


@pytest.mark.parametrize("length", [255, 256, 600])
def test_long_edited_title_in_word_and_email_attachment(api, length):
    client, payload = api
    title = ("Региональный план " * 40)[:length]
    payload["result"]["title"] = title
    assert client.patch(PATH + "/protocol", json=payload).json()["title"] == title
    word = client.get(PATH + "/export.docx")
    eml = client.get(PATH + "/email.eml")
    assert word.status_code == eml.status_code == 200
    message = BytesParser(policy=policy.default).parsebytes(eml.content)
    for content in (word.content, next(message.iter_attachments()).get_payload(decode=True)):
        document = Document(BytesIO(content))
        assert document.core_properties.title == title[:255]
        assert document.paragraphs[0].text == title


def test_correcting_speakers_keeps_manual_protocol_until_confirmed_rebuild(api, monkeypatch):
    client, payload = api
    document = {"speakers": [{"id": "S1", "name": "Иван"}, {"id": "S2", "name": "Анна"}],
                "segments": [{"speaker": "S1", "text": "Предлагаю план", "start": 0, "end": 2}]}
    store.update_meeting("ready", transcript_segments=document)
    manual = client.patch(PATH + "/protocol", json=payload).json()
    changed = client.patch(PATH + "/segments", json={"segment_indexes": [0], "speaker_id": "S2"}).json()
    renamed = client.patch(PATH + "/speakers/S1", json={"name": "Денис"}).json()
    for meeting in (changed, renamed):
        assert meeting["segments"][0]["speaker"] == "S2" and "Анна" in meeting["transcript"]
        assert meeting["status"] == "done" and meeting["result"] == manual["result"]
        assert meeting["result_revision"] == manual["result_revision"]
    assert store.claim_next_meeting(1) is None

    monkeypatch.setattr(main.keys, "snapshot", lambda: {"connected": True})
    assert client.post(PATH + "/retry").status_code == 409
    assert client.post(PATH + "/retry", params={"revision": payload["revision"]}).status_code == 409
    params = {"revision": manual["result_revision"]}
    queued = client.post(PATH + "/retry", params=params).json()
    assert queued["status"] == "queued" and queued["result"] == manual["result"]
    assert queued["transcript"] == renamed["transcript"]
    assert client.post(PATH + "/restart").json()["result_revision"] == params["revision"]
    assert client.post(PATH + "/stop").json()["status"] == "done"

    # Simulate a process restart and a model failure after the queued job was claimed.
    assert client.post(PATH + "/retry", params=params).status_code == 200
    assert store.claim_next_meeting(1) == "ready"
    assert store.requeue_interrupted_meetings() == 1
    assert store.claim_next_meeting(1) == "ready"
    monkeypatch.setattr(config, "get_api_key", lambda: "test")
    monkeypatch.setattr(config, "smtp_configured", lambda: False)
    monkeypatch.setattr(pipeline, "make_client", lambda key: object())

    async def review(client, document, **kwargs):
        return document

    monkeypatch.setattr(pipeline, "review_names", review)
    model = AsyncMock(side_effect=RuntimeError("model unavailable"))
    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", model)
    asyncio.run(pipeline._process_meeting("ready"))
    model.assert_awaited_once()
    failed = client.get(PATH).json()
    assert failed["status"] == "done" and failed["result_revision"] == params["revision"]
    assert failed["error"] and failed["status_message"] == "Обработка не удалась"
    assert client.get(PATH + "/export.docx").status_code == 200

    assert client.post(PATH + "/retry", params=params).status_code == 200
    assert store.claim_next_meeting(1) == "ready"
    model.side_effect = None
    model.return_value = {**ORIGINAL, "summary": "Новая редакция"}
    asyncio.run(pipeline._process_meeting("ready"))
    rebuilt = client.get(PATH).json()
    assert rebuilt["status"] == "done" and rebuilt["error"] is None
    assert rebuilt["result"]["summary"] == "Новая редакция" and rebuilt["result_revision"] != params["revision"]


@pytest.mark.parametrize("restart", [False, True])
def test_stopping_running_manual_rebuild_preserves_revision_and_speakers(api, monkeypatch, restart):
    client, payload = api
    saved = client.patch(PATH + "/protocol", json=payload).json()
    monkeypatch.setattr(main.keys, "snapshot", lambda: {"connected": True})
    assert client.post(PATH + "/retry", params={"revision": saved["result_revision"]}).status_code == 200
    assert store.claim_next_meeting(1) == "ready"
    try:
        control.request_stop("ready", restart=restart)
        control.finish("ready")
    finally:
        control.release("ready")
    row = client.get(PATH).json()
    assert row["status"] == ("queued" if restart else "done")
    assert row["result_revision"] == saved["result_revision"] and row["transcript"] == saved["transcript"]


def test_two_simultaneous_saves_cannot_overwrite_each_other(api):
    client, payload = api
    def save(title):
        return client.patch(PATH + "/protocol", json={**payload, "result": {**payload["result"], "title": title}})
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(save, ["Первый", "Второй"]))
    assert sorted(row.status_code for row in responses) == [200, 409]
    assert client.get(PATH).json()["title"] == next(r.json()["title"] for r in responses if r.is_success)


@pytest.mark.parametrize("method,suffix", [("patch", "/protocol"), ("post", "/protocol/proofread")])
def test_owner_and_state_checked_before_processing(api, monkeypatch, method, suffix):
    client, payload = api
    model = AsyncMock()
    monkeypatch.setattr(editor, "proofread", model)
    call = getattr(client, method)
    client.cookies.clear()
    assert call(PATH + suffix, json=payload).status_code == 401
    client.cookies.set(auth.USER_COOKIE, "other")
    assert call(PATH + suffix, json=payload).status_code == 404
    client.cookies.set(auth.USER_COOKIE, "owner")
    assert call(PATH + suffix, json={**payload, "revision": "0" * 64}).status_code == 409
    store.update_meeting("ready", status="queued")
    assert call(PATH + suffix, json=payload).status_code == 409
    model.assert_not_awaited()


def test_partial_document_rejected(api):
    client, payload = api
    assert client.patch(PATH + "/protocol", json={**payload, "result": {"title": "План"}}).status_code == 422
    assert client.get(PATH).json()["result"] == payload["result"]


@pytest.mark.parametrize("summary_only", [False, True])
def test_proofreading_unsaved_draft_calls_configured_model_and_never_saves(api, monkeypatch, summary_only):
    client, payload = api
    monkeypatch.setattr(config, "get_api_key", lambda: "configured-key")
    monkeypatch.setattr(config, "get_chat_model", lambda: "configured-model")
    draft = payload["result"]
    draft["summary"] = "Благодаря нового плана одобрили 12 задач."
    if summary_only:
        payload = {"summary": draft["summary"], "revision": payload["revision"]}
    proposal = dict(field="summary", before="Благодаря нового плана", after="Благодаря новому плану",
                    reason="Предлог «благодаря» требует дательного падежа.")
    choices = [proposal, dict(proposal, field="transcript"), dict(proposal, before="чужой текст"),
               dict(proposal, before="12 задач", after="18 задач")]
    completion = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"suggestions": choices}, ensure_ascii=False)))]))

    @asynccontextmanager
    async def options(**kwargs):
        yield SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    monkeypatch.setattr(editor, "make_client", lambda key, timeout: SimpleNamespace(with_options=options))
    response = client.post(PATH + "/protocol/proofread", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["suggestions"] == [proposal]
    sent = completion.await_args.kwargs
    assert sent["model"] == "configured-model"
    fields = json.loads(sent["messages"][1]["content"])
    assert fields["summary"] == draft["summary"]
    if summary_only:
        assert set(fields) == {"summary"}
    assert "Исходная реплика" not in str(fields) and "Иван" not in str(fields)
    assert client.get(PATH).json()["result"]["summary"] == ORIGINAL["summary"]
    completion.return_value.choices[0].message.content = "{}"
    assert client.post(PATH + "/protocol/proofread", json=payload).status_code == 502


def test_offline_save_and_unconfigured_proofreader(api, monkeypatch):
    client, payload = api
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    assert client.post(PATH + "/protocol/proofread", json=payload).status_code == 503
    assert client.patch(PATH + "/protocol", json=payload).status_code == 200


def test_long_field_keeps_all_text_and_only_unambiguous_suggestions(monkeypatch):
    monkeypatch.setattr(config, "PROTOCOL_CONTEXT_TOKENS", 4096)
    line = "Приняли согласно решения. " * 500
    parts = list(editor.batches({"summary": line}))
    assert len(parts) > 1
    assert "".join(p["summary"] for p in parts) == line
    repeated = dict(field="summary", before="согласно решения", after="согласно решению", reason="Дательный падеж.")
    assert editor.proposals(json.dumps({"suggestions": [repeated]}), parts[0], {"summary": line}) == []


def test_duplicate_proof_request_releases_slot_even_after_failure(monkeypatch):
    monkeypatch.setattr(config, "get_max_jobs", lambda: 2)
    with pytest.raises(ValueError):
        with editor.proofread_slot("owner"):
            with pytest.raises(editor.HTTPException) as err:
                with editor.proofread_slot("owner"):
                    pytest.fail("Two calls for the same user")
            assert err.value.status_code == 429
            raise ValueError("Provider failed")
    with editor.proofread_slot("owner"):
        with editor.proofread_slot("other"):
            with pytest.raises(editor.HTTPException) as err:
                with editor.proofread_slot("third"):
                    pytest.fail("Model capacity exceeded")
            assert err.value.status_code == 429


def test_plain_legacy_summary_and_literal_markup_stay_readable_in_exports(api):
    from backend import summary_format
    client, payload = api
    url = PATH + "/protocol"
    source = "**буквально** <пример> & условия\n2. Строка исходного протокола"
    assert client.patch(url, json={"revision": payload["revision"], "summary": source}).is_success
    assert source in client.get(PATH + "/email.txt").text
    word = Document(BytesIO(client.get(PATH + "/export.docx").content))
    assert all(s in [p.text for p in word.paragraphs] for s in source.splitlines())
    assert "&lt;пример&gt;" in summary_format.html("<пример>")
    assert "<em>важно</em>" in summary_format.html("*важно*")
