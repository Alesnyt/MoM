from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
from pathlib import Path

from . import config


class AudioError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int | None = None) -> str:
    limit = timeout if timeout is not None else config.FFMPEG_TIMEOUT_SECONDS
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg не уложился в {limit} с") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise AudioError(err[-2000:])
    return (result.stdout or "").strip()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def duration_seconds(path: Path) -> float:
    out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    try:
        value = float(out)
    except ValueError as exc:
        raise AudioError(f"Не удалось определить длительность файла: {out}") from exc
    if not math.isfinite(value) or value <= 0:
        raise AudioError("В записи не найдена звуковая дорожка")
    return value


def extract_audio(src: Path, dst: Path) -> float:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(dst),
        ]
    )
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("Не удалось извлечь аудио из webm")
    return duration_seconds(dst)


def extract_wav(src: Path, dst: Path) -> float:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("Не удалось подготовить WAV для распознавания")
    return duration_seconds(dst)


def split_wav(src: Path, dest_dir: Path, chunk_seconds: int) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = dest_dir / "chunk_%03d.wav"
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(pattern),
        ]
    )
    chunks = sorted(dest_dir.glob("chunk_*.wav"))
    if not chunks:
        raise AudioError("Не удалось разрезать аудио на фрагменты")
    return chunks


def split_audio(src: Path, dest_dir: Path, chunk_seconds: int) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = dest_dir / "chunk_%03d.mp3"
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-c",
            "copy",
            str(pattern),
        ]
    )
    chunks = sorted(dest_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise AudioError("Не удалось разрезать аудио на фрагменты")
    return chunks


async def extract_audio_async(src: Path, dst: Path) -> float:
    return await asyncio.to_thread(extract_audio, src, dst)


async def split_audio_async(src: Path, dest_dir: Path, chunk_seconds: int) -> list[Path]:
    return await asyncio.to_thread(split_audio, src, dest_dir, chunk_seconds)
