from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from . import config, store

COOKIE_NAME = "mom_admin"
USER_COOKIE = "mom_user"
SESSION_SECONDS = 12 * 60 * 60
_DUMMY_HASH: str | None = None


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        200_000,
    ).hex()
    return f"{salt}${digest}"


def dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("timing-oracle-placeholder")
    return _DUMMY_HASH


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


def tokens_match(got: str, expected: str) -> bool:
    left = got.strip().encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


def rate_allow(key: str) -> bool:
    return store.rate_allow(
        key,
        window=float(config.LOGIN_WINDOW_SECONDS),
        limit=int(config.LOGIN_MAX_ATTEMPTS),
    )


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
    store.put_session(
        token,
        "admin",
        username=username,
        expires=time.time() + SESSION_SECONDS,
    )
    return token


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = store.get_session_row(token)
    if not row or row.get("kind") != "admin":
        return None
    return {"username": row.get("username")}


def drop_session(token: str | None) -> None:
    if token:
        store.drop_session_token(token)


def status(token: str | None) -> dict[str, Any]:
    session = get_session(token)
    return {
        "configured": config.admin_configured(),
        "authenticated": bool(session),
        "username": (session or {}).get("username"),
        "setup_token_required": (not config.admin_configured()) and bool(config.get_setup_token()),
    }


def generate_password(length: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_user_session(user_id: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    store.put_session(
        token,
        "user",
        user_id=user_id,
        email=email,
        expires=time.time() + SESSION_SECONDS,
    )
    return token


def get_user_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = store.get_session_row(token)
    if not row or row.get("kind") != "user":
        return None
    return {"user_id": row.get("user_id"), "email": row.get("email")}


def drop_user_session(token: str | None) -> None:
    if token:
        store.drop_session_token(token)


def drop_user_sessions_for(user_id: str) -> None:
    store.drop_sessions_for_user(user_id)


def ldap_password_placeholder() -> str:
    return "ldap$" + secrets.token_hex(16)


def authenticate_platform_user(email: str, password: str) -> dict[str, Any] | None:
    record = store.get_user_auth(email)
    if not record:
        verify_password(password, dummy_hash())
        return None
    if record.get("auth_mode") == "ldap":
        from . import ldap_auth

        if not ldap_auth.authenticate(email, password):
            return None
        return record
    if not verify_password(password, record["password_hash"]):
        return None
    return record
