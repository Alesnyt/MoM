import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import auth, config, directory_scope, ldap_auth, main, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", tmp_path / "whisper")
    monkeypatch.setattr(config, "LDAP_ENABLED", True)
    monkeypatch.setattr(config, "LDAP_URL", "ldaps://ad.example.com")
    monkeypatch.setattr(config, "LDAP_BASE_DN", "DC=example,DC=com")
    monkeypatch.setattr(config, "LDAP_EMAIL_ATTR", "mail")
    store.init_db()


class Entry:
    def __init__(self, dn, **attrs):
        self.entry_dn = dn
        self.attrs = attrs

    def __contains__(self, attr):
        return attr in self.attrs

    def __getitem__(self, attr):
        return SimpleNamespace(value=self.attrs[attr])


class Connection:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []
        self.entries = []
        self.result = {}
        self.closed = False

    def search(self, **kwargs):
        self.calls.append(kwargs)
        entries, description = self.replies.get(kwargs["search_base"], ([], "success"))
        self.entries = entries
        self.result = {"description": description}
        return description == "success"

    def unbind(self):
        self.closed = True


def test_default_and_persistent_empty_selection_fail_closed(db, monkeypatch):
    assert directory_scope.search_bases() == ["DC=example,DC=com"]
    directory_scope.save("selected", [])
    store.init_db()
    assert directory_scope.search_bases() == []
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: pytest.fail("Empty allowlist must not query AD"))
    assert ldap_auth.search_people("Анна") == []


def test_dn_validation_dedup_and_nested_scopes(db):
    parent = "OU=Employees,DC=example,DC=com"
    child = "OU=IT," + parent
    other = r"OU=Staff\, East,DC=example,DC=com"
    saved = directory_scope.save("selected", [parent, child, parent.lower(), other])
    assert saved["bases"] == [parent, child, other]
    assert directory_scope.search_bases() == [parent, other]
    for invalid in ["", "Employees", "DC=example\nLDAP_ENABLED=0", "OU=broken,", "OU=bad\x00,DC=com"]:
        with pytest.raises(ValueError):
            directory_scope.save("selected", [invalid])
    # Failed validation must not discard the previous valid selection.
    assert directory_scope.snapshot()["bases"] == [parent, child, other]


def test_server_change_does_not_broaden_search(db, monkeypatch):
    directory_scope.save("selected", ["OU=Employees,DC=example,DC=com"])
    monkeypatch.setattr(config, "LDAP_URL", "ldaps://other.example.com")
    assert directory_scope.snapshot()["connection_matches"] is False
    assert directory_scope.search_bases() == []
    directory_scope.save("selected", ["OU=Staff,DC=other,DC=com"])
    assert directory_scope.search_bases() == ["OU=Staff,DC=other,DC=com"]


def test_people_search_hits_only_selected_subtrees_and_deduplicates(db, monkeypatch):
    a, b = "OU=IT,DC=example,DC=com", "OU=HR,DC=example,DC=com"
    directory_scope.save("selected", [a, b])
    anna = Entry("CN=Anna," + a, displayName="Анна", mail="anna@example.com")
    boris = Entry("CN=Boris," + b, displayName="Борис", mail="boris@example.com")
    conn = Connection({a: ([anna], "success"), b: ([anna, boris], "success")})
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: (conn, None))
    result = ldap_auth.search_people("a*(b)", limit=2)
    assert [p["email"] for p in result] == ["anna@example.com", "boris@example.com"]
    assert [c["search_base"] for c in conn.calls] == [a, b]
    assert all(c["search_scope"] == "SUBTREE" and c["dereference_aliases"] == "NEVER" for c in conn.calls)
    assert all("a\\2a\\28b\\29" in c["search_filter"] for c in conn.calls)
    assert all("(objectClass=person)" in c["search_filter"] and "(!(objectClass=computer))" in c["search_filter"] for c in conn.calls)
    assert conn.closed


def test_bad_scope_never_falls_back_to_root(db, monkeypatch):
    dn = "OU=Missing,DC=example,DC=com"
    directory_scope.save("selected", [dn])
    conn = Connection({dn: ([], "noSuchObject")})
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: (conn, None))
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth.search_people("Анна")
    assert [c["search_base"] for c in conn.calls] == [dn]
    assert conn.closed


def test_login_keeps_its_own_base_and_filter(db, monkeypatch):
    directory_scope.save("selected", [])
    monkeypatch.setattr(config, "LDAP_USER_FILTER", "(mail={username})")
    conn = Connection()
    ldap_auth._search(conn, "anna@example.com")
    assert conn.calls[0]["search_base"] == "DC=example,DC=com"
    assert conn.calls[0]["search_filter"] == "(mail=anna@example.com)"


def test_browse_root_uses_metadata_and_ignores_non_domain_context(db, monkeypatch):
    root, other, schema = "DC=example,DC=com", "DC=other,DC=com", "CN=Schema,CN=Configuration,DC=example,DC=com"
    conn = Connection({"": ([Entry("", namingContexts=[root, other, schema], defaultNamingContext=root)], "success"),
                       other: ([Entry(other, name="other")], "success")})
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: (conn, None))
    result = ldap_auth.browse_scopes()
    assert [item["dn"] for item in result["items"]] == [root, other]
    assert all(c["search_scope"] == "BASE" for c in conn.calls)
    assert all("mail" not in c["attributes"] for c in conn.calls)
    assert conn.closed


def test_browse_children_uses_one_level_and_returns_page_cookie(db, monkeypatch):
    import base64
    dn = "OU=Employees,DC=example,DC=com"
    class Paged(Connection):
        def search(self, **kwargs):
            ok = super().search(**kwargs)
            self.result["controls"] = {"1.2.840.113556.1.4.319": {"value": {"cookie": b"next"}}}
            return ok
    conn = Paged({dn: ([Entry("OU=IT," + dn, name="IT")], "success")})
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: (conn, None))
    result = ldap_auth.browse_scopes(dn, base64.b64encode(b"previous").decode())
    assert result["next_cursor"] == base64.b64encode(b"next").decode()
    assert conn.calls[0]["search_scope"] == "LEVEL"
    assert conn.calls[0]["paged_cookie"] == b"previous"
    assert conn.calls[0]["paged_size"] == 100
    assert result["items"][0]["dn"] == "OU=IT," + dn


def test_scope_admin_api_and_persistence(db, monkeypatch):
    client = TestClient(main.app)
    path = "/api/settings/ldap/scopes"
    payload = {"mode": "selected", "bases": ["OU=IT,DC=example,DC=com"]}
    assert client.get(path).status_code == 401
    assert client.post(path, json=payload).status_code == 401
    assert client.get("/api/settings/ldap/structure").status_code == 401
    store.put_session("normal", "user", user_id="u", email="a@example.com", expires=time.time() + 60)
    client.cookies.set(auth.COOKIE_NAME, "normal")
    assert client.post(path, json=payload).status_code == 401
    client.cookies.set(auth.COOKIE_NAME, auth.create_session("admin"))
    assert client.post(path, json=payload).status_code == 200
    store.init_db()
    assert client.get(path).json()["bases"] == payload["bases"]
    assert client.post(path, json={"mode": "selected", "bases": ["bad"]}).status_code == 400
    assert client.post(path, json={"mode": "typo"}).status_code == 422
    assert client.post(path, json={"mode": "selected", "bases": payload["bases"] * 33}).status_code == 422
    monkeypatch.setattr(ldap_auth, "browse_scopes", lambda *args: {"items": [], "next_cursor": None})
    assert client.get("/api/settings/ldap/structure").status_code == 200
    assert client.get("/api/settings/ldap/structure", params={"base_dn": "x" * 2001}).status_code == 422


def test_real_ldap_filter_and_subtree_exclude_contractors_and_computers(db, monkeypatch):
    from ldap3 import Connection as LdapConnection, Server, MOCK_SYNC

    root = "DC=example,DC=com"
    employees = "OU=Employees," + root
    contractors = "OU=Contractors," + root
    conn = LdapConnection(Server("mock-ad"), client_strategy=MOCK_SYNC)
    conn.strategy.add_entry(root, {"objectClass": ["domain"], "dc": "example"})
    for base in (employees, contractors):
        conn.strategy.add_entry(base, {"objectClass": ["organizationalUnit"], "ou": base.split(",")[0][3:]})
    for base, cn, mail, classes in [
        (employees, "Anna Employee", "staff@example.com", ["person", "organizationalPerson", "user"]),
        (contractors, "Anna Contractor", "vendor@example.com", ["person", "organizationalPerson", "user"]),
        (employees, "Anna PC", "pc@example.com", ["person", "organizationalPerson", "user", "computer"]),
    ]:
        conn.strategy.add_entry("CN=" + cn + "," + base,
            {"objectClass": classes, "cn": cn, "displayName": cn, "mail": mail})
    assert conn.bind()
    directory_scope.save("selected", [employees])
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: (conn, None))
    assert [p["email"] for p in ldap_auth.search_people("Anna")] == ["staff@example.com"]


def test_invalid_attribute_cannot_inject_people_filter(monkeypatch):
    monkeypatch.setattr(config, "LDAP_EMAIL_ATTR", "mail)(objectClass=*)")
    with pytest.raises(ldap_auth.LdapError, match="атрибута"):
        ldap_auth.people_filter("Анна")
