from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend import auth, config, keys, mail, main, pipeline, store, worker


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for module in (config, store):
        monkeypatch.setattr(module, "DB_PATH", data / "mom.db")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for module in (config, main, pipeline):
        monkeypatch.setattr(module, "UPLOAD_DIR", data / "uploads")
        monkeypatch.setattr(module, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    monkeypatch.setattr(config, "migrate_token_plan_asr", lambda: None)
    monkeypatch.setattr(config, "smtp_configured", lambda: True)
    monkeypatch.setattr(config, "get_public_base_url", lambda: "")
    monkeypatch.setattr(keys, "snapshot", lambda: {"connected": True})
    monkeypatch.setattr(keys, "reset_status", lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(mail, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(worker, "run_worker", idle)
    with TestClient(main.app) as client:
        user = store.create_user("owner@example.com", "unused")
        store.put_session("owner", "user", user_id=user["id"], email=user["email"], expires=time.time() + 3600)
        client.cookies.set(auth.USER_COOKIE, "owner")
        result = client.post("/api/meetings/transcript", data={"text": "Анна: Привет\nБорис: Сделаю\nВера: Проверю"})
        assert result.status_code == 200
        yield client, result.json(), sent


def test_selected_speakers_receive_protocol_only_after_completion(delivery, monkeypatch):
    client, meeting, sent = delivery
    mid = meeting["id"]
    path = f"/api/meetings/{mid}"
    renamed = client.patch(path + "/speakers/S1", json={"name": "Анна Петрова", "email": "ANNA@example.com"})
    assert renamed.status_code == 200
    assert renamed.json()["speakers"][0]["email"] == "anna@example.com"
    assert not renamed.json()["speakers"][0].get("send_protocol")
    # Saving a catalog contact is not permission to email that contact.
    client.patch(path + "/speakers/S3", json={"name": "Вера", "email": "vera@example.com"})
    queued = client.post(path + "/protocol", json={"recipients": [{"speaker_id": "S1", "email": "anna@example.com"}]})
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued"
    assert sent == []
    store.init_db()
    saved = client.get(path).json()
    assert saved["speakers"][0]["send_protocol"] is True
    assert saved["speakers"][2]["email"] == "vera@example.com"
    assert not saved["speakers"][2].get("send_protocol")
    assert saved["segments"] == meeting["segments"]

    monkeypatch.setattr(config, "get_api_key", lambda: "fake-key")
    monkeypatch.setattr(pipeline, "make_client", lambda key: object())

    async def analyze(*args, **kwargs):
        return {"title": "Совещание", "summary": "Готовый протокол", "participants": ["Анна Петрова"], "mom": {}}

    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", analyze)
    asyncio.run(pipeline.process_meeting(mid))
    assert [item[0] for item in sent] == ["owner@example.com", "anna@example.com"]
    assert all("Готовый протокол" in item[2] for item in sent)
    assert store.get_meeting(mid)["status"] == "done"


@pytest.mark.parametrize("recipients", [
    [{"speaker_id": "missing", "email": "a@example.com"}],
    [{"speaker_id": "unknown", "email": "a@example.com"}],
    [{"speaker_id": "S1", "email": "a@example.com\r\nBcc: attacker@example.com"}],
    [{"speaker_id": "S1", "email": "not-an-email"}],
    [{"speaker_id": "S1", "email": "a@example.com"}, {"speaker_id": "S1", "email": "b@example.com"}],
])
def test_invalid_recipients_do_not_queue_or_change_meeting(delivery, recipients):
    client, meeting, sent = delivery
    path = f'/api/meetings/{meeting["id"]}'
    response = client.post(path + "/protocol", json={"recipients": recipients})
    assert response.status_code == 400
    assert client.get(path).json()["status"] == "review"
    assert client.get(path).json()["speakers"] == meeting["speakers"]
    assert sent == []


def test_delivery_requires_smtp_but_protocol_without_recipients_does_not(delivery, monkeypatch):
    client, meeting, _ = delivery
    monkeypatch.setattr(config, "smtp_configured", lambda: False)
    path = f'/api/meetings/{meeting["id"]}/protocol'
    response = client.post(path, json={"recipients": [{"speaker_id": "S1", "email": "a@example.com"}]})
    assert response.status_code == 400
    assert "SMTP" in response.json()["detail"]
    assert client.post(path).status_code == 200


def test_another_user_cannot_set_recipients(delivery):
    client, meeting, sent = delivery
    other = store.create_user("other@example.com", "unused")
    store.put_session("other", "user", user_id=other["id"], email=other["email"], expires=time.time() + 3600)
    client.cookies.set(auth.USER_COOKIE, "other")
    response = client.post(f'/api/meetings/{meeting["id"]}/protocol', json={"recipients": [{"speaker_id": "S1", "email": "a@example.com"}]})
    assert response.status_code == 404
    assert store.get_meeting(meeting["id"])["status"] == "review"
    assert sent == []


def test_recipient_selection_cannot_change_after_queue(delivery):
    client, meeting, _ = delivery
    path = f'/api/meetings/{meeting["id"]}/protocol'
    assert client.post(path, json={"recipients": [{"speaker_id": "S1", "email": "a@example.com"}]}).status_code == 200
    assert client.post(path, json={"recipients": [{"speaker_id": "S2", "email": "b@example.com"}]}).status_code == 409
    assert not store.queue_review_protocol(meeting["id"], {"speakers": []})
    assert store.get_meeting(meeting["id"])["speakers"][0]["email"] == "a@example.com"


def test_name_reset_clears_contact_and_delivery_flag(delivery):
    client, meeting, _ = delivery
    document = {"speakers": [{"id": "S1", "name": "Анна", "email": "a@example.com", "send_protocol": True}],
                "segments": meeting["segments"], "source_type": "text"}
    store.update_meeting(meeting["id"], transcript_segments=document)
    response = client.patch(f'/api/meetings/{meeting["id"]}/speakers/S1', json={"name": "Спикер 1"})
    assert response.status_code == 200
    assert response.json()["speakers"] == [{"id": "S1", "name": "Спикер 1"}]
    assert response.json()["segments"] == meeting["segments"]


def test_same_name_can_be_linked_to_catalog_email(delivery):
    client, meeting, _ = delivery
    response = client.patch(f'/api/meetings/{meeting["id"]}/speakers/S1', json={"name": "Анна", "email": "a@example.com"})
    assert response.status_code == 200
    assert response.json()["speakers"][0] == {"id": "S1", "name": "Анна", "email": "a@example.com"}


def test_mail_deduplicates_and_continues_after_one_failure(delivery, monkeypatch):
    _, meeting, sent = delivery
    document = {"speakers": [
        {"id": "S1", "name": "Анна", "email": "OWNER@example.com", "send_protocol": True},
        {"id": "S2", "name": "Борис", "email": "b@example.com", "send_protocol": True},
        {"id": "S3", "name": "Вера", "email": "B@example.com", "send_protocol": True},
        {"id": "S4", "name": "Глеб", "email": "g@example.com", "send_protocol": True},
        {"id": "S5", "name": "Дина", "email": "d@example.com"},
    ], "segments": meeting["segments"]}
    store.update_meeting(meeting["id"], transcript_segments=document, status="done", result={"summary": "Текст"})

    def send(to, subject, body):
        sent.append(to)
        if to == "b@example.com":
            raise RuntimeError("SMTP refused")

    monkeypatch.setattr(mail, "send_mail", send)
    mail.notify_meeting_done(store.get_meeting(meeting["id"]))
    assert sent == ["owner@example.com", "b@example.com", "g@example.com"]
    assert "2 из 3" in store.get_meeting(meeting["id"])["status_message"]
    assert [row["action"] for row in store.list_audit()].count("email.fail") == 1


def test_review_or_disabled_smtp_never_sends(delivery, monkeypatch):
    _, meeting, sent = delivery
    mail.notify_meeting_done(meeting)
    monkeypatch.setattr(config, "smtp_configured", lambda: False)
    mail.notify_meeting_done({**meeting, "status": "done"})
    assert sent == []
