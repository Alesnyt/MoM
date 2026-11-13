from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from backend import config, directory_scope, directory_search as search, ldap_auth, main, people, store


@pytest.fixture
def directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    for name in ('UPLOAD_DIR','AUDIO_DIR','WHISPER_DIR'):
        monkeypatch.setattr(config,name,tmp_path/name)
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'mom.db')
    store.init_db()
    monkeypatch.setattr(config,'LDAP_ENABLED',True)
    monkeypatch.setattr(config,'LDAP_URL','ldaps://ad.example')
    monkeypatch.setattr(config,'LDAP_BASE_DN','dc=example')
    monkeypatch.setattr(search,'_cache',search.OrderedDict())
    monkeypatch.setattr(search,'_pending',{})
    monkeypatch.setattr(search,'_rates',search.OrderedDict())
    calls = []
    def lookup(query, **kwargs):
        calls.append((query,kwargs))
        return [{'name':'Анна','email':'anna@example.com','source':'ldap'}]
    monkeypatch.setattr(ldap_auth,'search_people',lookup)
    return calls


def test_live_cache_ttl_casefold_copy_and_errors(directory, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(search.time,'monotonic',lambda:clock[0])
    result = search.search(' АннА ',for_speakers=True)
    result[0]['name'] = 'Changed in UI'
    assert search.search('анна',for_speakers=True)[0]['name'] == 'Анна'
    assert len(directory) == 1
    clock[0] += 31
    search.search('анна',for_speakers=True)
    assert len(directory) == 2
    def fail(*a, **kw): raise ldap_auth.LdapError('Unavailable')
    monkeypatch.setattr(ldap_auth,'search_people',fail)
    for _ in range(2):
        with pytest.raises(ldap_auth.LdapError): search.search('other')
    assert not search._pending


@pytest.mark.parametrize('change', ['url','password','scope','fields','domains'])
def test_settings_changes_invalidate_live_results(directory, monkeypatch, change):
    search.search('анна',for_speakers=True)
    if change == 'url': monkeypatch.setattr(config,'LDAP_URL','ldaps://new.example')
    elif change == 'password': monkeypatch.setattr(config,'LDAP_BIND_PASSWORD','new-local-test-password')
    elif change == 'scope': directory_scope.save('selected',[])
    elif change == 'fields': directory_scope.save_sync_policy([],[])
    else: directory_scope.save_sync_policy(['job_title'],['other.example'])
    search.search('анна',for_speakers=True)
    assert len(directory) == 2
    assert 'new-local-test-password' not in repr(search._cache)


def test_identical_inflight_queries_share_one_ldap_call(directory, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    joined = threading.Event()
    original_future = search.Future
    class JoiningFuture(original_future):
        def result(self, *args, **kwargs):
            joined.set()
            return super().result(*args, **kwargs)
    monkeypatch.setattr(search, 'Future', JoiningFuture)
    calls = []
    def slow(*a, **kw):
        calls.append(1); entered.set(); assert release.wait(3)
        return [{'name':'Анна','email':'anna@example.com'}]
    monkeypatch.setattr(ldap_auth,'search_people',slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(search.search,'анна')
        assert entered.wait(2)
        second = pool.submit(search.search,'АННА')
        assert joined.wait(2)
        release.set()
        assert first.result(2) == second.result(2)
    assert len(calls) == 1


def test_parallel_limit_does_not_hold_cache_lock_and_policy_race_discards_results(directory,monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow(*a,**kw): entered.set(); assert release.wait(3); return [{'name':'private'}]
    monkeypatch.setattr(search,'MAX_INFLIGHT',1)
    monkeypatch.setattr(search,'MAX_WAITERS',0)
    monkeypatch.setattr(ldap_auth,'search_people',slow)
    with ThreadPoolExecutor() as pool:
        request = pool.submit(search.search,'анна',for_speakers=True)
        assert entered.wait(2)
        with pytest.raises(ldap_auth.LdapError,match='занят'): search.search('борис')
        with pytest.raises(ldap_auth.LdapError,match='занят'): search.search('анна',for_speakers=True)
        directory_scope.save_sync_policy([],['restricted.example'])
        release.set()
        with pytest.raises(ldap_auth.LdapError,match='изменились'): request.result(2)
    assert not search._pending and not search._cache


def test_dataset_mode_never_queries_live_ad_and_caches_are_bounded(directory,monkeypatch):
    store.set_app_setting('ldap_speaker_search',{'mode':'cache'})
    monkeypatch.setattr(people.corporate_context,'search_people',lambda q: ([{'name':'Cached','email':'c@example.com'}],None))
    assert people.suggest('ca')['people'][0]['name'] == 'Cached'
    assert directory == []
    monkeypatch.setattr(search,'MAX_CACHE',2)
    for q in ('aa','bb','cc'): search.search(q)
    assert len(search._cache) == 2


def test_search_rate_is_per_account_in_memory_and_has_retry_header(directory,monkeypatch):
    now = [100.]
    monkeypatch.setattr(search.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(search,'REQUESTS_PER_MINUTE',2)
    monkeypatch.setitem(main.app.dependency_overrides,main.require_user,lambda: {'user_id':'u'})
    monkeypatch.setattr(people,'suggest',lambda q: {'people':[]})
    client=TestClient(main.app)
    assert client.get('/api/people?q=aa').status_code == 200
    assert client.get('/api/people?q=bb').status_code == 200
    blocked = client.get('/api/people?q=cc')
    assert blocked.status_code == 429 and blocked.headers['retry-after'] == '60'
    assert search.allow('user:another')
    now[0] += 60
    assert client.get('/api/people?q=dd').status_code == 200
    assert client.get('/api/people',params={'q':'a'*201}).status_code == 422
