"""Parse the interim safety-publications HTML page.

The page has two tables: biweekly bundle links, then one row per publication
issued while the Safety Publications Tool is down. Publication rows are not
all marked up the same way. Some put the holder in a paragraph and the
models in a nested list; proposed ADs put the holder on the outer list item.
Proposed ADs and SIBs also carry a class icon (``adclass_pad.gif``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from easa_ads.normalize import (
    absolute_url,
    clean,
    document_kind,
    drop_category_labels,
    parse_date,
    rejoin_variant_suffixes,
)

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
_LAST_UPDATED = re.compile(r"Last updated:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", re.IGNORECASE)


@dataclass
class BiweeklyPeriod:
    period: int
    date_from: str
    date_to: str
    csv_url: str
    csv_label: str
    html_url: str
    html_label: str
    pdf_url: str
    pdf_label: str


@dataclass
class InterimPublication:
    number: str
    issued_by: str | None
    issue_date: str | None
    effective_date: str | None
    subject: str
    class_hint: str | None
    holders: list[str]
    models: list[str]
    documents: list[dict]


@dataclass
class InterimPage:
    last_updated: str | None
    periods: list[BiweeklyPeriod] = field(default_factory=list)
    publications: list[InterimPublication] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_interim_html(html: str) -> InterimPage:
    """Read biweekly links and publication rows from the interim page."""
    updated = _LAST_UPDATED.search(html or "")
    page = InterimPage(last_updated=updated.group(1) if updated else None)
    parser = _TableParser()
    parser.feed(html or "")
    parser.close()
    for table in parser.tables:
        kind = _table_kind(table)
        if kind == "biweekly":
            page.periods.extend(_biweekly_rows(table, page.warnings))
        elif kind == "publications":
            page.publications.extend(_publication_rows(table, page.warnings))
    return page


class _TableParser(HTMLParser):
    """Collect each table as rows of event lists.

    Events are small tuples (``text``, ``br``, ``class``, ``a_start``,
    ``a_end``, ``p_start``, ``p_end``, ``li_start``, ``li_end``). The
    publication parser rebuilds the holder tree from those events.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[list[tuple]]]] = []
        self._depth = 0
        self._skip: list[int] = []
        self._table_depth = 0
        self._current_table: list | None = None
        self._current_row: list | None = None
        self._current_cell: list | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            self._depth += 1
        attributes = {key: value or "" for key, value in attrs}
        if tag in {"script", "style"} or (
            tag == "span" and "spamspan" in attributes.get("class", "")
        ):
            self._skip.append(self._depth)
        if self._skip:
            return
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._current_row = []
            return
        if tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            return
        if self._current_cell is None:
            return
        if tag == "br":
            self._current_cell.append(("br", None))
        elif tag == "img":
            match = re.search(r"adclass_([a-z0-9]+)\.gif", attributes.get("src", ""), re.I)
            if match:
                self._current_cell.append(("class", match.group(1).upper()))
        elif tag == "a":
            self._current_cell.append(("a_start", attributes.get("href", "")))
        elif tag == "p":
            self._current_cell.append(("p_start", None))
        elif tag == "li":
            self._current_cell.append(("li_start", None))

    def handle_endtag(self, tag: str) -> None:
        if self._skip and self._depth == self._skip[-1]:
            self._skip.pop()
            if tag not in _VOID:
                self._depth -= 1
            return
        if not self._skip and self._table_depth >= 1:
            if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
                self._current_row.append(self._current_cell)
                self._current_cell = None
            elif tag == "tr" and self._current_row is not None and self._current_table is not None:
                self._current_table.append(self._current_row)
                self._current_row = None
            elif tag == "table" and self._table_depth == 1 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None
            elif tag == "a" and self._current_cell is not None:
                self._current_cell.append(("a_end", None))
            elif tag == "p" and self._current_cell is not None:
                self._current_cell.append(("p_end", None))
            elif tag == "li" and self._current_cell is not None:
                self._current_cell.append(("li_end", None))
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
        if tag not in _VOID:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip or self._current_cell is None or self._table_depth != 1:
            return
        if data:
            self._current_cell.append(("text", data))


def _cell_text(events: list[tuple]) -> str:
    parts: list[str] = []
    for kind, value in events:
        if kind == "text":
            parts.append(value)
        elif kind == "br":
            parts.append(" ")
    return clean("".join(parts))


def _table_kind(table: list[list[list[tuple]]]) -> str | None:
    for row in table:
        labels = [_cell_text(cell).casefold() for cell in row]
        if labels[:3] == ["biweekly", "from", "to"]:
            return "biweekly"
        if labels and labels[0] == "number" and "subject" in labels:
            return "publications"
    return None


def _links(events: list[tuple]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    href: str | None = None
    label: list[str] = []
    for kind, value in events:
        if kind == "a_start":
            href = value or ""
            label = []
        elif kind == "text" and href is not None:
            label.append(value)
        elif kind == "a_end" and href is not None:
            links.append((absolute_url(href), clean("".join(label))))
            href = None
    return links


def _first_link(events: list[tuple]) -> tuple[str, str]:
    found = _links(events)
    if not found:
        return "", ""
    return found[0]


def _biweekly_rows(table: list, warnings: list[str]) -> list[BiweeklyPeriod]:
    periods: list[BiweeklyPeriod] = []
    for row in table:
        if len(row) < 6:
            continue
        period_text = _cell_text(row[0])
        if not period_text.isdigit():
            continue
        csv_url, csv_label = _first_link(row[3])
        html_url, html_label = _first_link(row[4])
        pdf_url, pdf_label = _first_link(row[5])
        if not csv_url:
            warnings.append(f"biweekly {period_text} has no CSV link")
            continue
        periods.append(
            BiweeklyPeriod(
                period=int(period_text),
                date_from=parse_date(_cell_text(row[1])) or "",
                date_to=parse_date(_cell_text(row[2])) or "",
                csv_url=csv_url,
                csv_label=csv_label or "CSV (.zip)",
                html_url=html_url,
                html_label=html_label or "HTML (.zip)",
                pdf_url=pdf_url,
                pdf_label=pdf_label or "PDF",
            )
        )
    return periods


def _publication_rows(table: list, warnings: list[str]) -> list[InterimPublication]:
    publications: list[InterimPublication] = []
    for index, row in enumerate(table, start=1):
        if not row:
            continue
        number = _cell_text(row[0])
        if not number or number.casefold() == "number":
            continue
        if len(row) < 7:
            warnings.append(f"interim row {index} ({number}) has {len(row)} cells")
            continue
        hint = _class_hint(row)
        holders, models = _holders_and_models(row[4])
        documents = [
            {
                "url": url,
                "label": label or url.rstrip("/").rsplit("/", 1)[-1],
                "kind": document_kind(url, label),
            }
            for url, label in _links(row[6])
            if url
        ]
        publications.append(
            InterimPublication(
                number=number,
                issued_by=_cell_text(row[1]) or None,
                issue_date=parse_date(_cell_text(row[2])),
                effective_date=parse_date(_cell_text(row[5])),
                subject=_cell_text(row[3]),
                class_hint=hint,
                holders=holders,
                models=models,
                documents=documents,
            )
        )
    return publications


def _class_hint(row: list[list[tuple]]) -> str | None:
    for cell in row:
        for kind, value in cell:
            if kind == "class":
                return value
    return None


class _Node:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.children: list[_Node] = []


def _holders_and_models(events: list[tuple]) -> tuple[list[str], list[str]]:
    """Read the approval-holder cell.

    A paragraph is a holder and every list item under it is a model (family
    names and variants both, so a search for either hits). When there is no
    paragraph, the outer list item is the holder and its descendants are
    the models. That is how proposed-AD rows are marked up.
    """
    paragraphs: list[str] = []
    root = _Node()
    stack = [root]
    in_paragraph = False
    paragraph: list[str] = []
    for kind, value in events:
        if kind == "p_start":
            in_paragraph = True
            paragraph = []
        elif kind == "p_end":
            in_paragraph = False
            text = clean("".join(paragraph))
            if text:
                paragraphs.append(text)
        elif kind == "text":
            if in_paragraph:
                paragraph.append(value)
            else:
                stack[-1].text.append(value)
        elif kind == "br" and not in_paragraph:
            stack[-1].text.append(" ")
        elif kind == "li_start":
            node = _Node()
            stack[-1].children.append(node)
            stack.append(node)
        elif kind == "li_end" and len(stack) > 1:
            stack.pop()

    def own_text(node: _Node) -> str:
        return clean("".join(node.text))

    def flatten(nodes: list[_Node]) -> list[str]:
        found: list[str] = []
        for node in nodes:
            text = own_text(node)
            if text:
                found.append(text)
            found.extend(flatten(node.children))
        return found

    if paragraphs:
        holders = _unique(paragraphs)
        models = _split_model_labels(flatten(root.children))
    else:
        holders = []
        models = []
        for node in root.children:
            text = own_text(node)
            if text:
                holders.append(text)
            models.extend(flatten(node.children))
        holders = _unique(holders)
        models = _split_model_labels(models)
    return drop_category_labels(holders, models)


def _split_model_labels(labels: list[str]) -> list[str]:
    """Split family labels that still contain the archive's slash separator."""
    tokens: list[str] = []
    for label in labels:
        pieces = [clean(piece) for piece in label.split("/") if clean(piece)]
        tokens.extend(pieces or [label])
    return _unique(rejoin_variant_suffixes(tokens))


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
