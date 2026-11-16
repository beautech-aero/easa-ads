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

# Retired in November 2026. The page now only says the tool is back online
# and no longer lists biweekly files or publications. Ingest does not fetch it.
INTERIM_PAGE_URL = (
    f"{ORIGIN}/en/domains/aircraft-products/airworthiness-directives-ad/"
    "safety-publications-tool-interim-solution"
)

# Safety Publications Tool. Biweekly indexes and the live publication list
# are served here now that the interim page has been retired.
TOOL_ORIGIN = "https://ad.easa.europa.eu"
SAFETY_PUBLICATIONS_TOOL_URL = f"{TOOL_ORIGIN}/"
BIWEEKLY_INDEX_URL = f"{TOOL_ORIGIN}/biweekly"
TOOL_SEARCH_URL = f"{TOOL_ORIGIN}/search/advanced/result/"
SD_LIST_URL = f"{TOOL_ORIGIN}/sd-docs/page-1"

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
