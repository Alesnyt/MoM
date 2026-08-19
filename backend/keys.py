from __future__ import annotations

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

_status: dict[str, Any] = {
    "configured": False,
    "connected": False,
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
    data["hint"] = None
    data["base_url"] = None
    return data


def snapshot() -> dict[str, Any]:
    key = config.get_api_key()
    provider = config.detect_provider()
    label = config.provider_label(provider)
    if not key:
        return {
            "configured": False,
            "connected": False,
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
        "hint": mask_api_key(key),
        "message": _status["message"],
        "checked_at": _status["checked_at"],
        "provider": provider,
        "provider_label": label,
        "base_url": config.get_base_url() or None,
        "chat_model": config.get_chat_model(),
        "asr_model": config.get_asr_model(),
    }


def reset_status(message: str = "Ключ не задан") -> dict[str, Any]:
    _status.update(
        {
            "configured": False,
            "connected": False,
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
) -> dict[str, Any]:
    _status.update(
        {
            "configured": True,
            "connected": connected,
            "hint": mask_api_key(key),
            "message": message,
            "checked_at": checked_at,
        }
    )
    return snapshot()


async def _ping(client, model: str) -> None:
    await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
        temperature=0,
    )


async def verify_key(key: str | None = None) -> dict[str, Any]:
    secret = (key if key is not None else config.get_api_key()).strip()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not secret:
        return reset_status("Ключ не задан")

    provider = config.detect_provider(secret)
    label = config.provider_label(provider)
    bases = [config.get_base_url()] if config.get_base_url() else [""]
    models = [config.get_chat_model()]
    if provider == "qwen":
        bases = [config.QWEN_BASE_INTL, config.QWEN_BASE_CN]
        current = config.get_base_url()
        if current and current not in bases:
            bases.insert(0, current)
        models = list(dict.fromkeys([config.get_chat_model(), *config.QWEN_CHAT_MODELS]))

    last_error = ""
    ping_timeout = float(config.VERIFY_TIMEOUT_SECONDS)
    for base in bases:
        client = make_client(secret, base_url=base or None, timeout=ping_timeout)
        auth_rejected = False
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
                )
            except AuthenticationError as exc:
                last_error = (
                    f"{label} отклонил ключ на {base or 'api.openai.com'}"
                )
                detail = getattr(exc, "body", None) or getattr(exc, "message", None) or str(exc)
                if detail:
                    last_error = f"{last_error}: {str(detail)[:240]}"
                auth_rejected = True
                break
            except RateLimitError:
                config.apply_connection(base or config.get_base_url(), model, config.get_asr_model())
                return _mark(
                    connected=True,
                    message=f"{label} принял ключ, но сейчас упирается в лимит запросов.",
                    checked_at=checked_at,
                    key=secret,
                )
            except (APIConnectionError, APITimeoutError) as exc:
                last_error = f"Нет связи с {label}: {exc}"
            except APIStatusError as exc:
                last_error = f"{label} вернул {exc.status_code}: {exc.message or exc}"
            except Exception as exc:  # noqa: BLE001
                last_error = f"Проверка не удалась: {exc}"
        if auth_rejected:
            break

    return _mark(
        connected=False,
        message=last_error or f"Не удалось подключиться к {label}",
        checked_at=checked_at,
        key=secret,
    )
