from __future__ import annotations

import base64
import logging
import smtplib
import subprocess
import sys
from email.header import Header
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from . import config

log = logging.getLogger("mom.mail")


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


def smtp_snapshot(*, full: bool = False) -> dict:
    configured = config.smtp_configured()
    data = {"configured": configured}
    if not full:
        return data
    data.update(
        {
            "host": config.get_smtp_host() or None,
            "port": config.get_smtp_port(),
            "user": config.get_smtp_user() or None,
            "from_addr": config.get_smtp_from() or None,
            "starttls": config.get_smtp_starttls(),
            "has_password": bool(config.get_smtp_password()),
            "public_url": config.get_public_base_url() or None,
        }
    )
    return data


def send_mail(to: str, subject: str, body: str) -> None:
    host = config.get_smtp_host()
    from_addr = config.get_smtp_from()
    if not host or not from_addr:
        raise RuntimeError("Сначала укажите SMTP-сервер и адрес отправителя")
    if not to or "@" not in to:
        raise RuntimeError("Нет адреса получателя")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to
    message.set_content(body)

    port = config.get_smtp_port()
    user = config.get_smtp_user()
    password = config.get_smtp_password()
    if port == 465:
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
        if config.get_smtp_starttls():
            client.ehlo()
            client.starttls()
            client.ehlo()
    try:
        if user:
            client.login(user, password)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def notify_meeting_done(meeting: dict) -> None:
    if meeting.get("status") != "done":
        return
    if not config.smtp_configured():
        return
    from . import store

    user_id = meeting.get("user_id")
    user = store.get_user(user_id) if user_id else None
    to = (user or {}).get("email") or ""
    if not to:
        log.warning("Некому отправить письмо по встрече %s", meeting.get("id"))
        return
    extra = ""
    public = config.get_public_base_url()
    if public:
        extra = f"\n\nОткрыть в MoM: {public}/"
    body = (
        "Встреча расшифрована, протокол готов.\n\n"
        + email_body(meeting)
        + extra
    )
    try:
        send_mail(to, email_subject(meeting), body)
    except Exception as exc:  # noqa: BLE001 — don't fail the job if mail is down
        log.warning("Не отправилось письмо: %s", exc)
        store.record_audit(to, "email.fail", meeting.get("id") or "", "SMTP не принял письмо")
        store.update_meeting(
            meeting["id"],
            status_message="Готово. Письмо не отправилось — проверьте SMTP в админке",
        )
        return
    store.record_audit(to, "email.send", meeting.get("id") or "", "автоматически")
    store.update_meeting(meeting["id"], status_message="Готово, протокол отправлен на почту")
