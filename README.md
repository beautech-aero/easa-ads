# easa-ads

A normalized mirror of EASA airworthiness publications, refreshed from EASA's own public files. An agent can answer “which ADs apply to CF34-10E?” by reading the JSONL in this repository. There is no database and no third-party API. The git tree is the source of truth; `data/all/publications.jsonl` is the file a later job would upsert.

The publications are:

- **AD, EAD, ADR, PAD** — airworthiness directives, emergency ADs, ADR-class ADs, and proposed ADs, under `data/ad/`
- **SIB** — safety information bulletins, under `data/sib/`
- **SD** — safety directives, under `data/sd/`

`data/all/publications.jsonl` is the same records in one file. `class` tells them apart.

## Sources

| Source | What it is | How it is used |
| --- | --- | --- |
| [AD archive export](https://www.easa.europa.eu/en/datasets/144347/export/json) | One JSON document, every AD through 27 October 2026. Paging parameters are ignored. | Raw file kept. Each row becomes a record. |
| [SIB and SD archive export](https://www.easa.europa.eu/en/datasets/144348/export/json) | Same seven columns. `AD class` is `SIB` or `SD`. | Raw file kept. Filed by class. |
| [Interim page](https://www.easa.europa.eu/en/domains/aircraft-products/airworthiness-directives-ad/safety-publications-tool-interim-solution) | HTML published while the Safety Publications Tool is down. | Parsed for two things: the biweekly CSV links, and the table of individual publications newer than the latest CSV. |
| Biweekly CSV zips linked from that page | Semicolon-separated listing for a fortnight. No header and no class column. The label is sometimes misspelled `CVS (.zip)`. | Extracted CSV is committed and parsed. The HTML and PDF bundles of the same list are not downloaded; their URLs are in the period manifest. |
| [ad.easa.europa.eu](https://ad.easa.europa.eu/) | The normal Safety Publications Tool. | Not called. It was serving a maintenance page. `easa_ads/safety_publications_tool.py` is the place to add it later. |

Archive PDFs are not stored. Each record keeps the document URL.

When the same publication number appears in more than one source, one record is kept. The latest issue date supplies the subject. On a tie the archive wins, because that is where EASA records “SUPERSEDED BY”. Model lists prefer the archive or the interim page over the one-line biweekly summary.

## Layout

```
schema/publication.schema.json     record shape
data/stats.json                    counts and date ranges from the last ingest
data/parse-report.json             subjects whose supersession text had no number
data/raw/archive/                  archive JSON, byte for byte
data/raw/biweekly/022_2026-10-12_2026-10-25/
    easa_biweekly_22-2026.csv      extracted CSV
    manifest.json                  CSV, HTML, and PDF URLs for that period
data/raw/interim/page.json         publications and biweekly links as read from the page
data/ad/ads.jsonl                  one AD-family record per line
data/ad/by-year/2026.jsonl
data/ad/by-model.jsonl             exact model token -> ids
data/sib/sibs.jsonl                same layout for safety information bulletins
data/sd/sds.jsonl                  same layout for safety directives
data/all/publications.jsonl        every class
data/all/by-model.jsonl            exact model token -> id and class
```

JSONL objects use a fixed key order. Files are sorted by publication number, so a git diff is an added or changed line.

`data/ad/by-model.jsonl` indexes the exact token EASA published (`CF34-10E5`, `CF34-10E6`, …). It does not contain a key `CF34-10E` unless some row used that exact string.

## Record fields

The full description is `schema/publication.schema.json`. The fields a search uses most:

- `id` / `number` — publication number, unchanged (`2026-0203R1`, `US-2025-14-03`, `SD-2026-01`)
- `base_number`, `revision`, `correction` — `2026-0203` + `R1`, or `C1` for a correction issue
- `class` — `AD`, `EAD`, `ADR`, `PAD`, `SIB`, or `SD`
- `issue_date`, `effective_date` — ISO dates, or null
- `subject`
- `approval_holders` — list
- `models` — list of designation tokens
- `documents` — `{url, label, kind}`
- `superseded_by`, `supersedes` — ids parsed from subjects such as `SUPERSEDED BY FAA AD 2007-12-15` (stored as `US-2007-12-15`)
- `source` — `archive`, `biweekly`, or `interim` (the row whose subject was kept)
- `sources` — every place this number was seen
- `raw` — the original fields from `source`

A few old subjects say `SUPERSEDED:` and then describe the AD without naming the replacement. Those are listed in `data/parse-report.json` and left with an empty `superseded_by`.

## Querying

Prefix match on a model token. `CF34-10E` matches `CF34-10E5` and `CF34-10E6`.

```bash
python -m easa_ads query CF34-10E --bucket ad
python -m easa_ads query PW1500G
python -m easa_ads query CFM56 --bucket ad
```

The same search with ripgrep, which also hits subjects and holders:

```bash
rg "CF34-10E" data/ad/ads.jsonl
```

Exact token, from the index:

```bash
rg '"model":"CF34-10E5"' data/ad/by-model.jsonl
```

One publication:

```bash
rg '"id":"US-2025-14-03"' data/ad/ads.jsonl
```

## Updating

The ingest script uses only the Python standard library. It sends a named user agent, waits a second between requests, and retries transient HTTP errors.

```bash
python -m unittest discover -s tests -v
python -m easa_ads ingest
```

GitHub Actions runs the unit tests on every push and pull request. A second workflow runs ingest daily at 05:20 UTC and on manual dispatch, and it commits only when the files changed.

## Data from EASA

These records are copied from EASA's public website. [EASA's copyright notice](https://www.easa.europa.eu/en/copyright-disclaimer) says reproduction is authorised provided the source is acknowledged, except where a page says otherwise. This repository is not EASA's official distribution. Check the document on easa.europa.eu before using a record for compliance, and read the current reuse terms before republishing the files.
