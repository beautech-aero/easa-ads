"""Source URLs and the request identity sent to EASA."""

ORIGIN = "https://www.easa.europa.eu"

# Whole-archive exports. Paging parameters are ignored; each URL returns
# every row currently in that dataset.
ARCHIVE_AD_URL = f"{ORIGIN}/en/datasets/144347/export/json"
ARCHIVE_SIB_SD_URL = f"{ORIGIN}/en/datasets/144348/export/json"

ARCHIVE_AD_PAGE = (
    f"{ORIGIN}/en/domains/aircraft-products/airworthiness-directives-ad/"
    "archive-airworthiness-directives"
)
ARCHIVE_SIB_SD_PAGE = (
    f"{ORIGIN}/en/domains/aircraft-products/airworthiness-directives-ad/"
    "archive-safety-information-bulletin-and-safety-directives"
)

INTERIM_PAGE_URL = (
    f"{ORIGIN}/en/domains/aircraft-products/airworthiness-directives-ad/"
    "safety-publications-tool-interim-solution"
)

# Interactive Safety Publications Tool. It was serving a maintenance page
# when this dataset was built, so ingest does not call it.
SAFETY_PUBLICATIONS_TOOL_URL = "https://ad.easa.europa.eu/"

COPYRIGHT_URL = f"{ORIGIN}/en/copyright-disclaimer"

USER_AGENT = (
    "BeauTech-easa-ads/1.0 "
    "(+https://github.com/beautech-aero/easa-ads; public dataset mirror)"
)

# EASA publication classes. AD-family classes live under data/ad/.
AD_CLASSES = ("AD", "EAD", "ADR", "PAD")
SIB_CLASS = "SIB"
SD_CLASS = "SD"
ALL_CLASSES = AD_CLASSES + (SIB_CLASS, SD_CLASS)

SCHEMA_VERSION = 1
