import io
import time
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
    monkeypatch.setattr(ldap_auth, "sync_people", lambda: people)
    assert cc.sync_directory()["people_count"] == 1
    def unavailable():
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
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: (connection, None))
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


def test_directory_lease_coordinates_workers_and_discards_expired_owner(db, monkeypatch):
    first, signature = cc._claim_sync(False)
    assert cc.snapshot()["syncing"]
    with pytest.raises(ValueError, match="уже выполняется"):
        cc._claim_sync(False)
    assert cc._claim_sync(True) is None
    cc._update(sync_until=time.time() - 1, last_attempt=0)
    second, _ = cc._claim_sync(True)
    assert first != second
    assert cc._update(_expected_sync_id=first, people=[{"name": "Устаревший"}]) is None
    assert cc._update(_expected_sync_id=first, sync_until=0) is None
    assert cc.snapshot()["syncing"]
    cc._update(_expected_sync_id=second, sync_until=0, sync_id=None)
    # Daily sync is throttled across workers; a changed scope is immediately eligible.
    assert cc._claim_sync(True) is None
    directory_scope.save("selected", [])
    assert cc._claim_sync(True) is not None


def test_large_department_does_not_displace_dictionary_and_surnames_need_unique_matches():
    people = [{"name": f"Сотрудник {i}", "surname": f"Фамилия{i}", "email": f"{i}@example.com", "department": "ИТ"}
              for i in range(100)]
    people.extend([{"name": "Иван Петров", "surname": "Петров"}, {"name": "Петр Петров", "surname": "Петров"}])
    context = cc.Context(cc.parse_dictionary("terms.csv", dictionary()), people, 1, "1@example.com")
    assert {entry["term"] for entry in context.select()} == {"TrueConf", "SLA"}
    assert context.select("Фамилия45 проверит подключение", 1)[0]["term"] == "Сотрудник 45"
    assert all(entry["source"] == "dictionary" for entry in context.select("Петров проверит подключение"))
    assert context.select("Иван Петров проверит подключение", 1)[0]["term"] == "Иван Петров"
