"""Bounded, deterministic import of speaker-labelled UTF-8 transcripts."""
from __future__ import annotations

import re
import unicodedata

from starlette.responses import JSONResponse

MAX_TEXT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_TEXT_BYTES + 64 * 1024
MAX_SEGMENTS = 10_000
MAX_SPEAKERS = 200
SUFFIXES = {".txt", ".md", ".srt", ".vtt"}
STAMP = r"\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
PREFIX = re.compile(rf"^\[?({STAMP})\]?\s+(.*)$")
CUE = re.compile(rf"^({STAMP})\s+-->\s+({STAMP})(?:\s+.*)?$")


class TranscriptError(ValueError):
    pass


class TranscriptBodyLimit:
    """Limit actual bytes before Starlette spools multipart files (even chunked)."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path", "").rstrip("/") != "/api/meetings/transcript":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                return await JSONResponse({"detail": "Транскрипт больше 1 МБ"}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    numbers = [float(part) for part in parts]
    if numbers[-1] >= 60 or (len(parts) == 3 and numbers[1] >= 60):
        raise TranscriptError(f"Некорректный таймкод: {value}")
    return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))


def parse_transcript(text: str, suffix: str = ".txt") -> dict:
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise TranscriptError("Транскрипт больше 1 МБ")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise TranscriptError("Файл содержит недопустимые управляющие символы")
    speakers, segments, labels = [], [], {}

    def add(content, start=None, end=None, *, continuation=False):
        content = content.strip()
        # Common Markdown speaker labels: **Name:** or **Name**:
        content = re.sub(r"^\*\*(.+?):\*\*\s*", r"\1: ", content)
        content = re.sub(r"^\*\*(.+?)\*\*:\s*", r"\1: ", content)
        content = re.sub(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$", r"\1: \2", content)
        match = re.match(r"^([^:\n]+):\s*(.*)$", content, re.S)
        if match:
            name = unicodedata.normalize("NFC", " ".join(match[1].split()))
            if len(name) > 80:
                raise TranscriptError("Имя спикера длиннее 80 символов")
            content = match[2].strip()
        elif continuation and segments:
            segments[-1].setdefault("_continuations", []).append(content)
            return
        else:
            name = "Неясно"
        if not content:
            raise TranscriptError("После имени спикера нужна реплика")
        key = name.casefold()
        if key not in labels:
            if len(speakers) >= MAX_SPEAKERS:
                raise TranscriptError("Слишком много спикеров (максимум 200)")
            sid = "unknown" if name == "Неясно" else f"S{len(speakers) + 1}"
            labels[key] = sid
            speakers.append({"id": sid, "name": name})
        if len(segments) >= MAX_SEGMENTS:
            raise TranscriptError("Слишком много реплик (максимум 10000)")
        segments.append({"speaker": labels[key], "text": content, "start": start, "end": end if end is not None else start})

    if suffix in {".srt", ".vtt"}:
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = block.splitlines()
            if not lines:
                continue
            if suffix == ".vtt" and (lines[0].startswith("WEBVTT") or lines[0].split(" ")[0] in {"NOTE", "STYLE", "REGION"}):
                continue
            cue_index = next((i for i, line in enumerate(lines[:2]) if CUE.fullmatch(line.strip())), None)
            if cue_index is None:
                raise TranscriptError("Не удалось прочитать таймкоды субтитров")
            cue = CUE.fullmatch(lines[cue_index].strip())
            start, end = timestamp(cue[1]), timestamp(cue[2])
            if end < start:
                raise TranscriptError("Конец реплики раньше начала")
            add("\n".join(lines[cue_index + 1:]), start, end)
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = PREFIX.fullmatch(line)
            if match:
                add(match[2], timestamp(match[1]))
            else:
                add(line, continuation=True)
    if not segments:
        raise TranscriptError("Транскрипт пуст")
    for segment in segments:
        segment["text"] = "\n".join([segment["text"], *segment.pop("_continuations", [])])
    return {"source_type": "text", "speakers": speakers, "segments": segments, "note": None}
