"""The small Markdown vocabulary used by the visual summary editor and exports."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import count

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

EMPTY = "Саммари отсутствует."
# Literal HTML and external resources are plain text. Match the browser's parser.
_parser = MarkdownIt("zero", {"breaks": True}).enable(["list", "heading", "emphasis", "newline", "escape"])


def html(text: str) -> str:
    return _parser.render(text) or f"<p>{EMPTY}</p>"


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True)
class ListItem:
    list_id: int
    number: int | None


@dataclass(frozen=True)
class Paragraph:
    spans: list[Span]
    heading: bool = False
    depth: int = 0
    item: ListItem | None = None


def _spans(node: SyntaxTreeNode, bold: bool = False, italic: bool = False) -> Iterator[Span]:
    if node.type == "text":
        if node.content:
            yield Span(node.content, bold, italic)
    elif node.type in ("hardbreak", "softbreak"):
        yield Span("\n", bold, italic)
    else:
        for child in node.children:
            yield from _spans(child, bold or node.type == "strong", italic or node.type == "em")


def paragraphs(result: dict) -> Iterator[Paragraph]:
    text = result.get("summary") or ""
    if result.get("summary_format") != "markdown":
        for line in [s.strip() for s in text.splitlines() if s.strip()] or [EMPTY]:
            yield Paragraph([Span(line)])
        return
    root = SyntaxTreeNode(_parser.parse(text.strip() or EMPTY))
    list_ids = count()

    def visit(node: SyntaxTreeNode, depth: int, item: ListItem | None = None) -> Iterator[Paragraph]:
        if node.type in ("paragraph", "heading"):
            yield Paragraph(list(_spans(node)), node.type == "heading", depth, item)
        elif node.type in ("ordered_list", "bullet_list"):
            list_id = next(list_ids)
            start = int(node.attrs.get("start", 1))
            for index, entry in enumerate(node.children):
                marker = ListItem(list_id, start + index if node.type == "ordered_list" else None)
                for block in entry.children:
                    yield from visit(block, depth + 1, marker)
                    marker = None

    for node in root.children:
        yield from visit(node, 0)


def plain_text(result: dict) -> str:
    if result.get("summary_format") != "markdown":
        return (result.get("summary") or "").strip() or EMPTY
    lines = []
    for block in paragraphs(result):
        text = "".join(span.text for span in block.spans)
        if block.depth:
            prefix = "  " * (block.depth - 1)
            marker = ""
            if block.item:
                marker = "• " if block.item.number is None else f"{block.item.number}. "
            lines.append(prefix + (marker or "  ") + text.replace("\n", "\n" + prefix + "  "))
        else:
            if lines:
                lines.append("")
            lines.append(text)
    return "\n".join(lines)
