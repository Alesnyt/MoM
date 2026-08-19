from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any

from . import store
from .analyze import (
    analyze_long_transcript,
    format_transcript,
    make_client,
    merge_transcripts,
    transcribe_file,
)
from . import config
from .audio import extract_audio_async, split_audio_async
from .config import AUDIO_DIR, CHUNK_SECONDS, MAX_WHISPER_BYTES, UPLOAD_DIR


class _Progress:
    def __init__(self, meeting_id: str) -> None:
        self.meeting_id = meeting_id
        self.last_pct = -1
        self.last_t = 0.0

    def set(self, percent: int, message: str, status: str | None = None, force: bool = False) -> None:
        percent = max(0, min(100, int(percent)))
        now = time.monotonic()
        if not force and percent < self.last_pct + 1 and now - self.last_t < 0.7:
            return
        self.last_pct = percent
        self.last_t = now
        fields: dict[str, Any] = {"progress": percent, "status_message": message}
        if status:
            fields["status"] = status
        store.update_meeting(self.meeting_id, **fields)


def _clock(seconds: int) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


async def _analyze_with_heartbeat(client, transcript: str, title: str, progress: _Progress, prefix: str):
    start = time.monotonic()
    stop = asyncio.Event()

    async def beat() -> None:
        while not stop.is_set():
            elapsed = int(time.monotonic() - start)
            creep = min(8, elapsed // 20)
            progress.set(
                88 + creep,
                f"{prefix} · {_clock(elapsed)}",
                status="analyzing",
                force=True,
            )
            try:
                await asyncio.wait_for(stop.wait(), 3.0)
            except TimeoutError:
                continue

    task = asyncio.create_task(beat())
    try:
        def on_part(index: int, total: int) -> None:
            elapsed = int(time.monotonic() - start)
            base = 88 + int((index - 1) / max(1, total) * 10)
            progress.set(
                min(97, base),
                f"Qwen: часть {index} из {total} · {_clock(elapsed)}",
                status="analyzing",
                force=True,
            )

        return await analyze_long_transcript(client, transcript, title, on_part=on_part)
    finally:
        stop.set()
        await task


async def process_meeting(meeting_id: str) -> None:
    meeting = store.get_meeting(meeting_id)
    if not meeting:
        return
    api_key = config.get_api_key()
    if not api_key:
        store.update_meeting(
            meeting_id,
            status="error",
            status_message="Нет ключа OpenAI",
            error="Сохраните ключ в разделе «Администрирование»",
        )
        return

    src = UPLOAD_DIR / f"{meeting_id}{Path(meeting['filename']).suffix.lower() or '.webm'}"
    if not src.exists():
        src = next(UPLOAD_DIR.glob(f"{meeting_id}.*"), None)
    if not src or not src.exists():
        store.update_meeting(
            meeting_id,
            status="error",
            status_message="Исходный файл не найден",
            error="Запись исчезла с диска до начала обработки",
        )
        return

    audio_path = AUDIO_DIR / f"{meeting_id}.mp3"
    client = make_client(api_key)
    progress = _Progress(meeting_id)

    try:
        progress.set(5, "Извлекаю звуковую дорожку", status="extracting", force=True)
        duration = await extract_audio_async(src, audio_path)
        store.update_meeting(meeting_id, duration_seconds=duration)
        progress.set(15, "Аудио готово, запускаю Whisper", status="transcribing", force=True)

        payload = await _transcribe(client, audio_path, meeting_id, duration, progress)
        transcript = format_transcript(payload)
        if not transcript:
            raise RuntimeError("Транскрипт пустой — в записи нет распознанной речи")

        store.update_meeting(
            meeting_id,
            transcript=transcript,
            language=payload.get("language"),
        )
        parts = max(1, (len(transcript) + 79_999) // 80_000)
        if parts == 1:
            prefix = "Qwen пишет протокол"
        elif parts < 5:
            prefix = f"Qwen пишет протокол ({parts} запроса)"
        else:
            prefix = f"Qwen пишет протокол ({parts} запросов)"
        progress.set(88, f"{prefix} · 0:00", status="analyzing", force=True)
        result = await _analyze_with_heartbeat(client, transcript, meeting["title"], progress, prefix)
        title = (result.get("title") or meeting["title"]).strip() or meeting["title"]
        store.update_meeting(
            meeting_id,
            title=title,
            result=result,
            language=result.get("language") or payload.get("language"),
            status="done",
            status_message="Готово",
            error=None,
            progress=100,
        )
    except Exception as exc:  # noqa: BLE001 — surface any pipeline failure in UI
        store.update_meeting(
            meeting_id,
            status="error",
            status_message="Обработка не удалась",
            error=str(exc),
        )


async def _transcribe(client, audio_path: Path, meeting_id: str, duration: float, progress: _Progress) -> dict:
    asr_model = config.get_asr_model()

    def on_whisper(pct: int, message: str) -> None:
        overall = 16 + int(max(0, min(100, pct)) * 0.70)
        progress.set(overall, message, status="transcribing")

    if config.is_local_asr(asr_model):
        progress.set(16, f"Расшифровываю локально (Whisper {config.local_whisper_size()})", status="transcribing", force=True)
        return await transcribe_file(
            client,
            audio_path,
            asr_model,
            duration=duration,
            on_progress=on_whisper,
        )

    limit = 6 * 1024 * 1024 if config.is_qwen() else MAX_WHISPER_BYTES
    chunk_seconds = 4 * 60 if config.is_qwen() else CHUNK_SECONDS
    size = audio_path.stat().st_size
    if size <= limit:
        return await transcribe_file(
            client,
            audio_path,
            asr_model,
            duration=duration,
            on_progress=on_whisper,
        )

    chunks_dir = AUDIO_DIR / f"{meeting_id}_chunks"
    chunks = await split_audio_async(audio_path, chunks_dir, chunk_seconds)
    merged = []
    for index, chunk in enumerate(chunks):
        base = 16 + int(index / max(1, len(chunks)) * 70)
        progress.set(
            base,
            f"Расшифровываю фрагмент {index + 1} из {len(chunks)}",
            status="transcribing",
            force=True,
        )
        offset = index * chunk_seconds
        if offset > duration:
            offset = max(0.0, duration - chunk_seconds)
        payload = await transcribe_file(client, chunk, asr_model)
        merged.append((float(offset), payload))
    return merge_transcripts(merged)


def format_duration(seconds: float | None) -> str:
    if not seconds or not math.isfinite(seconds):
        return ""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин {secs:02d} с"
