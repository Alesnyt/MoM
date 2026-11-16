"""LLM diagnostics must be private and must not change provider behavior."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, BadRequestError
from openai import _base_client

# Use the SDK's HTTP implementation (OpenAI 3 uses httpx2, older releases httpx).
httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2

from backend import analyze, config, diagnostics, llm_metrics, name_review, prompts


def response(*, usage=None, finish="stop", content='{"title":"Meeting","summary":"Done","mentions":[]}'):
    return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
        finish_reason=finish, message=SimpleNamespace(content=content),
    )])


def client_for(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def observations(monkeypatch):
    rows = []
    monkeypatch.setattr(diagnostics, "record_llm", lambda phase, metrics: rows.append((phase, metrics)))
    return rows


def test_usage_and_sizes_are_numeric_and_do_not_include_private_values(observations):
    private = "PRIVATE: user@example.test /secret/config https://internal.test key-123"
    result = response(usage=SimpleNamespace(
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
        prompt_tokens_details=SimpleNamespace(cached_tokens=75),
        private_value=private,
    ), content=private)
    create = AsyncMock(return_value=result)
    messages = [{"role": "system", "content": private}, {"role": "user", "content": "текст"}]
    assert asyncio.run(llm_metrics.completion(client_for(create), phase="name_review",
                                             model=private, messages=messages)) is result
    phase, data = observations[0]
    assert phase == "name_review"
    assert data["input_chars"] == len(private) + len("текст")
    assert data["input_messages"] == 2
    assert (data["prompt_tokens"], data["completion_tokens"], data["total_tokens"]) == (100, 20, 120)
    assert (data["reasoning_tokens"], data["cached_tokens"]) == (12, 75)
    assert data["outcome"] == "ok" and data["finish_reason"] == "stop"
    assert data["seconds"] >= 0 and data["ttft_seconds"] is None
    assert data["sdk_attempts"] is None and data["http_attempts"] is None
    assert private not in json.dumps(observations)
    create.assert_awaited_once_with(model=private, messages=messages)


def test_missing_or_malformed_usage_stays_unknown(observations):
    result = response(usage={"prompt_tokens": "secret", "completion_tokens": -1, "total_tokens": True,
                             "completion_tokens_details": {"reasoning_tokens": float("nan")}},
                      finish="private-server-output")
    asyncio.run(llm_metrics.completion(client_for(AsyncMock(return_value=result)), messages=[]))
    data = observations[0][1]
    assert all(data[key] is None for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                           "reasoning_tokens", "cached_tokens"))
    assert data["finish_reason"] == "other"
    assert "private-server-output" not in json.dumps(observations)


@pytest.mark.parametrize("error", [ValueError("secret internal response"), asyncio.CancelledError()])
def test_errors_and_cancellation_are_recorded_without_exception_contents(observations, error):
    with pytest.raises(type(error)):
        asyncio.run(llm_metrics.completion(client_for(AsyncMock(side_effect=error)), messages=[]))
    data = observations[0][1]
    assert data["outcome"] == ("cancelled" if isinstance(error, asyncio.CancelledError) else "error")
    assert "secret" not in json.dumps(observations)
    assert llm_metrics._attempts.get() is None


def test_unavailable_recorder_does_not_discard_response_or_replace_error(monkeypatch):
    def fail(*args):
        raise OSError("diagnostics storage unavailable")
    monkeypatch.setattr(diagnostics, "record_llm", fail)
    result = response()
    assert asyncio.run(llm_metrics.completion(client_for(AsyncMock(return_value=result)))) is result
    with pytest.raises(ValueError, match="original provider failure"):
        asyncio.run(llm_metrics.completion(client_for(AsyncMock(side_effect=ValueError("original provider failure")))))


@pytest.mark.parametrize("cls,kind", [(APITimeoutError, "timeout"), (APIConnectionError, "connection")])
def test_network_failures_have_safe_categories(observations, cls, kind):
    error = cls(request=httpx.Request("POST", "https://private.test/secret"))
    with pytest.raises(cls):
        asyncio.run(llm_metrics.completion(client_for(AsyncMock(side_effect=error))))
    assert observations[0][1]["error_kind"] == kind
    assert observations[0][1]["http_status"] is None
    assert "private" not in json.dumps(observations)


def format_rejection():
    return BadRequestError("response_format not supported; private details", body={},
                           response=httpx.Response(400, request=httpx.Request("POST", "https://private.test")))


@pytest.mark.parametrize("operation", ["protocol", "name_review"])
def test_json_fallback_is_a_distinct_logical_call_with_unchanged_params(observations, operation):
    create = AsyncMock(side_effect=[format_rejection(), response()])
    client = client_for(create)
    if operation == "protocol":
        asyncio.run(analyze.analyze_transcript(client, "Private transcript", None, system_prompt="Return JSON"))
    else:
        asyncio.run(name_review._request(client, {"name_review_prompt": "Return JSON"}, [], []))
    assert [phase for phase, _ in observations] == [operation, operation]
    assert [data["outcome"] for _, data in observations] == ["error", "ok"]
    assert [data["retry_reason"] for _, data in observations] == ["none", "json_fallback"]
    assert observations[0][1]["http_status"] == 400
    assert create.await_count == 2
    assert "response_format" in create.await_args_list[0].kwargs
    assert "response_format" not in create.await_args_list[1].kwargs
    assert "retry_reason" not in create.await_args_list[1].kwargs


def test_protocol_parts_and_merge_are_identified_without_changing_chunking(observations, monkeypatch):
    monkeypatch.setattr(config, "PROTOCOL_CONTEXT_TOKENS", 4096)
    monkeypatch.setattr(config, "PROTOCOL_TOKENIZER_PATH", "")
    monkeypatch.setattr(prompts, "snapshot", lambda: {"protocol_prompt": "Return JSON"})
    text = "\n".join(f"[реплика {i}] " + "Я" * 1600 for i in (1, 2))
    client = client_for(AsyncMock(return_value=response()))
    asyncio.run(analyze.analyze_long_transcript(client, text, None))
    assert [phase for phase, _ in observations] == ["protocol_part", "protocol_part", "protocol_merge"]
    assert llm_metrics._scope.get() == ("protocol", "none")


def test_actual_sdk_retry_is_observed_without_network(observations):
    async def scenario():
        attempts = []
        async def transport(request):
            attempts.append(request.headers.get("x-stainless-retry-count"))
            if len(attempts) == 1:
                return httpx.Response(429, headers={"retry-after-ms": "1"},
                                      json={"error": {"message": "private provider details", "type": "rate_limit"}})
            return httpx.Response(200, json={
                "id": "private-id", "object": "chat.completion", "created": 1, "model": "private-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "private output"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            async with AsyncOpenAI(api_key="private-key", base_url="https://private.test/v1",
                                   http_client=http, max_retries=1) as client:
                llm_metrics.instrument_client(client)
                llm_metrics.instrument_client(client)
                assert http.event_hooks["request"].count(llm_metrics._on_request) == 1
                result = await llm_metrics.completion(client, model="private-model",
                                                      messages=[{"role": "user", "content": "private prompt"}])
                assert result.choices[0].message.content == "private output"
        assert attempts == ["0", "1"]
    asyncio.run(scenario())
    data = observations[0][1]
    assert data["http_attempts"] == data["sdk_attempts"] == 2
    assert data["http_status"] == 200 and data["outcome"] == "ok"
    assert len(observations) == 1
    assert "private" not in json.dumps(observations)


def test_overlapping_calls_keep_attempts_and_phases_separate(observations):
    async def scenario():
        hooks = {"request": [], "response": []}
        async def create(**kwargs):
            attempts = 2 if kwargs["model"] == "a" else 1
            for i in range(attempts):
                await asyncio.sleep(0)
                await hooks["request"][0](SimpleNamespace(headers={"x-stainless-retry-count": str(i)}))
                await hooks["response"][0](SimpleNamespace(status_code=200))
            return response()
        client = client_for(create)
        client._client = SimpleNamespace(event_hooks=hooks)
        llm_metrics.instrument_client(client)
        async def run(phase, model):
            with llm_metrics.request_scope(phase):
                await llm_metrics.completion(client, model=model)
        await asyncio.gather(run("protocol_part", "a"), run("name_review", "b"))
    asyncio.run(scenario())
    counts = {phase: data["sdk_attempts"] for phase, data in observations}
    assert counts == {"protocol_part": 2, "name_review": 1}


def test_make_client_installs_observers_and_closes_normally(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://private.test/v1")
    async def scenario():
        client = analyze.make_client("private-key")
        assert client._mom_metrics_hooks is True
        await client.close()
        assert client.is_closed()
    asyncio.run(scenario())
