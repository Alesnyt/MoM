from __future__ import annotations

import re
from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .audio import format_duration
from .speakers import participant_labels

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
PRIORITY_LABELS = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}


def _text(value: str) -> str:
    return _INVALID_XML.sub("", value)


def docx_filename(title: str) -> str:
    name = " ".join("".join(char if char.isalnum() or char in " -_" else " " for char in title).split())[:80]
    return f"{name or 'Протокол'}.docx"


def to_docx(meeting: dict, result: dict) -> bytes:
    document = Document()
    # The bundled template targets Word 2010. Use the current layout mode for new exports.
    for setting in document.settings.element.xpath("w:compat/w:compatSetting[@w:name='compatibilityMode']"):
        setting.set(qn("w:val"), "15")
    page = document.sections[0]
    page.page_width, page.page_height = Cm(21), Cm(29.7)
    page.left_margin = page.right_margin = Cm(2.1)
    page.top_margin = page.bottom_margin = Cm(2)
    for name, size in (("Normal", 11), ("Title", 24), ("Heading 1", 15), ("Heading 2", 12)):
        style = document.styles[name]
        style.font.name = "Arial"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        if name.startswith("Heading"):
            style.paragraph_format.space_before = Pt(14)
            style.paragraph_format.space_after = Pt(5)
    document.styles["Title"].paragraph_format.keep_with_next = True
    title_properties = document.styles["Title"].element.pPr
    if title_properties is not None:
        for border in title_properties.xpath("w:pBdr"):
            title_properties.remove(border)
    document.styles["Normal"].paragraph_format.space_after = Pt(6)

    title = _text(result["title"] or meeting["title"])
    document.core_properties.title = title
    document.add_heading(title, 0)
    if result["date_hint"]:
        document.add_paragraph("Дата встречи: " + _text(result["date_hint"]))
    if duration := format_duration(meeting.get("duration_seconds")):
        document.add_paragraph("Длительность: " + duration)
    if participants := participant_labels(meeting, result):
        document.add_paragraph("Участники: " + _text(", ".join(participants)))

    document.add_heading("Саммари", 1)
    summary_lines = [line.strip() for line in result["summary"].splitlines() if line.strip()]
    for line in summary_lines or ["Саммари отсутствует."]:
        document.add_paragraph(_text(line))
    for label, entries in (("Принятые решения", result["decisions"]), ("Контекст и открытые вопросы", result["key_points"])):
        if entries:
            document.add_heading(label, 1)
            for entry in entries:
                document.add_paragraph(_text(entry), style="List Bullet")

    document.add_heading("Поручения", 1)
    if result["action_items"]:
        table = document.add_table(rows=1, cols=4, style="Table Grid")
        table.autofit = False
        for column, width in zip(table.columns, (8.0, 3.25, 2.65, 2.9)):
            column.width = Cm(width)
        header = table.rows[0]
        header._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for cell, column, label in zip(header.cells, table.columns, ("Поручение", "Исполнитель", "Срок", "Приоритет")):
            cell.width = column.width
            cell.paragraphs[0].add_run(label).bold = True
            cell.paragraphs[0].paragraph_format.space_after = Pt(3)
        for item in result["action_items"]:
            values = (item["task"], item["assignee"] or "Не назначен", item["due"] or "Не указан", PRIORITY_LABELS[item["priority"]])
            row = table.add_row()
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            for cell, value in zip(row.cells, values):
                cell.text = _text(value)
                cell.paragraphs[0].paragraph_format.space_after = Pt(3)
    else:
        document.add_paragraph("Поручений не зафиксировано.")

    mom = result["mom"]
    if mom["agenda"]:
        document.add_heading("Ход обсуждения", 1)
        for block in mom["agenda"]:
            document.add_heading(_text(block["topic"] or "Тема"), 2)
            if block["discussion"]:
                document.add_paragraph(_text(block["discussion"]))
            if block["outcome"]:
                document.add_paragraph("Итог: " + _text(block["outcome"]))
    if mom["next_meeting"]:
        document.add_heading("Следующая встреча", 1)
        document.add_paragraph(_text(mom["next_meeting"]))

    output = BytesIO()
    document.save(output)
    return output.getvalue()
