"""Fault, quality and security regressions from the whole-project review."""
import asyncio
import copy
import csv
import io
import json
import os
import ssl
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from fastapi import Response
from fastapi.testclient import TestClient

from backend import (analyze, background, config, control, mail, main, people,
                     pipeline, prompts, sheet, speakers, store, term_discovery, worker)


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name, value in dict(DATA_DIR=tmp_path, DB_PATH=tmp_path/'mom.db', ENV_PATH=tmp_path/'.env',
                            UPLOAD_DIR=tmp_path/'uploads', AUDIO_DIR=tmp_path/'audio',
                            WHISPER_DIR=tmp_path/'whisper').items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(store, 'DB_PATH', config.DB_PATH)
    monkeypatch.setattr(worker, '_services', {})
    monkeypatch.setattr(config, 'ldap_configured', lambda: False)
    monkeypatch.setattr(config, 'get_max_jobs', lambda: 1)
    store.init_db()
    return tmp_path


@pytest.fixture
def settings(monkeypatch):
    names = ('OPENAI_API_KEY OPENAI_BASE_URL OPENAI_MODEL WHISPER_MODEL ADMIN_USER ADMIN_PASSWORD_HASH '
             'UI_THEME MAX_JOBS PUBLIC_BASE_URL SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_STARTTLS')
    for name in names.split():
        monkeypatch.setattr(config, name, getattr(config, name))
    with patch.dict(os.environ):
        yield


@pytest.mark.parametrize('port', [465, 587])
@pytest.mark.parametrize('reject', [False, True])
def test_smtp_validates_server_before_credentials_or_message(monkeypatch, port, reject):
    events = []
    for name, value in dict(host='mail.invalid', port=port, user='sender', password='test-only',
                            from_='sender@example.com', starttls=True).items():
        monkeypatch.setattr(config, 'get_smtp_'+name.rstrip('_'), lambda value=value: value)

    def validate(context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        events.append('tls')
        if reject:
            raise ssl.SSLCertVerificationError('untrusted certificate')

    class SMTP:
        def __init__(self, host, port, timeout, context=None):
            if context is not None:
                validate(context)
        def ehlo(self): pass
        def starttls(self, *, context): validate(context)
        def login(self, *args): events.append('login')
        def send_message(self, message): events.append('send')
        def quit(self): pass

    monkeypatch.setattr(mail.smtplib, 'SMTP', SMTP)
    monkeypatch.setattr(mail.smtplib, 'SMTP_SSL', SMTP)
    if reject:
        with pytest.raises(mail.TemporaryMailError):
            mail.send_mail('reader@example.com', 'Subject', 'Body')
        assert events == ['tls']
    else:
        mail.send_mail('reader@example.com', 'Subject', 'Body')
        assert events == ['tls', 'login', 'send']


def test_distinct_cited_decisions_and_tasks_survive_merge():
    part = {'decisions':['Запустить пилот', 'Выделить бюджет'],
            'decision_sources':[{'decision':'Запустить пилот', 'turns':[1]},
                                {'decision':'Выделить бюджет', 'turns':[1]}],
            'action_items':[{'task':'Подготовить отчёт', 'assignee':'Анна', 'due':'пятница', 'source_turns':[1]},
                            {'task':'Отправить договор', 'assignee':'Анна', 'due':'пятница', 'source_turns':[1]}]}
    merged = {key:copy.deepcopy(value[:1]) for key,value in part.items()}
    duplicate = copy.deepcopy(part)
    duplicate['action_items'][0].update(task='Подготовить отчёт.', source_turns=[2])
    duplicate['decision_sources'][0].update(decision='Запустить пилот.', turns=[2])
    final = analyze._retain_grounded_items(merged, [part, duplicate])
    assert final['decisions'] == part['decisions']
    assert len(final['action_items']) == 2
    assert final['action_items'][0]['source_turns'] == [1,2]
    assert final['decision_sources'][0]['turns'] == [1,2]
    assert part['action_items'][0]['source_turns'] == [1]


@pytest.mark.parametrize('duration', [0.05, 0.2, 0.3, 0.44])
def test_short_answers_reach_asr_and_keep_unambiguous_speaker(duration):
    turns = [{'start':0,'end':2,'speaker':'A'}, {'start':3,'end':3+duration,'speaker':'B'}]
    spans = speakers.speaker_spans(turns)
    assert spans == [{'start':0.0,'end':2.0}, {'start':3.0,'end':3+duration}]
    result = speakers.assign_speakers([{**spans[1], 'text':'Да'}], turns)
    assert result['segments'][0]['speaker'] == 'S2'
    # Tiny overlap with a long phrase remains uncertain.
    result = speakers.assign_speakers([{'start':3,'end':10,'text':'Длинная фраза'}], turns)
    assert result['segments'][0]['speaker'] == 'unknown'


def test_coarse_chunk_keeps_short_reply_and_spans_never_exceed_model_limit():
    cuts = speakers.plan_cuts({'start':0,'end':24}, [
        {'start':0,'end':20,'speaker':'A'}, {'start':21,'end':21.2,'speaker':'B'}])
    assert cuts[-1] == {'start':21,'end':21.2}
    for duration in (20.3, 25.3, 100.01):
        spans = speakers.speaker_spans([{'start':0,'end':duration,'speaker':'A'}], max_seconds=25)
        assert spans[0]['start'] == 0 and spans[-1]['end'] == duration
        assert all(0 < s['end']-s['start'] <= 25 for s in spans)
        assert all(a['end'] == b['start'] for a,b in zip(spans,spans[1:]))


@pytest.mark.parametrize('setter', [
    lambda: config.apply_connection('https://new.invalid/v1','new','local-whisper'),
    lambda: config.set_openai_api_key('sk-ws-example-test-key'),
    lambda: config.set_admin_credentials('new-admin','new-hash'),
    lambda: config.set_ui_theme('t2'),
    lambda: config.set_max_jobs(3),
    lambda: config.set_public_base_url('https://new.invalid'),
    lambda: config.set_smtp(host='new.invalid',port=465,user='new',password='new',from_addr='a@b.c',
                            starttls=True, public_url='https://new.invalid'),
])
def test_failed_persistence_keeps_all_live_settings(db, settings, monkeypatch, setter):
    before = {key:value for key,value in vars(config).items() if key.isupper()}
    env = dict(os.environ)
    def fail(values): raise PermissionError('test readonly')
    monkeypatch.setattr(config, '_upsert_env', fail)
    with pytest.raises(config.SettingsSaveError): setter()
    assert {key:getattr(config,key) for key in before} == before
    assert dict(os.environ) == env


def test_failed_save_returns_clear_error_without_success_audit(db, settings, monkeypatch):
    monkeypatch.setitem(main.app.dependency_overrides, main.require_admin, lambda:{'username':'audit'})
    def fail(values): raise OSError('test full disk')
    monkeypatch.setattr(config, '_upsert_env', fail)
    response = TestClient(main.app).post('/api/settings/models', json={'chat_model':'new'})
    assert response.status_code == 503
    assert 'не изменена' in response.json()['detail']
    assert not any(e['action']=='models.save' for e in store.list_audit())


def test_concurrent_settings_saves_keep_disk_and_runtime_consistent(db, settings):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda n:config.apply_connection(f'https://model{n}.invalid',f'model{n}','local-small'), range(20)))
    saved = dotenv_values(config.ENV_PATH, interpolate=False)
    for key in ('OPENAI_BASE_URL', 'OPENAI_MODEL', 'WHISPER_MODEL'):
        assert saved[key] == getattr(config,key) == os.environ[key]


def test_meeting_configuration_and_prompts_survive_admin_changes(db, settings, monkeypatch):
    config.set_runtime('old-key', 'https://old.invalid', 'old-model', 'local-small')
    monkeypatch.setattr(store, 'get_llm_settings', lambda:None)
    async def scenario():
        with config.model_settings_scope(), prompts.settings_scope():
            before = prompts.snapshot()
            await asyncio.to_thread(config.set_runtime, 'sk-ws-new-key', 'https://new.invalid', 'new-model', 'gigaam-large')
            monkeypatch.setattr(store, 'get_llm_settings', lambda:{'version':2,'protocol_prompt':'new',
                               'name_review_prompt':'new','name_review_enabled':False})
            assert config.get_chat_model() == await asyncio.to_thread(config.get_chat_model) == 'old-model'
            assert config.get_api_key() == 'old-key' and config.get_base_url() == 'https://old.invalid'
            assert config.detect_provider() == 'openai' and config.local_whisper_size() == 'small'
            assert config.get_asr_model() == 'local-small'
            assert prompts.snapshot() == before
        assert config.get_chat_model() == 'new-model'
        assert config.detect_provider() == 'qwen'
        assert prompts.snapshot()['version'] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('path', ['/api/meetings/transcript','/api/admin/context/import','/api/admin/glossaries/preview'])
def test_upload_authentication_precedes_reading_any_body(db, path):
    def forbidden_body():
        pytest.fail('Unauthenticated request body was consumed')
        yield b'never'
    response = TestClient(main.app).post(path, content=forbidden_body(),
                                        headers={'content-type':'multipart/form-data; boundary=test'})
    assert response.status_code == 401


@pytest.mark.parametrize('mode', ['live','cache'])
def test_directory_matches_are_visible_among_many_local_users(db, monkeypatch, mode):
    monkeypatch.setattr(config,'ldap_configured',lambda:True)
    monkeypatch.setattr(people.directory_scope,'speaker_search_mode',lambda:mode)
    monkeypatch.setattr(store,'search_user_emails',lambda q:[{'email':f'alex{i}@example.com'} for i in range(40)])
    found = [{'name':'Target Alex','email':'target@example.com'}]
    monkeypatch.setattr(people.ldap_auth,'search_people',lambda *a,**kw:found)
    monkeypatch.setattr(people.corporate_context,'search_people',lambda q:(found,None))
    result = people.suggest('alex')['people']
    assert len(result)==40 and result[0]['email']=='target@example.com'


def test_local_suggestions_do_not_load_all_accounts_or_meeting_counts(db, monkeypatch):
    for i in range(60): store.create_user(f'alex{i}@example.com','unused')
    monkeypatch.setattr(store, 'list_users', lambda:pytest.fail('full account scan'))
    assert len(people.suggest('alex')['people']) == 40
    assert people.suggest('alex2@example.com')['people'][0]['email'] == 'alex2@example.com'
    assert people.suggest('%')['people'] == []


def test_discovery_fingerprint_and_evidence_use_original_asr():
    original = {'segments':[{'text':'Алексеев проверил файн би','start':0,'end':5}]}
    digest, parts = term_discovery.original_parts(json.dumps(original))
    corrected = copy.deepcopy(original)
    corrected['segments'][0].update(original_text=parts[0]['text'],text='Александров проверил файн би')
    assert term_discovery.original_parts(json.dumps(corrected)) == (digest,parts)
    corrected['segments'][0].update(context_original_text=parts[0]['text'],original_text='Алексеев проверил FineBI',
                                    text='Александров проверил FineBI')
    assert term_discovery.original_parts(json.dumps(corrected)) == (digest,parts)


def test_csv_formulas_are_text_and_normal_cells_survive():
    values = ['=1+1','+1','-1','@SUM(A1:A2)',' \t=1','\tcmd','\rtext','\ntext','Анна','https://example.com','a,b']
    rows = list(csv.reader(io.StringIO(sheet.csv_bytes(['Header'], [[v] for v in values]).decode('utf-8-sig'))))
    assert [r[0] for r in rows[1:]] == ["'"+v for v in values[:8]] + values[8:]


async def idle(stop): await stop.wait()


def idle_backgrounds(monkeypatch):
    monkeypatch.setattr(worker, '_run_delivery', idle)
    monkeypatch.setattr(worker.corporate_context, 'run_sync', idle)
    monkeypatch.setattr(worker.glossary, 'run_imports', idle)
    monkeypatch.setattr(worker.term_discovery, 'run', idle)
    monkeypatch.setattr(background, 'RETRY_SECONDS', .05)


def test_queue_recovers_and_health_reports_failure_during_retry(db, monkeypatch):
    idle_backgrounds(monkeypatch)
    claim = store.claim_next_meeting
    calls = 0
    def flaky(limit):
        nonlocal calls
        calls += 1
        if calls==1: raise OSError('temporary database failure')
        return claim(limit)
    monkeypatch.setattr(store, 'claim_next_meeting', flaky)
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(stop))
        try:
            async with asyncio.timeout(3):
                while not calls or worker._services['queue'].state != 'retrying': await asyncio.sleep(.001)
                response = Response()
                assert main.health(response)['ok'] is False and response.status_code == 503
                while not worker.snapshot()['ok']: await asyncio.sleep(.005)
            assert not task.done() and calls >= 2
        finally:
            stop.set(); worker.notify_work()
            await task
        assert not worker.snapshot()['ok']
    asyncio.run(scenario())


def test_refresh_failure_after_claim_keeps_one_running_job(db, monkeypatch):
    idle_backgrounds(monkeypatch)
    user = store.create_user('worker@example.com','unused')
    store.create_meeting('first','First','one.webm',user['id'])
    started, release = None, None
    calls = []
    async def process(mid):
        calls.append(mid); started.set()
        await release.wait()
        store.update_meeting(mid,status='done')
    monkeypatch.setattr(worker, 'process_meeting', process)
    original = store.refresh_queue_messages
    broken = False
    def flaky():
        nonlocal broken
        if not broken:
            broken = True
            raise OSError('temporary refresh failure after claim')
        return original()
    monkeypatch.setattr(store, 'refresh_queue_messages', flaky)
    async def scenario():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        stop = asyncio.Event(); task = asyncio.create_task(worker.run_worker(stop))
        try:
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.sleep(.15)
            assert calls == ['first'] and control.cancel_job('first')
            release.set()
            async with asyncio.timeout(3):
                while store.get_meeting('first')['status']!='done': await asyncio.sleep(.01)
        finally:
            release.set(); stop.set(); worker.notify_work(); await task
    asyncio.run(scenario())


def test_background_service_restarts_after_unexpected_exit(monkeypatch):
    monkeypatch.setattr(background, 'RETRY_SECONDS', .01)
    async def scenario():
        stop, restarted = asyncio.Event(), asyncio.Event()
        calls = 0
        async def run(stop):
            nonlocal calls
            calls += 1
            if calls == 1: raise RuntimeError('unexpected service exit')
            restarted.set(); await stop.wait()
        service = background.Service('test')
        task = asyncio.create_task(service.supervise(run, stop))
        await asyncio.wait_for(restarted.wait(), 2)
        assert service.snapshot()['ok'] and calls==2
        stop.set(); await task
        assert service.state=='stopped'
    asyncio.run(scenario())


def test_failed_stop_persistence_does_not_discard_restart_request(db, monkeypatch):
    user = store.create_user('worker@example.com','unused')
    store.create_meeting('restart','Restart','one.webm',user['id'])
    store.update_meeting('restart',status='transcribing')
    control.request_stop('restart',restart=True)
    update = store.update_meeting
    def fail(*a,**kw): raise OSError('database unavailable')
    monkeypatch.setattr(store,'update_meeting',fail)
    with pytest.raises(OSError): control.finish('restart')
    monkeypatch.setattr(store,'update_meeting',update)
    assert control.finish('restart')=='restart'
    assert store.get_meeting('restart')['status']=='queued'
