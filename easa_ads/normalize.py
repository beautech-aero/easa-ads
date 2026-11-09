"""Turn one EASA row, from any source, into the shared publication record.

The archive export, the biweekly CSV, and the interim HTML page do not use
the same columns. This module is the only place that knows those differences.
Everything written under ``data/`` passes through ``build_record``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from easa_ads.const import AD_CLASSES, ALL_CLASSES, ORIGIN, SCHEMA_VERSION, SD_CLASS, SIB_CLASS

# Keys every stored record has, in this order, so git diffs stay local.
PUBLIC_KEYS = (
    "schema_version",
    "id",
    "number",
    "base_number",
    "revision",
    "correction",
    "revises",
    "cancels",
    "class",
    "class_source",
    "emergency",
    "cancelled",
    "marked_correction",
    "issue_date",
    "effective_date",
    "subject",
    "approval_holders",
    "models",
    "documents",
    "issued_by",
    "superseded_by",
    "superseded_by_text",
    "supersedes",
    "source",
    "sources",
    "raw",
)

# Number prefixes that, in the SIB/SD archive, belong to safety information
# bulletins and never to airworthiness directives. Used only when a row has
# no explicit EASA class (biweekly CSV and some interim rows).
_SIB_NUMBER = re.compile(
    r"^(?:"
    r"SAIB-|SAFO-|CASA-|BEA-|NM-|SW-|AV-|HQ-|FA-|AL-|AWB-|NE-|CE-|SN-|InFO-|"
    r"AIR-|AIR\d"
    r")",
    re.IGNORECASE,
)
# EASA's own SIB numbers are YYYY-NN. AD numbers are YYYY-NNNN. No AD in the
# archive uses a 2-digit sequence, so this is safe for rows that lack a class.
_EASA_SIB_NUMBER = re.compile(r"^\d{4}-\d{2}(?:R\d+)?(?:-CN)?$")

_CATEGORY_TOKENS = {
    "airworthiness",
    "fixed wings",
    "rotorcraft",
    "equipment",
    "engines",
    "propeller",
    "operations",
    "atm",
    "ans",
}

# A comma-separated piece that continues the previous legal name rather than
# starting another holder. Used for the biweekly holder cell only.
_HOLDER_CONTINUATION = re.compile(
    r"(?i)^("
    r"gmbh\b.*|inc\.?\b.*|sas\b.*|s\.a\.s\.?.*|s\.a\.\b.*|s\.p\.a\.?.*|"
    r"spa\b.*|ltd\.?\b.*|lp\b.*|llc\b.*|ag\b.*|kg\b.*|nv\b.*|b\.v\..*|"
    r"ab\b.*|plc\b.*|s\.r\.o\.?.*|co\b.*|costruzioni\b.*|helicopteres\b.*|"
    r"hélicoptères\b.*|spol\b.*|holdings\b.*"
    r")$"
)

_ANCHOR = re.compile(
    r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Publication numbers inside "SUPERSEDED BY ..." subjects. The alternatives
# are ordered so a prefixed number (US-2026-14-11, SD-2021-05, DCA/...,
# G-1266-PRE80) is taken whole before a bare year-sequence is considered.
# Two-digit years (FAA 99-18-19, DGAC 98-508-106) are real citation forms.
_CITED_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])("
    r"DCA(?:[/-][A-Z0-9]+)+"
    r"|[A-Z]{1,6}-\d{2,4}-(?:[A-Z]{2,}\d{2,}|\d{2,5})(?:-\d{2,4})?(?:-[A-Z])?(?:R\d+)?(?:-E)?(?:-CN)?"
    r"|[A-Z]\d{2}-\d{2,3}-\d{2,3}"
    r"|\d{4}-\d{2,4}(?:-\d{2})?(?:R\d+)?(?:-E)?(?:-CN)?"
    r"|\d{2,3}-\d{2,3}-\d{2,3}"
    r"|\d{4}-[A-Z]{2,}-\d+[A-Z0-9-]*"
    r")(?:\s+(R\d+))?"
)
_AGENCY = re.compile(
    r"\b(EASA|FAA|ANAC|TCCA|DGAC|CAA|LBA|ENAC|FATA|TRANSPORT|CANADIAN|BRAZIL|TC)\b",
    re.IGNORECASE,
)
_CANCELLED_SUBJECT = re.compile(
    r"(?i)^(?:\[correction\]\s*)?(?:cancellation notice|cancelled)\b"
)
_CUT_CITATION = re.compile(
    r"\s+-\s+SEE\b|\s+-\s+EASA DECISION\b|\s+DATED\b|\s+\[|\s+\(SB\b|\s+\(REF\.?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedNumber:
    """How a publication number breaks into a base, a revision, and status."""

    base_number: str
    revision: str | None
    correction: str | None
    revises: str | None
    cancels: str | None
    emergency: bool
    cancelled: bool


@dataclass(frozen=True)
class Supersession:
    ids: list[str]
    texts: list[str]
    unparsed: bool


def clean(value: str | None) -> str:
    """Collapse whitespace and drop characters that are not really text."""
    if not value:
        return ""
    text = (
        value.replace("\u00ad", "")
        .replace("\xa0", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value: str | None) -> str | None:
    """Return an ISO date, or None when the cell is empty.

    Archive exports use MM/DD/YYYY. The interim page and biweekly CSVs use
    YYYY-MM-DD. A non-empty value that matches neither is an error: guessing
    day-month order would silently swap dates.
    """
    text = clean(value)
    if not text or text in {"-", "—", "n/a", "N/A"}:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {value!r}")


def parse_number(number: str) -> ParsedNumber:
    """Split a publication number into base, revision, and status markers.

    ``-CN`` is a cancellation notice. ``R`` plus digits is a revision
    (``R01`` is revision 1, kept as published). ``C`` plus digits is a
    correction issue. A trailing ``-E``, ``R1-E``, or glued ``-ER1`` marks
    an emergency AD. ``-E`` stays part of ``base_number`` when the number
    has no revision, because ``2026-0191-E`` is the document's own number.
    """
    original = clean(number)
    if not original:
        raise ValueError("empty publication number")
    cancelled = False
    cancels: str | None = None
    work = original
    if work.endswith("-CN"):
        cancelled = True
        cancels = work[: -3]
        work = cancels

    emergency = False
    revision: str | None = None
    correction: str | None = None
    base = work

    glued = re.fullmatch(r"(?P<base>.*?)-E(?P<rev>R\d+)", work)
    suffixed = re.fullmatch(r"(?P<base>.*?)(?P<rev>R\d+)-E", work)
    revised = re.fullmatch(r"(?P<base>.*?)(?P<rev>R\d+)", work)
    corrected = re.fullmatch(r"(?P<base>.*?)(?P<cor>C\d+)", work)
    if glued and glued.group("base"):
        emergency = True
        revision = glued.group("rev")
        base = glued.group("base")
    elif suffixed and suffixed.group("base"):
        emergency = True
        revision = suffixed.group("rev")
        base = suffixed.group("base")
    elif revised and revised.group("base"):
        revision = revised.group("rev")
        base = revised.group("base")
    elif corrected and corrected.group("base"):
        correction = corrected.group("cor")
        base = corrected.group("base")
    elif work.endswith("-E"):
        emergency = True
        base = work

    revises: str | None = None
    if revision:
        revises = _previous_revision(base, revision)
    return ParsedNumber(
        base_number=base,
        revision=revision,
        correction=correction,
        revises=revises,
        cancels=cancels,
        emergency=emergency,
        cancelled=cancelled,
    )


def _previous_revision(base: str, revision: str) -> str:
    number = int(revision[1:])
    if number <= 1:
        return base
    width = len(revision) - 1
    previous = f"{number - 1:0{width}d}" if width > 1 else str(number - 1)
    return f"{base}R{previous}"


def bucket_for(class_name: str) -> str:
    """Folder name for a publication class. Unknown classes are an error."""
    if class_name in AD_CLASSES:
        return "ad"
    if class_name == SIB_CLASS:
        return "sib"
    if class_name == SD_CLASS:
        return "sd"
    raise ValueError(f"no folder for publication class {class_name!r}")


def infer_class(
    number: str,
    subject: str,
    *,
    explicit: str | None = None,
    icon: str | None = None,
) -> tuple[str, str]:
    """Return ``(class, class_source)``.

    The archive's own class wins. Otherwise an icon on the interim page,
    then the number and the subject. Rows that still look like ordinary
    ADs are class ``AD`` with source ``inferred``.
    """
    if explicit:
        if explicit not in ALL_CLASSES:
            raise ValueError(f"unknown EASA class {explicit!r} on {number}")
        return explicit, "archive"
    if icon:
        normalized_icon = icon.upper()
        if normalized_icon in ALL_CLASSES:
            return normalized_icon, "interim"
    if number.startswith("SD-"):
        return SD_CLASS, "inferred"
    if _SIB_NUMBER.match(number) or _EASA_SIB_NUMBER.match(number):
        return SIB_CLASS, "inferred"
    if re.match(r"(?i)^safety information bulletin\b", subject or ""):
        return SIB_CLASS, "inferred"
    if re.match(r"(?i)^(?:\[correction\]\s*)?proposed ad\b", subject or ""):
        return "PAD", "inferred"
    if parse_number(number).emergency:
        return "EAD", "inferred"
    return "AD", "inferred"


def absolute_url(href: str) -> str:
    """Make an EASA href absolute without rewriting a URL that already is."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("https://") or href.startswith("http://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if not href.startswith("/"):
        href = "/" + href
    return ORIGIN + href


def document_kind(url: str, label: str) -> str:
    path = url.split("?", 1)[0].lower()
    for extension, kind in (
        (".pdf", "pdf"),
        (".zip", "zip"),
        (".csv", "csv"),
        (".html", "html"),
        (".htm", "html"),
    ):
        if path.endswith(extension):
            return kind
    lowered = label.lower()
    if "pdf" in lowered:
        return "pdf"
    if "zip" in lowered:
        return "zip"
    if "csv" in lowered or "cvs" in lowered:
        return "csv"
    return "other"


def parse_documents_html(html: str | None) -> list[dict]:
    """Pull PDF/ZIP anchors out of the archive's ``Documents`` cell."""
    documents: list[dict] = []
    seen: set[str] = set()
    for href, inner in _ANCHOR.findall(html or ""):
        label = clean(re.sub(r"<[^>]+>", "", inner))
        url = absolute_url(href)
        if not url or url in seen:
            continue
        seen.add(url)
        if not label:
            label = url.rstrip("/").rsplit("/", 1)[-1]
        documents.append({"url": url, "label": label, "kind": document_kind(url, label)})
    return documents


def parse_designation(raw: str | None) -> tuple[list[str], list[str]]:
    """Split ``Approval Holder / Type Designation``.

    The archive stores the holder on the first line and the models on the
    second, separated by ``/``. Spaces around a slash are not special:
    ``AS 350 / EC 130`` becomes the two models a search would expect.
    ``HOLDER:model`` inside the model line is a second approval holder
    (common for STC holders) followed by that holder's model.

    SIB rows often end in a category path such as
    ``Safety Information Bulletin:Airworthiness/Fixed Wings``. Those words
    describe the bulletin, not an aircraft, so they are dropped.
    """
    text = (raw or "").replace("\u00ad", "").replace("\xa0", " ").replace("\ufeff", "")
    lines = [clean(line) for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return [], []
    extra_holders, models = _tokens_from_model_line("/".join(lines[1:]))
    holders = _dedupe([lines[0], *extra_holders])
    return holders, _dedupe(models)


def parse_biweekly_holders(raw: str | None) -> list[str]:
    """Split a biweekly holder cell on commas, keeping legal-name suffixes.

    ``DAHER AIRCRAFT, SAS`` stays one holder. ``AIRBUS S.A.S., ELBE
    FLUGZEUGWERKE GmbH`` becomes two. The archive's first line is a single
    name and is not passed through here.
    """
    text = clean(raw)
    if not text:
        return []
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return []
    merged = [parts[0]]
    for part in parts[1:]:
        previous = merged[-1]
        if (
            _HOLDER_CONTINUATION.match(part)
            or previous.endswith("&")
            or previous.endswith(" Co.")
            or previous.endswith(" Co")
        ):
            merged[-1] = f"{previous}, {part}"
        else:
            merged.append(part)
    return _dedupe(merged)


def parse_biweekly_models(raw: str | None) -> tuple[list[str], list[str]]:
    """Split the biweekly type-designation cell.

    Commas separate designations. Slashes inside one designation are the
    same model separator the archive uses. Returns extra holders (from
    ``HOLDER:model`` pieces) and model tokens.
    """
    text = clean(raw)
    if not text:
        return [], []
    holders: list[str] = []
    models: list[str] = []
    for part in (piece.strip() for piece in text.split(",")):
        if not part:
            continue
        extra, tokens = _tokens_from_model_line(part)
        holders.extend(extra)
        models.extend(tokens)
    return _dedupe(holders), _dedupe(models)


def _tokens_from_model_line(model_line: str) -> tuple[list[str], list[str]]:
    if not model_line:
        return [], []
    sib_categories = "safety information bulletin:" in model_line.casefold()
    tokens = [clean(token) for token in model_line.split("/") if clean(token)]
    if sib_categories:
        tokens = _drop_sib_categories(tokens)
    holders: list[str] = []
    models: list[str] = []
    for token in tokens:
        if ":" in token:
            left, right = (clean(piece) for piece in token.split(":", 1))
            if right and _looks_like_holder(left):
                holders.append(left)
                models.append(right)
                continue
        if token:
            models.append(token)
    return holders, rejoin_variant_suffixes(models)


def rejoin_variant_suffixes(tokens: list[str]) -> list[str]:
    """Stick variant suffixes back onto the model they were split from.

    EASA separates designations with ``/``, and some designations contain
    ``/`` themselves (``CFM56-7B20/2``, ``GEnx-1B70/75``, ``AT-600/-800``).
    A suffix is a short trailing piece (``2``, ``3B1``, ``B1``, ``-1C``)
    after a hyphenated model. Standalone models such as ``ATR42`` / ``200``
    or ``737-800`` are left split, because the following token is itself a
    model rather than a suffix.
    """
    if not tokens:
        return tokens
    suffix = re.compile(r"^(?:\d{1,2}[A-Z]{0,2}|\d[A-Z]\d[A-Z]?|[A-Z]\d)$")
    joined = [tokens[0]]
    for token in tokens[1:]:
        previous = joined[-1]
        hyphen_tail = token.startswith("-") and previous and not previous.endswith("-")
        variant_tail = (
            bool(suffix.fullmatch(token))
            and "-" in previous
            and any(character.isalpha() for character in previous)
            and any(character.isdigit() for character in previous)
        )
        if hyphen_tail or variant_tail:
            joined[-1] = f"{previous}/{token}"
            continue
        joined.append(token)
    return joined


def _drop_sib_categories(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    for token in tokens:
        if token.casefold().startswith("safety information bulletin"):
            rest = clean(token.split(":", 1)[1]) if ":" in token else ""
            if rest and rest.casefold() not in _CATEGORY_TOKENS:
                kept.append(rest)
            continue
        kept.append(token)

    def is_category(token: str) -> bool:
        return token.casefold() in _CATEGORY_TOKENS

    while kept and is_category(kept[0]):
        kept.pop(0)
    while kept and is_category(kept[-1]):
        kept.pop()
    return kept


def _looks_like_holder(value: str) -> bool:
    """True when the left side of ``HOLDER:model`` is an organisation name."""
    if not value or value.casefold().startswith("safety information"):
        return False
    if " " in value or "(" in value or "," in value:
        return True
    letters = [char for char in value if char.isalpha()]
    digits = [char for char in value if char.isdigit()]
    if not letters or digits:
        return False
    upper = sum(1 for char in letters if char.isupper())
    return upper >= max(3, int(len(letters) * 0.8))


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def extract_supersession(subject: str | None) -> Supersession:
    """Read ``SUPERSEDED BY ...`` citations out of a subject line.

    Subjects are the only place the archive records supersession. The text
    is not a controlled vocabulary: authorities and number punctuation vary,
    and a few old rows say ``SUPERSEDED:`` without giving a number. Those
    come back with ``unparsed=True`` so the ingest report can list them.

    FAA numbers are stored in this dataset with a ``US-`` prefix, ANAC with
    ``BR-``, and TCCA / Transport Canada with ``CF-``. The citation is
    rewritten to that id when the mapping is unambiguous.
    """
    text = clean(subject).replace("\u2013", "-").replace("\u2014", "-")
    if not text or re.search(r"NO SUPERSEDING", text, re.IGNORECASE):
        return Supersession([], [], False)
    match = re.search(r"SUPERSEDED\s+BY\s+(.+)$", text, re.IGNORECASE)
    if not match:
        unparsed = bool(re.search(r"\bSUPERSEDED\b", text, re.IGNORECASE))
        return Supersession([], [], unparsed)

    body = _CUT_CITATION.split(match.group(1).strip(), maxsplit=1)[0].strip(" .;")
    parts = re.split(r"\s+(?:AND|and)\s+", body)
    ids: list[str] = []
    texts: list[str] = []
    inherited = ""
    for part in parts:
        part = part.strip(" .;")
        if not part:
            continue
        found = _CITED_NUMBER.search(part)
        if not found:
            continue
        number = found.group(1)
        if found.group(2):
            number += found.group(2)
        zone = part[: found.start()]
        if _AGENCY.search(zone):
            authority = zone
            inherited = zone
        else:
            authority = inherited
        normalized = _normalize_citation(number, authority)
        if normalized and normalized not in ids:
            ids.append(normalized)
            texts.append(clean(part))
    pairs = sorted(zip(ids, texts), key=lambda pair: pair[0])
    if pairs:
        ids, texts = [list(column) for column in zip(*pairs)]
    else:
        ids, texts = [], []
    return Supersession(ids, texts, not ids)


def _normalize_citation(number: str, authority: str) -> str:
    number = number.strip()
    ad_hyphen = re.fullmatch(
        r"AD-(\d{2,4}-\d{2,4}(?:-\d{2})?(?:R\d+)?(?:-E)?)",
        number,
        re.IGNORECASE,
    )
    if ad_hyphen:
        number = ad_hyphen.group(1)
    if re.match(r"[A-Za-z]", number):
        return number
    auth = authority.upper()
    if "FAA" in auth:
        return "US-" + number
    if "ANAC" in auth or "BRAZIL" in auth:
        return "BR-" + number
    if (
        "TCCA" in auth
        or "TRANSPORT CANADA" in auth
        or "CANADIAN" in auth
        or re.search(r"\bTC\b", auth)
    ):
        return "CF-" + number
    if re.search(r"\bSD\b", auth):
        return "SD-" + number
    # Old DGAC citations use a two-digit year ("98-508-106") for a number
    # the archive stores as F-1998-508-106. UK CAA uses G-009-02-93.
    short_year = re.fullmatch(r"(\d{2})-(\d{2,3}-\d{2,3})", number)
    if short_year and "DGAC" in auth:
        year = int(short_year.group(1))
        century = 1900 if year >= 50 else 2000
        return f"F-{century + year}-{short_year.group(2)}"
    if re.fullmatch(r"\d{3}-\d{2}-\d{2}", number) and "CAA" in auth:
        return "G-" + number
    return number


def drop_category_labels(holders: list[str], models: list[str]) -> tuple[list[str], list[str]]:
    """Remove SIB category labels that the interim page nests under the holder.

    The archive flattens ``Safety Information Bulletin:Airworthiness/Fixed
    Wings`` into the model line. The interim page sometimes nests the same
    words as extra list items. They are not type designations.
    """
    names = [value.casefold() for value in holders + models]
    if not any(name.startswith("safety information") or name == "safety directives" for name in names):
        return holders, models
    skip_holders = {"safety information bulletin", "safety directives"}
    kept_holders = [value for value in holders if value.casefold() not in skip_holders]
    kept_models = [
        value
        for value in models
        if value.casefold() not in _CATEGORY_TOKENS
        and value.casefold() not in skip_holders
    ]
    return kept_holders, kept_models


def subject_says_cancelled(subject: str) -> bool:
    return bool(_CANCELLED_SUBJECT.match(clean(subject)))


def build_record(
    *,
    number: str,
    subject: str,
    issue_date: str | None,
    effective_date: str | None,
    holders: list[str],
    models: list[str],
    documents: list[dict],
    origin: str,
    source_url: str,
    raw: dict,
    explicit_class: str | None = None,
    icon_class: str | None = None,
    issued_by: str | None = None,
    dataset: str | None = None,
    period: int | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
) -> dict:
    """Build one source's view of a publication. Merge combines these."""
    number = clean(number)
    subject = clean(subject)
    parsed = parse_number(number)
    class_name, class_source = infer_class(
        number,
        subject,
        explicit=explicit_class,
        icon=icon_class,
    )
    supersession = extract_supersession(subject)
    source: dict = {"kind": origin, "url": source_url}
    if dataset:
        source["dataset"] = dataset
    if period is not None:
        source["period"] = period
    if period_from:
        source["from"] = period_from
    if period_to:
        source["to"] = period_to
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": number,
        "number": number,
        "base_number": parsed.base_number,
        "revision": parsed.revision,
        "correction": parsed.correction,
        "revises": parsed.revises,
        "cancels": parsed.cancels,
        "class": class_name,
        "class_source": class_source,
        "emergency": parsed.emergency or class_name == "EAD",
        "cancelled": parsed.cancelled or subject_says_cancelled(subject),
        "marked_correction": subject.startswith("[Correction]"),
        "issue_date": issue_date,
        "effective_date": effective_date,
        "subject": subject,
        "approval_holders": list(holders),
        "models": list(models),
        "documents": list(documents),
        "issued_by": clean(issued_by) or None,
        "superseded_by": supersession.ids,
        "superseded_by_text": supersession.texts,
        "supersedes": [],
        "source": origin,
        "sources": [source],
        "raw": raw,
        "_supersession_unparsed": supersession.unparsed,
        "_period": period or 0,
    }
    return record


def public_record(record: dict) -> dict:
    """Drop merge bookkeeping and lock the key order used on disk."""
    return {key: record[key] for key in PUBLIC_KEYS}


def link_supersession(records: list[dict]) -> None:
    """Fill ``supersedes`` from other records' ``superseded_by`` lists.

    A citation is linked only when the cited id is itself in the dataset.
    Citations that point outside the dataset stay on ``superseded_by``.
    """
    by_id = {record["id"]: record for record in records}
    incoming: dict[str, list[str]] = {record_id: [] for record_id in by_id}
    for record in records:
        for target in record["superseded_by"]:
            if target in incoming and target != record["id"]:
                incoming[target].append(record["id"])
    for record_id, predecessors in incoming.items():
        by_id[record_id]["supersedes"] = sorted(set(predecessors))
