from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate

from . import config, protocol_email, retention, store, word_protocol
from .analyze import ProtocolResult
from .speakers import SpeakerError, clean_speaker_email, participant_labels

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
    participants = participant_labels(meeting, result)
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


@dataclass(frozen=True)
class ProtocolMail:
    body: str
    html: str
    filename: str
    docx: bytes


def prepare_protocol_mail(meeting: dict) -> ProtocolMail:
    result = ProtocolResult.model_validate(meeting["result"]).model_dump()
    filename = word_protocol.docx_filename(result["title"] or meeting["title"])
    public_url = protocol_email.meeting_url(config.get_public_base_url())
    body = email_body({**meeting, "result": result}) + f"\nВо вложении: {filename}\n"
    if public_url:
        body += f"\nОткрыть в MoM: {public_url}\n"
    return ProtocolMail(body, protocol_email.render_html(meeting, result, filename, public_url),
                        filename, word_protocol.to_docx(meeting, result))


def _make_message(
    subject: str, body: str, *, html: str | None = None, attachment: tuple[str, bytes] | None = None,
) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    message["Subject"] = " ".join(subject.splitlines())
    message.set_content(body, cte="base64")
    if html is not None:
        message.add_alternative(html, subtype="html", cte="base64")
    if attachment is not None:
        filename, data = attachment
        maintype, subtype = word_protocol.MEDIA_TYPE.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message


def build_eml(meeting: dict) -> bytes:
    packet = prepare_protocol_mail(meeting)
    message = _make_message(email_subject(meeting), packet.body, html=packet.html,
                            attachment=(packet.filename, packet.docx))
    message["From"] = "MoM <mom@localhost>"
    message["Date"] = formatdate(localtime=True)
    message["X-Unsent"] = "1"
    return message.as_bytes()


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


def send_mail(
    to: str, subject: str, body: str, *, html: str | None = None, attachment: tuple[str, bytes] | None = None,
) -> None:
    host = config.get_smtp_host()
    from_addr = config.get_smtp_from()
    if not host or not from_addr:
        raise RuntimeError("Сначала укажите SMTP-сервер и адрес отправителя")
    if not to or "@" not in to:
        raise RuntimeError("Нет адреса получателя")
    message = _make_message(subject, body, html=html, attachment=attachment)
    message["From"] = from_addr
    message["To"] = to

    port = config.get_smtp_port()
    user = config.get_smtp_user()
    password = config.get_smtp_password()
    client: smtplib.SMTP | None = None
    try:
        try:
            if port == 465:
                client = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=20)
                if config.get_smtp_starttls():
                    client.ehlo()
                    client.starttls(context=ssl.create_default_context())
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


def _protocol_recipients(meeting: dict) -> list[str]:
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
    return recipients


def complete_protocol(meeting: dict, **fields) -> None:
    meeting_id = meeting["id"]
    user_id = meeting.get("user_id")
    finished = {**meeting, **fields}
    recipients = _protocol_recipients(finished)
    packet = None
    status_message = "Готово"
    if recipients:
        try:
            packet = prepare_protocol_mail(finished)
            status_message = "Готово, письма в очереди"
        except Exception:
            log.exception("Не удалось подготовить письмо с протоколом встречи %s", meeting_id)
            status_message = "Готово. Не удалось подготовить письмо с протоколом."
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        store.update_meeting(
            meeting_id, connection=conn, **fields, status="done", error=None, progress=100,
            status_message=status_message,
        )
        conn.execute("DELETE FROM protocol_mail WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM protocol_mail_format WHERE meeting_id = ?", (meeting_id,))
        if packet is not None:
            conn.execute(
                "INSERT INTO protocol_mail_format (meeting_id, html, filename, docx) VALUES (?, ?, ?, ?)",
                (meeting_id, packet.html, packet.filename, packet.docx),
            )
            conn.executemany(
                "INSERT INTO protocol_mail (meeting_id, recipient, subject, body) VALUES (?, ?, ?, ?)",
                [(meeting_id, to, email_subject(finished), packet.body) for to in recipients],
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
    if not pending:
        conn.execute("DELETE FROM protocol_mail_format WHERE meeting_id = ?", (meeting_id,))
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
            """SELECT mail.*, meetings.user_id, content.html, content.filename, content.docx
               FROM protocol_mail AS mail
               JOIN meetings ON meetings.id = mail.meeting_id AND meetings.status = 'done'
               LEFT JOIN protocol_mail_format AS content ON content.meeting_id = mail.meeting_id
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
        if row["docx"] is not None:
            packet = ProtocolMail(row["body"], row["html"], row["filename"], row["docx"])
        else:
            # Pending rows created before the MIME upgrade still have a saved protocol.
            meeting = store.get_meeting(row["meeting_id"])
            if not meeting or meeting["status"] != "done":
                raise RuntimeError("Сохранённый протокол недоступен")
            packet = prepare_protocol_mail(meeting)
        send_mail(row["recipient"], row["subject"], packet.body, html=packet.html,
                  attachment=(packet.filename, packet.docx))
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
