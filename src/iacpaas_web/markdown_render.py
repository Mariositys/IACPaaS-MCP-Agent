"""Безопасный рендер Markdown-ответов для веб-чата."""

import re

import nh3
from markdown_it import MarkdownIt

_md = MarkdownIt("gfm-like", {"html": False, "linkify": False})

_TABLE_SEPARATOR = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|)+")
_TABLE_HEADER = re.compile(r"(\|[^|\n]*(?:\|[^|\n]*)+\|)\s*$")

# nh3 is a second line of defense: raw HTML is already disabled in MarkdownIt.
_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "target", "title"},
    "code": {"class"},
    "ol": {"start"},
    "td": {"align"},
    "th": {"align"},
}


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip("|").split("|")]


def _normalize_inline_table(line: str) -> str:
    """Развернуть таблицу, которую модель записала одной строкой."""
    separator = _TABLE_SEPARATOR.search(line)
    if separator is None:
        return line

    prefix = line[: separator.start()]
    header = _TABLE_HEADER.search(prefix)
    if header is None:
        return line
    header_cells = _cells(header.group(1))
    if len(header_cells) < 2:
        return line

    cells: list[str] = []
    pos = separator.end()
    if pos < len(line) and line[pos] == "|":
        pos += 1
    while pos < len(line):
        end = line.find("|", pos)
        if end == -1:
            break
        cell = line[pos:end].strip()
        if cell:
            cells.append(cell)
        pos = end + 1
        if pos < len(line) and line[pos] == "|":
            pos += 1
    if not cells:
        return line

    rows = [
        "| " + " | ".join(cells[i : i + len(header_cells)]) + " |"
        for i in range(0, len(cells) - len(header_cells) + 1, len(header_cells))
    ]
    if not rows:
        return line

    before = prefix[: header.start()].rstrip()
    rebuilt = before + ("\n\n" if before else "")
    rebuilt += header.group(1).strip()
    rebuilt += "\n" + separator.group(0).strip()
    rebuilt += "\n" + "\n".join(rows)

    suffix = line[pos:].strip()
    if suffix:
        rebuilt += "\n\n" + suffix
    return rebuilt


def normalize_markdown(text: str) -> str:
    """Починить типичный сбой модели: Markdown-таблица без переводов строк."""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            fenced = not fenced
            lines.append(line)
        elif fenced:
            lines.append(line)
        else:
            lines.append(_normalize_inline_table(line))
    return "\n".join(lines)


def render_markdown(text: str | None) -> str:
    """Вернуть безопасный HTML для Markdown-текста ответа."""
    if not text:
        return ""
    return nh3.clean(
        _md.render(normalize_markdown(str(text))),
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
