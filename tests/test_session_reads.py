import asyncio
import sqlite3
from contextlib import closing

import pytest

from backend import auth, config, retention, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    for name in ('UPLOAD_DIR', 'AUDIO_DIR', 'WHISPER_DIR'):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'mom.db')
    store.init_db()
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    store.put_session('valid', 'user', user_id='u', email='u@example.com', expires=2000)
    store.put_session('expired', 'admin', username='boss', expires=1000)


def test_session_lookup_reads_while_another_writer_holds_database(db, monkeypatch):
    real_connect = sqlite3.connect
    statements = []
    def connect(*args, **kwargs):
        conn = real_connect(*args, **{**kwargs, 'timeout': .05})
        conn.set_trace_callback(statements.append)
        # Authentication must also work on a connection forbidding all writes.
        conn.execute('PRAGMA query_only=ON')
        return conn
    with closing(real_connect(store.DB_PATH)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE sessions SET username='pending' WHERE kind='admin'")
        monkeypatch.setattr(sqlite3, 'connect', connect)
        monkeypatch.setattr(store, 'ensure_dirs', lambda: pytest.fail('filesystem work during lookup'))
        monkeypatch.setattr(store, 'restrict_path', lambda *a: pytest.fail('chmod during lookup'))
        assert auth.get_user_session('valid')['user_id'] == 'u'
        assert auth.get_session('expired') is None
        assert store.get_session_row('unknown') is None
        writer.rollback()
    assert not any(sql.startswith(('DELETE', 'INSERT', 'UPDATE', 'BEGIN')) for sql in statements)
    assert not any('journal_mode' in sql for sql in statements)


def test_expiry_and_user_revocation(db):
    assert auth.get_session('valid') is None  # User cookie cannot become an admin cookie.
    assert auth.get_user_session('expired') is None
    assert store.get_session_row('expired') is None
    assert store.purge_expired_sessions() == 1
    assert store.get_session_row('valid')
    store.drop_sessions_for_user('u')
    assert store.get_session_row('valid') is None


@pytest.mark.parametrize('kind', ['admin', 'user'])
def test_stored_hash_cannot_authenticate_or_revoke_session(db, kind):
    if kind == 'admin':
        raw = auth.create_session('boss')
        lookup, logout = auth.get_session, auth.drop_session
        other_lookup = auth.get_user_session
        expected = {'username': 'boss'}
    else:
        user = store.create_user('u@example.com', 'hash')
        raw = auth.create_user_session(user['id'], user['email'], 0)
        lookup, logout = auth.get_user_session, auth.drop_user_session
        other_lookup = auth.get_session
        expected = {'user_id': user['id'], 'email': user['email']}
    with store.connect() as conn:
        stored = conn.execute('SELECT token FROM sessions WHERE token = ?',
                              (store.session_id(raw),)).fetchone()['token']
    assert stored != raw
    assert lookup(raw) == expected
    assert other_lookup(raw) is None
    assert lookup(stored) is None
    assert other_lookup(stored) is None
    logout(stored)
    assert lookup(raw) == expected
    logout(raw)
    assert lookup(raw) is None


@pytest.mark.parametrize('kind', ['admin', 'user'])
def test_legacy_plaintext_session_requires_new_login(db, kind, monkeypatch):
    with store.connect() as conn:
        conn.execute("INSERT INTO sessions(token,kind,username,user_id,email,expires) "
                     "VALUES ('legacy',?,'boss','u','u@example.com',2000)", (kind,))
    assert auth.get_session('legacy') is None
    assert auth.get_user_session('legacy') is None
    # Restarting the application must not restore acceptance of plaintext tokens.
    store.init_db()
    assert store.get_session_row('legacy') is None
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 3
    monkeypatch.setattr(store.time, 'time', lambda: 2000)
    assert store.purge_expired_sessions() == 3


def test_hashed_session_survives_restart_and_expires_at_deadline(db, monkeypatch):
    store.init_db()
    assert auth.get_user_session('valid') == {'user_id': 'u', 'email': 'u@example.com'}
    monkeypatch.setattr(store.time, 'time', lambda: 2000)
    assert auth.get_user_session('valid') is None


def test_session_cleanup_runs_off_event_loop_and_stops(db, monkeypatch):
    import threading
    event_thread = threading.get_ident()
    original = store.purge_expired_sessions
    calls = []
    def purge():
        calls.append(threading.get_ident())
        return original()
    monkeypatch.setattr(store, 'purge_expired_sessions', purge)
    async def pause(stop, delay):
        assert delay == 60
        stop.set()
    monkeypatch.setattr(retention.background, 'pause', pause)
    asyncio.run(retention.run_session_cleanup(asyncio.Event()))
    assert len(calls) == 1 and calls[0] != event_thread
    assert store.purge_expired_sessions() == 0



@pytest.mark.parametrize("change", ["password", "mode", "delete"])
def test_session_issuance_requires_authenticated_credential_version(db, change):
    profile = store.create_user("check@example.com", "previous")
    user = store.get_user_auth(profile["email"])
    def issue():
        return auth.create_user_session(user["id"], user["email"], user["auth_version"])
    existing = issue()
    if change == "delete":
        store.delete_user(user["id"])
    elif change == "password":
        store.update_user(user["id"], password_hash="replacement")
    else:
        store.update_user(user["id"], auth_mode="ldap")
    assert auth.get_user_session(existing) is None
    with pytest.raises(store.CredentialsChanged):
        issue()
    if change != "delete":
        current = store.get_user_auth(profile["email"])
        fresh = auth.create_user_session(current["id"], current["email"], current["auth_version"])
        assert auth.get_user_session(fresh)["user_id"] == user["id"]
