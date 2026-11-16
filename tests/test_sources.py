"""Parsers for the Safety Publications Tool and the biweekly index."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from easa_ads.biweekly import (
    load_biweekly_download,
    parse_biweekly_index,
    parse_csv,
    select_periods,
)
from easa_ads.normalize import document_kind
from easa_ads.safety_publications_tool import (
    is_newer_than_archive,
    parse_tool_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


class BiweeklyIndexTests(unittest.TestCase):
    def test_index_fixture(self) -> None:
        page = parse_biweekly_index((FIXTURES / "biweekly-index.html").read_text(encoding="utf-8"))
        self.assertEqual(page.years, [2024, 2025])
        self.assertEqual([period.period for period in page.periods], [22, 23])
        self.assertEqual(page.warnings, ["biweekly 24 has no CSV link"])
        period = page.periods[1]
        self.assertEqual(period.date_from, "2026-10-26")
        self.assertEqual(period.date_to, "2026-11-08")
        self.assertEqual(period.csv_label, "CSV")
        self.assertTrue(period.csv_url.endswith("23-2026.csv/biweekly"))
        self.assertTrue(period.html_url.endswith(".html/biweekly"))
        self.assertTrue(period.pdf_url.endswith(".pdf/biweekly"))

    def test_plain_csv_and_zip(self) -> None:
        plain = (FIXTURES / "biweekly.csv").read_bytes()
        name, data = load_biweekly_download(plain, filename="easa_biweekly_22-2026.csv")
        self.assertEqual(name, "easa_biweekly_22-2026.csv")
        rows = parse_csv(data, filename=name)
        self.assertEqual(rows[0].number, "US-2026-20-14")
        self.assertEqual(len(rows), 2)

        blob = (FIXTURES / "biweekly.csv.zip").read_bytes()
        zipped_name, zipped = load_biweekly_download(blob, filename=None)
        self.assertEqual(zipped_name, "easa_biweekly_22-2026.csv")
        self.assertEqual(parse_csv(zipped, filename=zipped_name)[0].number, "US-2026-20-14")

    def test_html_download_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_biweekly_download(b"<html>no results</html>", filename="x.csv")

    def test_select_periods_keeps_the_archive_overlap(self) -> None:
        page = parse_biweekly_index((FIXTURES / "biweekly-index.html").read_text(encoding="utf-8"))
        chosen = select_periods(
            page.periods,
            archive_max=date(2026, 10, 27),
            today=date(2026, 11, 16),
            overlap_days=30,
        )
        self.assertEqual([period.period for period in chosen], [22, 23])

    def test_stale_index_fails(self) -> None:
        page = parse_biweekly_index((FIXTURES / "biweekly-index.html").read_text(encoding="utf-8"))
        with self.assertRaises(RuntimeError) as caught:
            select_periods(
                page.periods,
                archive_max=date(2026, 10, 27),
                today=date(2027, 1, 15),
            )
        self.assertIn("page layout", str(caught.exception))


class ToolPageTests(unittest.TestCase):
    def test_tool_fixture(self) -> None:
        page = parse_tool_page((FIXTURES / "tool-page.html").read_text(encoding="utf-8"))
        self.assertFalse(page.empty)
        self.assertEqual(page.total, 3)
        self.assertEqual(len(page.publications), 3)
        ad, pad, uk = page.publications
        self.assertEqual(ad.number, "2026-0219")
        self.assertEqual(ad.issued_by, "EU")
        self.assertEqual(ad.issue_date, "2026-11-16")
        self.assertEqual(ad.effective_date, "2026-11-30")
        self.assertIsNone(ad.class_hint)
        self.assertEqual(ad.holders, ["ATR"])
        self.assertEqual(ad.models, ["ATR42", "200", "500", "ATR72", "212A"])
        self.assertEqual(ad.documents[0]["kind"], "pdf")
        self.assertEqual(ad.documents[0]["label"], "EASA_AD_2026_0219.pdf")
        self.assertEqual(pad.class_hint, "PAD")
        self.assertIsNone(pad.effective_date)
        self.assertEqual(pad.subject, "Powerplant – Engine Mounts – Inspection")
        self.assertNotIn("send comment", pad.subject)
        self.assertEqual(pad.holders, ["AIRBUS S.A.S."])
        self.assertEqual(pad.models, ["A330", "A330-201"])
        self.assertEqual(uk.issued_by, "UK")
        self.assertEqual(
            uk.holders,
            ["BAE SYSTEMS (OPERATIONS) LTD", "STC TWENTY ONE LIMITED"],
        )
        self.assertEqual(uk.models, ["AVRO146RJ", "85A", "EASA STC A.S.02766"])

    def test_empty_result(self) -> None:
        page = parse_tool_page(
            '<p class="msg-error">Sorry, no results found for your query.</p>'
        )
        self.assertTrue(page.empty)
        self.assertEqual(page.publications, [])

    def test_row_count_must_match_the_pager(self) -> None:
        html = (FIXTURES / "tool-page.html").read_text(encoding="utf-8")
        html = html.replace("1 to 3 out of a total of 3", "1 to 4 out of a total of 4")
        with self.assertRaises(ValueError):
            parse_tool_page(html)

    def test_unrecognised_page_fails(self) -> None:
        with self.assertRaises(ValueError):
            parse_tool_page("<html><p>The tool is back online.</p></html>")

    def test_newer_than_archive(self) -> None:
        self.assertTrue(is_newer_than_archive("2026-11-16", None))
        self.assertTrue(is_newer_than_archive("2026-11-16", "2026-11-05"))
        self.assertFalse(is_newer_than_archive("2026-11-05", "2026-11-05"))
        self.assertFalse(is_newer_than_archive(None, "2026-11-05"))


class DocumentKindTests(unittest.TestCase):
    def test_blob_url_uses_the_filename_segment(self) -> None:
        url = "https://ad.easa.europa.eu/blob/EASA_AD_2026_0219.pdf/AD_2026-0219_1"
        self.assertEqual(document_kind(url, "279 kb"), "pdf")


if __name__ == "__main__":
    unittest.main()
