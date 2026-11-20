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


def confirm(client, mid, recipients, revision=None, key="a" * 32):
    return client.post(f"/api/meetings/{mid}/protocol/send", json={
        "revision": revision or store.get_meeting(mid)["result_revision"], "recipients": recipients, "request_id": key,
    })


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
    queued = client.post(path + "/protocol")
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued"
    assert sent == []
    store.init_db()
    saved = client.get(path).json()
    assert not saved["speakers"][0].get("send_protocol")
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
    assert not store.get_meeting(mid)["mail_pending"]
    assert not mail.deliver_pending()
    assert confirm(client, mid, ["owner@example.com", "anna@example.com"]).is_success
    assert store.get_meeting(mid)["mail_pending"]
    store.init_db()
    while mail.deliver_pending():
        pass
    assert len(sent) == 1
    assert sent[0][0] == sent[0][3]["envelope_to"] == ["owner@example.com", "anna@example.com"]
    assert all("Готовый протокол" in item[2] for item in sent)
    assert all("Руководитель проекта" in item[3]["html"] for item in sent)
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
    assert "готовом протоколе" in response.json()["detail"]
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
    assert client.post(path).status_code == 200
    assert client.post(path, json={"recipients": [{"speaker_id": "S2", "email": "b@example.com"}]}).status_code == 409
    assert not store.queue_review_protocol(meeting["id"])
    assert not store.get_meeting(meeting["id"])["speakers"][0].get("email")


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
    path = f"/api/meetings/{mid}/protocol"
    assert client.post(path).is_success
    mail.complete_protocol(store.get_meeting(mid), result=result)
    assert confirm(client, mid, ["owner@example.com", "speaker@example.com"]).is_success
    captured = []

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_message(self, message, *, to_addrs):
            assert to_addrs == ["owner@example.com", "speaker@example.com"]
            captured.append(message.as_bytes())
            return {}

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
        assert str(message["To"]) == "owner@example.com, speaker@example.com"
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
    client, meeting, sent = delivery
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
        assert formatting["envelope_to"] == to
        return {"b@example.com": (550, b"Unknown recipient")}

    monkeypatch.setattr(mail, "send_mail", send)
    mail.complete_protocol(store.get_meeting(meeting["id"]), result={"summary": "Текст"})
    assert confirm(client, meeting["id"], ["owner@example.com", "b@example.com", "B@example.com", "g@example.com"]).is_success
    while mail.deliver_pending():
        pass
    assert sent == [["owner@example.com", "b@example.com", "g@example.com"]]
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
    response = client.post(f"/api/meetings/{mid}/protocol")
    assert response.status_code == 200
    mail.complete_protocol(store.get_meeting(mid), result={"summary": "Готово"})
    assert confirm(client, mid, ["owner@example.com", "speaker@example.com"]).is_success
    calls = []

    def send(to, _subject, _body, **formatting):
        calls.append((to, formatting["envelope_to"], formatting["message_id"]))
        if len(calls) == 1:
            return {"speaker@example.com": (450, b"try later")}
        return {}

    monkeypatch.setattr(mail, "send_mail", send)
    assert mail.deliver_pending()
    assert not mail.deliver_pending()
    assert store.get_meeting(mid)["mail_pending"]
    with store.connect() as conn:
        conn.execute("UPDATE protocol_mail SET due_at = 0 WHERE state = 'pending'")
    store.init_db()
    assert mail.deliver_pending()
    assert not mail.deliver_pending()
    addresses = ["owner@example.com", "speaker@example.com"]
    assert len(calls) == 2 and calls[0][2]
    assert calls == [(addresses, addresses, calls[0][2]), (addresses, addresses[1:], calls[0][2])]
    assert not store.get_meeting(mid)["mail_pending"]


def test_interrupted_smtp_attempt_is_not_sent_again(delivery):
    client, meeting, sent = delivery
    mid = meeting["id"]
    mail.complete_protocol(meeting, result={"summary": "Готово"})
    assert confirm(client, mid, ["owner@example.com"]).is_success
    with store.connect() as conn:
        conn.execute("UPDATE protocol_mail SET state = 'sending', due_at = 0 WHERE meeting_id = ?", (mid,))
    assert not mail.deliver_pending()
    assert not store.get_meeting(mid)["mail_pending"]
    assert "SMTP не подтвердил отправку" in store.get_meeting(mid)["status_message"]
    assert sent == []


def test_legacy_email_download_is_memory_only_and_owner_scoped(delivery, monkeypatch):
    import subprocess
    client, meeting, sent = delivery
    mid = meeting['id']
    path = f'/api/meetings/{mid}/email'
    assert client.post(path).status_code == 409
    store.update_meeting(mid, status='done', result={'title': 'План', 'summary': 'Согласовали запуск'})
    before = set(config.DATA_DIR.rglob('*'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('export launched a desktop application'))
    for method, suffix in (('POST', ''), ('GET', '.eml')):
        downloaded = client.request(method, path + suffix)
        assert downloaded.status_code == 200
        assert downloaded.headers['content-type'] == 'message/rfc822'
        assert downloaded.headers['cache-control'] == 'no-store'
        message = BytesParser(policy=policy.default).parsebytes(downloaded.content)
        attachment, = message.iter_attachments()
        document = Document(BytesIO(attachment.get_payload(decode=True)))
        assert any('Согласовали запуск' in p.text for p in document.paragraphs)
    assert set(config.DATA_DIR.rglob('*')) == before
    assert sent == []
    store.put_session('other', 'user', user_id='other', email='other@example.com', expires=time.time()+3600)
    client.cookies.set(auth.USER_COOKIE, 'other')
    assert client.post(path).status_code == 404
    client.cookies.clear()
    assert client.post(path).status_code == 401


def test_two_workers_cannot_send_the_same_group_twice(delivery, monkeypatch):
    from threading import Event

    client, meeting, _ = delivery
    mid = meeting["id"]
    assert client.post(f"/api/meetings/{mid}/protocol").is_success
    mail.complete_protocol(store.get_meeting(mid), result={"summary": "Готово"})
    assert confirm(client, mid, ["owner@example.com", "speaker@example.com"]).is_success
    started, release = Event(), Event()
    attempts = []

    def slow_send(to, _subject, _body, **kwargs):
        attempts.append(kwargs["envelope_to"])
        started.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(mail, "send_mail", slow_send)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(mail.deliver_pending)
        try:
            assert started.wait(5)
            assert not mail.deliver_pending()
        finally:
            release.set()
        assert first.result()
    assert attempts == [["owner@example.com", "speaker@example.com"]]
    assert not store.get_meeting(mid)["mail_pending"]


def test_all_addresses_refused_keep_individual_retry_status(delivery, monkeypatch):
    client, meeting, _ = delivery
    mid = meeting["id"]
    assert client.post(f"/api/meetings/{mid}/protocol").is_success
    mail.complete_protocol(store.get_meeting(mid), result={"summary": "Готово"})
    assert confirm(client, mid, ["owner@example.com", "speaker@example.com"]).is_success
    sends = []

    class SMTP:
        def __init__(self, *_args, **_kwargs): pass
        def quit(self): pass

        def send_message(self, message, *, to_addrs):
            sends.append((str(message["To"]), str(message["Message-ID"]), to_addrs))
            if len(sends) == 1:
                raise mail.smtplib.SMTPRecipientsRefused({
                    "owner@example.com": (550, b"invalid"),
                    "speaker@example.com": (451, b"temporary"),
                })
            return {}

    monkeypatch.setattr(mail.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(mail, "send_mail", real_send_mail)
    for key, value in dict(host="localhost", port=25, user="", starttls=False, from_="mom@example.com").items():
        monkeypatch.setattr(config, "get_smtp_" + key.rstrip("_"), lambda v=value: v)
    assert mail.deliver_pending()
    assert not mail.deliver_pending()
    with store.connect() as conn:
        conn.execute("UPDATE protocol_mail SET due_at = 0 WHERE state = 'pending'")
    assert mail.deliver_pending()
    assert sends[0][:2] == sends[1][:2]
    assert sends[0][2] == ["owner@example.com", "speaker@example.com"]
    assert sends[1][2] == ["speaker@example.com"]
    assert '1 из 2' in store.get_meeting(mid)["status_message"]


def test_only_the_owner_can_confirm_a_ready_protocol_with_valid_addresses(delivery, monkeypatch):
    client, meeting, sent = delivery
    mid = meeting["id"]
    assert confirm(client, mid, ["a@example.com"], revision="0" * 64).status_code == 409
    mail.complete_protocol(meeting, result={"summary": "Готово"})
    assert confirm(client, mid, ["a@example.com", "bad address"]).status_code == 400
    assert confirm(client, mid, ["a@example.com\r\nCc: x@example.com"]).status_code == 400
    store.put_session("other", "user", user_id="other", email="other@example.com", expires=time.time() + 3600)
    client.cookies.set(auth.USER_COOKIE, "other")
    assert confirm(client, mid, ["x@example.com"]).status_code == 404
    client.cookies.set(auth.USER_COOKIE, "owner")
    monkeypatch.setattr(config, "smtp_configured", lambda: False)
    assert confirm(client, mid, ["x@example.com"]).status_code == 400
    assert not mail.deliver_pending() and not sent


def test_final_send_freezes_the_confirmed_edit_and_deduplicates_retries(delivery):
    client, meeting, sent = delivery
    mid = meeting["id"]
    path = f"/api/meetings/{mid}/protocol"
    draft = main.ProtocolResult(title="План", summary="Первая версия", participants=["Анна"],
                                decisions=["Назначить пилот"],
                                decision_sources=[{"decision": "Назначить пилот", "turns": [2]}]).model_dump()
    mail.complete_protocol(meeting, result=draft)
    original = store.get_meeting(mid)["result_revision"]
    draft["summary"] = "Отредактированная версия"
    saved = client.patch(path, json={"revision": original, "summary": draft["summary"]})
    assert saved.is_success, saved.text
    assert saved.json()["result"] == {**draft, "edited_manually": True}
    revision = saved.json()["result_revision"]
    assert client.patch(path, json={"revision": original, "summary": "Устаревшая правка"}).status_code == 409
    assert not mail.deliver_pending() and not sent
    addresses = ["Colleague@example.com", "additional@example.com", "colleague@example.com"]
    assert confirm(client, mid, addresses, original).status_code == 409
    assert confirm(client, mid, addresses, revision).is_success
    assert confirm(client, mid, addresses, revision).is_success
    assert confirm(client, mid, ["other@example.com"], revision).status_code == 409
    assert confirm(client, mid, addresses, revision, key="b" * 32).status_code == 409
    eml = client.get(f"/api/meetings/{mid}/email.eml")
    message = BytesParser(policy=policy.default).parsebytes(eml.content)
    assert [a.addr_spec for a in message["To"].addresses] == ["colleague@example.com", "additional@example.com"]
    draft["summary"] = "Следующая редакция"
    changed = client.patch(path, json={"revision": revision, "result": draft})
    assert changed.is_success
    assert mail.deliver_pending() and not mail.deliver_pending()
    assert len(sent) == 1 and sent[0][0] == ["colleague@example.com", "additional@example.com"]
    assert "Отредактированная версия" in sent[0][2] and "Следующая редакция" not in sent[0][2]
    assert "Отредактированная версия" in sent[0][3]["html"] and "Следующая редакция" not in sent[0][3]["html"]
    paragraphs = Document(BytesIO(sent[0][3]["attachment"][1])).paragraphs
    assert any("Отредактированная версия" in p.text for p in paragraphs)
    assert confirm(client, mid, addresses, revision).is_success
    assert not mail.deliver_pending()
    assert confirm(client, mid, ["new@example.com"], changed.json()["result_revision"], key="b" * 32).is_success
    assert mail.deliver_pending() and sent[1][0] == ["new@example.com"]
    assert "Следующая редакция" in sent[1][2]
    assert sent[1][3]["message_id"] != sent[0][3]["message_id"]


def test_an_edit_during_word_rendering_requires_new_confirmation(delivery, monkeypatch):
    client, meeting, sent = delivery
    mid = meeting["id"]
    mail.complete_protocol(meeting, result={"summary": "До правки"})

    def render_and_edit(current):
        packet = mail.prepare_protocol_mail(current)
        store.update_meeting(mid, result={"summary": "После правки"})
        return packet

    monkeypatch.setattr(main, "prepare_protocol_mail", render_and_edit)
    assert confirm(client, mid, ["a@example.com"]).status_code == 409
    assert not mail.deliver_pending() and not sent


def test_merge_moves_all_turns_preserving_target_metadata_and_timecodes(delivery):
    client, meeting, _ = delivery
    mid = meeting["id"]
    target = {**meeting["speakers"][1], "email": "b@example.com", "job_title": "Инженер"}
    speakers = [meeting["speakers"][0], target, meeting["speakers"][2]]
    segments = [{"speaker": "S3" if i % 10 == 0 else "S1", "start": i, "end": i + .5,
                 "text": f"Фраза {i}", "original_text": f"Оригинал {i}"} for i in range(1100)]
    store.update_meeting(mid, transcript_segments={"speakers": speakers, "segments": segments})
    url = f"/api/meetings/{mid}/speakers/S1/merge"
    for invalid in ("S1", "unknown", "missing"):
        assert client.post(url, json={"target_id": invalid}).status_code == 400
    store.put_session("other", "user", user_id="other", email="other@example.com", expires=time.time() + 3600)
    client.cookies.set(auth.USER_COOKIE, "other")
    assert client.post(url, json={"target_id": "S2"}).status_code == 404
    client.cookies.set(auth.USER_COOKIE, "owner")
    merged = client.post(url, json={"target_id": "S2"})
    assert merged.is_success, merged.text
    result = merged.json()
    assert result["status"] == "review" and result["speakers"] == [target, speakers[2]]
    assert result["segments"] == [{**s, "speaker": "S2"} if s["speaker"] == "S1" else s for s in segments]
    assert client.post(url, json={"target_id": "S2"}).status_code == 400


def test_merge_after_manual_edit_preserves_the_authored_protocol(delivery):
    client, meeting, _ = delivery
    result = {"title": "Мои правки", "summary": "Согласованный текст", "edited_manually": True}
    mail.complete_protocol(meeting, result=result)
    merged = client.post(f"/api/meetings/{meeting['id']}/speakers/S1/merge", json={"target_id": "S2"})
    assert merged.is_success
    assert merged.json()["result"] == result and merged.json()["status"] == "done"


@pytest.mark.parametrize("copies", [1, 8], ids=["regional-pilot", "long-document"])
def test_formatted_summary_roundtrip_with_nested_lists_and_group_delivery(delivery, copies):
    from pathlib import Path
    from docx.oxml.ns import qn

    client, meeting, sent = delivery
    mid = meeting["id"]
    sample = (Path(__file__).parent / "fixtures/summary-pilot.md").read_text().strip()
    source = "\n\n".join([sample] * copies) + "\n\nПоследняя строка документа."
    original = main.ProtocolResult(title="Региональный пилот", summary="Исходный текст").model_dump()
    mail.complete_protocol(meeting, result=original)
    url = f"/api/meetings/{mid}"
    revision = client.get(url).json()["result_revision"]
    response = client.patch(url + "/protocol", json={"revision": revision, "summary": source, "summary_format": "markdown"})
    assert response.is_success
    saved = client.get(url).json()
    assert saved["result"]["summary"] == source
    assert saved["result"]["summary_format"] == "markdown"
    assert source in client.get(url + "/export.md").text
    assert not mail.deliver_pending() and not sent
    addresses = ["north@example.test", "east@example.test"]
    assert confirm(client, mid, addresses, saved["result_revision"]).is_success
    assert mail.deliver_pending() and len(sent) == 1
    recipients, _, body, packet = sent[0]
    assert recipients == addresses
    assert "  2. Сверяет даты" in body and "**" not in body and "###" not in body
    assert "<strong>Главный вывод.</strong>" in packet["html"] and "<em>Открытый вопрос.</em>" in packet["html"]
    assert packet["html"].count("<ol>") == 3 * copies and packet["html"].count("<ul>") == 2 * copies
    assert "8 встреч, 2 показателя" in body and "8 встреч, 2 показателя" in packet["html"]
    for data in [packet["attachment"][1], client.get(url + "/export.docx").content]:
        doc = Document(BytesIO(data))
        paragraphs = doc.paragraphs
        assert any(p.text == "Итоги пилота в регионах" and p.style.name == "Heading 2" for p in paragraphs)
        assert any(run.bold and run.text == "Главный вывод." for p in paragraphs for run in p.runs)
        assert any(run.italic and run.text == "Открытый вопрос." for p in paragraphs for run in p.runs)
        listed = [p for p in paragraphs if p._p.xpath("w:pPr/w:numPr/w:numId")]
        assert len(listed) == 12 * copies
        assert listed[1].paragraph_format.left_indent > listed[0].paragraph_format.left_indent
        num_ids = [p._p.xpath("w:pPr/w:numPr/w:numId")[0].get(qn("w:val")) for p in listed]
        assert num_ids[-1] == num_ids[-2] and num_ids[-1] != num_ids[5]
        assert any(p.text == "Последняя строка документа." for p in paragraphs)
