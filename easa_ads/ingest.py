"""Fetch EASA's public files and write the normalized dataset.

Raw snapshots are kept so a later change can be diffed. Normalized JSONL is
what a bot should read. Nothing here embeds text or writes to a database.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

from easa_ads.biweekly import (
    fetch_biweekly_periods,
    load_biweekly_download,
    parse_csv,
    stored_biweekly_gaps,
)
from easa_ads.const import (
    ARCHIVE_AD_PAGE,
    ARCHIVE_AD_URL,
    ARCHIVE_SIB_SD_PAGE,
    ARCHIVE_SIB_SD_URL,
    COPYRIGHT_URL,
    SAFETY_PUBLICATIONS_TOOL_URL,
)
from easa_ads.http import HttpClient
from easa_ads.merge import merge_contributions
from easa_ads.normalize import (
    bucket_for,
    build_record,
    link_supersession,
    parse_biweekly_holders,
    parse_biweekly_models,
    parse_date,
    parse_designation,
    parse_documents_html,
    public_record,
)
from easa_ads.safety_publications_tool import (
    dedupe_publications,
    fetch_recent_publications,
    fetch_safety_directives,
    is_newer_than_archive,
    listing_window,
)
MIN_ARCHIVE_AD = 10_000
MIN_ARCHIVE_SIB = 500
MIN_ARCHIVE_SD = 1

_ARCHIVE_FIELDS = (
    "AD class",
    "Class number",
    "Documents",
    "Issue Date",
    "Subject",
    "Approval Holder / Type Designation",
    "Effective date",
)


def ingest(data_dir: Path, client: HttpClient) -> dict:
    """Fetch every source, then replace ``data_dir`` contents on success.

    Validation happens before any write. A short or unreadable download
    leaves the previous dataset in place.
    """
    print("Fetching AD archive…")
    ad_blob = client.get(ARCHIVE_AD_URL).body
    ad_payload = _load_archive(ad_blob, "AD archive")
    print("Fetching SIB/SD archive…")
    sib_blob = client.get(ARCHIVE_SIB_SD_URL).body
    sib_payload = _load_archive(sib_blob, "SIB/SD archive")
    ad_rows = ad_payload["data"]
    sib_rows = sib_payload["data"]
    _require_archive_size(ad_rows, sib_rows)
    archive_dates = _archive_dates(ad_rows, sib_rows)
    ad_max = _max_issue_date(ad_rows)
    sib_max = _max_issue_date(row for row in sib_rows if row.get("AD class") == "SIB")
    if ad_max is None or sib_max is None:
        raise RuntimeError("archive issue dates could not be read")
    today = date.today()
    periods, index_warnings = fetch_biweekly_periods(
        client,
        archive_max=date.fromisoformat(ad_max),
        today=today,
    )

    biweekly_files: list[dict] = []
    for period in periods:
        print(f"Fetching biweekly {period.period} ({period.date_from} to {period.date_to}) CSV…")
        fetched = client.get(period.csv_url)
        member, csv_bytes = load_biweekly_download(
            fetched.body,
            filename=_download_name(fetched.headers, period.csv_url),
        )
        rows = parse_csv(csv_bytes, filename=member)
        biweekly_files.append(
            {
                "period": period,
                "member": member,
                "csv_bytes": csv_bytes,
                "rows": rows,
                "zip_filename": _content_filename(fetched.headers),
            }
        )

    covered = {
        (item["period"].period, item["period"].date_from, item["period"].date_to)
        for item in biweekly_files
    }
    retained = stored_biweekly_gaps(
        data_dir / "raw" / "biweekly",
        archive_ids=set(archive_dates),
        covered=covered,
    )
    for item in retained:
        period = item["period"]
        print(
            f"Keeping stored biweekly {period.period} "
            f"({period.date_from} to {period.date_to}); the archive does not list every row"
        )
    biweekly_files.extend(retained)
    biweekly_files.sort(key=lambda item: (item["period"].date_from, item["period"].period))

    start, end = listing_window(date.fromisoformat(ad_max), date.fromisoformat(sib_max), today)
    recent = fetch_recent_publications(client, start=start, end=end)
    directives = [
        row
        for row in fetch_safety_directives(client)
        if row.issue_date and start.isoformat() <= row.issue_date <= end.isoformat()
    ]
    listing = [
        row
        for row in dedupe_publications(recent + directives)
        if is_newer_than_archive(row.issue_date, archive_dates.get(row.number))
    ]

    contributions = _contributions(ad_rows, sib_rows, biweekly_files, listing)
    grouped: dict[str, list[dict]] = {}
    for record in contributions:
        grouped.setdefault(record["id"], []).append(record)
    merged = merge_contributions(grouped)
    link_supersession(merged)
    _require_unique(merged)

    stats = _stats(ad_payload, sib_payload, biweekly_files, listing, start, end, merged)
    report = _parse_report(merged, index_warnings)
    _write_all(
        data_dir,
        ad_blob=ad_blob,
        sib_blob=sib_blob,
        biweekly_files=biweekly_files,
        listing=listing,
        window_start=start,
        window_end=end,
        merged=merged,
        stats=stats,
        report=report,
    )
    print(
        f"Wrote {stats['normalized']['all']['records']} publications "
        f"({stats['normalized']['ad']['records']} AD-family, "
        f"{stats['normalized']['sib']['records']} SIB, "
        f"{stats['normalized']['sd']['records']} SD)"
    )
    return stats


def _load_archive(blob: bytes, label: str) -> dict:
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} was not a JSON object") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"{label} has no data array")
    return payload


def _require_archive_size(ad_rows: list, sib_rows: list) -> None:
    classes = Counter(row.get("AD class") for row in sib_rows)
    if len(ad_rows) < MIN_ARCHIVE_AD:
        raise RuntimeError(f"AD archive has {len(ad_rows)} rows, expected at least {MIN_ARCHIVE_AD}")
    if classes.get("SIB", 0) < MIN_ARCHIVE_SIB or classes.get("SD", 0) < MIN_ARCHIVE_SD:
        raise RuntimeError(f"SIB/SD archive class counts look wrong: {dict(classes)}")
    for label, rows in (("AD", ad_rows), ("SIB/SD", sib_rows)):
        missing = [field for field in _ARCHIVE_FIELDS if field not in rows[0]]
        if missing:
            raise RuntimeError(f"{label} archive is missing columns {missing}")


def _archive_dates(ad_rows, sib_rows) -> dict[str, str | None]:
    dates: dict[str, str | None] = {}
    for row in list(ad_rows) + list(sib_rows):
        number = str(row.get("Class number") or "")
        if number:
            dates[number] = parse_date(row.get("Issue Date"))
    return dates


def _max_issue_date(rows) -> str | None:
    dates = []
    for row in rows:
        try:
            parsed = parse_date(row.get("Issue Date"))
        except ValueError:
            parsed = None
        if parsed:
            dates.append(parsed)
    if not dates:
        return None
    return max(dates)


def _contributions(ad_rows, sib_rows, biweekly_files, listing) -> list[dict]:
    records: list[dict] = []
    seen_archive: set[tuple[str, str]] = set()
    for dataset, url, rows in (
        ("airworthiness-directives", ARCHIVE_AD_URL, ad_rows),
        ("sib-sd", ARCHIVE_SIB_SD_URL, sib_rows),
    ):
        for row in rows:
            record = _from_archive(row, dataset=dataset, source_url=url)
            key = (dataset, record["id"])
            if key in seen_archive:
                raise RuntimeError(f"duplicate {record['id']} in {dataset}")
            seen_archive.add(key)
            records.append(record)
    for item in biweekly_files:
        period = item["period"]
        seen_in_period: set[str] = set()
        for row in item["rows"]:
            if row.number in seen_in_period:
                continue
            seen_in_period.add(row.number)
            records.append(_from_biweekly(row, period))
    seen_listing: set[str] = set()
    for row in listing:
        if row.number in seen_listing:
            continue
        seen_listing.add(row.number)
        records.append(_from_listing(row))
    return records


def _from_archive(row: dict, *, dataset: str, source_url: str) -> dict:
    holders, models = parse_designation(row.get("Approval Holder / Type Designation"))
    return build_record(
        number=str(row.get("Class number") or ""),
        subject=str(row.get("Subject") or ""),
        issue_date=parse_date(row.get("Issue Date")),
        effective_date=parse_date(row.get("Effective date")),
        holders=holders,
        models=models,
        documents=parse_documents_html(row.get("Documents")),
        origin="archive",
        source_url=source_url,
        dataset=dataset,
        explicit_class=str(row.get("AD class") or "") or None,
        raw={
            "ad_class": row.get("AD class") or "",
            "number": row.get("Class number") or "",
            "issue_date": row.get("Issue Date") or "",
            "effective_date": row.get("Effective date") or "",
            "subject": row.get("Subject") or "",
            "approval_holder_type_designation": row.get("Approval Holder / Type Designation") or "",
            "documents_html": row.get("Documents") or "",
        },
    )


def _from_biweekly(row, period) -> dict:
    extra_holders, models = parse_biweekly_models(row.models)
    holders = parse_biweekly_holders(row.holders)
    for holder in extra_holders:
        if holder not in holders:
            holders.append(holder)
    return build_record(
        number=row.number,
        subject=row.subject,
        issue_date=parse_date(row.issue_date),
        effective_date=None,
        holders=holders,
        models=models,
        documents=[],
        origin="biweekly",
        source_url=period.csv_url,
        period=period.period,
        period_from=period.date_from,
        period_to=period.date_to,
        raw={
            "number": row.number,
            "issue_date": row.issue_date,
            "approval_holders": row.holders,
            "type_designation": row.models,
            "subject": row.subject,
        },
    )


def _from_listing(row) -> dict:
    # ``interim`` stays the source kind: it is the live, detailed list that
    # the retired interim page used to be, and the record schema already
    # allows that kind.
    return build_record(
        number=row.number,
        subject=row.subject,
        issue_date=row.issue_date,
        effective_date=row.effective_date,
        holders=list(row.holders),
        models=list(row.models),
        documents=list(row.documents),
        origin="interim",
        source_url=row.url,
        icon_class=row.class_hint,
        issued_by=row.issued_by,
        raw={
            "number": row.number,
            "issued_by": row.issued_by or "",
            "issue_date": row.issue_date or "",
            "effective_date": row.effective_date or "",
            "subject": row.subject,
            "class_hint": row.class_hint or "",
            "approval_holders": list(row.holders),
            "models": list(row.models),
            "documents": [{"url": doc["url"], "label": doc["label"]} for doc in row.documents],
        },
    )


def _require_unique(records: list[dict]) -> None:
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise RuntimeError("merged dataset has duplicate publication numbers")


def _stats(ad_payload, sib_payload, biweekly_files, listing, window_start, window_end, merged) -> dict:
    ad_rows = ad_payload["data"]
    sib_rows = sib_payload["data"]

    def span(rows, field="Issue Date"):
        dates = []
        for row in rows:
            try:
                parsed = parse_date(row.get(field))
            except ValueError:
                parsed = None
            if parsed:
                dates.append(parsed)
        if not dates:
            return None, None
        return min(dates), max(dates)

    ad_min, ad_max = span(ad_rows)
    sib_min, sib_max = span(sib_rows)
    listing_dates = [row.issue_date for row in listing if row.issue_date]
    by_bucket: dict[str, list[dict]] = {"ad": [], "sib": [], "sd": []}
    for record in merged:
        by_bucket[bucket_for(record["class"])].append(record)

    def bucket_stats(records: list[dict]) -> dict:
        dates = [record["issue_date"] for record in records if record["issue_date"]]
        return {
            "records": len(records),
            "by_class": dict(sorted(Counter(record["class"] for record in records).items())),
            "issue_date_min": min(dates) if dates else None,
            "issue_date_max": max(dates) if dates else None,
        }

    def source_kinds(record: dict) -> set[str]:
        return {source["kind"] for source in record["sources"]}

    overlap = Counter()
    for record in merged:
        kinds = source_kinds(record)
        if kinds == {"archive", "biweekly", "interim"}:
            overlap["in_all_three"] += 1
        if "archive" in kinds and "biweekly" in kinds:
            overlap["in_archive_and_biweekly"] += 1
        if "archive" in kinds and "interim" in kinds:
            overlap["in_archive_and_interim"] += 1
        if "biweekly" in kinds and "interim" in kinds:
            overlap["in_biweekly_and_interim"] += 1
        if kinds == {"biweekly"}:
            overlap["only_biweekly"] += 1
        if kinds == {"interim"}:
            overlap["only_interim"] += 1
        if kinds == {"archive"}:
            overlap["only_archive"] += 1

    all_dates = [record["issue_date"] for record in merged if record["issue_date"]]
    return {
        "schema_version": 1,
        "copyright": COPYRIGHT_URL,
        "archive_pages": {
            "airworthiness_directives": ARCHIVE_AD_PAGE,
            "sib_sd": ARCHIVE_SIB_SD_PAGE,
        },
        "sources": {
            "archive_ad": {
                "url": ARCHIVE_AD_URL,
                "records": len(ad_rows),
                "records_total": ad_payload.get("recordsTotal"),
                "by_class": dict(sorted(Counter(row["AD class"] for row in ad_rows).items())),
                "issue_date_min": ad_min,
                "issue_date_max": ad_max,
                "sha256": None,
            },
            "archive_sib_sd": {
                "url": ARCHIVE_SIB_SD_URL,
                "records": len(sib_rows),
                "records_total": sib_payload.get("recordsTotal"),
                "by_class": dict(sorted(Counter(row["AD class"] for row in sib_rows).items())),
                "issue_date_min": sib_min,
                "issue_date_max": sib_max,
            },
            "biweekly": [
                {
                    "period": item["period"].period,
                    "from": item["period"].date_from,
                    "to": item["period"].date_to,
                    "rows": len(item["rows"]),
                    "csv_url": item["period"].csv_url,
                    "csv_label": item["period"].csv_label,
                    "sha256": hashlib.sha256(item["csv_bytes"]).hexdigest(),
                }
                for item in biweekly_files
            ],
            "interim_page": {
                "url": SAFETY_PUBLICATIONS_TOOL_URL,
                "last_updated_raw": None,
                "records": len(listing),
                "issue_date_min": min(listing_dates) if listing_dates else None,
                "issue_date_max": max(listing_dates) if listing_dates else None,
                "biweekly_periods": len(biweekly_files),
                "window_from": window_start.isoformat(),
                "window_to": window_end.isoformat(),
                "note": (
                    "The interim page was retired in November 2026. These rows are the "
                    "Safety Publications Tool publications in the window that are missing "
                    "from the archive or newer than the archive's copy."
                ),
            },
            "safety_publications_tool": {
                "url": SAFETY_PUBLICATIONS_TOOL_URL,
                "ingested": True,
                "note": (
                    "Biweekly CSVs are listed at /biweekly and downloaded as plain text. "
                    "Publications issued after the latest CSV, and SIBs or SDs the "
                    "biweekly files do not carry, are read from the tool's result table."
                ),
            },
        },
        "normalized": {
            "ad": bucket_stats(by_bucket["ad"]),
            "sib": bucket_stats(by_bucket["sib"]),
            "sd": bucket_stats(by_bucket["sd"]),
            "all": {
                "records": len(merged),
                "by_class": dict(sorted(Counter(record["class"] for record in merged).items())),
                "issue_date_min": min(all_dates) if all_dates else None,
                "issue_date_max": max(all_dates) if all_dates else None,
            },
        },
        "overlap": dict(sorted(overlap.items())),
    }


def _parse_report(records: list[dict], interim_warnings: list[str]) -> dict:
    unparsed = [
        {"id": record["id"], "subject": record["subject"]}
        for record in records
        if record.get("_supersession_unparsed")
    ]
    unparsed.sort(key=lambda item: item["id"])
    hyphen_tokens = sorted(
        {
            token
            for record in records
            for token in record["models"]
            if token.startswith("-")
        }
    )
    return {
        "supersession_unparsed_count": len(unparsed),
        "supersession_unparsed": unparsed,
        "records_without_models": sum(1 for record in records if not record["models"]),
        "records_without_documents": sum(1 for record in records if not record["documents"]),
        "records_without_holder": sum(1 for record in records if not record["approval_holders"]),
        "class_inferred": sum(1 for record in records if record["class_source"] == "inferred"),
        "model_tokens_starting_with_hyphen": hyphen_tokens,
        "interim_warnings": interim_warnings,
    }


def _write_all(
    data_dir,
    *,
    ad_blob,
    sib_blob,
    biweekly_files,
    listing,
    window_start,
    window_end,
    merged,
    stats,
    report,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_archive = data_dir / "raw" / "archive"
    raw_archive.mkdir(parents=True, exist_ok=True)
    (raw_archive / "airworthiness-directives.json").write_bytes(ad_blob)
    (raw_archive / "sib-sd.json").write_bytes(sib_blob)

    biweekly_root = data_dir / "raw" / "biweekly"
    keep_dirs: set[str] = set()
    for item in biweekly_files:
        period = item["period"]
        directory_name = f"{period.period:03d}_{period.date_from}_{period.date_to}"
        keep_dirs.add(directory_name)
        directory = biweekly_root / directory_name
        directory.mkdir(parents=True, exist_ok=True)
        member = item["member"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", member):
            member = "publications.csv"
        (directory / member).write_bytes(item["csv_bytes"])
        manifest = {
            "period": period.period,
            "from": period.date_from,
            "to": period.date_to,
            "rows": len(item["rows"]),
            "csv": {
                "url": period.csv_url,
                "label": period.csv_label,
                "zip_filename": item["zip_filename"],
                "member": member,
                "sha256": hashlib.sha256(item["csv_bytes"]).hexdigest(),
                "bytes": len(item["csv_bytes"]),
            },
            "html": {"url": period.html_url, "label": period.html_label},
            "pdf": {"url": period.pdf_url, "label": period.pdf_label},
        }
        _write_json(directory / "manifest.json", manifest)
    if biweekly_root.exists():
        for child in biweekly_root.iterdir():
            if child.is_dir() and child.name not in keep_dirs:
                shutil.rmtree(child)

    interim_dir = data_dir / "raw" / "interim"
    interim_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        interim_dir / "page.json",
        {
            "url": SAFETY_PUBLICATIONS_TOOL_URL,
            "last_updated_raw": None,
            "window": {"from": window_start.isoformat(), "to": window_end.isoformat()},
            "biweekly": [
                {
                    "period": item["period"].period,
                    "from": item["period"].date_from,
                    "to": item["period"].date_to,
                    "csv_url": item["period"].csv_url,
                    "csv_label": item["period"].csv_label,
                    "html_url": item["period"].html_url,
                    "html_label": item["period"].html_label,
                    "pdf_url": item["period"].pdf_url,
                    "pdf_label": item["period"].pdf_label,
                }
                for item in biweekly_files
            ],
            "publications": [
                {
                    "number": row.number,
                    "url": row.url,
                    "issued_by": row.issued_by,
                    "issue_date": row.issue_date,
                    "effective_date": row.effective_date,
                    "subject": row.subject,
                    "class_hint": row.class_hint,
                    "approval_holders": row.holders,
                    "models": row.models,
                    "documents": row.documents,
                }
                for row in listing
            ],
        },
    )

    buckets: dict[str, list[dict]] = {"ad": [], "sib": [], "sd": []}
    for record in merged:
        buckets[bucket_for(record["class"])].append(public_record(record))
    filenames = {"ad": "ads.jsonl", "sib": "sibs.jsonl", "sd": "sds.jsonl"}
    for name, records in buckets.items():
        root = data_dir / name
        _write_jsonl(root / filenames[name], records)
        by_year: dict[str, list[dict]] = {}
        for record in records:
            year = record["issue_date"][:4] if record["issue_date"] else "undated"
            by_year.setdefault(year, []).append(record)
        _replace_jsonl_dir(
            root / "by-year",
            {f"{year}.jsonl": _jsonl(rows) for year, rows in sorted(by_year.items())},
        )
        _write_jsonl(root / "by-model.jsonl", _model_index(records, with_class=False))
    _write_jsonl(data_dir / "all" / "publications.jsonl", [public_record(record) for record in merged])
    _write_jsonl(
        data_dir / "all" / "by-model.jsonl",
        _model_index([public_record(record) for record in merged], with_class=True),
    )
    # Fill archive sha from the bytes actually stored, not a re-encoding of rows.
    stats["sources"]["archive_ad"]["sha256"] = hashlib.sha256(ad_blob).hexdigest()
    stats["sources"]["archive_sib_sd"]["sha256"] = hashlib.sha256(sib_blob).hexdigest()
    _write_json(data_dir / "stats.json", stats)
    _write_json(data_dir / "parse-report.json", report)


def _model_index(records: list[dict], *, with_class: bool) -> list[dict]:
    grouped: dict[str, list] = {}
    for record in records:
        for model in record["models"]:
            grouped.setdefault(model, [])
            if with_class:
                entry = {"id": record["id"], "class": record["class"]}
                if entry not in grouped[model]:
                    grouped[model].append(entry)
            elif record["id"] not in grouped[model]:
                grouped[model].append(record["id"])
    rows = []
    for model in sorted(grouped, key=lambda value: (value.casefold(), value)):
        if with_class:
            publications = sorted(grouped[model], key=lambda item: item["id"])
            rows.append({"model": model, "publications": publications})
        else:
            rows.append({"model": model, "ids": sorted(grouped[model])})
    return rows


def _download_name(headers, url: str) -> str | None:
    named = _content_filename(headers)
    if named:
        return named
    parts = [part for part in url.split("?", 1)[0].split("/") if part]
    for part in reversed(parts):
        if part.lower().endswith(".csv"):
            return part
    return None


def _content_filename(headers) -> str | None:
    disposition = headers.get("Content-Disposition") or ""
    match = re.search(r'filename="([^"]+)"', disposition)
    if match:
        return match.group(1)
    match = re.search(r"filename=([^;]+)", disposition)
    if match:
        return match.group(1).strip()
    return None


def _jsonl(records: list[dict]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_jsonl(records), encoding="utf-8", newline="\n")


def _replace_jsonl_dir(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    expected = set(files)
    for existing in directory.glob("*.jsonl"):
        if existing.name not in expected:
            existing.unlink()
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8", newline="\n")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
