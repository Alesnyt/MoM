import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from dotenv import dotenv_values, load_dotenv
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
import starlette.formparsers as multipart

from backend import analyze, config, name_review, upload
from backend.main import app


def test_unauthenticated_audio_never_creates_temporary_file(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated multipart body was parsed")
    monkeypatch.setattr(multipart, "SpooledTemporaryFile", forbidden)
    response = TestClient(app).post('/api/meetings', files={'file': ('test.wav', b'x' * 2_000_000)})
    assert response.status_code == 401


def request_for(chunks, content_length=None):
    headers = [(b'content-type', b'multipart/form-data; boundary=audit')]
    if content_length is not None:
        headers.append((b'content-length', str(content_length).encode()))
    remaining = iter(chunks)
    async def receive():
        chunk = next(remaining, None)
        return {'type': 'http.request', 'body': chunk or b'', 'more_body': chunk is not None}
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/meetings', 'headers': headers}, receive)


@pytest.mark.parametrize('declared', [None, 1])
def test_audio_stream_limit_cleans_partial_spool(monkeypatch, declared):
    monkeypatch.setattr(config, 'MAX_UPLOAD_BYTES', 1024)
    files = []
    real = multipart.SpooledTemporaryFile
    def track(*args, **kwargs):
        f = real(*args, **kwargs)
        files.append(f)
        return f
    monkeypatch.setattr(multipart, 'SpooledTemporaryFile', track)
    chunks = [b'--audit\r\nContent-Disposition: form-data; name="file"; filename="test.wav"\r\n\r\n', b'x'*32000, b'x'*40000]
    async def run():
        async with upload.audio_form(request_for(chunks, declared), lambda size: None):
            pytest.fail('Oversized stream accepted')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 413
    assert files and all(f.closed for f in files)


def test_declared_oversize_body_is_not_read(monkeypatch):
    monkeypatch.setattr(config, 'MAX_UPLOAD_BYTES', 1024)
    def chunks():
        pytest.fail('Oversized body read')
        yield b''
    async def run():
        async with upload.audio_form(request_for(chunks(), 100000), lambda size: None):
            pytest.fail('Oversized request accepted')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 413


@pytest.mark.parametrize('original,replacement', [('не согласен','полностью согласен'), ('Не согласен','Аля'), ('Оля','Мария'), ('Оля','Алевтина')])
def test_name_review_rejects_unverified_words_and_aliases(original, replacement):
    doc = {'segments': [{'text': original + ', ответь.'}]}
    before = copy.deepcopy(doc)
    report = name_review.apply_mentions(doc, [{'segment_index': 0, 'occurrence': 0,
        'original': original, 'replacement': replacement, 'decision': 'correct',
        'participant_id': 'S1', 'confidence': 'high', 'reason': 'Обращение'}], [{'id': 'S1', 'name': 'Аля'}])
    assert doc == before
    assert report[0]['decision'] == 'uncertain'


def test_name_review_accepts_component_of_full_participant_name():
    doc = {'segments': [{'text': 'Елена, ответь.'}]}
    name_review.apply_mentions(doc, [{'segment_index': 0, 'occurrence': 0,
        'original': 'Елена', 'replacement': 'Алёна', 'decision': 'correct',
        'participant_id': 'S1', 'confidence': 'high', 'reason': 'Обращение'}], [{'id': 'S1', 'name': 'Алёна Иванова'}])
    assert doc['segments'][0]['text'] == 'Алёна, ответь.'


@pytest.mark.parametrize('bad', [
    {'summary': {'text': 'test'}}, {'participants': None}, {'participants': [{}]},
    {'key_points': 'text'}, {'decisions': [None]}, {'title': []},
    {'action_items': None}, {'action_items': [{'task':'test','assignee':{}}]},
    {'action_items': [{'task':'test','due':[]}]}, {'mom': {'agenda': ['test']}},
    {'mom': {'agenda': [{'topic': 'test', 'outcome': {}}]}},
    {'mom': {'next_meeting': []}}, {'date_hint': {}},
])
def test_invalid_protocol_rejected_before_storage(bad):
    with pytest.raises(ValueError, match='Неверная структура протокола'):
        analyze._parse_result(json.dumps(bad))


def test_valid_protocol_and_missing_optional_fields():
    value = analyze._parse_result('```json\n{"summary":"Текст","action_items":[{"task":"Сделать"}]}\n```')
    assert value['participants'] == []
    assert value['mom']['agenda'] == []
    assert value['action_items'][0]['assignee'] is None


@pytest.mark.parametrize('value', ['alpha #beta', '${HOME}', "quote'and\\slash", 'line1\nLDAP_ENABLED=1\nline2', ' space ', '"quoted"'])
def test_settings_survive_dotenv_roundtrip(tmp_path, monkeypatch, value):
    target = tmp_path / '.env'
    target.write_text('# keep comment\nUNCHANGED=ok\nSMTP_PASSWORD=old\n')
    monkeypatch.setattr(config, 'ENV_PATH', target)
    config._upsert_env({'SMTP_PASSWORD': value, 'LDAP_BIND_PASSWORD': value})
    loaded = dotenv_values(target, interpolate=False)
    assert loaded == {'UNCHANGED': 'ok', 'SMTP_PASSWORD': value, 'LDAP_BIND_PASSWORD': value}
    assert target.stat().st_mode & 0o777 == 0o600
    monkeypatch.delenv('SMTP_PASSWORD', raising=False)
    monkeypatch.delenv('LDAP_BIND_PASSWORD', raising=False)
    load_dotenv(target, interpolate=False)
    import os
    assert os.environ['SMTP_PASSWORD'] == value
    assert os.environ['LDAP_BIND_PASSWORD'] == value


def test_concurrent_settings_updates_are_preserved(tmp_path, monkeypatch):
    target = tmp_path / '.env'
    target.write_text('')
    monkeypatch.setattr(config, 'ENV_PATH', target)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: config._upsert_env({f'KEY_{i}': str(i)}), range(12)))
    assert dotenv_values(target) == {f'KEY_{i}': str(i) for i in range(12)}


def test_settings_support_docker_file_bind_mount(tmp_path, monkeypatch):
    import errno
    target = tmp_path / '.env'
    target.write_text('UNCHANGED=ok\n')
    monkeypatch.setattr(config, 'ENV_PATH', target)
    real_replace = config.os.replace
    def mounted(source, destination):
        if destination == target:
            raise OSError(errno.EBUSY, 'Device or resource busy')
        return real_replace(source, destination)
    monkeypatch.setattr(config.os, 'replace', mounted)
    config._upsert_env({'SMTP_PASSWORD': 'alpha #beta'})
    assert dotenv_values(target, interpolate=False) == {'UNCHANGED': 'ok', 'SMTP_PASSWORD': 'alpha #beta'}
