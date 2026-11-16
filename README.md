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
| [AD archive export](https://www.easa.europa.eu/en/datasets/144347/export/json) | One JSON document, every AD. Paging parameters are ignored. | Raw file kept. Each row becomes a record. |
| [SIB and SD archive export](https://www.easa.europa.eu/en/datasets/144348/export/json) | Same seven columns. `AD class` is `SIB` or `SD`. | Raw file kept. Filed by class. |
| [Biweekly index](https://ad.easa.europa.eu/biweekly) | One row per fortnight, with CSV, HTML, and PDF links. Year pages use `?year=`. | Fortnights that still overlap the archive (about the last 90 days) are downloaded. A stored fortnight is kept after that if any of its publications are still missing from the archive. The index must list a CSV, and the newest fortnight must have ended within 28 days. |
| Biweekly CSV linked from that index | Semicolon-separated listing for a fortnight. No header and no class column. The file is plain text. | CSV is committed and parsed. The HTML and PDF of the same list are not downloaded; their URLs are in the period manifest. |
| [Safety Publications Tool](https://ad.easa.europa.eu/) | The live publication list. A date-filtered search returns ADs and SIBs; safety directives are a separate short list. | Rows in a window that starts 21 days before the older of the archive's newest AD and newest SIB, and never more than 120 days before the run. Rows the archive already has on the same day are included. On that tie the archive keeps the subject and the models; the tool adds the state-of-design code and the PDF link. A newer tool row supplies the models. Combined family labels such as `SA 330 / AS 332 / EC 225` are split on `/`. The window end stored in `stats.json` is the newest kept issue date, not the clock date of the run. |

The [interim page](https://www.easa.europa.eu/en/domains/aircraft-products/airworthiness-directives-ad/safety-publications-tool-interim-solution) was retired in November 2026. It now only says the tool is back online. Ingest does not fetch it. Records from the live tool still use `source: "interim"`, which is the detailed list that page used to be.

Archive PDFs are not stored. Each record keeps the document URL.

When the same publication number appears in more than one source, one record is kept. The latest issue date supplies the subject and the model list. On a tie the archive wins, because that is where EASA records “SUPERSEDED BY”. A same-date tool row still adds its state-of-design code and document links.

## Layout

```
schema/publication.schema.json     record shape
data/stats.json                    counts and date ranges from the last ingest
data/parse-report.json             subjects whose supersession text had no number
data/raw/archive/                  archive JSON, byte for byte
data/raw/biweekly/022_2026-10-12_2026-10-25/
    easa_biweekly_22-2026.csv      CSV as downloaded (plain text)
    manifest.json                  CSV, HTML, and PDF URLs for that period
data/raw/interim/page.json         tool publications kept from the latest run, and the biweekly links
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
- `source` — `archive`, `biweekly`, or `interim` (the row whose subject was kept; `interim` is the Safety Publications Tool)
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
