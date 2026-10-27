from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from typing import Any

from openai import BadRequestError

from . import control, prompts, store
from .analyze import _chat_kwargs
from .config import get_chat_model

log = logging.getLogger("mom.names")
CHUNK_CHARS = 24_000
NAME = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*(?: [^\W\d_]+(?:[-'’][^\W\d_]+)*){0,3}")


def named_participants(document: dict) -> list[dict]:
    return [
        {"id": p["id"], "name": p["name"]}
        for p in document.get("speakers") or []
        if p.get("name") and p.get("id") != "unknown"
        and not re.fullmatch(r"(?:Спикер|Speaker)\s*\d*|Неясно|S\d+", p["name"], re.I)
    ]


def _chunks(segments: list[dict]):
    start = 0
    while start < len(segments):
        end, size = start, 0
        while end < len(segments) and (end == start or size + len(segments[end]["text"]) <= CHUNK_CHARS):
            size += len(segments[end]["text"])
            end += 1
        # Neighbours are context only; each segment is edited in exactly one chunk.
        yield start, end, [
            {"segment_index": i, "speaker": segments[i].get("speaker"), "text": segments[i]["text"]}
            for i in range(max(0, start - 2), min(len(segments), end + 2))
        ]
        start = end


def _review_segments(segments: list[dict]) -> tuple[list[dict], list[tuple[int, int]]]:
    """Split oversized imported turns without changing their stored boundaries."""
    pieces, locations = [], []
    limit = max(1000, CHUNK_CHARS // 2)
    for index, segment in enumerate(segments):
        text = segment["text"]
        offset = 0
        while offset < len(text):
            end = min(len(text), offset + limit)
            if end < len(text):
                boundary = text.rfind(" ", offset + limit // 2, end)
                if boundary >= 0:
                    end = boundary + 1
            pieces.append({"speaker": segment.get("speaker"), "text": text[offset:end]})
            locations.append((index, offset))
            offset = end
    return pieces, locations


def _remap_mention(item: dict, pieces: list[dict], locations: list[tuple[int, int]], segments: list[dict]) -> dict | None:
    index, occurrence, original = item.get("segment_index"), item.get("occurrence"), item.get("original")
    if (type(index) is not int or not 0 <= index < len(pieces) or type(occurrence) is not int
            or occurrence < 0 or not isinstance(original, str) or not original or len(original) > 80):
        return None
    pattern = r"(?<![\w-])" + re.escape(original) + r"(?![\w-])"
    local = list(re.finditer(pattern, pieces[index]["text"]))
    if occurrence >= len(local):
        return None
    source_index, offset = locations[index]
    start = offset + local[occurrence].start()
    for source_occurrence, match in enumerate(re.finditer(pattern, segments[source_index]["text"])):
        if match.start() == start:
            return {**item, "segment_index": source_index, "occurrence": source_occurrence}
    return None


async def _request(client, settings: dict, participants: list[dict], segments: list[dict]) -> list:
    kwargs: dict[str, Any] = {
        "model": get_chat_model(), "temperature": 0,
        "messages": [
            {"role": "system", "content": settings["name_review_prompt"]},
            {"role": "user", "content": json.dumps({"participants": participants, "segments": segments}, ensure_ascii=False)},
        ],
        **_chat_kwargs(),
    }
    try:
        response = await client.chat.completions.create(**kwargs, response_format={"type": "json_object"})
    except BadRequestError as exc:
        if "response_format" not in str(exc).lower() and "json" not in str(exc).lower():
            raise
        response = await client.chat.completions.create(**kwargs)
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("mentions"), list):
        raise ValueError("Неверный формат проверки имён")
    return data["mentions"]


def apply_mentions(document: dict, mentions: list, participants: list[dict]) -> list[dict]:
    """Apply only validated, non-overlapping, occurrence-specific name patches."""
    ids = {p["id"] for p in participants}
    allowed = {p["id"]: {p["name"].casefold(), *(part.casefold() for part in p["name"].split())}
               for p in participants}
    segments = document["segments"]
    candidates: list[dict] = []
    for item in mentions:
        if not isinstance(item, dict):
            continue
        index, occurrence = item.get("segment_index"), item.get("occurrence")
        original, decision = item.get("original"), item.get("decision")
        if (type(index) is not int or not 0 <= index < len(segments)
                or type(occurrence) is not int or occurrence < 0
                or not isinstance(original, str) or len(original) > 80 or not NAME.fullmatch(original)
                or decision not in {"correct", "confirmed", "external", "uncertain"}):
            continue
        matches = list(re.finditer(r"(?<![\w-])" + re.escape(original) + r"(?![\w-])", segments[index]["text"]))
        if occurrence >= len(matches):
            continue
        match = matches[occurrence]
        replacement = item.get("replacement")
        reason = item.get("reason")
        participant_id = item.get("participant_id")
        entry = {
            "segment_index": index, "original": original, "occurrence": occurrence,
            "start": match.start(), "end": match.end(), "decision": decision,
            "replacement": None, "participant_id": participant_id if isinstance(participant_id, str) and participant_id in ids else None,
            "reason": reason[:1000] if isinstance(reason, str) else "",
        }
        if decision == "correct":
            if (entry["participant_id"] is not None and item.get("confidence") == "high"
                    and entry["reason"].strip() and isinstance(replacement, str)
                    and len(replacement) <= 80 and NAME.fullmatch(replacement) and replacement != original
                    and replacement.casefold() in allowed[entry["participant_id"]]
                    and all(word[0].isupper() for word in original.split())):
                entry["replacement"] = replacement
            else:
                entry["decision"] = "uncertain"
                entry["reason"] = "Предложенная замена не прошла проверку; имя оставлено без изменений."
        candidates.append(entry)
    # Conflicting proposals must never silently choose a winner.
    for entry in candidates:
        if any(other is not entry and other["segment_index"] == entry["segment_index"]
               and other["start"] < entry["end"] and entry["start"] < other["end"] for other in candidates):
            entry.update(decision="uncertain", replacement=None, reason="Противоречивые предложения; имя оставлено без изменений.")
    for entry in sorted(candidates, key=lambda e: (e["segment_index"], e["start"]), reverse=True):
        if entry["decision"] != "correct":
            continue
        segment = segments[entry["segment_index"]]
        segment.setdefault("original_text", segment["text"])
        text = segment["text"]
        segment["text"] = text[:entry["start"]] + entry["replacement"] + text[entry["end"]:]
    return candidates


async def review_names(client, document: dict, *, on_progress=None) -> dict:
    settings = prompts.snapshot()
    result = copy.deepcopy(document)
    # Every new check starts from the preserved source, not a previous LLM correction.
    for segment in result.get("segments") or []:
        if "original_text" in segment:
            segment["text"] = segment["original_text"]
    participants = named_participants(result)
    source = {"validator_version": 2, "speakers": participants, "segments": [{k: v for k, v in s.items() if k != "original_text"} for s in result.get("segments") or []],
              "prompt": settings["name_review_prompt"], "enabled": settings["name_review_enabled"],
              "model": get_chat_model()}
    fingerprint = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    previous = document.get("name_review") or {}
    if previous.get("fingerprint") == fingerprint and previous.get("status") == "done":
        return copy.deepcopy(document)
    report = {"status": "skipped", "mentions": [], "prompt_version": settings["version"],
              "checked_at": store.utc_now(), "fingerprint": fingerprint}
    result["name_review"] = report
    if not settings["name_review_enabled"]:
        report["message"] = "Проверка имён отключена администратором."
        return result
    if not participants or not result.get("segments"):
        report["message"] = "Для проверки имён сначала укажите имена спикеров."
        return result
    mentions: list = []
    try:
        pieces, locations = _review_segments(result["segments"])
        for start, end, context in _chunks(pieces):
            if on_progress:
                on_progress(f"Проверяю имена · фрагменты {start + 1}–{end} из {len(pieces)}")
            received = await _request(client, settings, participants, context)
            if on_progress:
                on_progress("Проверяю имена · проверяю предложенные исправления")
            for item in received:
                if isinstance(item, dict) and type(item.get("segment_index")) is int and start <= item["segment_index"] < end:
                    mapped = _remap_mention(item, pieces, locations, result["segments"])
                    if mapped:
                        mentions.append(mapped)
        report["mentions"] = apply_mentions(result, mentions, participants)
        report["status"] = "done"
        count = sum(m["decision"] == "correct" for m in report["mentions"])
        report["message"] = f"Проверка имён завершена. Исправлений: {count}."
    except control.ProcessingStopped:
        raise
    except Exception:
        log.exception("Проверка имён не удалась; протокол будет построен по исходному тексту")
        report.update(status="error", message="Не удалось проверить имена. Протокол составлен по исходному тексту.")
    return result
