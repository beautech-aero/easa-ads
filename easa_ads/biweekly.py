"""Read a biweekly publication list.

EASA now publishes the index at ``https://ad.easa.europa.eu/biweekly``.
Each row links a CSV, an HTML file, and a PDF for one fortnight. The CSV
is plain text (older copies were zip files; those are still accepted).
It has no header and no publication class. Columns, in order, are number,
issue date, approval holder, type designation, and subject. The HTML and
PDF files repeat the same list and are not parsed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from easa_ads.const import BIWEEKLY_INDEX_URL, TOOL_ORIGIN
from easa_ads.http import HttpClient
from easa_ads.normalize import clean, parse_date

# How far behind the archive's newest AD a fortnight must end before it is
# dropped. The archive lags, and a few ADs never appear in it at all.
OVERLAP_DAYS = 90
# A current index's newest fortnight should have ended within this many days.
FRESH_DAYS = 28

_YEAR_LINK = re.compile(r"biweekly/\?year=(\d{4})")

# A biweekly file is a few kilobytes. Refuse anything that does not look
# like that kind of file so a hostile or truncated zip cannot expand forever.
MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_ZIP_MEMBERS = 5


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
class BiweeklyIndex:
    periods: list[BiweeklyPeriod] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BiweeklyRow:
    number: str
    issue_date: str
    holders: str
    models: str
    subject: str


def csv_from_zip(blob: bytes) -> tuple[str, bytes]:
    """Return ``(member name, csv bytes)`` from a biweekly zip."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("biweekly download is not a zip file") from exc
    members = [info for info in archive.infolist() if not info.is_dir()]
    if not members:
        raise ValueError("biweekly zip is empty")
    if len(members) > MAX_ZIP_MEMBERS:
        raise ValueError(f"biweekly zip has {len(members)} members")
    csv_members = [info for info in members if info.filename.lower().endswith(".csv")]
    chosen = csv_members or members
    if len(chosen) != 1:
        names = ", ".join(info.filename for info in chosen)
        raise ValueError(f"biweekly zip does not contain exactly one CSV ({names})")
    info = chosen[0]
    if info.file_size > MAX_CSV_BYTES:
        raise ValueError(f"biweekly CSV {info.filename} is larger than {MAX_CSV_BYTES} bytes")
    name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
    with archive.open(info) as handle:
        data = handle.read(MAX_CSV_BYTES + 1)
    if len(data) > MAX_CSV_BYTES:
        raise ValueError(f"biweekly CSV {name} is larger than {MAX_CSV_BYTES} bytes")
    return name, data


def parse_csv(data: bytes, *, filename: str) -> list[BiweeklyRow]:
    """Parse biweekly CSV bytes. Raises if a row does not have five columns."""
    text = _decode(data, filename)
    reader = csv.reader(io.StringIO(text), delimiter=";", quotechar='"')
    rows: list[BiweeklyRow] = []
    for index, raw in enumerate(reader, start=1):
        if not any(cell.strip() for cell in raw):
            continue
        if index == 1 and raw and raw[0].strip().casefold() in {"number", "class number", "ad number"}:
            continue
        if len(raw) != 5:
            preview = ";".join(raw)[:180]
            raise ValueError(
                f"{filename} row {index} has {len(raw)} columns, expected 5: {preview}"
            )
        number = clean(raw[0])
        if not number:
            raise ValueError(f"{filename} row {index} has no publication number")
        rows.append(
            BiweeklyRow(
                number=number,
                issue_date=clean(raw[1]),
                holders=clean(raw[2]),
                models=clean(raw[3]),
                subject=clean(raw[4]),
            )
        )
    if not rows:
        raise ValueError(f"{filename} has no publication rows")
    return rows


def _decode(data: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{filename} is not UTF-8 or Windows-1252 text")


def load_biweekly_download(blob: bytes, *, filename: str | None) -> tuple[str, bytes]:
    """Return ``(member name, csv bytes)`` from a zip or a plain CSV."""
    if blob.startswith(b"PK"):
        return csv_from_zip(blob)
    sample = blob.lstrip()[:200].lower()
    if sample.startswith(b"<") or sample.startswith(b"<!doctype"):
        raise ValueError("biweekly download is HTML, not a CSV")
    if len(blob) > MAX_CSV_BYTES:
        raise ValueError(f"biweekly CSV is larger than {MAX_CSV_BYTES} bytes")
    name = filename or "biweekly.csv"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not name.lower().endswith(".csv"):
        name = "biweekly.csv"
    return name, blob


def parse_biweekly_index(html: str) -> BiweeklyIndex:
    """Read fortnight rows and year links from a biweekly index page."""
    index = BiweeklyIndex(years=sorted({int(year) for year in _YEAR_LINK.findall(html or "")}))
    parser = _IndexParser()
    parser.feed(html or "")
    parser.close()
    if not parser.found_table:
        return index
    for row in parser.rows:
        if len(row) < 6:
            continue
        period_text = _cell_text(row[0])
        if not period_text.isdigit():
            continue
        csv_url, csv_label = _first_link(row[3])
        html_url, html_label = _first_link(row[4])
        pdf_url, pdf_label = _first_link(row[5])
        if ".csv" not in csv_url.casefold():
            index.warnings.append(f"biweekly {period_text} has no CSV link")
            continue
        try:
            date_from = parse_date(_cell_text(row[1])) or ""
            date_to = parse_date(_cell_text(row[2])) or ""
        except ValueError as exc:
            index.warnings.append(f"biweekly {period_text} has an unreadable date ({exc})")
            continue
        if not date_from or not date_to:
            index.warnings.append(f"biweekly {period_text} is missing a date")
            continue
        index.periods.append(
            BiweeklyPeriod(
                period=int(period_text),
                date_from=date_from,
                date_to=date_to,
                csv_url=_absolute(csv_url),
                csv_label=csv_label or "CSV",
                html_url=_absolute(html_url),
                html_label=html_label or "HTML",
                pdf_url=_absolute(pdf_url),
                pdf_label=pdf_label or "PDF",
            )
        )
    return index


def select_periods(
    periods: list[BiweeklyPeriod],
    *,
    archive_max: date,
    today: date,
    overlap_days: int = OVERLAP_DAYS,
    fresh_days: int = FRESH_DAYS,
) -> list[BiweeklyPeriod]:
    """Keep fortnights that still overlap a lagging archive.

    Raises when the index has no CSV rows, or when the newest fortnight
    ended too long ago to be the live index.
    """
    if not periods:
        raise RuntimeError(
            "biweekly index listed no CSV links; the page layout may have changed"
        )
    newest_end = max(date.fromisoformat(_period_end(period)) for period in periods)
    if newest_end < today - timedelta(days=fresh_days):
        raise RuntimeError(
            f"newest biweekly period ended on {newest_end.isoformat()}, "
            f"more than {fresh_days} days before {today.isoformat()}; "
            "the page layout may have changed"
        )
    window_start = archive_max - timedelta(days=overlap_days)
    chosen = [
        period
        for period in periods
        if date.fromisoformat(_period_end(period)) >= window_start
    ]
    if not chosen:
        raise RuntimeError(
            "biweekly index had no period overlapping the archive; the dates could not be read"
        )
    chosen.sort(key=lambda period: (period.date_from, period.period))
    return chosen


def stored_biweekly_gaps(
    root: Path,
    *,
    archive_ids: set[str],
    covered: set[tuple[int, str, str]],
) -> list[dict]:
    """Return stored fortnights that still contain a publication the archive lacks.

    Fresh downloads cover the recent window. An older fortnight is kept when
    deleting it would drop a number that never reached the archive.
    """
    if not root.is_dir():
        return []
    kept: list[dict] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            period_no = int(manifest["period"])
            date_from = str(manifest["from"])
            date_to = str(manifest["to"])
            csv_meta = manifest["csv"]
            member = str(csv_meta["member"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{directory.name} manifest cannot be read") from exc
        key = (period_no, date_from, date_to)
        if key in covered:
            continue
        csv_path = directory / member
        if not csv_path.is_file():
            raise ValueError(f"{directory.name} is missing {member}")
        blob = csv_path.read_bytes()
        rows = parse_csv(blob, filename=member)
        if all(row.number in archive_ids for row in rows):
            continue
        html = manifest.get("html") or {}
        pdf = manifest.get("pdf") or {}
        kept.append(
            {
                "period": BiweeklyPeriod(
                    period=period_no,
                    date_from=date_from,
                    date_to=date_to,
                    csv_url=str(csv_meta.get("url") or ""),
                    csv_label=str(csv_meta.get("label") or "CSV"),
                    html_url=str(html.get("url") or ""),
                    html_label=str(html.get("label") or "HTML"),
                    pdf_url=str(pdf.get("url") or ""),
                    pdf_label=str(pdf.get("label") or "PDF"),
                ),
                "member": member,
                "csv_bytes": blob,
                "rows": rows,
                "zip_filename": csv_meta.get("zip_filename"),
            }
        )
    return kept


def fetch_biweekly_periods(
    client: HttpClient,
    *,
    archive_max: date,
    today: date,
) -> tuple[list[BiweeklyPeriod], list[str]]:
    """Download the current index and the previous year when the overlap needs it."""
    print("Fetching biweekly index…")
    index = parse_biweekly_index(client.get(BIWEEKLY_INDEX_URL).body.decode("utf-8", "replace"))
    warnings = list(index.warnings)
    periods = list(index.periods)
    window_start = archive_max - timedelta(days=OVERLAP_DAYS)
    newest_end = max((date.fromisoformat(_period_end(period)) for period in periods), default=None)
    stale = newest_end is None or newest_end < today - timedelta(days=FRESH_DAYS)
    short = not periods or _earliest_start(periods) > window_start
    if (stale or short) and index.years:
        year = max(index.years)
        print(f"Fetching biweekly index for {year}…")
        older = parse_biweekly_index(
            client.get(f"{BIWEEKLY_INDEX_URL}/?year={year}").body.decode("utf-8", "replace")
        )
        warnings.extend(older.warnings)
        periods = _merge_periods(periods, older.periods)
    return select_periods(periods, archive_max=archive_max, today=today), warnings


def _period_end(period: BiweeklyPeriod) -> str:
    """Later published date. One historical row has its from/to swapped."""
    return max(period.date_from, period.date_to)


def _earliest_start(periods: list[BiweeklyPeriod]) -> date:
    return min(date.fromisoformat(min(period.date_from, period.date_to)) for period in periods)


def _merge_periods(
    current: list[BiweeklyPeriod],
    older: list[BiweeklyPeriod],
) -> list[BiweeklyPeriod]:
    merged: dict[tuple[int, str, str], BiweeklyPeriod] = {}
    for period in older + current:
        merged[(period.period, period.date_from, period.date_to)] = period
    return list(merged.values())


def _absolute(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("https://") or href.startswith("http://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("/"):
        href = "/" + href
    return TOOL_ORIGIN + href


def _cell_text(events: list[tuple]) -> str:
    parts: list[str] = []
    for kind, value in events:
        if kind == "text":
            parts.append(value)
        elif kind == "br":
            parts.append(" ")
    return clean("".join(parts))


def _first_link(events: list[tuple]) -> tuple[str, str]:
    href: str | None = None
    label: list[str] = []
    for kind, value in events:
        if kind == "a_start":
            href = value or ""
            label = []
        elif kind == "text" and href is not None:
            label.append(value)
        elif kind == "img" and href is not None and value:
            label.append(value)
        elif kind == "a_end" and href is not None:
            text = clean("".join(label))
            # Prefer the icon's alt ("CSV") over the file-size text ("4 kb").
            alt = next((item for item in label if item in {"CSV", "HTML", "PDF"}), "")
            return href, clean(alt or text)
    return "", ""


class _IndexParser(HTMLParser):
    """Collect rows of the biweekly index table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found_table = False
        self.rows: list[list[list[tuple]]] = []
        self._in_table = False
        self._row: list | None = None
        self._cell: list | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "table" and attributes.get("id") == "tblBiweekly":
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
            alt = attributes.get("alt", "")
            if alt:
                self._cell.append(("img", alt))
        elif tag == "a":
            self._cell.append(("a_start", attributes.get("href", "")))

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
        elif tag == "a" and self._cell is not None:
            self._cell.append(("a_end", None))

    def handle_data(self, data: str) -> None:
        if self._cell is not None and data:
            self._cell.append(("text", data))
