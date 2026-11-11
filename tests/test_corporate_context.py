import io
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from backend import auth, config, corporate_context as cc, directory_scope, ldap_auth, main, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(config, "ldap_configured", lambda: True)
    monkeypatch.setattr(config, "get_ldap_url", lambda: "ldaps://example.com")
    monkeypatch.setattr(config, "get_ldap_base_dn", lambda: "DC=example,DC=com")
    monkeypatch.setattr(config, "get_ldap_email_attr", lambda: "mail")
    store.init_db()


def dictionary():
    return "Термин;Варианты;Описание;Подразделение\nTrueConf;труконф|тру конф;Видеоконференции;ИТ\nSLA;эс эл эй;Уровень обслуживания;\n".encode()


def test_csv_excel_replace_atomically_and_reject_bad_files(db):
    cc.import_dictionary("terms.csv", dictionary(), "admin")
    before = cc.snapshot()
    assert before["term_count"] == 2
    for filename, content in [("bad.xls", b"x"), ("bad.xlsx", b"x"), ("bad.csv", b"term\n"),
                              ("bad.csv", b"term\na\na"), ("large.csv", b"x" * (cc.MAX_FILE_BYTES + 1))]:
        with pytest.raises(ValueError):
            cc.import_dictionary(filename, content, "admin")
        assert cc.snapshot()["revision"] == before["revision"]
    book = Workbook()
    book.active.append(["Термин", "Варианты", "Описание", "Подразделение"])
    book.active.append(["MoM", "момер", "Протоколы", "ИТ"])
    output = io.BytesIO()
    book.save(output)
    cc.import_dictionary("terms.xlsx", output.getvalue(), "admin")
    assert [entry["term"] for entry in cc.snapshot()["terms"]] == ["MoM"]
    book.active["A2"] = "=1+1"
    output = io.BytesIO()
    book.save(output)
    with pytest.raises(ValueError, match="формулы"):
        cc.import_dictionary("terms.xlsx", output.getvalue(), "admin")
    assert cc.snapshot()["terms"][0]["term"] == "MoM"


def test_sync_preserves_complete_snapshot_on_failure_and_respects_scope_changes(db, monkeypatch):
    people = [{"name": "Анна Иванова", "email": "anna@example.com", "department": "ИТ", "job_title": "Архитектор"}]
    monkeypatch.setattr(ldap_auth, "sync_people", lambda **kwargs: people)
    assert cc.sync_directory()["people_count"] == 1
    def unavailable(**kwargs):
        raise ldap_auth.LdapError("Каталог недоступен")
    monkeypatch.setattr(ldap_auth, "sync_people", unavailable)
    with pytest.raises(ldap_auth.LdapError):
        cc.sync_directory()
    assert cc.snapshot()["people_count"] == 1
    assert cc.snapshot()["sync_error"] == "Каталог недоступен"
    directory_scope.save("selected", [])
    assert cc.snapshot()["people_count"] == 0
    assert cc.for_meeting({}).entries == []


def test_context_selection_normalization_and_originals(db):
    cc.import_dictionary("terms.csv", dictionary(), "admin")
    context = cc.for_meeting({})
    assert context.select("Встреча в тру конф", limit=1)[0]["term"] == "TrueConf"
    document = {"segments": [{"speaker": "S1", "start": 1, "end": 2,
                             "text": "Откройте тру конф. Проверим эс эл эй."}]}
    result = context.apply(document)
    assert result["segments"][0]["text"] == "Откройте TrueConf. Проверим SLA."
    assert result["segments"][0]["context_original_text"] == document["segments"][0]["text"]
    assert result["segments"][0]["speaker"] == "S1"
    assert result["segments"][0]["start"] == 1
    assert "context_original_text" not in document["segments"][0]
    assert context.apply(result)["segments"] == result["segments"]
    assert len(context.hotwords().encode()) <= 500
    cc.save_settings(False, True, "admin")
    assert cc.for_meeting({}).hotwords() == ""
    assert cc.for_meeting({}).apply(document)["segments"] == document["segments"]


def test_ambiguous_aliases_and_word_boundaries_are_preserved():
    terms = [{"term": "Модуль А", "aliases": ["альфа"], "department": "ИТ", "description": ""},
             {"term": "Модуль Б", "aliases": ["альфа"], "department": "Продажи", "description": ""},
             {"term": "TrueConf", "aliases": ["труконф"], "department": "", "description": ""}]
    context = cc.Context(terms, [], 1)
    text = "Альфа, альфавит и труконференция."
    assert context.apply({"segments": [{"text": text}]})["segments"][0]["text"] == text


def test_department_and_known_terms_rank_above_unrelated_entries():
    terms = [{"term": f"Термин {i}", "aliases": [], "department": "", "description": ""} for i in range(100)]
    terms.append({"term": "Пилот", "aliases": ["пилотный проект"], "department": "ИТ", "description": ""})
    people = [{"name": "Анна Иванова", "email": "anna@example.com", "department": "ИТ", "job_title": "Архитектор"}]
    context = cc.Context(terms, people, 1, "anna@example.com")
    assert context.select("пилотный проект", 1)[0]["term"] == "Пилот"
    assert context.select("Термин 98", 1)[0]["term"] == "Термин 98"


class Entry:
    entry_dn = "CN=Anna,OU=IT,DC=example,DC=com"
    def __contains__(self, key):
        return key in {"displayName", "mail", "department", "title"}
    def __getitem__(self, key):
        return SimpleNamespace(value={"displayName": "Анна Иванова", "mail": "anna@example.com",
                                      "department": "ИТ", "title": "Архитектор"}.get(key, ""))


def test_ldap_sync_pages_deduplicates_and_rejects_truncation(db, monkeypatch):
    directory_scope.save("selected", ["OU=IT,DC=example,DC=com"])
    class Connection:
        def __init__(self):
            self.calls, self.closed, self.fail = [], False, False
        def search(self, **kwargs):
            self.calls.append(kwargs)
            self.entries = [Entry()]
            self.result = {"description": "sizeLimitExceeded" if self.fail else "success",
                "controls": {"1.2.840.113556.1.4.319": {"value": {"cookie": b"next" if len(self.calls) == 1 else b""}}}}
        def unbind(self):
            self.closed = True
    connection = Connection()
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda **kwargs: (connection, None))
    assert ldap_auth.sync_people() == [{"name": "Анна Иванова", "email": "anna@example.com",
                                       "department": "ИТ", "job_title": "Архитектор", "surname": ""}]
    assert len(connection.calls) == 2
    assert connection.calls[1]["paged_cookie"] == b"next"
    assert all(call["search_base"] == "OU=IT,DC=example,DC=com" for call in connection.calls)
    assert connection.closed
    connection.fail = True
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth.sync_people()


def test_admin_routes_require_admin_and_failed_import_keeps_dictionary(db):
    client = TestClient(main.app, raise_server_exceptions=True)
    for route in ["/api/admin/context", "/api/admin/context/sync", "/api/admin/context/import"]:
        response = client.get(route) if route.endswith("context") else client.post(route)
        assert response.status_code == 401
    store.put_session("admin", "admin", username="admin", expires=time.time() + 3600)
    client.cookies.set(auth.COOKIE_NAME, "admin")
    response = client.post("/api/admin/context/import", files={"file": ("terms.csv", dictionary())})
    assert response.status_code == 200, response.text
    assert response.json()["term_count"] == 2
    response = client.post("/api/admin/context/import", files={"file": ("bad.csv", b"bad")})
    assert response.status_code == 400
    assert client.get("/api/admin/context?q=true").json()["matched_terms"] == 1
    assert client.post("/api/admin/context/settings", json={"enabled": False, "sync_ad": False}).status_code == 200
    for invalid in ["25:00", "09:30\n", "0١:30"]:
        assert client.post("/api/admin/context/settings", json={"enabled": True, "sync_ad": True, "sync_time": invalid}).status_code == 422
    assert client.post("/api/admin/context/settings", json={"enabled": True, "sync_ad": True, "sync_time": "23:45"}).json()["sync_time"] == "23:45"
    assert client.post("/api/admin/context/settings", json={"enabled": False, "sync_ad": True}).json()["sync_time"] == "23:45"


def test_directory_lease_coordinates_workers_and_discards_expired_owner(db, monkeypatch):
    monkeypatch.setattr(cc.time, "time", lambda: datetime.fromisoformat("2026-11-11T12:00:00+03:00").timestamp())
    first, signature = cc._claim_sync(False)
    assert cc.snapshot()["syncing"]
    with pytest.raises(ValueError, match="уже выполняется"):
        cc._claim_sync(False)
    assert cc._claim_sync(True) is None
    cc._update_job(first, sync_until=time.time() - 1, last_attempt=0)
    second, _ = cc._claim_sync(True)
    assert first != second
    assert cc._publish_directory(first, signature, [{"name": "Устаревший"}]) is False
    assert cc._update_job(first, sync_until=0) is False
    assert cc.snapshot()["syncing"]
    cc._update_job(second, sync_until=0, sync_id=None)
    # Daily sync is throttled across workers; a changed scope is immediately eligible.
    assert cc._claim_sync(True) is None
    directory_scope.save("selected", [])
    assert cc._claim_sync(True) is not None


def test_sync_calendar_time_manual_run_and_restart(db, monkeypatch):
    clock = [datetime.fromisoformat("2026-11-11T06:00:00+03:00").timestamp()]
    monkeypatch.setattr(cc.time, "time", lambda: clock[0])
    monkeypatch.setattr(ldap_auth, "sync_people", lambda **kwargs: [])
    cc.save_settings(True, True, "admin", sync_time="09:30")
    assert cc.snapshot()["next_sync_at"] == "2026-11-11T06:30:00+00:00"
    cc.sync_directory()  # Manual run does not move the configured daily time.
    assert cc.snapshot()["next_sync_at"] == "2026-11-11T06:30:00+00:00"
    clock[0] = datetime.fromisoformat("2026-11-11T09:29:59+03:00").timestamp()
    assert cc.sync_directory(scheduled=True) == {}
    clock[0] += 1
    assert cc.sync_directory(scheduled=True)["sync_time"] == "09:30"
    assert cc.sync_directory(scheduled=True) == {}
    store.init_db()  # No scheduling state relies on the current worker process.
    assert cc.snapshot()["next_sync_at"] == "2026-11-12T06:30:00+00:00"
    clock[0] = datetime.fromisoformat("2026-11-12T10:00:00+03:00").timestamp()
    assert cc.sync_directory(scheduled=True)["sync_time"] == "09:30"  # Catch up once after downtime.
    assert cc.snapshot()["next_sync_at"] == "2026-11-13T06:30:00+00:00"


def test_schedule_change_midnight_disabled_mode_and_retry(db, monkeypatch):
    clock = [datetime.fromisoformat("2026-11-11T18:00:00+03:00").timestamp()]
    monkeypatch.setattr(cc.time, "time", lambda: clock[0])
    cc.save_settings(True, True, "admin", sync_time="00:15")
    assert cc.snapshot()["next_sync_at"] == "2026-11-11T21:15:00+00:00"
    assert cc.sync_directory(scheduled=True) == {}
    def unavailable(**kwargs):
        raise ldap_auth.LdapError("Каталог недоступен")
    monkeypatch.setattr(ldap_auth, "sync_people", unavailable)
    clock[0] = datetime.fromisoformat("2026-11-12T00:15:00+03:00").timestamp()
    with pytest.raises(ldap_auth.LdapError):
        cc.sync_directory(scheduled=True)
    assert cc.snapshot()["next_sync_at"] == "2026-11-11T21:30:00+00:00"
    assert cc.sync_directory(scheduled=True) == {}
    clock[0] += 900
    monkeypatch.setattr(ldap_auth, "sync_people", lambda **kwargs: [])
    assert cc.sync_directory(scheduled=True)["sync_error"] is None
    cc.save_settings(True, False, "admin")
    assert cc.snapshot()["sync_time"] == "00:15"
    assert cc.snapshot()["next_sync_at"] is None
    assert cc.sync_directory(scheduled=True) == {}
    cc.save_settings(True, True, "admin")
    assert cc.snapshot()["next_sync_at"] == "2026-11-12T21:15:00+00:00"
    for invalid in ["24:00", "1:30", "12:60", "09:30\n", ""]:
        with pytest.raises(ValueError):
            cc.save_settings(True, True, "admin", sync_time=invalid)


def test_people_lookup_reports_missing_cache_without_hiding_saved_users(db, monkeypatch):
    from backend import people
    # Previously selected cache may become unavailable after a scope change.
    store.set_app_setting("ldap_speaker_search", {"mode": "cache"})
    monkeypatch.setattr(store, "list_users", lambda: [{"email": "anna@example.com"}])
    def unavailable(_query):
        pytest.fail("Speaker lookup must never call LDAP")
    monkeypatch.setattr(ldap_auth, "search_people", unavailable)
    result = people.suggest("anna")
    assert result["ldap"] is True
    assert result["people"][0]["email"] == "anna@example.com"
    assert "AD ещё не обновлён" in result["error"]
    assert "Private" not in result["error"]


def test_cached_directory_partial_search_survives_outage_and_refreshes(db, monkeypatch):
    from backend import people
    roster = [{"name": "Анна Семёнова", "email": "anna@example.com", "job_title": "Архитектор"},
              {"name": "Семён Петров", "email": "petr@example.com"}]
    monkeypatch.setattr(ldap_auth, "search_people", lambda *args: pytest.fail("No LDAP in speaker lookup"))
    cc._update(people=roster, directory_signature=cc.directory_signature(), synced_at=store.utc_now(), sync_error="server offline")
    cc.save_search_mode("cache", "admin")
    monkeypatch.setattr(store, "list_users", lambda: [{"email": "anna@example.com"}])
    assert people.suggest("anna")["people"] == [dict(roster[0], source="ldap")]
    assert cc.search_people("семено АН")[0] == [roster[0]]
    assert len(cc.search_people("сем")[0]) == 2
    # Another process commits a new snapshot; the warm index must be invalidated.
    cc._update(people=[{"name": "Анна Соколова", "email": "anna@example.com"}])
    assert cc.search_people("сем")[0] == []
    assert cc.search_people("сокол")[0][0]["name"] == "Анна Соколова"
    directory_scope.save_sync_policy([], ["example.com"])
    assert cc.search_people("сокол")[0] == []
    assert cc.search_people("сокол")[1]


def test_speaker_search_defaults_to_live_and_can_switch_without_rebuilding_cache(db, monkeypatch):
    from backend import people
    cached = {"name": "Анна из кэша", "email": "anna@example.com"}
    live = {"name": "Анна из AD", "email": "anna@example.com"}
    calls = []
    def search(query, *, for_speakers=False):
        calls.append((query, for_speakers))
        return [live]
    monkeypatch.setattr(ldap_auth, "search_people", search)
    monkeypatch.setattr(ldap_auth, "sync_people", lambda **kwargs: [cached])
    cc.sync_directory()
    assert cc.snapshot()["search_mode"] == "live"  # Sync never implicitly enables cache.
    assert people.suggest("Анна")["people"][0]["name"] == live["name"]
    assert calls == [("Анна", True)]
    revision = cc.snapshot()["revision"]
    cc.save_search_mode("cache", "admin")
    store.init_db()
    result = people.suggest("Анна")
    assert result["search_mode"] == "cache" and result["people"][0]["name"] == cached["name"]
    assert len(calls) == 1 and cc.snapshot()["revision"] == revision
    cc.save_settings(False, False, "admin")
    assert cc.snapshot()["search_mode"] == "cache"  # Recognition and scheduling are independent.
    cc.save_search_mode("live", "admin")
    result = people.suggest("Анна")
    assert result["search_mode"] == "live" and result["people"][0]["name"] == live["name"]
    assert len(calls) == 2


def test_live_outage_does_not_silently_switch_to_cache_or_expose_diagnostics(db, monkeypatch):
    from backend import people
    monkeypatch.setattr(store, "list_users", lambda: [{"email": "anna@example.com"}])
    cc._update(people=[{"name": "Анна", "email": "anna@example.com"}],
               directory_signature=cc.directory_signature(), synced_at=store.utc_now())
    def unavailable(*args, **kwargs):
        raise ldap_auth.LdapError("Private server diagnostics")
    monkeypatch.setattr(ldap_auth, "search_people", unavailable)
    result = people.suggest("anna")
    assert result["search_mode"] == "live"
    assert result["people"] == [{"name": "anna@example.com", "email": "anna@example.com", "source": "mom"}]
    assert "AD временно недоступен" in result["error"] and "Private" not in result["error"]


def test_speaker_search_mode_api_admin_only_and_requires_current_successful_sync(db, monkeypatch):
    client = TestClient(main.app)
    route = "/api/admin/context/search"
    assert client.post(route, json={"mode": "cache"}).status_code == 401
    store.put_session("user", "user", user_id="u", email="user@example.com", expires=time.time() + 3600)
    client.cookies.set(auth.COOKIE_NAME, "user")
    assert client.post(route, json={"mode": "live"}).status_code == 401
    store.put_session("admin", "admin", username="admin", expires=time.time() + 3600)
    client.cookies.set(auth.COOKIE_NAME, "admin")
    assert client.post(route, json={"mode": "cache"}).status_code == 400
    assert cc.snapshot()["search_mode"] == "live"
    assert client.post(route, json={"mode": "other"}).status_code == 422
    monkeypatch.setattr(ldap_auth, "sync_people", lambda **kwargs: [{"name": "Анна"}])
    cc.sync_directory()
    response = client.post(route, json={"mode": "cache"})
    assert response.status_code == 200 and response.json()["search_cache_ready"]
    assert response.json()["search_mode"] == "cache"
    directory_scope.save_sync_policy([], ["example.com"])
    assert not cc.snapshot()["search_cache_ready"]
    assert client.post(route, json={"mode": "cache"}).status_code == 400
    assert client.post(route, json={"mode": "live"}).status_code == 200


@pytest.mark.parametrize("domains", [["*.ru"], ["user@company.ru"], ["https://company.ru"], ["x)(mail=*)"], ["a..ru"], ["-a.ru"]])
def test_directory_policy_rejects_invalid_domains_atomically(db, domains):
    before = directory_scope.sync_policy()
    with pytest.raises(ValueError):
        directory_scope.save_sync_policy([], domains)
    assert directory_scope.sync_policy() == before


def test_directory_policy_normalizes_and_rejects_unused_fields(db):
    assert directory_scope.save_sync_policy(["surname"], [" @EXAMPLE.com ", "example.com"]) == {
        "fields": ["surname"], "domains": ["example.com"]}
    with pytest.raises(ValueError):
        directory_scope.save_sync_policy(["password"], [])


def test_directory_policy_requests_only_selected_fields_and_enforces_domain_and_ou(db, monkeypatch):
    directory_scope.save("selected", ["OU=IT,DC=example,DC=com"])
    directory_scope.save_sync_policy(["department"], ["example.com"])
    class Contractor(Entry):
        def __getitem__(self, key):
            return SimpleNamespace(value="external@contractor.com") if key == "mail" else super().__getitem__(key)
    class Connection:
        entries = [Entry(), Contractor()]
        result = {"description": "success", "controls": {"1.2.840.113556.1.4.319": {"value": {"cookie": b""}}}}
        def search(self, **kwargs):
            assert kwargs["search_base"] == "OU=IT,DC=example,DC=com"
            assert set(kwargs["attributes"]) == {"mail", "cn", "displayName", "department"}
            assert "(mail=*@example.com)" in kwargs["search_filter"]
        def unbind(self):
            pass
    conn = Connection()
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda **kwargs: (conn, None))
    assert ldap_auth.sync_people() == [{"name": "Анна Иванова", "email": "anna@example.com", "department": "ИТ"}]
    outside = Entry()
    outside.entry_dn = "CN=Anna,OU=Contractors,DC=example,DC=com"
    conn.entries = [outside]
    with pytest.raises(ldap_auth.LdapError, match="вне выбранной области"):
        ldap_auth.sync_people()


def test_directory_policy_api_requires_admin_and_invalidates_old_snapshot(db):
    client = TestClient(main.app)
    route = "/api/admin/context/directory"
    payload = {"fields": ["job_title"], "domains": ["example.com"]}
    assert client.post(route, json=payload).status_code == 401
    store.put_session("admin", "admin", username="admin", expires=time.time() + 3600)
    client.cookies.set(auth.COOKIE_NAME, "admin")
    cc._update(people=[{"name": "Анна"}], directory_signature=cc.directory_signature())
    result = client.post(route, json=payload)
    assert result.status_code == 200
    assert result.json()["directory_policy"] == payload
    assert result.json()["people_count"] == 0
    assert not result.json()["directory_current"]
    assert client.post(route, json={"fields": ["bad"], "domains": []}).status_code == 400


def test_large_department_does_not_displace_dictionary_and_surnames_need_unique_matches():
    people = [{"name": f"Сотрудник {i}", "surname": f"Фамилия{i}", "email": f"{i}@example.com", "department": "ИТ"}
              for i in range(100)]
    people.extend([{"name": "Иван Петров", "surname": "Петров"}, {"name": "Петр Петров", "surname": "Петров"}])
    context = cc.Context(cc.parse_dictionary("terms.csv", dictionary()), people, 1, "1@example.com")
    assert {entry["term"] for entry in context.select()} == {"TrueConf", "SLA"}
    assert context.select("Фамилия45 проверит подключение", 1)[0]["term"] == "Сотрудник 45"
    assert all(entry["source"] == "dictionary" for entry in context.select("Петров проверит подключение"))
    assert context.select("Иван Петров проверит подключение", 1)[0]["term"] == "Иван Петров"
