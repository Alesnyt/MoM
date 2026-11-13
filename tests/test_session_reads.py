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


def test_expiry_legacy_tokens_and_revocation(db):
    with store.connect() as conn:
        conn.execute("INSERT INTO sessions(token,kind,username,expires) VALUES ('legacy','admin','boss',2000)")
    assert auth.get_session('legacy')['username'] == 'boss'
    assert auth.get_session('valid') is None  # User cookie cannot become an admin cookie.
    assert auth.get_user_session('expired') is None
    assert store.get_session_row('expired') is None
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 3
    assert store.purge_expired_sessions() == 1
    assert store.get_session_row('valid')
    assert store.get_session_row('legacy')
    store.drop_session_token('legacy')
    store.drop_sessions_for_user('u')
    assert store.get_session_row('legacy') is None
    assert store.get_session_row('valid') is None


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
