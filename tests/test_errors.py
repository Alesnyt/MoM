from __future__ import annotations

from pathlib import Path

from backend.audio import looks_like_audio
from backend.mail import email_body
from backend.pipeline import public_error
from backend.audio import AudioError


def test_looks_like_webm(tmp_path: Path) -> None:
    path = tmp_path / "a.webm"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 20)
    assert looks_like_audio(path)


def test_looks_like_mp4(tmp_path: Path) -> None:
    path = tmp_path / "a.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8)
    assert looks_like_audio(path)


def test_rejects_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "a.webm"
    path.write_bytes(b"this is not audio at all, really")
    assert looks_like_audio(path) is False


def test_public_error_hides_ffmpeg() -> None:
    assert "tmp" not in public_error(AudioError("Не удалось обработать аудио. Проверьте, что файл — запись, а не повреждённый контейнер."))
    assert "журнал" in public_error(RuntimeError("Traceback /tmp/secret.wav boom"))


def test_email_body_source() -> None:
    meeting = {
        "title": "Стендап",
        "result": {
            "title": "Стендап",
            "summary": "Коротко.",
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "mom": {},
        },
    }
    body = email_body(meeting)
    assert "Саммари встречи: Стендап" in body
    assert "Коротко." in body
