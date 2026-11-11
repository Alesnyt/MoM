from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from io import BytesIO
from threading import Barrier
from unittest.mock import patch

import pytest
from docx import Document


from fastapi.testclient import TestClient

from backend import auth, config, keys, mail, main, pipeline, store, worker

real_send_mail = mail.send_mail


def test_word_participants_keep_long_titles_in_separate_blocks():
    from backend.word_protocol import to_docx
    names = ["Анна Петрова", "Борис Иванов", "Вера Соколова"]
    titles = ["Руководитель департамента технологического развития дата-сервисов", "Архитектор, ведущий эксперт"]
    meeting = {"title": "Обсуждение", "speakers": [dict(name=name, job_title=title) for name, title in zip(names, titles)]}
    result = {"title": "Обсуждение", "date_hint": "", "participants": names, "summary": "Согласовали план.",
              "decisions": [], "key_points": [], "action_items": [], "mom": {"agenda": [], "next_meeting": ""}}
    document = Document(BytesIO(to_docx(meeting, result)))
    blocks = [paragraph for paragraph in document.paragraphs if any(paragraph.text.startswith(name) for name in names)]
    assert [paragraph.text for paragraph in blocks] == [names[0] + "\n" + titles[0], names[1] + "\n" + titles[1], names[2]]
    assert all(paragraph.runs[0].bold and paragraph.paragraph_format.keep_together for paragraph in blocks)

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
    monkeypatch.setattr(mail, "send_mail", lambda to, subject, body, **formatting: sent.append((to, subject, body, formatting)))

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
    renamed = client.patch(path + "/speakers/S1", json={"name": "Анна Петрова", "email": "ANNA@example.com",
                                                       "job_title": "Руководитель проекта"})
    assert renamed.status_code == 200
    assert renamed.json()["speakers"][0]["email"] == "anna@example.com"
    assert renamed.json()["speakers"][0]["job_title"] == "Руководитель проекта"
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
        assert args[-1] == [{"name": "Анна Петрова", "job_title": "Руководитель проекта"}]
        return {"title": "Совещание", "summary": "Готовый протокол", "participants": ["Анна Петрова"], "mom": {}}

    monkeypatch.setattr(pipeline, "_analyze_with_heartbeat", analyze)
    asyncio.run(pipeline.process_meeting(mid))
    assert sent == []
    assert store.get_meeting(mid)["mail_pending"]
    store.init_db()
    while mail.deliver_pending():
        pass
    assert [item[0] for item in sent] == ["owner@example.com", "anna@example.com"]
    assert all("Готовый протокол" in item[2] for item in sent)
    assert all("Руководитель проекта" in item[3]["html"] for item in sent)
    assert sent[0][3]["attachment"] == sent[1][3]["attachment"]
    assert sent[0][3]["attachment"][0] == "Совещание.docx"
    assert any("Руководитель проекта" in p.text for p in Document(BytesIO(sent[0][3]["attachment"][1])).paragraphs)
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
    path = f'/api/meetings/{meeting["id"]}/speakers/S1'
    response = client.patch(path, json={"name": "Анна", "email": "a@example.com", "job_title": "Аналитик"})
    assert response.status_code == 200
    assert response.json()["speakers"][0] == {"id": "S1", "name": "Анна", "email": "a@example.com", "job_title": "Аналитик"}
    assert client.patch(path, json={"name": "Другой участник"}).json()["speakers"][0] == {"id": "S1", "name": "Другой участник"}


def test_downloaded_email_and_smtp_have_the_word_attachment(delivery, monkeypatch):
    client, meeting, _ = delivery
    mid = meeting["id"]
    result = {"title": "План & сроки", "summary": "Первый абзац\n\nВторой абзац",
              "decisions": ["Утвердить <пилот>"], "action_items": [{"task": "Проверить пилот", "assignee": "Анна"}]}
    mail.complete_protocol(meeting, result=result)
    captured = []

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_message(self, message):
            captured.append(message.as_bytes())

        def quit(self):
            pass

    with patch.object(mail, "send_mail", real_send_mail), patch.object(mail.smtplib, "SMTP", SMTP):
        monkeypatch.setattr(config, "get_smtp_host", lambda: "smtp.example.com")
        monkeypatch.setattr(config, "get_smtp_from", lambda: "mom@example.com")
        monkeypatch.setattr(config, "get_smtp_user", lambda: "")
        monkeypatch.setattr(config, "get_smtp_port", lambda: 25)
        monkeypatch.setattr(config, "get_smtp_starttls", lambda: False)
        assert mail.deliver_pending()
    response = client.get(f"/api/meetings/{mid}/email.eml")
    assert response.status_code == 200
    assert response.headers["content-type"] == "message/rfc822"
    assert response.headers["cache-control"] == "no-store"
    word = client.get(f"/api/meetings/{mid}/export.docx")
    for raw in (captured[0], response.content):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        assert "Первый абзац" in message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        assert "Утвердить &lt;пилот&gt;" in html and "<table" in html
        attachment, = message.iter_attachments()
        assert attachment.get_filename() == "План сроки.docx"
        attached_doc = Document(BytesIO(attachment.get_payload(decode=True)))
        export_doc = Document(BytesIO(word.content))
        assert [p.text for p in attached_doc.paragraphs] == [p.text for p in export_doc.paragraphs]
        assert attached_doc.tables[0].cell(1, 0).text == "Проверить пилот"


def test_simultaneous_speaker_edits_keep_both_updates(delivery):
    _, meeting, _ = delivery
    actor = {"user_id": meeting["user_id"], "email": "owner@example.com"}
    start = Barrier(2, timeout=5)

    def rename(speaker, name):
        start.wait()
        return main.rename_meeting_speaker(meeting["id"], speaker, main.SpeakerNameIn(name=name), actor)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(rename, "S1", "Алёна")
        second = executor.submit(rename, "S2", "Борис Петров")
        assert first.result()["status"] == second.result()["status"] == "review"
    speakers = store.get_meeting(meeting["id"])["speakers"]
    assert {speaker["name"] for speaker in speakers} == {"Алёна", "Борис Петров", "Вера"}


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

    def send(to, subject, body, **formatting):
        sent.append(to)
        if to == "b@example.com":
            raise RuntimeError("SMTP refused")

    monkeypatch.setattr(mail, "send_mail", send)
    mail.complete_protocol(store.get_meeting(meeting["id"]), result={"summary": "Текст"})
    while mail.deliver_pending():
        pass
    assert sent == ["owner@example.com", "b@example.com", "g@example.com"]
    assert "2 из 3" in store.get_meeting(meeting["id"])["status_message"]
    assert [row["action"] for row in store.list_audit()].count("email.fail") == 1


def test_review_or_disabled_smtp_never_sends(delivery, monkeypatch):
    _, meeting, sent = delivery
    assert not mail.deliver_pending()
    monkeypatch.setattr(config, "smtp_configured", lambda: False)
    mail.complete_protocol(meeting, result={"summary": "Текст"})
    assert not store.get_meeting(meeting["id"])["mail_pending"]
    assert not mail.deliver_pending()
    assert sent == []


def test_delivery_retries_only_the_recipient_with_a_temporary_failure(delivery, monkeypatch):
    client, meeting, _ = delivery
    mid = meeting["id"]
    response = client.post(f"/api/meetings/{mid}/protocol", json={"recipients": [
        {"speaker_id": "S1", "email": "speaker@example.com"},
    ]})
    assert response.status_code == 200
    mail.complete_protocol(store.get_meeting(mid), result={"summary": "Готово"})
    calls = []

    def send(to, _subject, _body, **formatting):
        calls.append(to)
        if to == "speaker@example.com" and calls.count(to) == 1:
            raise mail.TemporaryMailError("try later")

    monkeypatch.setattr(mail, "send_mail", send)
    assert mail.deliver_pending()
    assert mail.deliver_pending()
    assert not mail.deliver_pending()
    assert store.get_meeting(mid)["mail_pending"]
    with store.connect() as conn:
        conn.execute("UPDATE protocol_mail SET due_at = 0 WHERE state = 'pending'")
    store.init_db()
    assert mail.deliver_pending()
    assert not mail.deliver_pending()
    assert calls == ["owner@example.com", "speaker@example.com", "speaker@example.com"]
    assert not store.get_meeting(mid)["mail_pending"]


def test_interrupted_smtp_attempt_is_not_sent_again(delivery):
    _, meeting, sent = delivery
    mid = meeting["id"]
    mail.complete_protocol(meeting, result={"summary": "Готово"})
    with store.connect() as conn:
        conn.execute("UPDATE protocol_mail SET state = 'sending', due_at = 0 WHERE meeting_id = ?", (mid,))
    assert not mail.deliver_pending()
    assert not store.get_meeting(mid)["mail_pending"]
    assert "SMTP не подтвердил отправку" in store.get_meeting(mid)["status_message"]
    assert sent == []
