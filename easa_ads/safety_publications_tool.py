"""Read recent publications from the Safety Publications Tool.

``https://ad.easa.europa.eu/`` is the live tool. Its advanced search, filtered
by issue date, is the list the retired interim page used to publish: ADs
issued after the latest biweekly CSV, and SIBs the biweekly files do not
carry. Safety directives are a separate short list.

The tool can also export CSV. That export's designation column glues the
holder to the type with no separator, so the HTML table is parsed instead.
Each row has the publication number, a state-of-design flag, the issue date,
a class icon when the row is not an ordinary AD, the subject, the
holder/type/model tree, the effective date, and attachment links.

A tool row dated the same day as the archive is still kept, so its
state-of-design code and PDF link are merged. The archive keeps the subject
and the model list on that tie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urlencode

from easa_ads.const import ALL_CLASSES, SD_LIST_URL, TOOL_ORIGIN, TOOL_SEARCH_URL
from easa_ads.http import HttpClient
from easa_ads.normalize import clean, document_kind, drop_category_labels, parse_date, split_model_labels

# The archive's newest SIB or AD can be a few weeks behind a publication
# that was issued just before it. Look that far back so those are not missed.
LOOKBACK_DAYS = 21
# A date-filtered search covering a few weeks is a few hundred rows at most.
# Anything larger means the filter was ignored and the full catalogue came back.
MAX_ROWS = 400
_EMPTY = "Sorry, no results found for your query."
_DISPLAY = re.compile(
    r"Displaying records\s+(\d+)\s+to\s+(\d+)\s+out of a total of\s+(\d+)\s+publications",
    re.IGNORECASE,
)
_FILE_EXTENSIONS = (".pdf", ".zip", ".csv", ".html", ".htm")


@dataclass
class ToolPublication:
    number: str
    issued_by: str | None
    issue_date: str | None
    effective_date: str | None
    subject: str
    class_hint: str | None
    holders: list[str]
    models: list[str]
    documents: list[dict]
    url: str


@dataclass
class ToolPage:
    empty: bool
    total: int
    range_from: int
    range_to: int
    publications: list[ToolPublication] = field(default_factory=list)


def listing_window(archive_ad_max: date, archive_sib_max: date, today: date) -> tuple[date, date]:
    """Issue-date window that covers the archive lag and the open fortnight."""
    start = min(archive_ad_max, archive_sib_max) - timedelta(days=LOOKBACK_DAYS)
    if start > today:
        return today, today
    return start, today


def is_newer_than_archive(issue_date: str | None, archive_date: str | None) -> bool:
    """True when the tool row is absent from the archive or at least as new.

    An equal issue date is kept. Merge still prefers the archive subject and
    models on that tie, and adds the tool's state-of-design code and documents.
    """
    if not archive_date:
        return True
    if not issue_date:
        return False
    return issue_date >= archive_date


def fetch_recent_publications(
    client: HttpClient,
    *,
    start: date,
    end: date,
) -> list[ToolPublication]:
    """Fetch the date-filtered AD and SIB search, following its pager."""
    if end < start:
        raise ValueError(f"tool window ends {end.isoformat()} before it starts")
    print(f"Fetching Safety Publications Tool {start.isoformat()} to {end.isoformat()}…")
    publications = _read_pages(
        client,
        lambda page: _search_url(start, end, page),
        empty_is_error=(end - start).days >= 7,
    )
    outside = [
        item.number
        for item in publications
        if not item.issue_date
        or item.issue_date < start.isoformat()
        or item.issue_date > end.isoformat()
    ]
    if outside:
        sample = ", ".join(outside[:5])
        raise RuntimeError(
            f"{len(outside)} tool rows fall outside {start.isoformat()}..{end.isoformat()} "
            f"({sample}); the date filter may have been ignored"
        )
    return publications


def fetch_safety_directives(client: HttpClient) -> list[ToolPublication]:
    """Fetch the safety-directive list. It is the full SD catalogue, newest first."""
    print("Fetching safety directives…")
    publications = _read_pages(client, _sd_page_url, empty_is_error=True)
    if not publications:
        raise RuntimeError(
            "safety directive list is empty; the page layout may have changed"
        )
    return publications


def parse_tool_page(html: str) -> ToolPage:
    """Parse one Safety Publications Tool result page."""
    text = html or ""
    has_table = 'class="ad-list"' in text or "class='ad-list'" in text
    if _EMPTY in text and not has_table:
        return ToolPage(empty=True, total=0, range_from=0, range_to=0)
    match = _DISPLAY.search(text)
    if not match or not has_table:
        raise ValueError(
            "Safety Publications Tool page has no publication table; the layout may have changed"
        )
    range_from = int(match.group(1))
    range_to = int(match.group(2))
    total = int(match.group(3))
    if range_from < 1 or range_to < range_from or total < range_to:
        raise ValueError(
            f"Safety Publications Tool page range {range_from}-{range_to} of {total} is inconsistent"
        )
    parser = _ResultParser()
    parser.feed(text)
    parser.close()
    if not parser.found_table:
        raise ValueError(
            "Safety Publications Tool page has no publication table; the layout may have changed"
        )
    publications: list[ToolPublication] = []
    for row in parser.rows:
        if not row:
            continue
        number = _plain(row[0])
        if not number or number.casefold() == "number":
            continue
        if len(row) != 7:
            raise ValueError(f"tool row {number} has {len(row)} cells, expected 7")
        hint = _class_hint(row)
        if hint and hint not in ALL_CLASSES:
            raise ValueError(f"tool row {number} has unknown class {hint}")
        try:
            issue_date = parse_date(_plain(row[2]))
            effective_date = parse_date(_plain(row[5]))
        except ValueError as exc:
            raise ValueError(f"tool row {number} has an unreadable date ({exc})") from exc
        if not issue_date:
            raise ValueError(f"tool row {number} has no issue date")
        holders, models = _holders_and_models(row[4])
        publications.append(
            ToolPublication(
                number=number,
                issued_by=_flag(row[1]),
                issue_date=issue_date,
                effective_date=effective_date,
                subject=_subject(row[3]),
                class_hint=hint,
                holders=holders,
                models=models,
                documents=_documents(row[6]),
                url=_number_url(row[0], number),
            )
        )
    expected = range_to - range_from + 1
    if len(publications) != expected:
        raise ValueError(
            f"Safety Publications Tool page listed {expected} publications "
            f"but {len(publications)} could be read"
        )
    return ToolPage(
        empty=False,
        total=total,
        range_from=range_from,
        range_to=range_to,
        publications=publications,
    )


def _read_pages(client: HttpClient, url_for_page, *, empty_is_error: bool) -> list[ToolPublication]:
    first = parse_tool_page(client.get(url_for_page(1)).body.decode("utf-8", "replace"))
    if first.empty:
        if empty_is_error:
            raise RuntimeError(
                "Safety Publications Tool returned no publications; the page layout may have changed"
            )
        return []
    if first.range_from != 1:
        raise RuntimeError(
            f"Safety Publications Tool results start at record {first.range_from}, not 1"
        )
    if first.total > MAX_ROWS:
        raise RuntimeError(
            f"Safety Publications Tool reported {first.total} rows, "
            f"above the {MAX_ROWS}-row cap; the date filter may have been ignored"
        )
    page_size = first.range_to - first.range_from + 1
    if page_size < 1:
        raise RuntimeError("Safety Publications Tool page size could not be read")
    pages = (first.total + page_size - 1) // page_size
    publications = list(first.publications)
    for page in range(2, pages + 1):
        print(f"Fetching tool results page {page} of {pages}…")
        current = parse_tool_page(client.get(url_for_page(page)).body.decode("utf-8", "replace"))
        if current.empty or current.total != first.total:
            raise RuntimeError(
                f"Safety Publications Tool page {page} does not match the first page's total"
            )
        publications.extend(current.publications)
    if len(publications) != first.total:
        raise RuntimeError(
            f"Safety Publications Tool listed {first.total} publications "
            f"but {len(publications)} were read"
        )
    return dedupe_publications(publications)


def _search_url(start: date, end: date, page: int) -> str:
    query = urlencode(
        {
            "fi_action": "advanced",
            "fi_date_start": start.isoformat(),
            "fi_date_end": end.isoformat(),
        }
    )
    if page <= 1:
        return f"{TOOL_SEARCH_URL}?{query}"
    return f"{TOOL_SEARCH_URL.rstrip('/')}/page-{page}?{query}"


def _sd_page_url(page: int) -> str:
    root = SD_LIST_URL
    if root.endswith("/page-1"):
        root = root[: -len("/page-1")]
    return f"{root}/page-{page}"


def dedupe_publications(publications: list[ToolPublication]) -> list[ToolPublication]:
    chosen: dict[str, ToolPublication] = {}
    for item in publications:
        current = chosen.get(item.number)
        if current is None or (len(item.models), len(item.documents)) > (
            len(current.models),
            len(current.documents),
        ):
            chosen[item.number] = item
    return list(chosen.values())


def _number_url(events: list[tuple], number: str) -> str:
    for url, _label in _links(events):
        if url:
            return url
    return f"{TOOL_ORIGIN}/ad/{number}"


def _class_hint(row: list[list[tuple]]) -> str | None:
    for cell in row:
        for kind, value in cell:
            if kind == "class" and value:
                return value
    return None


def _flag(events: list[tuple]) -> str | None:
    for kind, value in events:
        if kind == "flag" and value:
            return clean(value) or None
    return None


def _plain(events: list[tuple]) -> str:
    parts: list[str] = []
    for kind, value in events:
        if kind == "text":
            parts.append(value)
        elif kind == "br":
            parts.append(" ")
    return clean("".join(parts))


def _subject(events: list[tuple]) -> str:
    """Subject text, without the 'send comment' link that proposed ADs carry."""
    parts: list[str] = []
    anchor = 0
    for kind, value in events:
        if kind == "a_start":
            anchor += 1
        elif kind == "a_end" and anchor:
            anchor -= 1
        elif anchor:
            continue
        elif kind == "text":
            parts.append(value)
        elif kind == "br":
            parts.append(" ")
    return clean("".join(parts))


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
            url = _absolute(href)
            if url.startswith("https://") or url.startswith("http://"):
                links.append((url, clean("".join(label))))
            href = None
    return links


def _documents(events: list[tuple]) -> list[dict]:
    documents: list[dict] = []
    for url, label in _links(events):
        filename = _filename(url)
        documents.append(
            {
                "url": url,
                "label": filename or label or url.rstrip("/").rsplit("/", 1)[-1],
                "kind": document_kind(url, filename or label),
            }
        )
    return documents


def _filename(url: str) -> str:
    for segment in url.split("?", 1)[0].split("/"):
        lowered = segment.lower()
        if any(lowered.endswith(extension) for extension in _FILE_EXTENSIONS):
            return segment
    return ""


def _holders_and_models(events: list[tuple]) -> tuple[list[str], list[str]]:
    holders, models = _tree(events)
    models = split_model_labels(models)
    cleaned_holders, cleaned_models = drop_category_labels(holders, models)
    # An SD whose only tokens are the category labels still names them.
    # Dropping those leaves an empty designation, which the archive does not.
    if not cleaned_holders and not cleaned_models and (holders or models):
        return holders, models
    return cleaned_holders, cleaned_models


def _tree(events: list[tuple]) -> tuple[list[str], list[str]]:
    root = _Node("")
    stack = [root]
    for kind, value in _balance(events):
        if kind == "li_start":
            node = _Node(value or "")
            stack[-1].children.append(node)
            stack.append(node)
        elif kind == "li_end" and len(stack) > 1:
            stack.pop()
        elif kind == "text":
            stack[-1].text.append(value)
        elif kind == "br":
            stack[-1].text.append(" ")
    holders: list[str] = []
    models: list[str] = []

    def walk(node: _Node) -> None:
        text = clean("".join(node.text))
        if node.kind == "tc_holder" and text:
            holders.append(text)
        elif node.kind in {"type", "model"} and text:
            models.append(text)
        for child in node.children:
            walk(child)

    walk(root)
    return _unique(holders), _unique(models)


def _balance(events: list[tuple]) -> list[tuple]:
    """Close list items the tool page leaves open before the next tag."""
    balanced: list[tuple] = []
    stack: list[str] = []
    for kind, value in events:
        if kind == "ul_start":
            stack.append("ul")
            balanced.append((kind, value))
        elif kind == "li_start":
            if stack and stack[-1] == "li":
                stack.pop()
                balanced.append(("li_end", None))
            stack.append("li")
            balanced.append((kind, value))
        elif kind == "ul_end":
            while stack and stack[-1] == "li":
                stack.pop()
                balanced.append(("li_end", None))
            if stack and stack[-1] == "ul":
                stack.pop()
            balanced.append((kind, value))
        elif kind == "li_end":
            if stack and stack[-1] == "li":
                stack.pop()
                balanced.append((kind, value))
        else:
            balanced.append((kind, value))
    while stack and stack[-1] == "li":
        stack.pop()
        balanced.append(("li_end", None))
    return balanced


class _Node:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.text: list[str] = []
        self.children: list[_Node] = []


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _absolute(href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return ""
    if href.startswith("https://") or href.startswith("http://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("/"):
        href = "/" + href
    return TOOL_ORIGIN + href


def _li_kind(class_attr: str) -> str:
    tokens = class_attr.split()
    for name in ("tc_holder", "type", "model"):
        if name in tokens:
            return name
    return ""


class _ResultParser(HTMLParser):
    """Collect rows of the tool's ``ad-list`` table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found_table = False
        self.rows: list[list[list[tuple]]] = []
        self._in_table = False
        self._row: list | None = None
        self._cell: list | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "").split()
        if tag == "table" and "ad-list" in classes:
            self._in_table = True
            self.found_table = True
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._row = []
            return
        if tag in {"td", "th"} and self._row is not None:
            self._cell = []
            return
        if self._cell is None:
            return
        if tag == "br":
            self._cell.append(("br", None))
        elif tag == "img":
            source = attributes.get("src", "")
            match = re.search(r"adclass_([a-z0-9]+)\.gif", source, re.IGNORECASE)
            if match:
                self._cell.append(("class", match.group(1).upper()))
            elif "/flags/" in source and attributes.get("alt"):
                self._cell.append(("flag", attributes.get("alt", "")))
        elif tag == "a":
            self._cell.append(("a_start", attributes.get("href", "")))
        elif tag == "li":
            self._cell.append(("li_start", _li_kind(attributes.get("class", ""))))
        elif tag == "ul":
            self._cell.append(("ul_start", None))

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_table = False
        elif self._cell is not None and tag == "a":
            self._cell.append(("a_end", None))
        elif self._cell is not None and tag == "li":
            self._cell.append(("li_end", None))
        elif self._cell is not None and tag == "ul":
            self._cell.append(("ul_end", None))

    def handle_data(self, data: str) -> None:
        if self._cell is not None and data:
            self._cell.append(("text", data))
