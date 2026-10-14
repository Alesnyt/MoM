from backend.ldap_auth import escape_filter, user_filter
from backend import auth, config, ldap_auth, store
import pytest


def test_escape_filter_metacharacters() -> None:
    assert escape_filter("a*b(c)\\d") == "a\\2ab\\28c\\29\\5cd"


def test_people_filter_escapes_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.ldap_auth import people_filter

    monkeypatch.setattr(config, "LDAP_EMAIL_ATTR", "mail")
    built = people_filter("a*b")
    assert "a\\2ab" in built
    assert "(cn=*a\\2ab*)" in built
    assert "(displayName=*a\\2ab*)" in built


def test_suggest_uses_local_users_without_ldap(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import people

    db = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    store.init_db()
    store.create_user("anna@example.com", auth.hash_password("secret-pass"), 5)
    store.create_user("boris@example.com", auth.hash_password("secret-pass"), 5)
    found = people.suggest("ann")
    assert found["ldap"] is False
    assert [item["name"] for item in found["people"]] == ["anna@example.com"]
    everyone = people.suggest("")
    assert {item["email"] for item in everyone["people"]} == {"anna@example.com", "boris@example.com"}


def test_user_filter_substitutes_escaped_login(monkeypatch) -> None:
    monkeypatch.setattr(config, "LDAP_USER_FILTER", "(mail={username})")
    assert user_filter("a*@example.com") == "(mail=a\\2a@example.com)"


def test_ldap_user_does_not_accept_local_password(tmp_path, monkeypatch) -> None:
    db = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    store.init_db()
    store.create_user("a@example.com", auth.ldap_password_placeholder(), 5, "ldap")
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    try:
        auth.authenticate_platform_user("a@example.com", "secret")
        raised = False
    except Exception as exc:
        raised = True
        assert "выключен" in str(exc).lower() or "LDAP" in str(exc)
    assert raised


def test_directory_down_is_not_a_wrong_password(monkeypatch) -> None:
    _enable_ldap(monkeypatch)

    def down():
        raise TimeoutError("timed out")

    monkeypatch.setattr("backend.ldap_auth._service_connection", down)
    with pytest.raises(ldap_auth.LdapError, match="недоступен"):
        ldap_auth.authenticate("a@example.com", "secret")


def test_wrong_directory_password(monkeypatch) -> None:
    _enable_ldap(monkeypatch)
    monkeypatch.setattr("backend.ldap_auth._service_connection", lambda: (_Conn(), object()))
    monkeypatch.setattr(
        "backend.ldap_auth._search",
        lambda _connection, _username: [("cn=a,dc=example,dc=com", "a@example.com")],
    )
    monkeypatch.setattr("backend.ldap_auth._bind_as", lambda _server, _dn, _password: False)
    assert ldap_auth.authenticate("a@example.com", "wrong") is False


def _enable_ldap(monkeypatch) -> None:
    monkeypatch.setattr(config, "LDAP_ENABLED", True)
    monkeypatch.setattr(config, "LDAP_URL", "ldaps://ldap.example.com")
    monkeypatch.setattr(config, "LDAP_BASE_DN", "dc=example,dc=com")
    monkeypatch.setattr(config, "LDAP_USER_FILTER", "(mail={username})")


class _Conn:
    def unbind(self) -> None:
        return None


def test_local_user_still_uses_password(tmp_path, monkeypatch) -> None:
    db = tmp_path / "mom.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    store.init_db()
    store.create_user("a@example.com", auth.hash_password("secret-pass"), 5, "local")
    assert auth.authenticate_platform_user("a@example.com", "secret-pass")
    assert auth.authenticate_platform_user("a@example.com", "wrong") is None


def test_admin_directory_suggests_catalog_mail(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from fastapi.testclient import TestClient

    import backend.main as main
    from backend import keys, worker

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", tmp_path / "whisper")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    monkeypatch.setattr(config, "migrate_token_plan_asr", lambda: None)
    monkeypatch.setattr(keys, "reset_status", lambda message="": None)

    async def _idle(stop) -> None:
        await stop.wait()

    monkeypatch.setattr(worker, "run_worker", _idle)
    _enable_ldap(monkeypatch)
    monkeypatch.setattr(
        "backend.ldap_auth.search_people",
        lambda query, limit=20: [
            {"name": "Анна", "email": "anna@example.com", "source": "ldap"},
            {"name": "Без почты", "email": "", "source": "ldap"},
        ],
    )

    with TestClient(main.app) as client:
        assert client.get("/api/admin/directory", params={"q": "ан"}).status_code == 401
        token = "admin-directory"
        store.put_session(token, "admin", username="boss", expires=time.time() + 3600)
        client.cookies.set(auth.COOKIE_NAME, token)
        short = client.get("/api/admin/directory", params={"q": "а"})
        assert short.status_code == 200
        assert short.json()["people"] == []
        found = client.get("/api/admin/directory", params={"q": "ан"})
        assert found.status_code == 200, found.text
        assert found.json() == {
            "people": [{"name": "Анна", "email": "anna@example.com"}],
            "ldap": True,
        }
