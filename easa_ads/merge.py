"""Combine the same publication number when more than one source has it.

The archive is a current-status extract: its subject is where "SUPERSEDED
BY" is maintained, and its model list is the full slash-separated
designation. A biweekly CSV is the notice from the week the document was
published, with only the type family. The interim page is the live list for
documents issued after the archive snapshot, and its model tree is detailed.

For a number that appears more than once, the latest issue date supplies
the subject and dates. On a tie the archive wins, then the interim page,
then a biweekly file. Models come from the latest archive or interim row
that actually lists them, so a one-token biweekly summary does not wipe a
full model list dated the same day.
"""

from __future__ import annotations

from easa_ads.normalize import (
    build_record,
    extract_supersession,
    parse_number,
    subject_says_cancelled,
)

_ORIGIN_RANK = {"archive": 3, "interim": 2, "biweekly": 1}
_SOURCE_RANK = {"archive": 3, "interim": 2, "inferred": 1}


def merge_contributions(groups: dict[str, list[dict]]) -> list[dict]:
    """Merge contributions keyed by publication number. Order is not kept."""
    merged = [_merge_one(contributions) for contributions in groups.values()]
    merged.sort(key=lambda record: record["id"])
    return merged


def _merge_one(contributions: list[dict]) -> dict:
    if len(contributions) == 1:
        return contributions[0]
    primary = max(contributions, key=_primary_key)
    chosen_class, class_source = _choose_class(contributions)
    holders, models = _choose_holders_and_models(contributions)
    subject = primary["subject"]
    parsed = parse_number(primary["id"])
    supersession = extract_supersession(subject)
    issued_by = next(
        (item["issued_by"] for item in contributions if item.get("issued_by")),
        None,
    )
    record = build_record(
        number=primary["id"],
        subject=subject,
        issue_date=primary["issue_date"] or _first_date(contributions, "issue_date"),
        effective_date=primary["effective_date"] or _first_date(contributions, "effective_date"),
        holders=holders,
        models=models,
        documents=_union_documents(contributions),
        origin=primary["source"],
        source_url=primary["sources"][0]["url"],
        raw=primary["raw"],
        explicit_class=chosen_class if class_source == "archive" else None,
        icon_class=chosen_class if class_source == "interim" else None,
        issued_by=issued_by,
    )
    # build_record re-infers when the chosen class is only a guess. Put the
    # chosen class back so a biweekly row cannot override the archive.
    record["class"] = chosen_class
    record["class_source"] = class_source
    record["emergency"] = parsed.emergency or chosen_class == "EAD"
    record["cancelled"] = parsed.cancelled or subject_says_cancelled(subject)
    record["marked_correction"] = subject.startswith("[Correction]")
    record["superseded_by"] = supersession.ids
    record["superseded_by_text"] = supersession.texts
    record["_supersession_unparsed"] = supersession.unparsed
    record["sources"] = _union_sources(contributions)
    record["source"] = primary["source"]
    record["raw"] = primary["raw"]
    record["approval_holders"] = holders
    record["models"] = models
    record["base_number"] = parsed.base_number
    record["revision"] = parsed.revision
    record["correction"] = parsed.correction
    record["revises"] = parsed.revises
    record["cancels"] = parsed.cancels
    return record


def _primary_key(record: dict) -> tuple:
    return (
        record["issue_date"] or "",
        _ORIGIN_RANK.get(record["source"], 0),
        record.get("_period") or 0,
    )


def _choose_class(contributions: list[dict]) -> tuple[str, str]:
    best = max(
        contributions,
        key=lambda record: (
            _SOURCE_RANK.get(record["class_source"], 0),
            _ORIGIN_RANK.get(record["source"], 0),
        ),
    )
    return best["class"], best["class_source"]


def _choose_holders_and_models(contributions: list[dict]) -> tuple[list[str], list[str]]:
    detailed = [record for record in contributions if record["source"] in {"archive", "interim"}]
    pool = [record for record in (detailed or contributions) if record["models"]]
    if not pool:
        pool = detailed or contributions
    best = max(
        pool,
        key=lambda record: (
            record["issue_date"] or "",
            len(record["models"]),
            1 if record["source"] == "archive" else 0,
            record.get("_period") or 0,
        ),
    )
    holders = list(best["approval_holders"])
    seen = set(holders)
    for record in detailed:
        for holder in record["approval_holders"]:
            if holder not in seen:
                holders.append(holder)
                seen.add(holder)
    return holders, list(best["models"])


def _union_documents(contributions: list[dict]) -> list[dict]:
    ordered = sorted(
        contributions,
        key=lambda record: (
            {"archive": 0, "interim": 1, "biweekly": 2}.get(record["source"], 9),
            record.get("_period") or 0,
        ),
    )
    documents: list[dict] = []
    seen: set[str] = set()
    for record in ordered:
        for document in record["documents"]:
            if document["url"] in seen:
                continue
            seen.add(document["url"])
            documents.append(document)
    return documents


def _union_sources(contributions: list[dict]) -> list[dict]:
    ordered = sorted(
        contributions,
        key=lambda record: (
            {"archive": 0, "biweekly": 1, "interim": 2}.get(record["source"], 9),
            (record["sources"][0].get("dataset") or ""),
            record.get("_period") or 0,
        ),
    )
    sources: list[dict] = []
    seen: set[tuple] = set()
    for record in ordered:
        source = record["sources"][0]
        key = (
            source.get("kind"),
            source.get("dataset"),
            source.get("period"),
            source.get("url"),
        )
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
    return sources


def _first_date(contributions: list[dict], field: str) -> str | None:
    for record in contributions:
        if record.get(field):
            return record[field]
    return None
