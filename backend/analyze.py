from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from . import config

_local_asr_lock = asyncio.Lock()

SYSTEM_PROMPT = """Ты — ассистент, который готовит протокол встречи (Minutes of Meeting, MoM).
По транскрипту созвона верни ТОЛЬКО JSON по схеме ниже.
Пиши на языке транскрипта. Не выдумывай факты, имена, сроки и решения, которых нет в тексте.
Реплики помечены именами спикеров («Спикер 1» и т.п.) — это разные люди. Бери эти имена как участников, если в речи не прозвучало настоящее имя.
Если поле неизвестно — используй null или пустой массив.

Схема:
{
  "title": "краткий деловой заголовок встречи",
  "language": "ru|en|...",
  "participants": ["Имя или роль, если названы"],
  "date_hint": "дата/время из речи или null",
  "summary": "связное саммари на 2–5 абзацев: цель, ход, итог",
  "key_points": ["ключевые тезисы"],
  "decisions": ["принятые решения"],
  "action_items": [
    {
      "task": "конкретное поручение",
      "assignee": "исполнитель или null",
      "due": "срок, если назван, иначе null",
      "priority": "high|medium|low"
    }
  ],
  "mom": {
    "agenda": [
      {
        "topic": "тема блока",
        "discussion": "что обсудили",
        "outcome": "итог блока"
      }
    ],
    "next_meeting": "договорённость о следующем созвоне или null"
  }
}
"""


def make_client(
    api_key: str,
    base_url: str | None = None,
    timeout: float | None = None,
) -> AsyncOpenAI:
    url = config.get_base_url() if base_url is None else base_url
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": 600.0 if timeout is None else timeout,
    }
    if url:
        kwargs["base_url"] = url
    return AsyncOpenAI(**kwargs)


def _is_asr(model: str) -> bool:
    name = (model or "").lower()
    return "asr" in name and "omni" not in name


def _delta_text(delta: Any) -> str:
    content = getattr(delta, "content", None) if delta is not None else None
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        return "".join(parts)
    return str(content)


def _parse_result(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Модель вернула не объект")
    data.setdefault("title", "Встреча")
    data.setdefault("language", "ru")
    data.setdefault("participants", [])
    data.setdefault("date_hint", None)
    data.setdefault("summary", "")
    data.setdefault("key_points", [])
    data.setdefault("decisions", [])
    data.setdefault("action_items", [])
    mom = data.get("mom") or {}
    if not isinstance(mom, dict):
        mom = {}
    mom.setdefault("agenda", [])
    mom.setdefault("next_meeting", None)
    data["mom"] = mom
    cleaned_items = []
    for item in data["action_items"]:
        if not isinstance(item, dict):
            continue
        task = str(item.get("task") or "").strip()
        if not task:
            continue
        priority = str(item.get("priority") or "medium").lower()
        if priority not in {"high", "medium", "low"}:
            priority = "medium"
        cleaned_items.append(
            {
                "task": task,
                "assignee": item.get("assignee") or None,
                "due": item.get("due") or None,
                "priority": priority,
            }
        )
    data["action_items"] = cleaned_items
    return data


async def _transcribe_local(
    path: Path,
    model: str,
    language: str | None,
    duration: float | None,
    on_progress: Any,
) -> dict[str, Any]:
    if _local_asr_lock.locked() and on_progress:
        on_progress(1, "Жду свободный слот распознавания")
    async with _local_asr_lock:
        if config.is_gigaam_asr(model):
            from .gigaam_asr import transcribe_gigaam_sync

            return await asyncio.to_thread(transcribe_gigaam_sync, path, language, duration, on_progress)
        from .local_asr import transcribe_local_sync

        return await asyncio.to_thread(transcribe_local_sync, path, language, duration, on_progress)


async def transcribe_file(
    client: AsyncOpenAI,
    path: Path,
    model: str,
    language: str | None = None,
    duration: float | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    if config.is_gigaam_asr(model) or config.is_whisper_asr(model) or config.is_local_asr(model):
        return await _transcribe_local(path, model, language, duration, on_progress)

    if config.is_qwen() or model.startswith("qwen"):
        return await transcribe_qwen(client, path, model, language, duration, on_progress)

    async def _once(with_timestamps: bool) -> dict[str, Any]:
        handle = path.open("rb")
        kwargs: dict[str, Any] = {
            "model": model,
            "file": handle,
            "response_format": "verbose_json",
        }
        if with_timestamps:
            kwargs["timestamp_granularities"] = ["segment"]
        if language:
            kwargs["language"] = language
        try:
            result = await client.audio.transcriptions.create(**kwargs)
        finally:
            handle.close()
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    try:
        return await _once(True)
    except Exception:
        return await _once(False)


async def transcribe_qwen(
    client: AsyncOpenAI,
    path: Path,
    model: str,
    language: str | None = None,
    duration: float | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    import base64

    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    del raw
    data_uri = f"data:audio/mpeg;base64,{encoded}"
    del encoded
    last_error: Exception | None = None
    for candidate in config.asr_model_candidates(model):
        try:
            payload = await _transcribe_qwen_once(client, data_uri, candidate, language)
            if candidate != config.get_asr_model():
                config.apply_connection(
                    config.get_base_url(),
                    config.get_chat_model(),
                    candidate,
                )
            return payload
        except APIStatusError as exc:
            text = str(exc).lower()
            if exc.status_code in {400, 404} and (
                "model_not_found" in text or "model not exist" in text or "not found" in text
            ):
                last_error = exc
                continue
            raise
    if config.is_token_plan():
        return await _transcribe_local(path, config.LOCAL_ASR_MODEL, language, duration, on_progress)
    raise last_error or RuntimeError("Не удалось расшифровать аудио ни одной ASR-моделью Qwen")


async def _transcribe_qwen_once(
    client: AsyncOpenAI,
    data_uri: str,
    model: str,
    language: str | None,
) -> dict[str, Any]:
    audio_part = {
        "type": "input_audio",
        "input_audio": {"data": data_uri, "format": "mp3"},
    }
    content: list[dict[str, Any]] = [audio_part]
    kwargs: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": content}]}
    extra: dict[str, Any] = {}
    if _is_asr(model):
        asr_options: dict[str, Any] = {"enable_itn": True}
        if language:
            asr_options["language"] = language
        extra["asr_options"] = asr_options
    else:
        prompt = "Распознай речь на записи дословно. Верни только транскрипт, без комментариев и без markdown."
        if language:
            prompt = f"{prompt} Язык речи: {language}."
        content.append({"type": "text", "text": prompt})
        extra["modalities"] = ["text"]
        extra["enable_thinking"] = False
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
    if extra:
        kwargs["extra_body"] = extra
    if kwargs.get("stream"):
        text = await _collect_stream_text(client, kwargs)
        return {"language": language, "text": text, "segments": []}

    response = await client.chat.completions.create(**kwargs)
    message = response.choices[0].message
    text = (message.content or "").strip()
    language_out = language
    for item in getattr(message, "annotations", None) or []:
        if isinstance(item, dict) and item.get("language"):
            language_out = item.get("language")
        elif hasattr(item, "language") and item.language:
            language_out = item.language
    return {"language": language_out, "text": text, "segments": []}


async def _collect_stream_text(client: AsyncOpenAI, kwargs: dict[str, Any]) -> str:
    stream = await client.chat.completions.create(**kwargs)
    parts: list[str] = []
    async for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        piece = _delta_text(delta)
        if piece:
            parts.append(piece)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Модель вернула пустой транскрипт")
    return text


def merge_transcripts(chunks: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    language = None
    for offset, payload in chunks:
        language = language or payload.get("language")
        text = (payload.get("text") or "").strip()
        if text:
            texts.append(text)
        for segment in payload.get("segments") or []:
            start = float(segment.get("start") or 0) + offset
            end = float(segment.get("end") or 0) + offset
            body = (segment.get("text") or "").strip()
            if not body:
                continue
            segments.append({"start": start, "end": end, "text": body})
    return {
        "language": language,
        "text": "\n".join(texts).strip(),
        "segments": segments,
    }


def format_transcript(payload: dict[str, Any]) -> str:
    segments = payload.get("segments") or []
    if not segments:
        return (payload.get("text") or "").strip()
    lines = []
    for segment in segments:
        start = float(segment.get("start") or 0)
        minutes = int(start // 60)
        seconds = int(start % 60)
        text = (segment.get("text") or "").strip()
        lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
    return "\n".join(lines)


async def analyze_transcript(client: AsyncOpenAI, transcript: str, title_hint: str | None) -> dict[str, Any]:
    user = "Транскрипт встречи:\n\n" + transcript
    if title_hint:
        user = f"Предложенный заголовок (можно уточнить): {title_hint}\n\n" + user
    try:
        response = await client.chat.completions.create(
            model=config.get_chat_model(),
            temperature=0.2,
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
    except Exception:
        response = await client.chat.completions.create(
            model=config.get_chat_model(),
            temperature=0.2,
            extra_body={"enable_thinking": False},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user + "\n\nВерни только JSON."},
            ],
        )
    content = response.choices[0].message.content or "{}"
    return _parse_result(content)


async def analyze_long_transcript(
    client: AsyncOpenAI,
    transcript: str,
    title_hint: str | None,
    on_part: Any = None,
) -> dict[str, Any]:
    limit = 90_000
    if len(transcript) <= limit:
        if on_part:
            on_part(1, 1)
        return await analyze_transcript(client, transcript, title_hint)

    parts = []
    step = 80_000
    total = max(1, (len(transcript) + step - 1) // step)
    for index, start in enumerate(range(0, len(transcript), step), start=1):
        if on_part:
            on_part(index, total + 1)
        chunk = transcript[start : start + step]
        summary = await analyze_transcript(
            client,
            f"Это часть {index} длинной встречи. Составь промежуточный протокол.\n\n{chunk}",
            title_hint,
        )
        parts.append(summary)

    if on_part:
        on_part(total + 1, total + 1)
    merged_source = json.dumps(parts, ensure_ascii=False, indent=2)
    return await analyze_transcript(
        client,
        "Ниже несколько частичных протоколов одной встречи. Объедини их в один итоговый JSON "
        "без дублей, сохранив все поручения и решения.\n\n" + merged_source,
        title_hint,
    )
