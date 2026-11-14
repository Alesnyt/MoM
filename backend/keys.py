from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import anyio

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from . import config
from .analyze import make_client

log = logging.getLogger("mom.keys")

_status: dict[str, Any] = {}


def _connection_id(key: str, base: str, model: str) -> tuple[str, str, str]:
    return hashlib.sha256(key.encode()).hexdigest(), base, model


def mask_api_key(key: str) -> str | None:
    key = key.strip()
    if not key:
        return None
    if len(key) <= 8:
        return "••••"
    return f"{key[:7]}…{key[-4:]}"


def public_snapshot() -> dict[str, Any]:
    data = snapshot()
    model_unavailable = bool(data.get("denied_models"))
    data["model_unavailable"] = model_unavailable
    data["hint"] = None
    data["base_url"] = None
    data["chat_model"] = None
    data["asr_model"] = None
    data["denied_models"] = []
    if model_unavailable:
        data["message"] = "Выбранная модель недоступна этому ключу"
    elif data.get("auth_rejected"):
        data["message"] = "Ключ не принят"
    elif data.get("rate_limited"):
        data["message"] = "Провайдер ограничил запросы. Повторите проверку позже."
    elif data.get("configured") and not data.get("connected") and data.get("checked_at"):
        data["message"] = "Нет связи с провайдером"
    return data


def snapshot() -> dict[str, Any]:
    key = config.get_api_key()
    provider = config.detect_provider()
    base = config.get_base_url()
    model = config.get_chat_model()
    checked = _status if _status.get("connection") == _connection_id(key, base, model) else {}
    return {
        "configured": bool(key),
        "connected": bool(checked.get("connected")),
        "auth_rejected": bool(checked.get("auth_rejected")),
        "rate_limited": bool(checked.get("rate_limited")),
        "denied_models": list(checked.get("denied_models") or []),
        "hint": mask_api_key(key),
        "message": checked.get("message", "Подключение ещё не проверено" if key else "Ключ не задан"),
        "checked_at": checked.get("checked_at"),
        "provider": provider if key else "none",
        "provider_label": config.provider_label(provider),
        "base_url": (base or None) if key else None,
        "chat_model": model if key else None,
        "asr_model": config.canonical_asr_model() if key else None,
    }


def reset_status(message: str = "Ключ не задан") -> dict[str, Any]:
    _status.clear()
    if config.get_api_key():
        _status.update(
            connection=_connection_id(config.get_api_key(), config.get_base_url(), config.get_chat_model()),
            message=message,
        )
    return snapshot()


def note_check_failed(message: str) -> None:
    reset_status(message)
    _status["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _model_id(raw: object) -> str:
    if isinstance(raw, str):
        name = raw.strip()
    elif isinstance(raw, dict):
        name = str(raw.get("id") or "").strip()
    else:
        name = str(getattr(raw, "id", None) or "").strip()
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name


def chat_model_ids(raw_ids: list[str], *, provider: str, private: bool = False) -> list[str]:
    """Keep models that can write a protocol. Image, audio and embedding models stay out."""
    skip = ("image", "audio", "live", "tts", "embed", "aqa", "veo", "lyria", "banana", "research", "whisper")
    picked: list[str] = []
    for raw in raw_ids:
        name = _model_id(raw)
        folded = name.lower()
        if not name or any(part in folded for part in skip):
            continue
        if provider == "gemini" and not (folded.startswith("gemini") and ("flash" in folded or folded.endswith("-pro") or "-pro-" in folded)):
            continue
        if provider == "qwen" and not (folded.startswith("qwen") and "asr" not in folded):
            continue
        if provider == "openai" and not private and not folded.startswith(("gpt-", "gpt", "o1", "o3", "o4", "chatgpt")):
            continue
        if provider == "openrouter" and "/" not in name:
            continue
        picked.append(name)
    return list(dict.fromkeys(picked))


@asynccontextmanager
async def _checking_client(secret: str, base: str):
    client = make_client(secret, base_url=base or None, timeout=float(config.VERIFY_TIMEOUT_SECONDS))
    try:
        yield client
    finally:
        # A disconnected browser cancels the enclosing AnyIO scope. Give the
        # connection pool a bounded opportunity to close even in that case.
        with anyio.move_on_after(5, shield=True):
            try:
                await client.close()
            except Exception:
                log.warning("Не удалось закрыть соединение проверки модели", exc_info=True)


async def list_chat_models() -> list[str]:
    secret = config.get_api_key().strip()
    if not secret:
        return []
    provider = config.detect_provider()
    async with _checking_client(secret, config.get_base_url()) as client:
        listed = await client.models.list()
    data = getattr(listed, "data", None)
    raw = [_model_id(item) for item in (data or [])]
    names = chat_model_ids(raw, provider=provider, private=config.is_private_base())
    if provider == "openrouter":
        names.sort(key=str.lower)
    current = config.get_chat_model()
    if current and current not in names:
        names.insert(0, current)
    elif current in names:
        names.remove(current)
        names.insert(0, current)
    return names


def _denied_message(denied: list[str]) -> str:
    names = ", ".join(denied)
    return f"Ключ не открывает модели: {names}. Выберите другую в админке."


async def _ping(client, model: str) -> None:
    await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
        temperature=0,
    )


def _model_denied(exc: APIStatusError) -> bool:
    if exc.status_code != 403:
        return False
    text = str(getattr(exc, "body", None) or exc).lower()
    return "unpurchased" in text or "model" in text or "eligible" in text


async def verify_key(key: str | None = None) -> dict[str, Any]:
    secret = (key if key is not None else config.get_api_key()).strip()
    if not secret:
        return reset_status()
    base = config.get_base_url()
    model = config.get_chat_model()
    connection = _connection_id(secret, base, model)
    label = config.provider_label(config.detect_provider(secret))
    result: dict[str, Any] = {"connection": connection}
    try:
        async with _checking_client(secret, base) as client:
            await _ping(client, model)
        result.update(connected=True, message=f"{label} принял ключ. Модель: {model}")
    except AuthenticationError:
        result.update(auth_rejected=True, message=f"{label} отклонил ключ. Проверьте его в админке.")
    except RateLimitError:
        result.update(rate_limited=True, message=f"{label} ограничил запросы. Повторите проверку позже.")
    except (APIConnectionError, APITimeoutError):
        result["message"] = f"Нет связи с {label}."
    except APIStatusError as exc:
        log.warning("%s вернул HTTP %s при проверке модели", label, exc.status_code)
        text = str(getattr(exc, "body", None) or exc).lower()
        if exc.status_code == 401:
            result.update(auth_rejected=True, message=f"{label} отклонил ключ. Проверьте его в админке.")
        elif _model_denied(exc) or (
            exc.status_code in {400, 404} and any(term in text for term in ("model", "not found", "not exist"))
        ):
            result.update(denied_models=[model], message=_denied_message([model]))
        else:
            result["message"] = f"{label} не ответил ({exc.status_code})."
    except Exception:
        log.exception("Проверка ключа не удалась")
        result["message"] = "Не удалось проверить ключ."

    if connection == _connection_id(config.get_api_key(), config.get_base_url(), config.get_chat_model()):
        _status.update(
            {"connected": False, "auth_rejected": False, "rate_limited": False, "denied_models": [],
             "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result},
        )
    return snapshot()
