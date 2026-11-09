"""Look up publications by model token.

A query matches a model when the token equals the query or starts with it,
case-insensitively. ``CF34-10E`` therefore matches ``CF34-10E5`` and
``CF34-10E6``, which is how those engine variants are published.
"""

from __future__ import annotations

import json
from pathlib import Path


def model_matches(token: str, query: str) -> bool:
    needle = query.casefold().strip()
    if not needle:
        return False
    haystack = token.casefold()
    return haystack == needle or haystack.startswith(needle)


def load_publications(data_dir: Path, bucket: str) -> list[dict]:
    """Read one JSONL file. ``bucket`` is ``ad``, ``sib``, ``sd``, or ``all``."""
    paths = {
        "ad": data_dir / "ad" / "ads.jsonl",
        "sib": data_dir / "sib" / "sibs.jsonl",
        "sd": data_dir / "sd" / "sds.jsonl",
        "all": data_dir / "all" / "publications.jsonl",
    }
    if bucket not in paths:
        raise ValueError(f"unknown bucket {bucket!r}")
    path = paths[bucket]
    if not path.exists():
        raise FileNotFoundError(f"no dataset at {path}; run: python -m easa_ads ingest")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def find_by_model(records: list[dict], query: str) -> list[dict]:
    matched = [
        record
        for record in records
        if any(model_matches(model, query) for model in record.get("models") or [])
    ]
    matched.sort(key=lambda record: (record.get("issue_date") or "", record["id"]))
    return matched


def format_results(records: list[dict]) -> str:
    lines = [f"{len(records)} publication{'s' if len(records) != 1 else ''}"]
    for record in records:
        holder = "; ".join(record.get("approval_holders") or [])
        lines.append(
            f"{record['id']}\t{record['class']}\t{record.get('issue_date') or ''}\t"
            f"{record.get('subject') or ''}\t{holder}"
        )
    return "\n".join(lines)
