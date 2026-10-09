from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

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

_status: dict[str, Any] = {
    "configured": False,
    "connected": False,
    "auth_rejected": False,
    "denied_models": [],
    "hint": None,
    "message": "Ключ не задан",
    "checked_at": None,
    "provider": "none",
    "base_url": None,
    "chat_model": None,
    "asr_model": None,
}


def mask_api_key(key: str) -> str | None:
    key = key.strip()
    if not key:
        return None
    if len(key) <= 8:
        return "••••"
    return f"{key[:7]}…{key[-4:]}"


def public_snapshot() -> dict[str, Any]:
    data = snapshot()
    message = data.get("message") or ""
    model_unavailable = "модел" in message.lower()
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
    elif data.get("configured") and not data.get("connected"):
        data["message"] = "Нет связи с провайдером"
    return data


def snapshot() -> dict[str, Any]:
    key = config.get_api_key()
    provider = config.detect_provider()
    label = config.provider_label(provider)
    if not key:
        return {
            "configured": False,
            "connected": False,
            "auth_rejected": False,
            "denied_models": [],
            "hint": None,
            "message": "Ключ не задан",
            "checked_at": None,
            "provider": "none",
            "provider_label": label,
            "base_url": None,
            "chat_model": None,
            "asr_model": None,
        }
    return {
        "configured": True,
        "connected": bool(_status["connected"]),
        "auth_rejected": bool(_status.get("auth_rejected")),
        "denied_models": list(_status.get("denied_models") or []),
        "hint": mask_api_key(key),
        "message": _status["message"],
        "checked_at": _status["checked_at"],
        "provider": provider,
        "provider_label": label,
        "base_url": config.get_base_url() or None,
        "chat_model": config.get_chat_model(),
        "asr_model": config.canonical_asr_model(),
    }


def reset_status(message: str = "Ключ не задан") -> dict[str, Any]:
    _status.update(
        {
            "configured": False,
            "connected": False,
            "auth_rejected": False,
            "denied_models": [],
            "hint": None,
            "message": message,
            "checked_at": None,
            "provider": "none",
            "base_url": None,
            "chat_model": None,
            "asr_model": None,
        }
    )
    return snapshot()


def _mark(
    *,
    connected: bool,
    message: str,
    checked_at: str,
    key: str,
    auth_rejected: bool = False,
    denied_models: list[str] | None = None,
) -> dict[str, Any]:
    _status.update(
        {
            "configured": True,
            "connected": connected,
            "auth_rejected": auth_rejected,
            "denied_models": list(denied_models or []),
            "hint": mask_api_key(key),
            "message": message,
            "checked_at": checked_at,
        }
    )
    return snapshot()


def note_check_failed(message: str) -> None:
    _status["connected"] = False
    _status["auth_rejected"] = False
    _status["denied_models"] = []
    _status["message"] = message
    _status["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def chat_models_for_check(*, thorough: bool) -> list[str]:
    current = config.get_chat_model()
    if thorough and config.detect_provider() == "qwen":
        return list(dict.fromkeys([current, *config.QWEN_CHAT_MODELS]))
    return [current]


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


async def verify_key(key: str | None = None, *, thorough: bool = True) -> dict[str, Any]:
    secret = (key if key is not None else config.get_api_key()).strip()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not secret:
        return reset_status("Ключ не задан")

    provider = config.detect_provider(secret)
    label = config.provider_label(provider)
    current = config.get_base_url()
    if provider == "qwen" and thorough:
        bases = [config.QWEN_BASE_INTL, config.QWEN_BASE_CN]
        if current and current not in bases:
            bases.insert(0, current)
    else:
        bases = [current] if current else [""]
    models = chat_models_for_check(thorough=thorough)

    last_error = ""
    denied: list[str] = []
    ping_timeout = float(config.VERIFY_TIMEOUT_SECONDS)
    for base in bases:
        client = make_client(secret, base_url=base or None, timeout=ping_timeout)
        for model in models:
            try:
                await _ping(client, model)
                asr_model = config.get_asr_model()
                if config.is_token_plan() and not config.is_local_asr(asr_model) and asr_model != "qwen-audio-3.0-asr-flash":
                    asr_model = config.default_asr_model()
                config.apply_connection(base, model, asr_model)
                return _mark(
                    connected=True,
                    message=f"{label} принял ключ. Модель: {model}",
                    checked_at=checked_at,
                    key=secret,
                    denied_models=denied,
                )
            except AuthenticationError as exc:
                detail = getattr(exc, "body", None) or getattr(exc, "message", None) or str(exc)
                log.warning("Ключ отклонён на %s: %s", base or "api.openai.com", str(detail)[:500])
                return _mark(
                    connected=False,
                    auth_rejected=True,
                    message=f"{label} отклонил ключ. Проверьте его в админке.",
                    checked_at=checked_at,
                    key=secret,
                )
            except RateLimitError:
                config.apply_connection(base or config.get_base_url(), model, config.get_asr_model())
                return _mark(
                    connected=True,
                    message=f"{label} принял ключ, но сейчас упирается в лимит запросов.",
                    checked_at=checked_at,
                    key=secret,
                )
            except (APIConnectionError, APITimeoutError) as exc:
                log.warning("Нет связи с %s: %s", label, exc)
                last_error = f"Нет связи с {label}."
                break
            except APIStatusError as exc:
                log.warning("%s вернул %s: %s", label, exc.status_code, exc.message or exc)
                text = str(getattr(exc, "body", None) or exc).lower()
                if exc.status_code == 401:
                    return _mark(
                        connected=False,
                        auth_rejected=True,
                        message=f"{label} отклонил ключ. Проверьте его в админке.",
                        checked_at=checked_at,
                        key=secret,
                    )
                if _model_denied(exc) or (
                    exc.status_code in {400, 404}
                    and ("model" in text or "not found" in text or "not exist" in text)
                ):
                    if model not in denied:
                        denied.append(model)
                    last_error = _denied_message(denied)
                    continue
                last_error = f"{label} не ответил ({exc.status_code})."
                break
            except Exception:
                log.exception("Проверка ключа не удалась")
                last_error = "Не удалось проверить ключ."

    return _mark(
        connected=False,
        message=last_error or f"Не удалось подключиться к {label}",
        checked_at=checked_at,
        key=secret,
        denied_models=denied,
    )
