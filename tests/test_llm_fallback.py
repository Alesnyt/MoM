"""Protocol/name review must not double network, quota or authentication failures."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import (
    APIConnectionError, APITimeoutError, AuthenticationError, BadRequestError,
    InternalServerError, RateLimitError,
)

from backend import analyze, name_review


def provider_error(cls, status, message):
    response = SimpleNamespace(status_code=status, headers={}, request=SimpleNamespace(url="https://llm.example.test/chat"))
    return cls(message, response=response, body={"message": message})


def request(operation, create):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    if operation == "protocol":
        return analyze.analyze_transcript(client, "Текст встречи", "Встреча", system_prompt="Return JSON")
    return name_review._request(client, {"name_review_prompt": "Return JSON"}, [], [])


@pytest.mark.parametrize("operation", ["protocol", "names"])
@pytest.mark.parametrize("error", [
    APIConnectionError(request=SimpleNamespace(url="https://llm.example.test/chat")),
    APITimeoutError(request=SimpleNamespace(url="https://llm.example.test/chat")),
    provider_error(AuthenticationError, 401, "Invalid API key"),
    provider_error(RateLimitError, 429, "Quota exhausted"),
    provider_error(InternalServerError, 500, "Provider unavailable"),
    provider_error(BadRequestError, 400, "Invalid JSON in messages"),
    provider_error(BadRequestError, 400, "Context length exceeded"),
    asyncio.CancelledError(),
])
def test_unrelated_errors_are_not_retried(operation, error):
    create = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        asyncio.run(request(operation, create))
    assert create.await_count == 1


@pytest.mark.parametrize("operation", ["protocol", "names"])
@pytest.mark.parametrize("message", [
    "response_format is not supported with this model",
    "Unsupported response_format parameter",
    "json_object is not available",
    "JSON mode is not supported by this model",
    "Unknown parameter: response_format",
])
def test_explicit_format_rejection_retries_once(operation, message):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"title":"Встреча","summary":"Итог","mentions":[]}'
    ))])
    create = AsyncMock(side_effect=[provider_error(BadRequestError, 400, message), response])
    asyncio.run(request(operation, create))
    assert create.await_count == 2
    assert create.await_args_list[0].kwargs["response_format"] == {"type": "json_object"}
    assert "response_format" not in create.await_args_list[1].kwargs


def test_fallback_error_propagates_without_third_attempt():
    failure = provider_error(RateLimitError, 429, "Quota exhausted")
    create = AsyncMock(side_effect=[
        provider_error(BadRequestError, 400, "response_format not supported"), failure,
    ])
    with pytest.raises(RateLimitError):
        asyncio.run(request("protocol", create))
    assert create.await_count == 2


def asr_request(tmp_path, monkeypatch, create):
    monkeypatch.setattr(analyze.config, 'is_local_asr', lambda model: False)
    monkeypatch.setattr(analyze.config, 'is_gigaam_asr', lambda model: False)
    monkeypatch.setattr(analyze.config, 'is_whisper_asr', lambda model: False)
    monkeypatch.setattr(analyze.config, 'is_qwen', lambda: False)
    path = tmp_path / 'audio.mp3'
    path.write_bytes(b'audio')
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    return analyze.transcribe_file(client, path, 'whisper-1', language='ru')


@pytest.mark.parametrize('error', [
    APIConnectionError(request=SimpleNamespace(url='https://asr.example.test')),
    APITimeoutError(request=SimpleNamespace(url='https://asr.example.test')),
    provider_error(AuthenticationError, 401, 'Invalid API key'),
    provider_error(RateLimitError, 429, 'Quota exhausted'),
    provider_error(InternalServerError, 500, 'Provider unavailable'),
    provider_error(BadRequestError, 400, 'Invalid audio file'),
    provider_error(BadRequestError, 400, 'response_format verbose_json unsupported'),
    provider_error(BadRequestError, 400, 'timestamp_granularities must be an array'),
    asyncio.CancelledError(),
])
def test_asr_does_not_repeat_unrelated_errors(tmp_path, monkeypatch, error):
    create = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        asyncio.run(asr_request(tmp_path, monkeypatch, create))
    assert create.await_count == 1
    assert create.await_args.kwargs['file'].closed


@pytest.mark.parametrize('message', [
    'timestamp_granularities is not supported by this model',
    'Unknown parameter: timestamp_granularities',
    'Unsupported timestamp_granularities',
])
def test_asr_explicit_timestamp_rejection_reopens_file_once(tmp_path, monkeypatch, message):
    seen = []
    result = {'text': 'Полный текст', 'segments': [{'start': 0, 'end': 1, 'text': 'Полный текст'}]}
    async def create(**kwargs):
        seen.append(kwargs)
        assert kwargs['file'].read() == b'audio'
        if len(seen) == 1:
            raise provider_error(BadRequestError, 400, message)
        return result
    assert asyncio.run(asr_request(tmp_path, monkeypatch, create)) == result
    assert len(seen) == 2
    assert seen[0]['timestamp_granularities'] == ['segment']
    assert 'timestamp_granularities' not in seen[1]
    assert all(item['file'].closed and item['language'] == 'ru' for item in seen)


def test_asr_fallback_does_not_retry_its_failure(tmp_path, monkeypatch):
    create = AsyncMock(side_effect=[
        provider_error(BadRequestError, 400, 'timestamp_granularities unsupported'),
        provider_error(RateLimitError, 429, 'Quota exhausted'),
    ])
    with pytest.raises(RateLimitError):
        asyncio.run(asr_request(tmp_path, monkeypatch, create))
    assert create.await_count == 2
