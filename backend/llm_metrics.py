"""Numeric-only observations of existing LLM calls; never retain their contents."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from httpx2 import TimeoutException, TransportError
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from .diagnostics import MAX_SDK_ATTEMPTS


_scope: ContextVar[tuple[str, str]] = ContextVar("llm_metric_scope", default=("protocol", "none"))


@dataclass
class _Attempts:
    http: int = 0
    sdk: int | None = None
    status: int | None = None
    sends: int = 0
    previous_end: float | None = None
    details: list[dict] = field(default_factory=list)

    def finish_send(self, started: float, status: int | None, kind: str) -> None:
        ended = time.perf_counter()
        self.sends += 1
        detail = {"attempt": self.sends, "seconds": round(ended - started, 6),
                  "http_status": status, "error_kind": kind,
                  "gap_before_seconds": round(started - self.previous_end, 6) if self.previous_end is not None else None}
        self.previous_end = ended
        self.status = status
        if len(self.details) < MAX_SDK_ATTEMPTS:
            self.details.append(detail)
        else:
            self.details[-1] = detail


_attempts: ContextVar[_Attempts | None] = ContextVar("llm_metric_attempts", default=None)


class ObservedAsyncOpenAI(AsyncOpenAI):
    """Observe a complete send before the pinned SDK handles retries or backoff.

    Response hooks alone miss connection errors and failures while reading the body.
    The SDK still owns the transport, timeout, redirects and retry policy.
    """

    async def _send_request(self, request, **kwargs):
        observation = _attempts.get()
        if observation is None:
            return await super()._send_request(request, **kwargs)
        started = time.perf_counter()
        status, kind = None, "other"
        try:
            response = await super()._send_request(request, **kwargs)
            status = response.status_code
            kind = _status_kind(status)
            return response
        except (TimeoutException, TimeoutError):
            kind = "timeout"
            raise
        except TransportError:
            kind = "connection"
            raise
        except asyncio.CancelledError:
            kind = "cancelled"
            raise
        finally:
            try:
                observation.finish_send(started, status, kind)
            except Exception:
                pass


def _integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


async def _on_request(request: Any) -> None:
    try:
        observation = _attempts.get()
        if observation is None:
            return
        observation.http += 1
        observation.status = None
        # The SDK's numeric header distinguishes retries from HTTP redirects.
        raw = request.headers.get("x-stainless-retry-count", "")
        if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 4:
            observation.sdk = max(observation.sdk or 0, int(raw) + 1)
    except Exception:
        pass  # Metrics must never alter an HTTP request or expose its headers.


async def _on_response(response: Any) -> None:
    try:
        observation = _attempts.get()
        status = _integer(response.status_code)
        if observation is not None and status is not None and 100 <= status <= 599:
            observation.status = status
    except Exception:
        pass


def instrument_client(client: Any) -> None:
    """Attach observations to the SDK-owned HTTP pool without changing its options.

    Unsupported/custom clients still work; their transport counts stay unknown.
    Hooks do not inspect URLs, bodies, authorization or other response headers.
    """
    try:
        if getattr(client, "_mom_metrics_hooks", False) is True:
            return
        hooks = client._client.event_hooks
        if not isinstance(hooks, dict):
            return
        requests, responses = hooks.get("request"), hooks.get("response")
        if not isinstance(requests, list) or not isinstance(responses, list):
            return
        if _on_request not in requests:
            requests.append(_on_request)
        if _on_response not in responses:
            responses.append(_on_response)
        client._mom_metrics_hooks = True
    except Exception:
        pass


@contextmanager
def request_scope(phase: str, retry_reason: str = "none") -> Iterator[None]:
    token = _scope.set((phase, retry_reason))
    try:
        yield
    finally:
        _scope.reset(token)


def _input_size(messages: Any) -> tuple[int, int]:
    if not isinstance(messages, (list, tuple)):
        return 0, 0
    chars = 0
    for message in messages:
        content = _field(message, "content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, (list, tuple)):
            for part in content:
                text = _field(part, "text")
                if isinstance(text, str):
                    chars += len(text)
    return chars, len(messages)


def _usage(response: Any) -> dict:
    usage = _field(response, "usage")
    completion = _field(usage, "completion_tokens_details")
    prompt = _field(usage, "prompt_tokens_details")
    return {
        "prompt_tokens": _integer(_field(usage, "prompt_tokens")),
        "completion_tokens": _integer(_field(usage, "completion_tokens")),
        "total_tokens": _integer(_field(usage, "total_tokens")),
        "reasoning_tokens": _integer(_field(completion, "reasoning_tokens")),
        "cached_tokens": _integer(_field(prompt, "cached_tokens")),
    }


def _error_kind(exc: Exception, status: int | None) -> str:
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, APIConnectionError):
        return "connection"
    return _status_kind(status) if status is not None and status >= 400 else "other"


def _status_kind(status: int) -> str:
    if status < 400:
        return "none"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status in {401, 403}:
        return "auth"
    if 500 <= status <= 599:
        return "server"
    if 400 <= status <= 499:
        return "request"
    return "other"


async def completion(client: Any, *, phase: str | None = None,
                     retry_reason: str | None = None, **kwargs: Any) -> Any:
    """One SDK invocation, including its internal retries, with unchanged behavior.

    Duration includes network, server queue/inference and SDK backoff. This API
    is non-streaming: first-token latency and decode speed cannot be measured.
    """
    observed = _Attempts()
    token = _attempts.set(observed)
    started = time.perf_counter()
    outcome, error_kind, status, response = "error", "other", None, None
    try:
        response = await client.chat.completions.create(**kwargs)
        outcome, error_kind = "ok", "none"
        return response
    except asyncio.CancelledError:
        outcome, error_kind = "cancelled", "cancelled"
        raise
    except Exception as exc:
        try:
            status = _integer(getattr(exc, "status_code", None))
            error_kind = _error_kind(exc, status)
        except Exception:
            pass
        raise
    finally:
        elapsed = time.perf_counter() - started
        _attempts.reset(token)
        try:
            from . import diagnostics

            current_phase, current_retry = _scope.get()
            chars, messages = _input_size(kwargs.get("messages"))
            finish = "unknown"
            choices = _field(response, "choices")
            if isinstance(choices, (list, tuple)) and choices:
                raw_finish = _field(choices[0], "finish_reason")
                if raw_finish is not None:
                    finish = raw_finish if isinstance(raw_finish, str) and raw_finish in {
                        "stop", "length", "tool_calls", "content_filter", "function_call",
                    } else "other"
            has_hooks = getattr(client, "_mom_metrics_hooks", False) is True
            diagnostics.record_llm(phase or current_phase, {
                "seconds": elapsed, "input_chars": chars, "input_messages": messages,
                **_usage(response), "finish_reason": finish, "outcome": outcome,
                "error_kind": error_kind,
                "http_status": status if status is not None and 100 <= status <= 599 else observed.status,
                "sdk_attempts": observed.sends or (observed.sdk if has_hooks else None),
                "http_attempts": observed.http if has_hooks else None,
                **({"sdk_attempt_details": observed.details, "omitted_sdk_attempts": observed.sends - len(observed.details)}
                   if observed.sends else {}),
                "retry_reason": retry_reason or current_retry, "ttft_seconds": None,
            })
        except Exception:
            pass  # No logging of exceptions: a provider can put secrets in them.
