from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from openai import APIStatusError, AsyncOpenAI, BadRequestError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import config, prompts
from .performance import stage_timer
from .prompts import PROTOCOL_PROMPT as SYSTEM_PROMPT  # Public default for integrations.
from .protocol_text import deduplicate

log = logging.getLogger("mom.llm")

_local_asr_lock = asyncio.Lock()


@asynccontextmanager
async def local_asr_slot():
    """Serialize local ASR and diarization, including GigaAM speaker spans."""
    with stage_timer("local_slot_wait"):
        await _local_asr_lock.acquire()
    try:
        yield
    finally:
        _local_asr_lock.release()


def _chat_kwargs() -> dict[str, Any]:
    if config.detect_provider() == "qwen":
        return {"extra_body": {"enable_thinking": False}}
    return {}


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
    if config.is_gemini_key(api_key) or (url and "generativelanguage.googleapis.com" in url):
        kwargs["default_headers"] = {"x-goog-api-key": api_key}
    elif config.is_openrouter_key(api_key) or (url and "openrouter.ai" in url):
        kwargs["default_headers"] = {"HTTP-Referer": "https://github.com/Alesnyt/MoM", "X-Title": "MoM"}
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


class ProtocolModel(BaseModel):
    model_config = ConfigDict(strict=True)


def _turn_numbers(value: Any) -> list[int]:
    return [number for number in value if type(number) is int and number > 0] if isinstance(value, list) else []


class ProtocolAction(ProtocolModel):
    task: str = Field(min_length=1)
    assignee: str | None = None
    due: str | None = None
    priority: Literal["high", "medium", "low"] = "medium"
    source_turns: list[int] = Field(default_factory=list)

    _validate_turns = field_validator("source_turns", mode="before")(_turn_numbers)


class DecisionSource(ProtocolModel):
    decision: str
    turns: list[int] = Field(default_factory=list)

    _validate_turns = field_validator("turns", mode="before")(_turn_numbers)


class ProtocolTopic(ProtocolModel):
    topic: str
    discussion: str = ""
    outcome: str = ""


class ProtocolMinutes(ProtocolModel):
    agenda: list[ProtocolTopic] = Field(default_factory=list)
    next_meeting: str | None = None


class ProtocolResult(ProtocolModel):
    title: str = "Встреча"
    language: str = "ru"
    participants: list[str] = Field(default_factory=list)
    date_hint: str | None = None
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    decision_sources: list[DecisionSource] = Field(default_factory=list)
    action_items: list[ProtocolAction] = Field(default_factory=list)
    mom: ProtocolMinutes = Field(default_factory=ProtocolMinutes)

    @field_validator("decision_sources", mode="before")
    @classmethod
    def validate_decision_sources(cls, value: Any) -> list:
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict) and isinstance(entry.get("decision"), str)]


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
    try:
        return deduplicate(ProtocolResult.model_validate(data).model_dump())
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise ValueError(f"Неверная структура протокола от модели: {fields}") from None


async def _transcribe_local(
    path: Path,
    model: str,
    language: str | None,
    duration: float | None,
    on_progress: Any,
) -> dict[str, Any]:
    if _local_asr_lock.locked() and on_progress:
        on_progress(1, "Жду свободный слот распознавания")
    async with local_asr_slot():
        with stage_timer("asr"):
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
    log.info("asr model=%s file=%s", model, path.name)
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
            if candidate != model:
                log.info("Для записи использована резервная ASR-модель %s", candidate)
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


def json_format_unsupported(exc: BadRequestError) -> bool:
    """Only retry without JSON mode when the provider explicitly rejects it."""
    message = str(exc).lower()
    format_named = any(term in message for term in ("response_format", "json_object", "json mode"))
    unsupported = any(term in message for term in (
        "not supported", "unsupported", "does not support", "not available",
        "not allowed", "unknown parameter", "unrecognized request argument",
    ))
    return format_named and unsupported


async def analyze_transcript(
    client: AsyncOpenAI, transcript: str, title_hint: str | None, *, system_prompt: str | None = None,
) -> dict[str, Any]:
    system_prompt = system_prompt if system_prompt is not None else prompts.snapshot()["protocol_prompt"]
    model = config.get_chat_model()
    log.info("llm model=%s символов %s", model, len(transcript))
    user = "Транскрипт встречи:\n\n" + transcript
    if title_hint:
        user = f"Предложенный заголовок (можно уточнить): {title_hint}\n\n" + user
    try:
        response = await client.chat.completions.create(
            model=model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user},
            ],
            **_chat_kwargs(),
        )
    except BadRequestError as exc:
        if not json_format_unsupported(exc):
            raise
        log.warning("llm model=%s без json_object, повтор запроса", model)
        response = await client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user + "\n\nВерни только JSON."},
            ],
            **_chat_kwargs(),
        )
    content = response.choices[0].message.content or "{}"
    return _parse_result(content)


_TURN_ID = re.compile(r"^\[реплика (\d+)\] ", re.MULTILINE)
_SOURCE_INSTRUCTIONS = (
    "Если реплики пронумерованы, укажи подтверждающие номера. "
    "В decision_sources добавь объекты {\"decision\": точный текст решения, \"turns\": [номера]}. "
    "В каждом поручении добавь source_turns с номерами реплик. "
    "Ссылайся на явное согласие или назначение, а при отсутствии подтверждения оставь список пустым.\n\n"
)
_MERGE_INSTRUCTIONS = (
    "Объедини промежуточные протоколы в один итоговый JSON. Удали смысловые дубли. "
    "Сохрани каждое отдельное решение и поручение с его исполнителем, сроком и номерами исходных реплик. "
    "Для decision_sources используй точный текст итоговых решений. Не придумывай новые номера.\n\n"
)


def _transcript_chunks(transcript: str, budget: int) -> list[str]:
    chunks: list[str] = []
    lines: list[str] = []
    size = 0
    for line in transcript.splitlines(keepends=True):
        line_size = len(line.encode())
        if size + line_size > budget and lines:
            chunks.append("".join(lines))
            lines, size = [], 0
        if line_size > budget:
            prefix = _TURN_ID.match(line)
            label = prefix[0] if prefix else ""
            remaining = line[len(label):].encode()
            while remaining:
                head = remaining[:budget - len(label.encode())].decode(errors="ignore")
                if len(remaining) > len(head.encode()):
                    boundary = head.rfind(" ")
                    if boundary > len(head) // 2:
                        head = head[:boundary + 1]
                chunks.append(label + head)
                remaining = remaining[len(head.encode()):]
        else:
            lines.append(line)
            size += line_size
    if lines:
        chunks.append("".join(lines))
    return chunks or [transcript]


def _with_sources(result: dict, allowed: set[int]) -> dict:
    decisions = {item: set() for item in result.get("decisions") or []}
    for entry in result.get("decision_sources") or []:
        decision = entry["decision"].strip()
        if decision in decisions:
            decisions[decision].update(allowed.intersection(entry["turns"]))
    result["decision_sources"] = [{"decision": item, "turns": sorted(turns)} for item, turns in decisions.items() if turns]
    for item in result.get("action_items") or []:
        item["source_turns"] = sorted(set(item.get("source_turns") or []) & allowed)
    return result


def _merge_locally(parts: list[dict]) -> dict:
    combined = {**parts[0]}
    combined["summary"] = "\n\n".join(dict.fromkeys(part["summary"] for part in parts if part.get("summary")))
    for field in ("participants", "key_points", "decisions", "decision_sources", "action_items"):
        combined[field] = [item for part in parts for item in part.get(field, [])]
    combined["participants"] = list(dict.fromkeys(combined["participants"]))
    combined["mom"] = {
        "agenda": [item for part in parts for item in part.get("mom", {}).get("agenda", [])],
        "next_meeting": next((part["mom"]["next_meeting"] for part in reversed(parts)
                              if part.get("mom", {}).get("next_meeting")), None),
    }
    return deduplicate(combined)


def _retain_grounded_items(result: dict, parts: list[dict]) -> dict:
    """Keep cited decisions and tasks if a merge skips them."""
    decisions = result.setdefault("decisions", [])
    sources = result.setdefault("decision_sources", [])
    actions = result.setdefault("action_items", [])
    for part in parts:
        for entry in part.get("decision_sources", []):
            decision = entry["decision"]
            if decision in decisions:
                sources.append(entry)
            elif not any(set(entry["turns"]).intersection(existing["turns"]) for existing in sources):
                decisions.append(decision)
                sources.append(entry)
        for action in part.get("action_items", []):
            turns = action.get("source_turns")
            if not turns:
                continue
            same_assignment = [item for item in actions if item.get("assignee") == action.get("assignee")
                               and item.get("due") == action.get("due")]
            same_task = next((item for item in same_assignment if item["task"] == action["task"]), None)
            if same_task is not None:
                same_task["source_turns"] = sorted(set(turns).union(same_task.get("source_turns") or []))
            elif not any(set(turns).intersection(item.get("source_turns") or []) for item in same_assignment):
                actions.append(action)
    return deduplicate(result)


async def analyze_long_transcript(
    client: AsyncOpenAI,
    transcript: str,
    title_hint: str | None,
    on_part: Any = None,
    *, participant_roles: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    system_prompt = prompts.snapshot()["protocol_prompt"]
    # UTF-8 size is a conservative input estimate for byte-based tokenizers.
    # Leave room for the response, wrappers and any provider chat framing.
    context = config.PROTOCOL_CONTEXT_TOKENS
    budget = (context - min(8192, context // 4) - 768 - len(system_prompt.encode())
              - len((title_hint or "").encode())
              - max(len(_SOURCE_INSTRUCTIONS.encode()), len(_MERGE_INSTRUCTIONS.encode())))
    roster = ""
    if participant_roles:
        header = ("Должности участников из каталога, справочные данные. Используй их для контекста. "
                  "В поле participants верни только имена.\n")
        remaining = max(0, budget // 4) - len(header.encode()) - 4
        entries = []
        for person in participant_roles:
            entry = json.dumps(person, ensure_ascii=False)
            size = len(entry.encode()) + 2
            if size <= remaining:
                entries.append(entry)
                remaining -= size
        if entries:
            roster = header + "[" + ", ".join(entries) + "]\n\n"
            budget -= len(roster.encode())
    if budget < 256:
        raise ValueError("Промпт не помещается в окно контекста. Проверьте PROTOCOL_CONTEXT_TOKENS.")
    chunks = _transcript_chunks(transcript, budget)
    total = len(chunks) + (len(chunks) > 1)
    parts = []
    allowed: set[int] = set()
    for index, chunk in enumerate(chunks, start=1):
        if on_part:
            on_part(index, total)
        sources = {int(value) for value in _TURN_ID.findall(chunk)}
        allowed.update(sources)
        intro = f"Это часть {index} из {len(chunks)}. Составь промежуточный протокол.\n\n" if len(chunks) > 1 else ""
        prompt = roster + intro + (_SOURCE_INSTRUCTIONS if sources else "") + chunk
        summary = await analyze_transcript(client, prompt, title_hint, system_prompt=system_prompt)
        parts.append(_with_sources(summary, sources))

    while len(parts) > 1:
        merged = []
        cursor = 0
        while cursor < len(parts):
            group = [parts[cursor]]
            cursor += 1
            while cursor < len(parts):
                candidate = json.dumps([*group, parts[cursor]], ensure_ascii=False, separators=(",", ":"))
                if len(candidate.encode()) > budget:
                    break
                group.append(parts[cursor])
                cursor += 1
            if len(group) == 1:
                merged.append(group[0])
                continue
            if on_part:
                on_part(total, total)
            payload = json.dumps(group, ensure_ascii=False, separators=(",", ":"))
            result = await analyze_transcript(client, roster + _MERGE_INSTRUCTIONS + payload, title_hint, system_prompt=system_prompt)
            merged.append(_with_sources(_retain_grounded_items(_with_sources(result, allowed), group), allowed))
        if len(merged) == len(parts):
            return _with_sources(_merge_locally(merged), allowed)
        parts = merged
    return parts[0]
