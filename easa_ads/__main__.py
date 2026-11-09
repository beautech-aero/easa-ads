"""Command line: ``python -m easa_ads ingest`` and ``python -m easa_ads query``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easa_ads.http import HttpClient
from easa_ads.ingest import ingest
from easa_ads.query import find_by_model, format_results, load_publications


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="easa_ads",
        description="Fetch and query the EASA publications dataset.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory for raw snapshots and normalized JSONL (default: data)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_parser = sub.add_parser("ingest", help="Fetch EASA sources and rewrite data/")
    ingest_parser.add_argument(
        "--min-interval",
        type=float,
        default=1.0,
        help="Seconds to wait between HTTP requests (default: 1)",
    )

    query_parser = sub.add_parser("query", help="List publications whose model tokens match")
    query_parser.add_argument("model", help="Model or type designation, matched as a prefix")
    query_parser.add_argument(
        "--bucket",
        choices=("ad", "sib", "sd", "all"),
        default="all",
        help="Which file to search (default: all)",
    )

    args = parser.parse_args(argv)
    if args.command == "ingest":
        client = HttpClient(min_interval=args.min_interval)
        ingest(args.data_dir, client)
        return 0
    records = load_publications(args.data_dir, args.bucket)
    print(format_results(find_by_model(records, args.model)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
