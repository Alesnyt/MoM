import asyncio

from fastapi.testclient import TestClient
import pytest

from backend import config, keys, main


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch):
    monkeypatch.setitem(main.app.dependency_overrides, main.require_admin, lambda: {"username": "admin"})
    monkeypatch.setattr(main, "_health", lambda **_: {"openai": keys.snapshot()})
    monkeypatch.setattr(keys, "_status", dict(keys._status))
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-" + "x" * 20)


def test_browser_disconnect_stops_the_model_check(monkeypatch):
    async def exercise():
        checking = asyncio.Event()
        cancelled = asyncio.Event()
        incoming = asyncio.Queue()
        sent = []

        async def verify(_key):
            checking.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def send(message):
            sent.append(message)

        monkeypatch.setattr(keys, "verify_key", verify)
        path = "/api/settings/verify"
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode(), "headers": [], "query_string": b"",
        }
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        request = asyncio.create_task(main.app(scope, incoming.get, send))
        try:
            await asyncio.wait_for(checking.wait(), timeout=2)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(request, timeout=2)
            assert cancelled.is_set()
            assert sent[0]["status"] == 499
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())


def test_key_can_be_saved_before_its_cancellable_check(monkeypatch):
    calls = []

    async def verify(key):
        calls.append(key or config.get_api_key())

    monkeypatch.setattr(keys, "verify_key", verify)
    monkeypatch.setattr(config, "set_openai_api_key", lambda key: setattr(config, "OPENAI_API_KEY", key))
    monkeypatch.setattr(main.store, "record_audit", lambda *args: None)
    key = "new-key-" + "y" * 20
    client = TestClient(main.app)
    response = client.post("/api/settings?verify=false", json={"openai_api_key": key})
    assert response.status_code == 200
    status = response.json()["openai"]
    assert status["configured"] and not status["connected"] and status["checked_at"] is None
    assert calls == []

    assert client.post("/api/settings/verify").status_code == 200
    assert calls == [key]
    assert client.post("/api/settings", json={"openai_api_key": key}).status_code == 200
    assert calls == [key, key]
