"""Read one biweekly CSV out of the zip EASA links from the interim page.

The CSV is the source of truth for that fortnight. It has no header and no
publication class. Columns, in order, are number, issue date, approval
holder, type designation, and subject. The HTML and PDF bundles linked
beside it repeat the same list and are not parsed.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass

from easa_ads.normalize import clean

# A biweekly file is a few kilobytes. Refuse anything that does not look
# like that kind of file so a hostile or truncated zip cannot expand forever.
MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_ZIP_MEMBERS = 5


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
