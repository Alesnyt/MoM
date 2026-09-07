from __future__ import annotations

import base64
import subprocess
import sys
from email.header import Header
from email.utils import formatdate
from pathlib import Path

from . import config


def email_subject(meeting: dict) -> str:
    result = meeting.get("result") or {}
    title = (result.get("title") or meeting.get("title") or "встреча").strip()
    return f"MoM: {title}"


def email_body(meeting: dict) -> str:
    result = meeting.get("result") or {}
    lines: list[str] = []
    lines.append(f"Саммари встречи: {result.get('title') or meeting.get('title') or ''}")
    lines.append("")
    if result.get("date_hint"):
        lines.append(f"Дата: {result['date_hint']}")
    participants = result.get("participants") or []
    if participants:
        lines.append(f"Участники: {', '.join(participants)}")
    lines.append("")
    lines.append((result.get("summary") or "").strip() or "Саммари отсутствует.")
    key_points = result.get("key_points") or []
    if key_points:
        lines.append("")
        lines.append("Ключевые тезисы:")
        lines.extend(f"• {item}" for item in key_points)
    decisions = result.get("decisions") or []
    if decisions:
        lines.append("")
        lines.append("Решения:")
        lines.extend(f"• {item}" for item in decisions)
    lines.append("")
    lines.append("Поручения:")
    actions = result.get("action_items") or []
    if not actions:
        lines.append("Поручений не зафиксировано.")
    else:
        for item in actions:
            who = item.get("assignee") or "не назначен"
            due = item.get("due") or "без срока"
            lines.append(f"• {item.get('task')} — {who}; срок: {due}")
    next_meeting = (result.get("mom") or {}).get("next_meeting")
    if next_meeting:
        lines.append("")
        lines.append(f"Следующая встреча: {next_meeting}")
    return "\n".join(lines).strip() + "\n"


def build_eml(subject: str, body: str) -> bytes:
    payload = base64.encodebytes(body.replace("\n", "\r\n").encode("utf-8")).decode("ascii")
    text = "\r\n".join(
        [
            "From: MoM <mom@localhost>",
            "To: ",
            f"Date: {formatdate(localtime=True)}",
            f"Subject: {Header(subject, 'utf-8')}",
            "MIME-Version: 1.0",
            "Content-Type: text/plain; charset=UTF-8",
            "Content-Transfer-Encoding: base64",
            "X-Unsent: 1",
            "",
            payload.strip(),
            "",
        ]
    )
    return text.encode("utf-8")


def _slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:60] or "mom"


def open_or_save_eml(meeting: dict) -> dict[str, str]:
    config.ensure_dirs()
    export_dir = config.DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    result = meeting.get("result") or {}
    path = export_dir / f"{_slug(result.get('title') or meeting.get('title') or 'mom')}.eml"
    path.write_bytes(build_eml(email_subject(meeting), email_body(meeting)))
    config.restrict_path(path, 0o600)
    opened = _open_local_eml(path)
    return {"mode": opened}


def _open_local_eml(path: Path) -> str:
    if sys.platform != "darwin":
        return "saved"
    subprocess.run(["xattr", "-c", str(path)], capture_output=True, check=False)
    for app, mode in (("Microsoft Outlook", "opened-outlook"), ("Mail", "opened-mail")):
        proc = subprocess.run(["open", "-a", app, str(path)], capture_output=True, check=False)
        if proc.returncode == 0:
            return mode
    subprocess.run(["open", str(path)], capture_output=True, check=False)
    return "opened"
