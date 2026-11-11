
from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from .audio import format_duration
from .speakers import participant_labels
from .word_protocol import PRIORITY_LABELS


def render_html(meeting: dict, result: dict, filename: str, public_url: str = "") -> str:
    heading = 'margin:22px 0 7px;font-size:18px;line-height:1.3'
    paragraph = 'margin:0 0 10px;line-height:1.55'
    cell = 'padding:7px 8px;border:1px solid #9ca3af;vertical-align:top;overflow-wrap:anywhere'
    title = escape(result["title"] or meeting["title"])
    lines = [
        '<html lang="ru"><body style="margin:0;padding:0">',
        '<div style="max-width:720px;margin:0 auto;padding:20px 16px;font-family:Arial,sans-serif;font-size:14px;line-height:1.55">',
        f'<h1 style="margin:0 0 12px;font-size:24px;line-height:1.25">{title}</h1>',
    ]
    details = (
        ("Дата встречи", result["date_hint"]),
        ("Длительность", format_duration(meeting.get("duration_seconds"))),
        ("Участники", ", ".join(participant_labels(meeting, result))),
    )
    for label, value in details:
        if value:
            lines.append(f'<p style="{paragraph}"><strong>{label}:</strong> {escape(value)}</p>')

    lines.append(f'<h2 style="{heading}">Саммари</h2>')
    summary = [line.strip() for line in result["summary"].splitlines() if line.strip()]
    lines.extend(f'<p style="{paragraph}">{escape(line)}</p>' for line in summary or ["Саммари отсутствует."])
    for label, items in (("Принятые решения", result["decisions"]),
                         ("Контекст и открытые вопросы", result["key_points"])):
        if items:
            lines.append(f'<h2 style="{heading}">{label}</h2><ul style="margin:0 0 12px;padding-left:24px">')
            lines.extend(f'<li style="margin-bottom:5px">{escape(item)}</li>' for item in items)
            lines.append("</ul>")

    lines.append(f'<h2 style="{heading}">Поручения</h2>')
    if result["action_items"]:
        lines.append('<table width="100%" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;text-align:left;font-size:13px">')
        lines.append("<thead><tr>")
        for label, width in (("Поручение", 45), ("Исполнитель", 25), ("Срок", 15), ("Приоритет", 15)):
            lines.append(f'<th scope="col" width="{width}%" style="{cell}">{label}</th>')
        lines.append("</tr></thead><tbody>")
        for item in result["action_items"]:
            values = (item["task"], item["assignee"] or "Не назначен", item["due"] or "Не указан",
                      PRIORITY_LABELS[item["priority"]])
            lines.append("<tr>" + "".join(f'<td style="{cell}">{escape(value)}</td>' for value in values) + "</tr>")
        lines.append("</tbody></table>")
    else:
        lines.append(f'<p style="{paragraph}">Поручений не зафиксировано.</p>')

    if next_meeting := result["mom"]["next_meeting"]:
        lines.append(f'<h2 style="{heading}">Следующая встреча</h2>')
        lines.append(f'<p style="{paragraph}">{escape(next_meeting)}</p>')
    lines.append(f'<p style="margin:20px 0 6px"><strong>Во вложении</strong> {escape(filename)}</p>')
    if public_url:
        href = escape(public_url, quote=True)
        lines.append(f'<p style="{paragraph}"><a href="{href}">Открыть в MoM</a></p>')
    lines.append("</div></body></html>")
    return "\n".join(lines)


def meeting_url(base_url: str) -> str:
    url = base_url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return f"{url.rstrip('/')}/" if parsed.scheme in {"http", "https"} and parsed.netloc else ""
