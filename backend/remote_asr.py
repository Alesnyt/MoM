"""Client for the separate OpenAI-compatible speech service configured by the admin."""
from __future__ import annotations

import io
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO

import httpx2 as httpx

from . import config


class AsrServiceError(RuntimeError):
    """Safe to show to a user. Upstream error bodies may contain sensitive data."""


@asynccontextmanager
async def connect(connection: config.AsrConnection, *, timeout: float = 600):
    try:
        url = config.normalize_asr_url(connection.base_url)
    except ValueError as exc:
        raise AsrServiceError(str(exc)) from None
    if not connection.key:
        raise AsrServiceError("Укажите ключ API на вкладке «Расшифровка».")
    try:
        async with httpx.AsyncClient(
            base_url=url + "/", headers={"Authorization": f"Bearer {connection.key}"},
            timeout=httpx.Timeout(timeout, connect=min(10, timeout)), follow_redirects=False,
        ) as client:
            yield SpeechService(client, connection.model)
    except httpx.TimeoutException:
        raise AsrServiceError("Сервер расшифровки не ответил вовремя.") from None
    except httpx.InvalidURL:
        raise AsrServiceError("Некорректный адрес API расшифровки.") from None
    except httpx.RequestError:
        raise AsrServiceError("Не удалось подключиться к API расшифровки. Проверьте адрес, сеть и сертификат.") from None


def _json(response: httpx.Response) -> dict:
    if response.status_code in {401, 403}:
        raise AsrServiceError("API расшифровки отклонил ключ. Проверьте ключ и доступ к модели.")
    if not response.is_success:
        raise AsrServiceError(f"API расшифровки вернул HTTP {response.status_code}. Проверьте адрес и модель.")
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AsrServiceError("API расшифровки вернул ответ в неизвестном формате.")
    return data


class SpeechService:
    def __init__(self, client: httpx.AsyncClient, model: str):
        self.client = client
        self.model = model
        self.format = "verbose_json"
        self.timestamps = True

    async def models(self) -> list[str]:
        response = await self.client.get("models")
        if response.status_code in {404, 405, 501}:
            return []
        items = _json(response).get("data")
        if not isinstance(items, list):
            raise AsrServiceError("API вернул некорректный список моделей.")
        transcription = set()
        unspecified = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                continue
            mode = item.get("mode")
            if mode == "audio_transcription":
                transcription.add(item["id"])
            elif not mode:
                # The standard model object has no mode. Preserve such IDs when
                # the server does not advertise any explicit transcription models.
                unspecified.add(item["id"])
        return sorted(transcription or unspecified)

    async def transcribe(self, path: Path) -> dict:
        with path.open("rb") as audio:
            return await self._upload(audio, "audio.wav")

    async def probe(self) -> None:
        # Exercise the selected model and Bearer key with one second of generated silence.
        with io.BytesIO() as sound:
            with wave.open(sound, "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(bytes(32000))
            await self._upload(sound, "test.wav")

    async def _upload(self, audio: BinaryIO, name: str) -> dict:
        if not self.model:
            raise AsrServiceError("Укажите модель на вкладке «Расшифровка».")
        while True:
            audio.seek(0)
            fields = {"model": self.model, "response_format": self.format}
            if self.timestamps:
                fields["timestamp_granularities[]"] = "segment"
            response = await self.client.post("audio/transcriptions", data=fields,
                                              files={"file": (name, audio, "audio/wav")})
            if response.status_code in {400, 422}:
                message = response.text.lower()
                rejected = any(term in message for term in (
                    "unsupported", "not supported", "does not support", "not allowed",
                    "unknown parameter", "unrecognized", "extra_forbidden", "input should be",
                    "must be one of", "not compatible", "invalid_value",
                ))
                if self.format == "verbose_json" and rejected and "response_format" in message:
                    self.format = "json"
                    self.timestamps = False
                    continue
                if self.timestamps and rejected and "timestamp_granularities" in message:
                    self.timestamps = False
                    continue
            data = _json(response)
            segments = data.get("segments") or []
            if (not isinstance(data.get("text"), str) or not isinstance(segments, list)
                    or any(not isinstance(s, dict) or not isinstance(s.get("text"), str) for s in segments)):
                raise AsrServiceError("В ответе API расшифровки ожидается поле text и, при наличии, список segments.")
            return {"text": data["text"], "segments": segments, "language": data.get("language")}
