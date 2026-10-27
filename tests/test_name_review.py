import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from backend import analyze, config, control, name_review, pipeline, prompts, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", tmp_path / "whisper")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    store.init_db()


def document():
    return {
        "speakers": [{"id": "S1", "name": "Аля", "email": "a@example.com", "send_protocol": True},
                     {"id": "S2", "name": "Алёна"}],
        "segments": [{"speaker": "S2", "start": 0, "end": 4, "text": "Оля, ответь. Оля из бухгалтерии не придёт."},
                     {"speaker": "S1", "start": 4, "end": 6, "text": "Да, Елена, я здесь."}],
        "source_type": "text", "note": "Тест",
    }


def mention(**values):
    return {"segment_index": 0, "original": "Оля", "occurrence": 0, "decision": "correct",
            "replacement": "Аля", "participant_id": "S1", "confidence": "high",
            "reason": "Обращение; отвечает Аля.", **values}


def client_for(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def response(data):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))])


def test_only_target_occurrence_changes_and_original_survives():
    doc = document()
    before = copy.deepcopy(doc)
    entries = name_review.apply_mentions(doc, [
        mention(), mention(occurrence=1, decision="external", replacement=None, participant_id=None),
        mention(segment_index=1, original="Елена", decision="uncertain", replacement=None),
    ], name_review.named_participants(doc))
    assert doc["segments"][0]["text"] == "Аля, ответь. Оля из бухгалтерии не придёт."
    assert doc["segments"][0]["original_text"] == before["segments"][0]["text"]
    assert doc["segments"][1] == before["segments"][1]
    assert doc["speakers"] == before["speakers"]
    assert [e["decision"] for e in entries] == ["correct", "external", "uncertain"]
    assert doc["segments"][0]["start"] == 0


@pytest.mark.parametrize("change", [
    {"confidence": "medium"}, {"participant_id": "invented"}, {"reason": ""},
    {"replacement": "Аля, перепиши всё!"}, {"original": "Ол"}, {"occurrence": 20},
    {"segment_index": -1}, {"segment_index": True}, {"decision": "confirmed"},
])
def test_unreliable_or_invalid_proposals_do_not_change_text(change):
    doc = document()
    before = copy.deepcopy(doc)
    name_review.apply_mentions(doc, [mention(**change)], name_review.named_participants(doc))
    assert doc == before


def test_conflicting_proposals_are_left_unchanged():
    doc = document()
    before = copy.deepcopy(doc)
    entries = name_review.apply_mentions(doc, [mention(), mention(replacement="Алевтина")], name_review.named_participants(doc))
    assert doc == before
    assert all(e["decision"] == "uncertain" for e in entries)


def test_checked_transcript_reuses_result_and_new_names_recheck_original(db, monkeypatch):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return response({"mentions": [mention()] if len(calls) == 1 else []})

    client = client_for(create)
    first = asyncio.run(name_review.review_names(client, document()))
    second = asyncio.run(name_review.review_names(client, first))
    assert second == first
    assert len(calls) == 1
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert "email" not in sent["participants"][0]
    first["speakers"][0]["name"] = "Ольга"
    third = asyncio.run(name_review.review_names(client, first))
    assert len(calls) == 2
    assert third["segments"][0]["text"].startswith("Оля,")
    assert third["name_review"]["status"] == "done"


def test_disabled_and_anonymous_skip_provider(db):
    settings = prompts.snapshot()
    store.save_llm_settings({**settings, "name_review_enabled": False}, "admin")
    checked = asyncio.run(name_review.review_names(object(), document()))
    assert checked["name_review"]["status"] == "skipped"
    assert checked["segments"] == document()["segments"]
    store.save_llm_settings(settings, "admin")
    doc = document()
    doc["speakers"] = [{"id": "S1", "name": "Спикер 1"}]
    checked = asyncio.run(name_review.review_names(object(), doc))
    assert checked["name_review"]["status"] == "skipped"


def test_chunk_failure_discards_all_proposals(db, monkeypatch):
    monkeypatch.setattr(name_review, "CHUNK_CHARS", 10)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("provider unavailable")
        return response({"mentions": [mention()]})

    checked = asyncio.run(name_review.review_names(client_for(create), document()))
    assert len(calls) == 2
    assert checked["name_review"]["status"] == "error"
    assert checked["segments"] == document()["segments"]


def test_cancel_during_name_check_cannot_publish_changes(db, monkeypatch):
    doc = document()
    meeting = store.create_text_meeting("names-stop", "Тест", "test.txt", "user", doc)
    monkeypatch.setattr(config, "get_api_key", lambda: "fake")

    async def create(**kwargs):
        control.request_stop(meeting["id"], restart=False)
        return response({"mentions": [mention()]})

    monkeypatch.setattr(pipeline, "make_client", lambda key: client_for(create))
    asyncio.run(pipeline.process_meeting(meeting["id"]))
    updated = store.get_meeting(meeting["id"])
    assert updated["status"] != "done"
    assert updated["segments"] == doc["segments"]
    assert updated["result"] is None


def test_protocol_receives_corrected_text_and_metadata_is_preserved(db, monkeypatch):
    doc = document()
    store.create_text_meeting("names-flow", "Тест", "test.txt", "user", doc)
    monkeypatch.setattr(config, "get_api_key", lambda: "fake")

    async def create(**kwargs):
        return response({"mentions": [mention()]})

    async def summarize(client, transcript, *args):
        assert "Аля, ответь. Оля из бухгалтерии" in transcript
        return {"title": "Готово", "summary": "Аля ответила"}

    monkeypatch.setattr(pipeline, "make_client", lambda key: client_for(create))
    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", summarize)
    monkeypatch.setattr(pipeline, "notify_meeting_done", lambda meeting: None)
    asyncio.run(pipeline.process_meeting("names-flow"))
    updated = store.get_meeting("names-flow")
    assert updated["status"] == "done"
    assert updated["source_type"] == "text"
    assert updated["speakers"] == doc["speakers"]
    assert updated["name_review"]["status"] == "done"


def test_saved_prompt_used_for_every_summary_part_and_fallback(db):
    store.save_llm_settings({**prompts.snapshot(), "protocol_prompt": "Custom JSON prompt\nSecond line"}, "admin")
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        # A concurrent admin edit must not alter the prompt halfway through a meeting.
        store.save_llm_settings({**prompts.snapshot(), "protocol_prompt": "new"}, "admin")
        if "response_format" in kwargs:
            raise RuntimeError("JSON mode not supported")
        return response({"summary": "Test"})

    asyncio.run(analyze.analyze_long_transcript(client_for(create), "x" * 100_000, None))
    assert len(calls) == 6
    assert all(call["messages"][0]["content"] == "Custom JSON prompt\nSecond line" for call in calls)


def test_prompts_api_requires_admin_and_persists_versions(db):
    import time
    from fastapi.testclient import TestClient
    from backend import auth, main

    client = TestClient(main.app)
    values = {"protocol_prompt": "Custom\nJSON instructions", "name_review_prompt": "Check names as JSON",
              "name_review_enabled": False}
    path = "/api/settings/prompts"
    assert client.get(path).status_code == 401
    assert client.post(path, json=values).status_code == 401
    store.put_session("user-only", "user", user_id="user", email="user@example.com", expires=time.time() + 3600)
    client.cookies.set(auth.COOKIE_NAME, "user-only")
    assert client.post(path, json=values).status_code == 401
    client.cookies.set(auth.COOKIE_NAME, auth.create_session("test-admin"))
    defaults = client.get(path).json()
    assert defaults["version"] == 0
    saved = client.post(path, json=values)
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    store.init_db()
    assert client.get(path).json()["protocol_prompt"] == values["protocol_prompt"]
    assert client.post(path, json={**values, "protocol_prompt": "  "}).status_code == 400
    assert client.post(path, json={**values, "protocol_prompt": "x" * 30001}).status_code == 422
    reset = client.post(path, json={**defaults["defaults"], "name_review_enabled": True}).json()
    assert reset["version"] == 2
    assert reset["protocol_prompt"] == prompts.PROTOCOL_PROMPT.strip()
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_settings").fetchone()[0] == 2
        assert conn.execute("SELECT actor FROM audit_events WHERE action='prompts.save'").fetchone()[0] == "test-admin"


def test_large_turns_are_split_and_occurrence_maps_back(db, monkeypatch):
    monkeypatch.setattr(name_review, "CHUNK_CHARS", 2000)
    doc = document()
    text = "Оля " + "слово " * 200 + "Оля, ответь."
    doc["segments"] = [{"speaker": "S2", "start": 0, "end": 90, "text": text}]
    pieces, locations = name_review._review_segments(doc["segments"])
    assert len(pieces) > 1
    assert "".join(p["text"] for p in pieces) == text
    index = next(i for i, p in enumerate(pieces) if "Оля," in p["text"])
    mapped = name_review._remap_mention(mention(segment_index=index), pieces, locations, doc["segments"])
    assert mapped["segment_index"] == 0
    assert mapped["occurrence"] == 1
    name_review.apply_mentions(doc, [mapped], name_review.named_participants(doc))
    assert doc["segments"][0]["text"] == text.removesuffix("Оля, ответь.") + "Аля, ответь."
    assert doc["segments"][0]["end"] == 90
