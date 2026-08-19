from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from . import config

COOKIE_NAME = "mom_admin"
USER_COOKIE = "mom_user"
SESSION_SECONDS = 12 * 60 * 60
_sessions: dict[str, dict[str, Any]] = {}
_user_sessions: dict[str, dict[str, Any]] = {}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        200_000,
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    try:
        check = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            200_000,
        ).hex()
    except ValueError:
        return False
    return hmac.compare_digest(check, digest)


def check_credentials(username: str, password: str) -> bool:
    expected_user = config.get_admin_user()
    expected_hash = config.get_admin_password_hash()
    if not expected_user or not expected_hash:
        return False
    if username != expected_user:
        verify_password(password, expected_hash)
        return False
    return verify_password(password, expected_hash)


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "username": username,
        "expires": time.time() + SESSION_SECONDS,
    }
    return token


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    session = _sessions.get(token)
    if not session:
        return None
    if session["expires"] < time.time():
        _sessions.pop(token, None)
        return None
    return session


def drop_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def status(token: str | None) -> dict[str, Any]:
    session = get_session(token)
    return {
        "configured": config.admin_configured(),
        "authenticated": bool(session),
        "username": (session or {}).get("username"),
    }


def generate_password(length: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_user_session(user_id: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    _user_sessions[token] = {
        "user_id": user_id,
        "email": email,
        "expires": time.time() + SESSION_SECONDS,
    }
    return token


def get_user_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    session = _user_sessions.get(token)
    if not session:
        return None
    if session["expires"] < time.time():
        _user_sessions.pop(token, None)
        return None
    return session


def drop_user_session(token: str | None) -> None:
    if token:
        _user_sessions.pop(token, None)


def drop_user_sessions_for(user_id: str) -> None:
    for token, session in list(_user_sessions.items()):
        if session.get("user_id") == user_id:
            _user_sessions.pop(token, None)
