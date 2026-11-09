"""Extension point for the interactive Safety Publications Tool.

``https://ad.easa.europa.eu/`` is the normal tool. When this dataset was
built it answered with a maintenance page, and the public archive export
plus the interim page already cover the publications. Ingest does not call
this host, so a maintenance response cannot fail the daily run.

To add the tool later:

1. Implement ``fetch_records`` so it returns contributions in the same shape
   as the archive normalizer (see ``easa_ads.normalize.build_record``).
2. Call it from ``easa_ads.ingest`` and merge on publication number.
3. Keep the polite ``HttpClient`` (it already pauses between requests).
"""

from __future__ import annotations

from easa_ads.const import SAFETY_PUBLICATIONS_TOOL_URL
from easa_ads.http import HttpClient


def fetch_records(client: HttpClient) -> list[dict]:
    """Fetch publications from ad.easa.europa.eu.

    Not used by ingest. ``client`` is accepted so a future implementation
    shares the same rate limit as the other sources.
    """
    raise NotImplementedError(
        f"{SAFETY_PUBLICATIONS_TOOL_URL} is not ingested while the "
        "Safety Publications Tool is offline. See this module's docstring."
    )
