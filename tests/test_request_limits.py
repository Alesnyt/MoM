"""Oversized and rate-limited requests stop before JSON parsing or auth work."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import auth, main, request_limits, store


def request(path, chunks, *, headers=(), method="POST"):
    messages = []
    reads = []

    async def run():
        iterator = iter(chunks)

        async def receive():
            chunk = next(iterator, None)
            if chunk is None:
                await asyncio.Event().wait()
            reads.append(chunk)
            return {"type": "http.request", "body": chunk, "more_body": True}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": b"",
                 "client": ("testclient", 1234), "server": ("testserver", 80),
                 "headers": [(b"host", b"testserver"), *headers]}
        await asyncio.wait_for(main.app(scope, receive, send), timeout=2)

    asyncio.run(run())
    return messages, reads


@pytest.fixture(autouse=True)
def allowed_rate(monkeypatch):
    monkeypatch.setattr(auth, "rate_allow", lambda key: True)


@pytest.mark.parametrize("path", list(request_limits.AUTH_ACTIONS))
def test_declared_oversize_auth_does_not_read_body(path):
    messages, reads = request(path, [], headers=[(b"content-length", b"2000000")])
    assert messages[0]["status"] == 413
    assert not reads
    assert (b"x-content-type-options", b"nosniff") in messages[0]["headers"]


@pytest.mark.parametrize("declared", [None, b"1"])
@pytest.mark.parametrize("content_type", [b"application/json", b"text/plain", b"multipart/form-data"])
def test_stream_limit_counts_actual_bytes_before_json_parser(declared, content_type):
    headers = [(b"content-type", content_type)]
    if declared:
        headers.append((b"content-length", declared))
    first = b"x" * request_limits.AUTH_BODY_BYTES
    messages, reads = request("/api/session/login", [first, b"x", b"never read"], headers=headers)
    assert messages[0]["status"] == 413
    assert reads == [first, b"x"]


def test_general_api_limit_also_stops_chunked_body():
    chunk = b"x" * request_limits.API_BODY_BYTES
    messages, reads = request("/api/settings/prompts", [chunk, b"x", b"never read"])
    assert messages[0]["status"] == 413
    assert len(reads) == 2


@pytest.mark.parametrize("path", list(request_limits.AUTH_ACTIONS))
def test_rate_limit_precedes_body_read_and_model_validation(monkeypatch, path):
    calls = []
    monkeypatch.setattr(auth, "rate_allow", lambda key: calls.append(key) or False)
    messages, reads = request(path, [])
    assert messages[0]["status"] == 429
    assert (b"retry-after", b"60") in messages[0]["headers"]
    assert not reads
    assert calls == [request_limits.AUTH_ACTIONS[path] + ":testclient"]


def test_valid_body_at_limit_is_replayed_and_rate_counted_once(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "rate_allow", lambda key: calls.append(key) or True)
    monkeypatch.setattr(auth, "authenticate_platform_user", lambda email, password: {"id": "one", "email": email})
    monkeypatch.setattr(auth, "create_user_session", lambda *args: "test-session")
    monkeypatch.setattr(store, "record_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "get_user", lambda user_id: {"id": user_id, "email": "one@example.com", "archive_limit": 5})
    monkeypatch.setattr(main.calendar_service, "start_ldap", lambda *args: None)
    payload = json.dumps({"email": "one@example.com", "password": "secret"}).encode()
    payload += b" " * (request_limits.AUTH_BODY_BYTES - len(payload))
    response = TestClient(main.app).post("/api/session/login", content=payload, headers={"content-type": "application/json"})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"]
    assert calls == ["user:testclient"]


def test_invalid_json_counts_as_login_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "rate_allow", lambda key: calls.append(key) or True)
    response = TestClient(main.app).post("/api/auth/login", content=b"{", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert calls == ["admin:testclient"]


@pytest.mark.parametrize("path", list(request_limits.STREAMED_FORMS))
def test_streamed_uploads_authenticate_without_reading_large_body(path):
    messages, reads = request(path, [], headers=[(b"content-length", b"2000000000")])
    assert messages[0]["status"] == 401
    assert not reads


@pytest.mark.parametrize("length", [b"bad", b"-1", b"9" * 10000])
def test_bad_or_unbounded_content_length_rejected_without_integer_overflow(length):
    messages, reads = request("/api/auth/login", [], headers=[(b"content-length", length)])
    assert messages[0]["status"] == (413 if length.isdigit() else 400)
    assert not reads


def test_other_api_body_can_exceed_login_limit(monkeypatch):
    main.app.dependency_overrides[main.require_admin] = lambda: {"username": "admin"}
    try:
        # Model validation, rather than a body limit or auth failure, must run.
        response = TestClient(main.app).post("/api/settings/prompts", json={"protocol_prompt": "a" * 20000})
        assert response.status_code == 422
    finally:
        main.app.dependency_overrides.pop(main.require_admin, None)
