from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("mom.speakers")


class SpeakerError(ValueError):
    pass


UNKNOWN_ID = "unknown"
UNKNOWN_NAME = "Неясно"
# A GigaAM chunk is 24s. Shorter ASR phrases stay whole.
COARSE_SECONDS = 8.0
MIN_SLICE_SECONDS = 0.45


def plan_cuts(segment: dict[str, Any], turns: list[dict[str, Any]]) -> list[dict[str, float]] | None:
    """Time slices of a long chunk that contains more than one voice.

    Returns None when the chunk should stay one phrase: it is short, or only
    one speaker occupies it.
    """
    start = float(segment.get("start") or 0)
    end = float(segment.get("end") or start)
    if end - start < COARSE_SECONDS:
        return None
    slices: list[dict[str, Any]] = []
    for turn in turns:
        if not turn.get("speaker"):
            continue
        left = max(start, float(turn.get("start") or 0))
        right = min(end, float(turn.get("end") or 0))
        if right - left < MIN_SLICE_SECONDS:
            continue
        slices.append({"start": left, "end": right, "speaker": str(turn["speaker"])})
    slices.sort(key=lambda item: (item["start"], item["end"]))
    merged: list[dict[str, Any]] = []
    for item in slices:
        if (
            merged
            and merged[-1]["speaker"] == item["speaker"]
            and item["start"] - merged[-1]["end"] <= 0.3
        ):
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        else:
            merged.append(dict(item))
    voices = {item["speaker"] for item in merged}
    if len(voices) < 2:
        return None
    return [{"start": item["start"], "end": item["end"]} for item in merged]


def expand_coarse_segments(
    segments: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    transcribe,
) -> list[dict[str, Any]]:
    """Replace a long mixed chunk with one phrase per voice boundary."""
    expanded: list[dict[str, Any]] = []
    for segment in segments:
        cuts = plan_cuts(segment, turns)
        if not cuts:
            expanded.append(segment)
            continue
        pieces: list[dict[str, Any]] = []
        for cut in cuts:
            try:
                text = str(transcribe(cut["start"], cut["end"]) or "").strip()
            except Exception:
                log.exception("Не удалось распознать фрагмент %.2f–%.2f", cut["start"], cut["end"])
                pieces = []
                break
            if text:
                pieces.append({"start": cut["start"], "end": cut["end"], "text": text})
        expanded.extend(pieces or [segment])
    return expanded


def assign_speakers(segments: list[dict[str, Any]], turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach anonymous speaker ids to ASR segments by maximum time overlap."""
    clean_segments = [_segment(item) for item in segments if (item.get("text") or "").strip()]
    clean_turns = [_turn(item) for item in turns if item.get("speaker")]
    labels = _stable_labels(clean_turns)
    for turn in clean_turns:
        turn["speaker"] = labels[str(turn["speaker"])]
    speakers = [{"id": sid, "name": f"Спикер {sid[1:]}"} for sid in _unique([turn["speaker"] for turn in clean_turns])]
    if not speakers:
        speakers = [{"id": "S1", "name": "Спикер 1"}]
    fallback = speakers[0]["id"]
    assigned: list[dict[str, Any]] = []
    for segment in clean_segments:
        totals: dict[str, float] = {}
        for turn in clean_turns:
            overlap = _overlap(segment["start"], segment["end"], turn["start"], turn["end"])
            if overlap <= 0:
                continue
            totals[turn["speaker"]] = totals.get(turn["speaker"], 0.0) + overlap
        ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if not clean_turns:
            best_id = fallback
        elif not ranked or ranked[0][1] < MIN_SLICE_SECONDS:
            best_id = UNKNOWN_ID
        elif len(ranked) == 1 or ranked[1][1] < MIN_SLICE_SECONDS or ranked[0][1] >= ranked[1][1] * 1.8:
            best_id = ranked[0][0]
        else:
            best_id = UNKNOWN_ID
        assigned.append({**segment, "speaker": best_id})
    if any(item["speaker"] == UNKNOWN_ID for item in assigned):
        speakers.append({"id": UNKNOWN_ID, "name": UNKNOWN_NAME})
    speakers.sort(key=_speaker_sort_key)
    return {"speakers": speakers, "segments": assigned, "note": None}


def render_transcript(document: dict[str, Any]) -> str:
    names = {item["id"]: item["name"] for item in document.get("speakers") or []}
    lines: list[str] = []
    for segment in document.get("segments") or []:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0)
        minutes, seconds = divmod(int(start), 60)
        name = names.get(segment.get("speaker"), "Спикер")
        lines.append(f"[{minutes:02d}:{seconds:02d}] {name}: {text}")
    return "\n".join(lines)


def rename_speaker(document: dict[str, Any], speaker_id: str, name: str) -> tuple[dict[str, Any], str, str]:
    speakers = [dict(item) for item in document.get("speakers") or []]
    target = next((item for item in speakers if item["id"] == speaker_id), None)
    if not target:
        raise SpeakerError("Такого спикера нет")
    new_name = _clean_name(name)
    if any(item["id"] != speaker_id and item["name"].casefold() == new_name.casefold() for item in speakers):
        raise SpeakerError("Такое имя уже есть у другого спикера")
    old_name = target["name"]
    target["name"] = new_name
    updated = {**document, "speakers": speakers}
    return updated, old_name, new_name


def reassign_segments(document: dict[str, Any], indexes: list[int], speaker_id: str) -> dict[str, Any]:
    segments = [dict(item) for item in document.get("segments") or []]
    speakers = [dict(item) for item in document.get("speakers") or []]
    if not indexes:
        raise SpeakerError("Не выбрана реплика")
    if any(index < 0 or index >= len(segments) for index in indexes):
        raise SpeakerError("Реплика не найдена")
    target = speaker_id
    if target == "new":
        target = _next_id(speakers)
        speakers.append({"id": target, "name": f"Спикер {target[1:]}"})
    elif target not in {item["id"] for item in speakers}:
        raise SpeakerError("Такого спикера нет")
    for index in indexes:
        segments[index]["speaker"] = target
    return {**document, "speakers": speakers, "segments": segments}


def _replace_name(text: str, old: str, new: str) -> str:
    if re.fullmatch(r"Спикер \d+", old):
        return re.sub(re.escape(old) + r"(?!\d)", new, text)
    pattern = rf"(?<![\w\-]){re.escape(old)}(?![\w\-])"
    return re.sub(pattern, new, text)


def replace_speaker_label(value: Any, old: str, new: str) -> Any:
    if not old or old == new:
        return value
    if isinstance(value, str):
        return _replace_name(value, old, new)
    if isinstance(value, list):
        return [replace_speaker_label(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_speaker_label(item, old, new) for key, item in value.items()}
    return value


def _segment(item: dict[str, Any]) -> dict[str, Any]:
    start = float(item.get("start") or 0)
    end = float(item.get("end") or start)
    if end < start:
        end = start
    return {"start": start, "end": end, "text": str(item.get("text") or "").strip()}


def _turn(item: dict[str, Any]) -> dict[str, Any]:
    start = float(item.get("start") or 0)
    end = float(item.get("end") or start)
    return {"start": start, "end": max(end, start), "speaker": str(item["speaker"])}


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _stable_labels(turns: list[dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    number = 1
    for turn in sorted(turns, key=lambda item: (item["start"], item["end"])):
        raw = str(turn["speaker"])
        if raw not in labels:
            labels[raw] = f"S{number}"
            number += 1
    return labels


def _speaker_sort_key(item: dict[str, Any]) -> tuple[int, int | str]:
    sid = str(item["id"])
    if sid.startswith("S") and sid[1:].isdigit():
        return (0, int(sid[1:]))
    return (1, sid)


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _next_id(speakers: list[dict[str, Any]]) -> str:
    taken = {item["id"] for item in speakers}
    number = 1
    while f"S{number}" in taken:
        number += 1
    return f"S{number}"


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise SpeakerError("Введите имя спикера")
    if len(cleaned) > 80:
        raise SpeakerError("Имя длиннее 80 символов")
    return cleaned
