"""Normalizer tests. They use fixtures copied from EASA rows, not the network."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from easa_ads.biweekly import csv_from_zip, parse_csv
from easa_ads.interim import parse_interim_html
from easa_ads.merge import merge_contributions
from easa_ads.normalize import (
    PUBLIC_KEYS,
    build_record,
    extract_supersession,
    parse_biweekly_holders,
    parse_designation,
    parse_number,
    public_record,
)
from easa_ads.query import find_by_model, model_matches

FIXTURES = Path(__file__).parent / "fixtures"


class NumberTests(unittest.TestCase):
    def test_revision_correction_and_markers(self) -> None:
        cases = {
            "2026-0203": ("2026-0203", None, None, False, False, None),
            "2026-0203R1": ("2026-0203", "R1", None, False, False, "2026-0203"),
            "2026-0203C1": ("2026-0203", None, "C1", False, False, None),
            "2026-0191-E": ("2026-0191-E", None, None, True, False, None),
            "2026-0126-CN": ("2026-0126", None, None, False, True, None),
            "2024-0058R2-CN": ("2024-0058", "R2", None, False, True, "2024-0058R1"),
            "2026-0001-E-CN": ("2026-0001-E", None, None, True, True, None),
            "G-2025-0001R1-E": ("G-2025-0001", "R1", None, True, False, "G-2025-0001"),
            "2009-0234-ER1": ("2009-0234", "R1", None, True, False, "2009-0234"),
            "US-93-14-19R1": ("US-93-14-19", "R1", None, False, False, "US-93-14-19"),
            "SD-2025-02R1": ("SD-2025-02", "R1", None, False, False, "SD-2025-02"),
            "SD-2021-05-CN": ("SD-2021-05", None, None, False, True, None),
            "ISR-I-53-19-10-5R01": ("ISR-I-53-19-10-5", "R01", None, False, False, "ISR-I-53-19-10-5"),
            "85-001R3": ("85-001", "R3", None, False, False, "85-001R2"),
            "SAIB-2026-17": ("SAIB-2026-17", None, None, False, False, None),
        }
        for number, expected in cases.items():
            parsed = parse_number(number)
            actual = (
                parsed.base_number,
                parsed.revision,
                parsed.correction,
                parsed.emergency,
                parsed.cancelled,
                parsed.revises,
            )
            self.assertEqual(actual, expected, number)
            if parsed.cancelled:
                self.assertEqual(parsed.cancels, number[: -3])


class SupersessionTests(unittest.TestCase):
    def test_known_phrases(self) -> None:
        cases = [
            ("SUPERSEDED BY EASA AD 2026-0174", ["2026-0174"]),
            ("SUPERSEDED  BY EASA AD 2012-0168", ["2012-0168"]),
            ("SUPERSEDED BY FAA AD 2026-14-11", ["US-2026-14-11"]),
            ("SUPERSEDED BY FAA AD-2026-14-11", ["US-2026-14-11"]),
            ("SUPERSEDED BY FAA AD 99-18-19", ["US-99-18-19"]),
            ("SUPERSEDED BY ANAC BRAZIL AD 2026-07-02", ["BR-2026-07-02"]),
            ("SUPERSEDED BY TCCA AD CF-2026-27", ["CF-2026-27"]),
            ("SUPERSEDED BY EASA SD-2021-05", ["SD-2021-05"]),
            ("SUPERSEDED BY EASA SD 2021-01", ["SD-2021-01"]),
            ("SUPERSEDED BY EASA SIB 2009-18", ["2009-18"]),
            ("SUPERSEDED BY DGAC AD F-2005-181 AND F-2005-182", ["F-2005-181", "F-2005-182"]),
            ("SUPERSEDED BY DGAC FRANCE AD 98-508-106", ["F-1998-508-106"]),
            ("SUPERSEDED BY CAA UK AD 009\u201302\u201393", ["G-009-02-93"]),
            ("Superseded by CAA UK AD G-1266-PRE80", ["G-1266-PRE80"]),
            ("SUPERSEDED BY FAA AD 83-08-01 R1 (HARTZELL PROPELLERS)", ["US-83-08-01R1"]),
            (
                "NOT ELIGIBLE - SUPERSEDED BY FAA AD 2021-05-17 - SEE EASA AD 2019-0179",
                ["US-2021-05-17"],
            ),
            ("CANCELLATION NOTICE - NO SUPERSEDING AD ISSUED", []),
            ("Engine – High Pressure Turbine Blades – Inspection", []),
        ]
        for subject, expected in cases:
            parsed = extract_supersession(subject)
            self.assertEqual(parsed.ids, expected, subject)
            self.assertFalse(parsed.unparsed, subject)

    def test_superseded_without_a_number_is_reported(self) -> None:
        parsed = extract_supersession("SUPERSEDED: Landing Gear - Main landing gear - Inspection")
        self.assertTrue(parsed.unparsed)
        self.assertEqual(parsed.ids, [])


class DesignationTests(unittest.TestCase):
    def test_archive_shapes(self) -> None:
        holders, models = parse_designation(
            "ROLLS-ROYCE DEUTSCHLAND Ltd & Co KG\n"
            "TRENT 1000/Trent 1000-AE3/Trent 1000-CE3"
        )
        self.assertEqual(holders, ["ROLLS-ROYCE DEUTSCHLAND Ltd & Co KG"])
        self.assertEqual(models, ["TRENT 1000", "Trent 1000-AE3", "Trent 1000-CE3"])

        holders, models = parse_designation(
            "AIRBUS HELICOPTERS\nAS 350 / EC 130/AS 350 B3"
        )
        self.assertEqual(models, ["AS 350", "EC 130", "AS 350 B3"])

        holders, models = parse_designation(
            "GENERAL ELECTRIC\nCF34-10/CF34-10E5/CF34-10E6"
        )
        self.assertIn("CF34-10E5", models)

        _holders, models = parse_designation(
            "CFM INTERNATIONAL\n"
            "CFM56-7B/CFM56-7B20/CFM56-7B20/2/CFM56-7B20/3/CFM56-7B22/B1"
        )
        self.assertIn("CFM56-7B20/2", models)
        self.assertIn("CFM56-7B20/3", models)
        self.assertIn("CFM56-7B22/B1", models)
        self.assertNotIn("2", models)

        _holders, models = parse_designation("ATR\nATR42/200/300/500/ATR72/101/212A")
        self.assertEqual(models, ["ATR42", "200", "300", "500", "ATR72", "101", "212A"])

        holders, _models = parse_designation("B/E AEROSPACE INC.\n717-200")
        self.assertEqual(holders, ["B/E AEROSPACE INC."])

        holders, models = parse_designation(
            "AIRBUS S.A.S.\n"
            "A330/ELBE FLUGZEUGWERKE GmbH:EASA STC 10063798"
        )
        self.assertEqual(
            holders,
            ["AIRBUS S.A.S.", "ELBE FLUGZEUGWERKE GmbH"],
        )
        self.assertEqual(models, ["A330", "EASA STC 10063798"])

        _holders, models = parse_designation(
            "BOEING\n"
            "737/737-800/Safety Information Bulletin:Airworthiness/Fixed Wings"
        )
        self.assertEqual(models, ["737", "737-800"])

    def test_biweekly_holder_commas(self) -> None:
        self.assertEqual(
            parse_biweekly_holders("DAHER AIRCRAFT, SAS"),
            ["DAHER AIRCRAFT, SAS"],
        )
        self.assertEqual(
            parse_biweekly_holders("AIRBUS S.A.S., ELBE FLUGZEUGWERKE GmbH"),
            ["AIRBUS S.A.S.", "ELBE FLUGZEUGWERKE GmbH"],
        )
        self.assertEqual(
            parse_biweekly_holders(
                "BREEZER AIRCRAFT GmbH & Co. KG., CZECH SPORT AIRCRAFT , "
                "EVEKTOR, SPOL. S R.O., TOMARK, s.r.o."
            ),
            [
                "BREEZER AIRCRAFT GmbH & Co. KG.",
                "CZECH SPORT AIRCRAFT",
                "EVEKTOR, SPOL. S R.O.",
                "TOMARK, s.r.o.",
            ],
        )


class SourceParsingTests(unittest.TestCase):
    def test_interim_fixture(self) -> None:
        page = parse_interim_html((FIXTURES / "interim.html").read_text(encoding="utf-8"))
        self.assertEqual(page.last_updated, "06/11/2026")
        self.assertEqual(len(page.periods), 1)
        period = page.periods[0]
        self.assertEqual(period.period, 20)
        self.assertEqual(period.csv_label, "CVS (.zip)")
        self.assertTrue(period.csv_url.endswith("/downloads/144357/en"))
        self.assertEqual(len(page.publications), 2)
        ad = page.publications[0]
        self.assertEqual(ad.number, "2026-0202R1")
        self.assertEqual(ad.issued_by, "EU")
        self.assertEqual(ad.models[:3], ["AS 350", "EC 130", "AS 350 B3"])
        self.assertEqual(ad.documents[0]["kind"], "pdf")
        pad = page.publications[1]
        self.assertEqual(pad.class_hint, "PAD")
        self.assertEqual(pad.holders, ["AIRBUS HELICOPTERS DEUTSCHLAND"])
        self.assertNotIn("ADs", pad.subject)
        self.assertTrue(pad.subject.startswith("Proposed AD:"))

    def test_biweekly_csv_zip(self) -> None:
        blob = (FIXTURES / "biweekly.csv.zip").read_bytes()
        name, data = csv_from_zip(blob)
        self.assertEqual(name, "easa_biweekly_22-2026.csv")
        rows = parse_csv(data, filename=name)
        self.assertEqual(rows[0].number, "US-2026-20-14")
        self.assertEqual(len(rows), 2)


class MergeAndQueryTests(unittest.TestCase):
    def test_archive_wins_models_when_dates_match(self) -> None:
        archive = build_record(
            number="2026-0199",
            subject="SUPERSEDED BY EASA AD 2026-0200",
            issue_date="2026-10-20",
            effective_date="2026-11-03",
            holders=["ALLSTAR PZL GLIDER"],
            models=["SZD-54 Perkoz", "SZD-54-2 Perkoz"],
            documents=[{"url": "https://www.easa.europa.eu/a.pdf", "label": "a.pdf", "kind": "pdf"}],
            origin="archive",
            source_url="https://www.easa.europa.eu/en/datasets/144347/export/json",
            dataset="airworthiness-directives",
            explicit_class="AD",
            raw={"subject": "archive"},
        )
        biweekly = build_record(
            number="2026-0199",
            subject="Flight Controls - Airbrake Control Torsion Tube - Inspections",
            issue_date="2026-10-20",
            effective_date=None,
            holders=["ALLSTAR PZL GLIDER"],
            models=["SZD-54 Perkoz"],
            documents=[],
            origin="biweekly",
            source_url="https://www.easa.europa.eu/en/downloads/144341/en",
            period=22,
            period_from="2026-10-12",
            period_to="2026-10-25",
            raw={"subject": "biweekly"},
        )
        merged = merge_contributions({"2026-0199": [archive, biweekly]})
        self.assertEqual(len(merged), 1)
        record = merged[0]
        self.assertEqual(record["source"], "archive")
        self.assertEqual(record["subject"], "SUPERSEDED BY EASA AD 2026-0200")
        self.assertEqual(record["models"], ["SZD-54 Perkoz", "SZD-54-2 Perkoz"])
        self.assertEqual(record["superseded_by"], ["2026-0200"])
        self.assertEqual({item["kind"] for item in record["sources"]}, {"archive", "biweekly"})
        self.assertEqual(set(public_record(record)), set(PUBLIC_KEYS))

    def test_newer_interim_subject_keeps_archive_models_if_interim_has_none(self) -> None:
        archive = build_record(
            number="2026-0001",
            subject="Original subject",
            issue_date="2026-10-01",
            effective_date="2026-10-15",
            holders=["ATR"],
            models=["ATR42", "ATR72"],
            documents=[],
            origin="archive",
            source_url="https://archive.example",
            explicit_class="AD",
            raw={"from": "archive"},
        )
        interim = build_record(
            number="2026-0001",
            subject="[Correction] Original subject",
            issue_date="2026-11-02",
            effective_date="2026-11-16",
            holders=["ATR"],
            models=[],
            documents=[],
            origin="interim",
            source_url="https://interim.example",
            raw={"from": "interim"},
        )
        record = merge_contributions({"2026-0001": [archive, interim]})[0]
        self.assertEqual(record["source"], "interim")
        self.assertTrue(record["subject"].startswith("[Correction]"))
        self.assertEqual(record["models"], ["ATR42", "ATR72"])
        self.assertTrue(record["marked_correction"])

    def test_model_prefix_query(self) -> None:
        self.assertTrue(model_matches("CF34-10E5", "CF34-10E"))
        self.assertTrue(model_matches("cf34-10e", "CF34-10E"))
        self.assertFalse(model_matches("CF34-8C", "CF34-10E"))
        records = [
            {"id": "US-1", "class": "AD", "issue_date": "2025-01-01", "models": ["CF34-10E6"], "approval_holders": [], "subject": ""},
            {"id": "US-2", "class": "AD", "issue_date": "2024-01-01", "models": ["CF34-8C5"], "approval_holders": [], "subject": ""},
        ]
        found = find_by_model(records, "CF34-10E")
        self.assertEqual([item["id"] for item in found], ["US-1"])


class SchemaTests(unittest.TestCase):
    def test_schema_lists_the_public_keys(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "schema" / "publication.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["required"], list(PUBLIC_KEYS))


if __name__ == "__main__":
    unittest.main()
