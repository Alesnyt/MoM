from __future__ import annotations

import logging
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate

from . import config, control, protocol_email, retention, store, summary_format, word_protocol
from .analyze import StoredProtocolResult
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
    lines.append(summary_format.plain_text(result))
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
    result = StoredProtocolResult.model_validate(meeting["result"]).model_dump()
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


def build_eml(meeting: dict, recipients: list[str] | None = None) -> bytes:
    packet = prepare_protocol_mail(meeting)
    message = _make_message(email_subject(meeting), packet.body, html=packet.html,
                            attachment=(packet.filename, packet.docx))
    message["From"] = "MoM <mom@localhost>"
    message["Date"] = formatdate(localtime=True)
    message["X-Unsent"] = "1"
    if not recipients and any(speaker.get("send_protocol") for speaker in meeting.get("speakers") or []):
        recipients = _legacy_draft_recipients(meeting)
    if recipients:
        message["To"] = ", ".join(recipients)
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
    to: str | list[str], subject: str, body: str, *, html: str | None = None,
    attachment: tuple[str, bytes] | None = None, envelope_to: list[str] | None = None,
    message_id: str | None = None,
) -> dict[str, tuple[int, bytes]]:
    host = config.get_smtp_host()
    from_addr = config.get_smtp_from()
    if not host or not from_addr:
        raise RuntimeError("Сначала укажите SMTP-сервер и адрес отправителя")
    recipients = [to] if isinstance(to, str) else to
    if not recipients or any("@" not in address for address in recipients):
        raise RuntimeError("Нет адреса получателя")
    message = _make_message(subject, body, html=html, attachment=attachment)
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)
    if message_id:
        message["Message-ID"] = message_id

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
            if envelope_to is None:
                return client.send_message(message)
            return client.send_message(message, to_addrs=envelope_to)
        except smtplib.SMTPRecipientsRefused as exc:
            if envelope_to is not None:
                return exc.recipients
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


def _legacy_draft_recipients(meeting: dict) -> list[str]:
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
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck cancellation under the same lock that publishes the result.
        if control.is_stopped(meeting_id):
            raise control.ProcessingStopped()
        store.update_meeting(
            meeting_id, connection=conn, **fields, status="done", error=None, progress=100,
            status_message="Готово",
        )
        removed = store.prune_user_meetings(user_id, connection=conn) if user_id else []
    retention.remove_pruned(removed)


def queue_protocol_mail(conn, meeting: dict, packet: ProtocolMail, recipients: list[str]) -> None:
    meeting_id = meeting["id"]
    conn.execute("DELETE FROM protocol_mail WHERE meeting_id = ?", (meeting_id,))
    conn.execute("INSERT OR REPLACE INTO protocol_mail_format (meeting_id, html, filename, docx) VALUES (?, ?, ?, ?)",
                 (meeting_id, packet.html, packet.filename, packet.docx))
    subject = email_subject(meeting)
    conn.executemany("INSERT INTO protocol_mail (meeting_id, recipient, subject, body) VALUES (?, ?, ?, ?)",
                     [(meeting_id, address, subject, packet.body) for address in recipients])
    store.update_meeting(meeting_id, connection=conn, status_message="Готово, письмо в очереди")


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
        message = f"Готово. Письмо принято для получателей: {sent} из {total}."
        if pending:
            message += " Остальные адреса в очереди."
        if counts.get("failed"):
            message += " Есть ошибки доставки."
        if counts.get("unknown"):
            message += " Для некоторых адресов SMTP не подтвердил отправку."
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
               WHERE mail.state = 'pending' AND mail.due_at <= ?
               AND NOT EXISTS (SELECT 1 FROM protocol_mail busy WHERE busy.meeting_id = mail.meeting_id AND busy.state = 'sending')
               ORDER BY mail.due_at, mail.id LIMIT 1""", (now,),
        ).fetchone()
        if row:
            recipients = conn.execute(
                "SELECT id, recipient, attempts, state, due_at FROM protocol_mail WHERE meeting_id = ? ORDER BY id",
                (row["meeting_id"],),
            ).fetchall()
            claimed = [r for r in recipients if r["state"] == "pending" and r["due_at"] <= now]
            conn.execute(
                """UPDATE protocol_mail SET state = 'sending', attempts = attempts + 1, due_at = ?
                   WHERE meeting_id = ? AND state = 'pending' AND due_at <= ?""",
                (now + CLAIM_SECONDS, row["meeting_id"], now),
            )
    retention.remove_pruned(removed)
    if not row:
        return False

    state = "sent"
    refused: dict[str, tuple[int, bytes]] = {}
    try:
        if row["docx"] is not None:
            packet = ProtocolMail(row["body"], row["html"], row["filename"], row["docx"])
        else:
            # Pending rows created before the MIME upgrade still have a saved protocol.
            meeting = store.get_meeting(row["meeting_id"])
            if not meeting or meeting["status"] != "done":
                raise RuntimeError("Сохранённый протокол недоступен")
            packet = prepare_protocol_mail(meeting)
        # Keep To and Message-ID stable on retry, and only retry unaccepted envelopes.
        generation = str(row["meeting_id"]) + ":" + str(recipients[0]["id"])
        identifier = uuid.uuid5(uuid.NAMESPACE_OID, generation).hex
        refused = send_mail([r["recipient"] for r in recipients], row["subject"], packet.body,
                            envelope_to=[r["recipient"] for r in claimed], message_id=f"<{identifier}@mom.local>",
                            html=packet.html, attachment=(packet.filename, packet.docx)) or {}
    except TemporaryMailError:
        state = "pending"
    except smtplib.SMTPResponseException:
        state = "failed"
    except (OSError, smtplib.SMTPServerDisconnected):
        state = "unknown"
    except Exception:
        state = "failed"
        log.exception("Не удалось отправить протокол встречи %s", row["meeting_id"])

    updated = []
    removed = []
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for recipient in claimed:
            outcome = state
            address = recipient["recipient"]
            if address in refused:
                code, _ = refused[address]
                outcome = "pending" if 400 <= code < 500 else "failed"
            attempts = recipient["attempts"]
            if outcome == "pending" and attempts >= len(RETRY_DELAYS):
                outcome = "failed"
            retry = outcome == "pending"
            due_at = time.time() + RETRY_DELAYS[attempts] if retry else 0
            result = conn.execute(
                "UPDATE protocol_mail SET state = ?, due_at = ?, subject = ?, body = ? WHERE id = ? AND state = 'sending'",
                (outcome, due_at, row["subject"] if retry else "", row["body"] if retry else "", recipient["id"]),
            )
            if result.rowcount:
                updated.append((address, outcome))
        if updated and not _delivery_status(conn, row["meeting_id"]) and row["user_id"]:
            removed = store.prune_user_meetings(row["user_id"], connection=conn)
    for address, outcome in updated:
        detail = {"sent": "автоматически", "pending": "временный отказ, будет повтор",
                  "failed": "SMTP не принял письмо", "unknown": "результат отправки неизвестен"}[outcome]
        store.record_audit(address, "email.send" if outcome == "sent" else "email.fail", row["meeting_id"], detail)
    retention.remove_pruned(removed)
    return True
