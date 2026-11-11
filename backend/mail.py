from __future__ import annotations

import base64
import logging
import smtplib
import subprocess
import sys
import time
from email.header import Header
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from . import config, retention, store
from .speakers import SpeakerError, clean_speaker_email

log = logging.getLogger("mom.mail")
RETRY_DELAYS = (30, 120, 600)
CLAIM_SECONDS = 300


class TemporaryMailError(RuntimeError):
    pass


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
    client: smtplib.SMTP | None = None
    try:
        try:
            if port == 465:
                client = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                client = smtplib.SMTP(host, port, timeout=20)
                if config.get_smtp_starttls():
                    client.ehlo()
                    client.starttls()
                    client.ehlo()
            if user:
                client.login(user, password)
        except (OSError, smtplib.SMTPServerDisconnected) as exc:
            if isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code >= 500:
                raise
            raise TemporaryMailError("Не удалось открыть соединение SMTP") from exc
        try:
            client.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            if any(400 <= code < 500 for code, _ in exc.recipients.values()):
                raise TemporaryMailError("SMTP временно отклонил получателя") from exc
            raise
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise TemporaryMailError("SMTP временно отклонил письмо") from exc
            raise
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                client.close()


def _protocol_messages(meeting: dict) -> list[tuple[str, str, str]]:
    if not config.smtp_configured():
        return []
    user_id = meeting.get("user_id")
    user = store.get_user(user_id) if user_id else None
    candidates = [(user or {}).get("email") or ""]
    candidates.extend(
        speaker.get("email") or "" for speaker in meeting.get("speakers") or []
        if speaker.get("send_protocol") is True and speaker.get("id") != "unknown"
    )
    recipients: list[str] = []
    for address in candidates:
        if not address:
            continue
        try:
            normalized = clean_speaker_email(address)
        except SpeakerError:
            log.warning("Некорректный адрес получателя по встрече %s", meeting.get("id"))
            continue
        if normalized not in recipients:
            recipients.append(normalized)
    extra = ""
    public = config.get_public_base_url()
    if public:
        extra = f"\n\nОткрыть в MoM: {public}/"
    body = (
        "Встреча расшифрована, протокол готов.\n\n"
        + email_body(meeting)
        + extra
    )
    subject = email_subject(meeting)
    return [(to, subject, body) for to in recipients]


def complete_protocol(meeting: dict, **fields) -> None:
    meeting_id = meeting["id"]
    user_id = meeting.get("user_id")
    messages = _protocol_messages({**meeting, **fields})
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        store.update_meeting(
            meeting_id, connection=conn, **fields, status="done", error=None, progress=100,
            status_message="Готово, письма в очереди" if messages else "Готово",
        )
        conn.execute("DELETE FROM protocol_mail WHERE meeting_id = ?", (meeting_id,))
        conn.executemany(
            "INSERT INTO protocol_mail (meeting_id, recipient, subject, body) VALUES (?, ?, ?, ?)",
            [(meeting_id, *message) for message in messages],
        )
        removed = store.prune_user_meetings(user_id, connection=conn) if user_id else []
    retention.remove_pruned(removed)


def _delivery_status(conn, meeting_id: str) -> bool:
    counts = dict(conn.execute(
        "SELECT state, COUNT(*) FROM protocol_mail WHERE meeting_id = ? GROUP BY state", (meeting_id,),
    ))
    total = sum(counts.values())
    sent = counts.get("sent", 0)
    pending = bool(counts.get("pending") or counts.get("sending"))
    if sent == total:
        message = "Готово, протокол отправлен на почту"
    else:
        message = f"Готово. Отправлено писем: {sent} из {total}."
        if pending:
            message += " Остальные письма в очереди."
        if counts.get("failed"):
            message += " Есть ошибки доставки."
        if counts.get("unknown"):
            message += " Для некоторых писем SMTP не подтвердил отправку."
    conn.execute("UPDATE meetings SET status_message = ? WHERE id = ? AND status = 'done'", (message, meeting_id))
    return pending


def deliver_pending() -> bool:
    if not config.smtp_configured():
        return False
    now = time.time()
    removed = []
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        uncertain = conn.execute(
            """UPDATE protocol_mail SET state = 'unknown', subject = '', body = ''
               WHERE state = 'sending' AND due_at <= ? RETURNING meeting_id""",
            (now,),
        ).fetchall()
        for meeting_id in {entry["meeting_id"] for entry in uncertain}:
            if not _delivery_status(conn, meeting_id):
                owner = conn.execute("SELECT user_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
                if owner and owner["user_id"]:
                    removed.extend(store.prune_user_meetings(owner["user_id"], connection=conn))
        row = conn.execute(
            """SELECT mail.*, meetings.user_id FROM protocol_mail AS mail
               JOIN meetings ON meetings.id = mail.meeting_id AND meetings.status = 'done'
               WHERE mail.state = 'pending' AND mail.due_at <= ? ORDER BY mail.due_at, mail.id LIMIT 1""", (now,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE protocol_mail SET state = 'sending', attempts = attempts + 1, due_at = ? WHERE id = ?",
                (now + CLAIM_SECONDS, row["id"]),
            )
    retention.remove_pruned(removed)
    if not row:
        return False
    state = "sent"
    due_at = 0
    try:
        send_mail(row["recipient"], row["subject"], row["body"])
    except TemporaryMailError:
        state = "failed"
        if row["attempts"] < len(RETRY_DELAYS):
            state = "pending"
            due_at = time.time() + RETRY_DELAYS[row["attempts"]]
    except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
        state = "failed"
    except (OSError, smtplib.SMTPServerDisconnected):
        state = "unknown"
    except Exception:
        state = "failed"
        log.exception("Не удалось отправить протокол встречи %s", row["meeting_id"])

    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        keep_message = state == "pending"
        result = conn.execute(
            "UPDATE protocol_mail SET state = ?, due_at = ?, subject = ?, body = ? WHERE id = ? AND state = 'sending'",
            (state, due_at, row["subject"] if keep_message else "", row["body"] if keep_message else "", row["id"]),
        )
        removed = []
        if result.rowcount:
            if not _delivery_status(conn, row["meeting_id"]) and row["user_id"]:
                removed = store.prune_user_meetings(row["user_id"], connection=conn)
    if result.rowcount:
        detail = {"sent": "автоматически", "pending": "временный отказ, будет повтор",
                  "failed": "SMTP не принял письмо", "unknown": "результат отправки неизвестен"}[state]
        store.record_audit(row["recipient"], "email.send" if state == "sent" else "email.fail", row["meeting_id"], detail)
    retention.remove_pruned(removed)
    return True
