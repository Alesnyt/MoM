from __future__ import annotations

import asyncio
import logging
import math
import shutil
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger("mom.audio")


class AudioError(RuntimeError):
    pass


def looks_like_audio(path: Path) -> bool:
    try:
        data = path.read_bytes()[:64]
    except OSError:
        return False
    if len(data) < 12:
        return False
    if data[:4] in {b"RIFF", b"OggS", b"fLaC"}:
        return True
    if data[:4] == b"\x1aE\xdf\xa3":
        return True
    if data[:3] == b"ID3":
        return True
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    return data[4:8] == b"ftyp"


def _run(cmd: list[str], timeout: int | None = None) -> str:
    limit = timeout if timeout is not None else config.FFMPEG_TIMEOUT_SECONDS
    tool = Path(cmd[0]).name if cmd else "ffmpeg"
    target = Path(cmd[-1]).name if cmd else ""
    if tool == "ffmpeg":
        log.info("ffmpeg → %s", target)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired as exc:
        log.error("ffmpeg timeout after %s s: %s → %s", limit, tool, target)
        raise AudioError(f"ffmpeg не уложился в {limit} с") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        log.error("ffmpeg failed (%s) → %s: %s", result.returncode, target, err[-4000:])
        raise AudioError(
            "Не удалось обработать аудио. Проверьте, что файл — запись, а не повреждённый контейнер."
        )
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
        log.error("ffprobe duration parse failed for %s: %s", path.name, out)
        raise AudioError("Не удалось определить длительность записи") from exc
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


def cut_wav(src: Path, dst: Path, start: float, end: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.0, end - start)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{duration:.3f}",
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
        raise AudioError("Не удалось вырезать фрагмент по границе спикера")


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
