"""Regression scenarios confirmed by the AD audit on 2026-11-12."""
import asyncio
import os
import sqlite3
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from ldap3.core.exceptions import LDAPSocketReceiveError

from backend import config, corporate_context as cc, directory_scope, ldap_auth, main, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'ENV_PATH', tmp_path / '.env')
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'mom.db')
    settings = {'LDAP_ENABLED':True, 'LDAP_URL':'ldaps://audit.invalid',
        'LDAP_BASE_DN':'DC=example,DC=com', 'LDAP_EMAIL_ATTR':'mail',
        'LDAP_USER_FILTER':'(mail={username})', 'LDAP_BIND_DN':'CN=service,DC=example,DC=com',
        'LDAP_BIND_PASSWORD':'local-test-only', 'LDAP_STARTTLS':False, 'LDAP_TLS_VERIFY':True}
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
        monkeypatch.setenv(name, ('1' if value else '0') if isinstance(value, bool) else value)
    config._upsert_env({name:os.environ[name] for name in settings})
    store.init_db()
    monkeypatch.setitem(main.app.dependency_overrides, main.require_admin, lambda:{'username':'audit'})


def ldap_payload(**changes):
    return {'enabled':True, 'url':'ldaps://new-audit.invalid', 'base_dn':'DC=example,DC=com',
        'user_filter':'(mail={username})', 'bind_dn':'CN=service,DC=example,DC=com',
        'bind_password':'new-local-test-password', 'starttls':False, 'tls_verify':True, 'email_attr':'mail', **changes}


@pytest.mark.parametrize('failure', [PermissionError, OSError])
def test_failed_save_keeps_runtime_environment_file_and_cache(db, monkeypatch, failure):
    cc._update(people=[{'name':'Previous'}], directory_signature=cc.directory_signature(), synced_at=store.utc_now())
    old = {key:getattr(config,key) for key in os.environ if key.startswith('LDAP_') and hasattr(config,key)}
    old_env = {key:os.environ[key] for key in old}
    before = config.ENV_PATH.read_bytes()
    def fail(values):
        raise failure('test unavailable settings file')
    monkeypatch.setattr(config, '_upsert_env', fail)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post('/api/settings/ldap', json=ldap_payload())
    assert response.status_code == 503
    assert 'подключение не изменено' in response.json()['detail']
    assert {key:getattr(config,key) for key in old} == old
    assert {key:os.environ[key] for key in old} == old_env
    assert config.ENV_PATH.read_bytes() == before
    assert cc.snapshot()['search_cache_ready']
    assert cc.search_people('previous')[0] == [{'name':'Previous'}]
    assert not any(event['action']=='ldap.save' for event in store.list_audit())


def test_successful_save_is_applied_only_after_persistence_and_preserves_blank_password(db, monkeypatch):
    old_url = config.get_ldap_url()
    real_write = config._upsert_env
    observed = []
    def write(values):
        observed.append(config.get_ldap_url())
        assert config.get_ldap_url() == old_url and os.environ['LDAP_URL'] == old_url
        real_write(values)
    monkeypatch.setattr(config, '_upsert_env', write)
    config.set_ldap(**ldap_payload(bind_password=''))
    saved = dotenv_values(config.ENV_PATH, interpolate=False)
    assert observed == [old_url]
    assert config.get_ldap_url() == os.environ['LDAP_URL'] == saved['LDAP_URL'] == 'ldaps://new-audit.invalid'
    assert config.get_ldap_bind_password() == saved['LDAP_BIND_PASSWORD'] == 'local-test-only'


def test_concurrent_ldap_saves_keep_last_disk_and_runtime_values_together(db, monkeypatch):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    real_write = config._upsert_env
    writes = []
    def write(values):
        writes.append(values['LDAP_URL'])
        if values['LDAP_URL'] == 'ldaps://first.invalid':
            entered.set()
            assert release.wait(3)
        real_write(values)
    monkeypatch.setattr(config, '_upsert_env', write)
    def second():
        second_started.set()
        config.set_ldap(**ldap_payload(url='ldaps://second.invalid', bind_password='second-secret'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(config.set_ldap, **ldap_payload(url='ldaps://first.invalid', bind_password='first-secret'))
        assert entered.wait(3)
        later = pool.submit(second)
        try:
            assert second_started.wait(3)
            assert config.get_ldap_url() == 'ldaps://audit.invalid'
            assert writes == ['ldaps://first.invalid']
        finally:
            release.set()
        first.result(timeout=3)
        later.result(timeout=3)
    saved = dotenv_values(config.ENV_PATH, interpolate=False)
    assert writes == ['ldaps://first.invalid','ldaps://second.invalid']
    assert config.get_ldap_url() == saved['LDAP_URL'] == os.environ['LDAP_URL'] == 'ldaps://second.invalid'
    assert config.get_ldap_bind_password() == saved['LDAP_BIND_PASSWORD'] == 'second-secret'


@pytest.mark.parametrize('left,right', [
    (r'OU=R\,D,DC=example,DC=com',r'OU=R\2cD,DC=example,DC=com'),
    ('OU=ИТ,DC=example,DC=com',r'OU=\d0\98\D0\A2,DC=example,DC=com'),
    (r'OU=Team\ ,DC=example,DC=com',r'OU=Team\20,DC=example,DC=com'),
    (r'CN=Back\\slash,DC=example,DC=com',r'CN=Back\5cslash,DC=example,DC=com'),
    ('CN=Anna+UID=123,DC=example,DC=com','UID=123+CN=Anna,DC=example,DC=com'),
])
def test_equivalent_dn_encodings_have_same_scope_key(left, right):
    assert directory_scope.dn_key(left) == directory_scope.dn_key(right)


@pytest.mark.parametrize('left,right', [
    (r'OU=R\,OU\=D,DC=example,DC=com','OU=R,OU=D,DC=example,DC=com'),
    ('CN=Anna+UID=123,DC=example,DC=com','CN=Anna,UID=123,DC=example,DC=com'),
    (r'OU=Team\20,DC=example,DC=com','OU=Team,DC=example,DC=com'),
    (r'OU=R\2cD,DC=example,DC=com',r'OU=R\\2cD,DC=example,DC=com'),
])
def test_decoding_never_broadens_dn_hierarchy_or_double_decodes(left, right):
    assert directory_scope.dn_key(left) != directory_scope.dn_key(right)


def test_escaped_dn_sync_and_auth_accept_equivalent_subtree_but_reject_outsiders(db, monkeypatch):
    base = r'OU=R\2CD,DC=example,DC=com'
    directory_scope.save('selected', [base, r'OU=R\,D,DC=example,DC=com'])
    assert directory_scope.search_bases() == [base]
    monkeypatch.setenv('LDAP_AUTH_BASE_DNS', '["OU=R\\\\2CD,DC=example,DC=com"]')
    class Entry:
        entry_dn = r'CN=Anna,OU=R\,D,DC=example,DC=com'
        def __contains__(self,key): return key in {'cn','mail'}
        def __getitem__(self,key): return SimpleNamespace(value='Anna' if key=='cn' else 'anna@example.com')
    entry = Entry()
    class Connection:
        entries = [entry]
        result = {'description':'success','controls':{'1.2.840.113556.1.4.319':{'value':{'cookie':b''}}}}
        def search(self,**kwargs): return True
        def unbind(self): pass
    connection = Connection()
    monkeypatch.setattr(ldap_auth, '_service_connection', lambda **kwargs:(connection, None))
    assert cc.sync_directory()['people_count'] == 1
    assert ldap_auth._search(connection, 'anna@example.com') == [(entry.entry_dn,'anna@example.com')]
    entry.entry_dn = 'CN=Anna,OU=Outside,DC=example,DC=com'
    with pytest.raises(ldap_auth.LdapError, match='вне выбранной области'):
        cc.sync_directory()
    assert cc.snapshot()['people_count'] == 1


def test_retry_waits_fifteen_minutes_after_long_failure_and_survives_restart(db, monkeypatch):
    clock = [datetime.fromisoformat('2026-11-12T21:00:00+03:00').timestamp()]
    monkeypatch.setattr(cc.time, 'time', lambda:clock[0])
    cc.save_settings(True, True, 'audit')
    def download(**kwargs):
        clock[0] += 20*60
        raise ldap_auth.LdapError('test timeout after slow pages')
    monkeypatch.setattr(ldap_auth, 'sync_people', download)
    with pytest.raises(ldap_auth.LdapError): cc.sync_directory()
    assert cc.snapshot()['next_sync_at'] == '2026-11-12T18:35:00+00:00'
    store.init_db()
    assert cc._claim_sync(True) is None
    clock[0] += 899
    assert cc._claim_sync(True) is None
    clock[0] += 1
    assert cc._claim_sync(True) is not None


def test_old_failure_uses_finished_timestamp_and_manual_retry_is_always_available(db, monkeypatch):
    now = datetime.fromisoformat('2026-11-12T21:20:00+03:00').timestamp()
    monkeypatch.setattr(cc.time, 'time', lambda:now)
    cc.save_settings(True, True, 'audit')
    store.set_app_setting('ldap_sync_job', {'status':'error', 'last_attempt':now-1200,
        'attempt_signature':cc.directory_signature(), 'sync_error':'test error',
        'finished_at':'2026-11-12T18:20:00+00:00'})
    assert cc.snapshot()['next_sync_at'] == '2026-11-12T18:35:00+00:00'
    assert cc._claim_sync(True) is None
    cc.request_sync('audit')
    assert cc._claim_sync(True) is not None


def test_claim_failure_becomes_visible_and_logged_without_changing_cache(db, monkeypatch, caplog):
    cc._update(people=[{'name':'Previous'}], directory_signature=cc.directory_signature(), synced_at=store.utc_now())
    cc.request_sync('audit')
    async def run():
        stop = asyncio.Event()
        def fail(scheduled):
            stop.set()
            raise sqlite3.OperationalError('test database is locked')
        monkeypatch.setattr(cc, '_claim_sync', fail)
        await cc.run_sync(stop)
    asyncio.run(run())
    status = cc.snapshot()
    assert status['sync_progress']['status'] == 'error' and not status['syncing']
    assert 'OperationalError' in status['sync_error']
    assert status['people_count'] == 1 and status['search_cache_ready']
    assert 'test database is locked' in caplog.text
    assert any(event['action']=='ldap.sync.error' for event in store.list_audit())
    assert cc.request_sync('audit')['sync_progress']['status'] == 'queued'


def test_scheduler_reports_database_write_failure_and_can_recover(db, monkeypatch, caplog):
    cc.request_sync('audit')
    observed = store.get_app_setting('ldap_sync_job')
    with monkeypatch.context() as patch:
        patch.setattr(cc, '_write_job', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('test full disk')))
        cc._record_scheduler_failure(observed, sqlite3.OperationalError('test failed claim'))
    assert 'test full disk' in caplog.text
    assert cc._claim_sync(True) is not None


def test_scheduler_backs_off_when_failed_claim_cannot_be_saved(db, monkeypatch):
    cc.request_sync('audit')
    clock, attempts = [0.0], []
    async def run():
        stop = asyncio.Event()
        async def direct(function, *args, **kwargs):
            return function(*args, **kwargs)
        async def tick(awaitable, timeout):
            awaitable.close()
            clock[0] += timeout
            if clock[0] >= 64:
                stop.set()
            raise TimeoutError
        def fail(scheduled):
            attempts.append(clock[0])
            raise sqlite3.OperationalError('test unavailable database')
        monkeypatch.setattr(cc.time, 'monotonic', lambda:clock[0])
        monkeypatch.setattr(cc.asyncio, 'to_thread', direct)
        monkeypatch.setattr(cc.asyncio, 'wait_for', tick)
        monkeypatch.setattr(cc, '_claim_sync', fail)
        monkeypatch.setattr(cc, '_write_job', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('test full disk')))
        await cc.run_sync(stop)
    asyncio.run(run())
    assert attempts == [0, 60]
    assert cc._sync_job()['status'] == 'queued'


def test_scheduled_start_failure_does_not_display_previous_run_progress(db):
    token, signature = cc._claim_sync(False)
    cc._publish_directory(token, signature, [{'name':'Previous'}])
    observed = store.get_app_setting('ldap_sync_job')
    cc._record_scheduler_failure(observed, sqlite3.OperationalError('test failed claim'))
    status = cc.snapshot()
    assert status['sync_progress']['status'] == 'error'
    assert status['sync_progress']['received'] == 0
    assert status['sync_progress']['started_at'] is None
    assert status['people_count'] == 1 and status['search_cache_ready']
    assert cc._sync_job()['actor'] == 'system'


@pytest.mark.parametrize('failure', ['actor_read', 'thread_start'])
def test_failure_after_claim_releases_job_and_records_error(db, monkeypatch, failure):
    cc.request_sync('audit')
    if failure == 'actor_read':
        original, calls = cc._sync_job, []
        def read(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('test setup failure')
            return original(*args)
        monkeypatch.setattr(cc, '_sync_job', read)
    else:
        monkeypatch.setattr(cc.threading.Thread, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('test setup failure')))
    with pytest.raises(RuntimeError, match='test setup failure'):
        cc.sync_directory(scheduled=True)
    status = cc.snapshot()
    assert status['sync_progress']['status'] == 'error' and not status['syncing']
    assert 'RuntimeError' in status['sync_error']
    assert any(event['action']=='ldap.sync.error' for event in store.list_audit())


def test_late_scheduler_error_cannot_overwrite_new_owner_or_finished_job(db):
    cc.request_sync('audit')
    observed = store.get_app_setting('ldap_sync_job')
    token, signature = cc._claim_sync(True)
    cc._record_scheduler_failure(observed, RuntimeError('old error'))
    assert cc._sync_job()['sync_id'] == token
    cc._publish_directory(token, signature, [{'name':'New'}])
    cc._record_scheduler_failure(observed, RuntimeError('old error'))
    assert cc.snapshot()['sync_progress']['status'] == 'succeeded'


@pytest.mark.parametrize('failure,expected', [
    (TimeoutError('private details'), 'не ответил'),
    (ConnectionRefusedError('private details'), 'адрес, порт'),
    (ssl.SSLError('private details'), 'сертификат'),
    (LDAPSocketReceiveError('private details'), 'Ответ AD не получен'),
    (RuntimeError('private details'), 'журнале'),
])
def test_probe_and_sync_report_safe_connection_diagnostics(db, monkeypatch, caplog, failure, expected):
    def fail(**kwargs): raise failure
    monkeypatch.setattr(ldap_auth, '_service_connection', fail)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post('/api/settings/ldap/test', json={'username':''})
    assert response.status_code == 400 and expected in response.json()['detail']
    assert 'private details' not in response.text
    with pytest.raises(ldap_auth.LdapError, match=expected): cc.sync_directory()
    assert expected in cc.snapshot()['sync_error']
    assert 'private details' in caplog.text


def test_cleanup_failure_does_not_hide_ldap_probe_read_error(db, monkeypatch):
    class Connection:
        def unbind(self): raise OSError('test unbind failed')
    monkeypatch.setattr(ldap_auth, '_service_connection', lambda:(Connection(), None))
    monkeypatch.setattr(ldap_auth, '_search', lambda *args: (_ for _ in ()).throw(TimeoutError('private timeout')))
    response = TestClient(main.app, raise_server_exceptions=False).post('/api/settings/ldap/test',json={'username':'anna@example.com'})
    assert response.status_code == 400 and 'не ответил' in response.json()['detail']
