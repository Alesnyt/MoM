"""Exercise the actual LDAP parser/paging and durable cache job lifecycle."""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from ldap3 import Connection, MOCK_SYNC, Server

from backend import auth, config, corporate_context as cc, directory_scope, ldap_auth, main, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(config, "ldap_configured", lambda: True)
    monkeypatch.setattr(config, "get_ldap_url", lambda: "ldaps://test.example.com")
    monkeypatch.setattr(config, "get_ldap_base_dn", lambda: "DC=example,DC=com")
    monkeypatch.setattr(config, "get_ldap_email_attr", lambda: "mail")
    store.init_db()


def test_actual_ldap_paging_empty_query_domains_and_selected_attributes(db, monkeypatch):
    base = "OU=Staff,DC=example,DC=com"
    conn = Connection(Server("local-test"), client_strategy=MOCK_SYNC)
    conn.strategy.add_entry(base, {"objectClass": "organizationalUnit"})
    for i in range(1005):
        conn.strategy.add_entry(f"CN=Person{i},{base}", {"objectClass": "person", "cn": f"Person{i}",
            "displayName": f"Person {i}", "mail": f"person{i}@example.com", "title": "Engineer", "department": "IT"})
    conn.strategy.add_entry(f"CN=Contractor,{base}", {"objectClass": "person", "mail": "contractor@vendor.com"})
    conn.strategy.add_entry(f"CN=Computer,{base}", {"objectClass": ["person", "computer"], "mail": "computer@example.com"})
    conn.strategy.add_entry("CN=Outside,OU=Other,DC=example,DC=com", {"objectClass": "person", "mail": "outside@example.com"})
    conn.bind()
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda **kwargs: (conn, None))
    directory_scope.save("selected", [base])
    directory_scope.save_sync_policy(["department"], ["example.com"])
    events = []
    people = ldap_auth.sync_people(progress=events.append)
    assert len(people) == 1005
    assert all(set(p) == {"name", "email", "department"} and p["department"] == "IT" for p in people)
    completed = [event for event in events if event["pages"] and event["received"]]
    assert {event["received"] for event in completed} == {500, 1000, 1005}
    assert events[-1]["pages"] == 3 and events[-1]["scopes_done"] == 1
    assert conn.closed


def test_bind_test_does_not_imply_bulk_read_permission(db, monkeypatch):
    class Fake:
        result = {"description": "insufficientAccessRights", "result": 50}
        entries = []
        def search(self, **kwargs):
            return False
        def unbind(self):
            pass
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda **kwargs: (Fake(), None))
    assert ldap_auth.test_connection()["ok"]
    with pytest.raises(ldap_auth.LdapError, match="insufficientAccessRights.*50"):
        cc.sync_directory()
    assert cc.snapshot()["sync_progress"]["status"] == "error"
    assert "insufficientAccessRights" in cc.snapshot()["sync_error"]


def test_bulk_has_no_global_deadline_and_no_50000_person_cap(db, monkeypatch):
    # Advancing the clock between pages models a long transfer without slowing the suite.
    clock = [time.monotonic()]
    monkeypatch.setattr(ldap_auth.time, "monotonic", lambda: clock[0])
    class Entry:
        def __init__(self, i):
            self.i, self.entry_dn = i, f"CN=Person{i},DC=example,DC=com"
        def __contains__(self, key):
            return key in {"cn", "mail"}
        def __getitem__(self, key):
            return SimpleNamespace(value=f"Person{self.i}" if key == "cn" else f"p{self.i}@example.com")
    class Fake:
        page = 0
        def search(self, **kwargs):
            assert kwargs["time_limit"] == 0 and kwargs["paged_size"] == 500
            clock[0] += 120
            start = self.page * 500
            self.entries = [Entry(i) for i in range(start, min(start + 500, 50001))]
            self.page += 1
            self.result = {"description": "success", "controls": {"1.2.840.113556.1.4.319": {
                "value": {"cookie": bytearray(str(self.page).encode()) if start + 500 < 50001 else b""}}}}
        def unbind(self):
            pass
    conn = Fake()
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda **kwargs: (conn, None))
    assert len(ldap_auth.sync_people()) == 50001
    assert conn.page == 101


def test_sync_timeouts_are_separate_from_interactive_connection(db, monkeypatch):
    captured = []
    class Fake:
        closed = False
        def __init__(self, server, **kwargs):
            captured.append(kwargs)
        def open(self):
            pass
        def bind(self):
            return True
        def unbind(self):
            pass
    monkeypatch.setattr("ldap3.Connection", Fake)
    monkeypatch.setattr(ldap_auth, "_server", lambda **kwargs: None)
    monkeypatch.setattr(config, "get_ldap_starttls", lambda: False)
    ldap_auth._service_connection()
    ldap_auth._service_connection(sync=True)
    assert [item["receive_timeout"] for item in captured] == [8, 300]


def test_queue_returns_immediately_survives_restart_and_runs_with_schedule_disabled(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ldap_auth, "sync_people", lambda **kwargs: calls.append(True) or [{"name": "Anna"}])
    cc.save_settings(True, False, "admin", use_ad=False)
    client = TestClient(main.app)
    assert client.get("/api/admin/directory/cache").status_code == 401
    assert client.post("/api/admin/directory/sync").status_code == 401
    for path in ("settings", "search", "policy"):
        assert client.post(f"/api/admin/directory/{path}", json={}).status_code == 401
    store.put_session("testadmin", "admin", username="testadmin", expires=time.time() + 3600)
    client.cookies.set(auth.COOKIE_NAME, "testadmin")
    response = client.post("/api/admin/directory/sync")
    assert response.status_code == 202 and response.json()["sync_progress"]["status"] == "queued"
    assert calls == []
    assert client.post("/api/admin/directory/sync").status_code == 400
    store.init_db()
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(cc.run_sync(stop))
        try:
            for _ in range(100):
                if cc.snapshot()["sync_progress"]["status"] == "succeeded":
                    break
                await asyncio.sleep(.01)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2)
    asyncio.run(run())
    assert calls == [True]
    assert cc.snapshot()["people_count"] == 1
    assert cc.snapshot()["search_mode"] == "live"
    assert cc.for_meeting({}).entries == []
    assert client.post("/api/admin/directory/settings", json={"sync_time": "21:30"}).json()["use_ad"] is False
    cc.save_search_mode("cache", "admin")
    cc.save_settings(True, None, "admin", use_ad=True)
    assert len(cc.for_meeting({}).entries) == 1
    assert cc.snapshot()["sync_ad"] is False


def test_progress_and_heartbeat_do_not_publish_partial_or_invalidate_index(db, monkeypatch):
    cc._update(people=[{"name": "Previous"}], directory_signature=cc.directory_signature(), synced_at=store.utc_now())
    version = store.get_app_setting("corporate_context_version")
    started, release = threading.Event(), threading.Event()
    def download(*, progress):
        progress({"phase": "reading", "received": 500, "pages": 1})
        started.set()
        assert release.wait(10)
        return [{"name": "New"}]
    monkeypatch.setattr(ldap_auth, "sync_people", download)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cc.sync_directory)
        try:
            assert started.wait(3)
            status = cc.snapshot()
            assert status["sync_progress"]["received"] == 500 and status["syncing"]
            assert cc.search_people("previous")[0] == [{"name": "Previous"}]
            assert store.get_app_setting("corporate_context_version") == version
            lease = cc._sync_job()["sync_until"]
            deadline = time.monotonic() + 7
            while cc._sync_job()["sync_until"] == lease and time.monotonic() < deadline:
                time.sleep(.05)
            assert cc._sync_job()["sync_until"] > lease
            assert store.get_app_setting("corporate_context_version") == version
            assert cc._claim_sync(True) is None
        finally:
            release.set()
        assert future.result(timeout=3)["sync_progress"]["status"] == "succeeded"
    assert cc.search_people("new")[0] == [{"name": "New"}]


def test_unexpected_error_is_logged_audited_and_preserves_snapshot(db, monkeypatch, caplog):
    cc._update(people=[{"name": "Previous"}], directory_signature=cc.directory_signature(), synced_at=store.utc_now())
    def download(*, progress):
        progress({"phase": "reading", "received": 500, "pages": 1})
        raise RuntimeError("test failure detail")
    monkeypatch.setattr(ldap_auth, "sync_people", download)
    with pytest.raises(RuntimeError):
        cc.sync_directory("admin")
    assert "test failure detail" in caplog.text
    status = cc.snapshot()
    assert not status["syncing"] and status["people_count"] == 1
    assert status["sync_progress"]["received"] == 500 and "RuntimeError" in status["sync_error"]
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM audit_events WHERE action='ldap.sync.error'").fetchone()[0] == 1


def test_changed_policy_or_lost_lease_cannot_publish_stale_snapshot(db):
    token, signature = cc._claim_sync(False)
    directory_scope.save_sync_policy([], [])
    with pytest.raises(ValueError, match="Настройки AD изменились"):
        cc._publish_directory(token, signature, [{"name": "Wrong"}])
    cc._update_job(token, sync_until=0)
    assert cc.snapshot()["sync_progress"]["status"] == "interrupted"
    new_token, new_signature = cc._claim_sync(False)
    assert not cc._publish_directory(token, signature, [{"name": "Wrong"}])
    assert not cc._update_job(token, sync_until=time.time() + 1000)
    assert cc._publish_directory(new_token, new_signature, [{"name": "Right"}])
    assert cc._read()["people"] == [{"name": "Right"}]

@pytest.mark.parametrize('failure,match', [('paging', 'постраничную'), ('repeat', 'повторил страницу'),
    ('referral', 'другой сервер'), ('socket', 'не ответил')])
def test_incomplete_bulk_responses_are_not_published(db, monkeypatch, failure, match):
    cc._update(people=[{"name":"Previous"}], directory_signature=cc.directory_signature(), synced_at=store.utc_now())
    class Fake:
        entries, response = [], []
        closed = False
        def search(self, **kwargs):
            if failure == 'socket':
                raise TimeoutError('test socket timeout')
            self.result = {'description':'success','controls':{'1.2.840.113556.1.4.319':{'value':{'cookie':b'next'}}}}
            if failure == 'paging':
                self.result['controls'] = {}
            if failure == 'referral':
                self.response = [{'type':'searchResRef','uri':['ldap://other.invalid']}]
        def unbind(self):
            self.closed = True
    conn = Fake()
    monkeypatch.setattr(ldap_auth, '_service_connection', lambda **kwargs:(conn, None))
    with pytest.raises(ldap_auth.LdapError, match=match):
        cc.sync_directory()
    assert cc._read()['people'] == [{'name':'Previous'}]
    assert conn.closed and not cc.snapshot()['syncing']


def test_upgrade_preserves_cache_schedule_and_context_edits_during_download(db):
    signature = cc.directory_signature()
    cc._update(people=[{'name':'Existing'}],directory_signature=signature,synced_at=store.utc_now(),
               sync_time='22:30',sync_ad=False,sync_error='Previous error',last_attempt=time.time())
    assert cc.snapshot()['use_ad'] is True
    assert cc.snapshot()['search_cache_ready']
    assert cc.snapshot()['sync_error'] == 'Previous error'
    token, _ = cc._claim_sync(False)
    cc.import_dictionary('terms.csv', 'Термин;Варианты\nMoM;момер\n'.encode(), 'admin')
    cc.save_settings(False, None, 'admin', use_ad=False)
    assert cc._publish_directory(token, signature, [{'name':'New'}])
    result = cc.snapshot()
    assert result['sync_time'] == '22:30' and result['sync_ad'] is False
    assert result['term_count'] == 1 and not result['use_ad'] and not result['enabled']
    assert result['sync_error'] is None and result['search_cache_ready']
