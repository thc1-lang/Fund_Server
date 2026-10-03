from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from qualitative_analysis.document_store import DocumentStore
from qualitative_analysis.fiscal_periods import detect_fiscal_period
from qualitative_analysis.ingestion import ingest_company
from qualitative_analysis.normalization import build_normalized_document, content_hash, stable_document_id
from qualitative_analysis.qualitative_extraction import extract_document
from qualitative_analysis.extraction_models import QualitativeClaim
from qualitative_analysis.temporal_comparison import compare_latest_vs_previous
from qualitative_analysis.state_synthesis import DeterministicStateSynthesisProvider


def _pdf(text: str, encrypted: bool = False) -> bytes:
    raw = io.BytesIO()
    writer = canvas.Canvas(raw)
    writer.drawString(72, 760, text)
    writer.save()
    raw.seek(0)
    if not encrypted:
        return raw.getvalue()
    reader = PdfReader(raw)
    out = io.BytesIO()
    protected = PdfWriter()
    for page in reader.pages:
        protected.add_page(page)
    protected.encrypt("")
    protected.write(out)
    out.seek(0)
    return out.getvalue()


class QualitativeAnalysisTests(unittest.TestCase):
    def test_whitespace_hash_and_stable_identity(self):
        self.assertEqual(content_hash("a  b\r\n"), content_hash("a b"))
        common = dict(ticker="INCY", source_url="HTTPS://Example.test/doc/?b=2&a=1", document_type="news_release", publication_date="2026-01-01", event_date=None)
        first = stable_document_id(**common, text="same")
        second = stable_document_id(**common, text="same")
        self.assertEqual(first, second)
        self.assertEqual(first, stable_document_id(**{**common, "publication_date": "2026-02-02"}, text="same"))
        self.assertNotEqual(first, stable_document_id(**common, text="different"))

    def test_fiscal_period_rules_are_conservative(self):
        self.assertEqual(detect_fiscal_period(title="Q1 2026 results").fiscal_quarter, 1)
        self.assertEqual(detect_fiscal_period(title="Second Quarter 2026 results").fiscal_quarter, 2)
        self.assertEqual(detect_fiscal_period(title="Third Quarter Fiscal Year 2026").fiscal_quarter, 3)
        self.assertEqual(detect_fiscal_period(title="Fourth Quarter Fiscal 2026").fiscal_quarter, 4)
        annual = detect_fiscal_period(title="2025 Annual Report")
        self.assertEqual((annual.fiscal_year, annual.fiscal_quarter), (2025, None))
        self.assertEqual(detect_fiscal_period(title="FY2025").period_label, "FY 2025")
        self.assertEqual(detect_fiscal_period(body="Year Ended December 31, 2025").fiscal_year, 2025)
        self.assertIsNone(detect_fiscal_period(title="Release dated May 2026").fiscal_year)
        self.assertEqual(detect_fiscal_period(metadata={"fiscal_year": 2024, "fiscal_quarter": 3}).detection_method, "metadata")

    def test_year_first_fiscal_quarter_forms_are_all_supported(self):
        expected = {
            "2024 first quarter": 1,
            "2025 second quarter": 2,
            "2026 third quarter": 3,
            "2026 fourth quarter": 4,
        }
        for phrase, quarter in expected.items():
            with self.subTest(phrase=phrase):
                period = detect_fiscal_period(title=phrase)
                self.assertEqual((period.fiscal_year, period.fiscal_quarter), (int(phrase[:4]), quarter))

    def test_proxy_period_is_document_aware_and_does_not_use_comparative_quarters(self):
        document = build_normalized_document(
            ticker="RMD", company_name="ResMed Inc.", title="RMD DEF 14A 2025-06-30",
            metadata={"category": "reports", "form_type": "DEF 14A", "source_url": "https://www.sec.gov/proxy.htm"},
            text="Executive compensation for 2024 first quarter and 2025 second quarter.",
            local_path="proxy.pdf",
        )
        self.assertEqual(document.document_type, "proxy_statement")
        self.assertIsNone(document.period_label)
        self.assertEqual(document.period_type, "UNKNOWN")
        self.assertEqual(document.metadata["period_detection_status"], "NOT_APPLICABLE")
        self.assertIsNone(document.metadata["period_detection_error"])

    def test_period_detection_error_is_nonfatal_and_diagnostic(self):
        with patch("qualitative_analysis.normalization.detect_fiscal_period", side_effect=RuntimeError("test parser failure")):
            document = build_normalized_document(
                ticker="RMD", company_name="ResMed Inc.", title="RMD 8-K",
                metadata={"category": "reports", "form_type": "8-K"},
                text="Current report with no reliable reporting period.", local_path="current-report.pdf",
            )
        self.assertEqual(document.document_type, "other_report")
        self.assertIsNone(document.period_label)
        self.assertEqual(document.period_type, "UNKNOWN")
        self.assertEqual(document.period_detection_method, "FAILED_OR_UNKNOWN")
        self.assertEqual(document.period_detection_confidence, 0.0)
        self.assertEqual(document.metadata["period_detection_status"], "ERROR")
        self.assertIn("RuntimeError", document.metadata["period_detection_error"])

    def test_documents_without_periods_still_normalize(self):
        for form_type, title, document_type in (
            ("8-K", "RMD Current Report", "other_report"),
            (None, "RMD News Release", "news_release"),
        ):
            with self.subTest(title=title):
                metadata = {"category": "reports" if form_type else "news"}
                if form_type:
                    metadata["form_type"] = form_type
                document = build_normalized_document(
                    ticker="RMD", company_name="ResMed Inc.", title=title,
                    metadata=metadata, text="No issuer reporting period is stated.", local_path=f"{title}.pdf",
                )
                self.assertEqual(document.document_type, document_type)
                self.assertIsNone(document.period_label)
                self.assertEqual(document.period_type, "UNKNOWN")
                self.assertIn(document.metadata["period_detection_status"], {"UNKNOWN", "NOT_APPLICABLE"})

    def test_quarterly_filing_headers_override_unrelated_annual_references(self):
        q1 = detect_fiscal_period(
            title="Quarterly Report",
            body="FORM 10-Q\nFor the quarterly period ended March 31, 2026\nUS government fiscal year 2026",
            document_type="quarterly_report",
            source_url="https://ir.example/2026%20Q1%20PLTR%2010-Q.pdf",
        )
        q2 = detect_fiscal_period(
            title="Quarterly Report",
            body="FORM 10-Q\nFor the three months ended June 30, 2026\ncustomer fiscal year 2026",
            document_type="quarterly_report",
            source_url="https://ir.example/2026%20Q2%20PLTR%2010-Q.pdf",
        )
        self.assertEqual((q1.period_label, q1.period_type), ("Q1 2026", "QUARTER_ACTUAL"))
        self.assertEqual((q2.period_label, q2.period_type), ("Q2 2026", "QUARTER_ACTUAL"))
        self.assertIn("filing_header", q2.detection_sources)
        self.assertIn("source_filename", q2.detection_sources)

    def test_partner_fiscal_year_does_not_become_issuer_period(self):
        period = detect_fiscal_period(
            title="Palantir and Fujitsu partnership",
            body="Fujitsu fiscal year ended March 31, 2026",
            document_type="news_release",
        )
        self.assertIsNone(period.period_label)
        self.assertEqual(period.period_type, "UNKNOWN")

    def test_annual_report_wins_over_quarterly_comparatives(self):
        period = detect_fiscal_period(
            title="2025 Annual Report",
            body="For the fiscal year ended December 31, 2025. Q1 2025 comparative information follows.",
            document_type="annual_report",
        )
        self.assertEqual((period.period_label, period.period_type), ("FY 2025", "ANNUAL_ACTUAL"))

    def test_non_calendar_fiscal_year_uses_issuer_metadata(self):
        period = detect_fiscal_period(
            metadata={"fiscal_year_end_month": 1},
            body="For the quarterly period ended April 30, 2026",
            document_type="quarterly_report",
        )
        self.assertEqual(period.period_label, "Q1 FY2027")
        self.assertEqual(period.period_end_date, "2026-04-30")
        self.assertEqual(period.issuer_fiscal_calendar["calendar_type"], "non_calendar")

    def test_non_calendar_nvidia_labels_roll_over_and_order_by_issuer_fiscal_year(self):
        april = detect_fiscal_period(
            metadata={"issuer_fiscal_calendar": {"fiscal_year_end_month": 1, "week_based": True, "weeks_per_year": 53}},
            body=("FORM 10-Q For the quarterly period ended April 26, 2026. "
                  "NVIDIA reports the first quarters of fiscal years 2027 and 2026."),
            document_type="quarterly_report",
        )
        july = detect_fiscal_period(
            metadata={"issuer_fiscal_calendar": {"fiscal_year_end_month": 1, "week_based": True, "weeks_per_year": 53}},
            body=("FORM 10-Q For the quarterly period ended July 26, 2026. "
                  "NVIDIA reports the second quarters of fiscal years 2027 and 2026."),
            document_type="quarterly_report",
        )
        self.assertEqual((april.period_label, april.period_end_date), ("Q1 FY2027", "2026-04-26"))
        self.assertEqual((july.period_label, july.period_end_date), ("Q2 FY2027", "2026-07-26"))
        self.assertEqual(april.issuer_fiscal_calendar["weeks_per_year"], 53)
        def claim(claim_id, document_id, period, text):
            return QualitativeClaim(
                claim_id=claim_id, ticker="NVDA", company_name="NVIDIA Corporation", document_id=document_id,
                document_type="quarterly_report", fiscal_year=period.fiscal_year,
                fiscal_quarter=period.fiscal_quarter, period_label=period.period_label,
                dimension="demand", topic="ai", subtopic=None, claim_text=text,
                direction="increasing", magnitude=None, certainty="actual", evidence_text=text,
                source_location={"line": 1}, source_url=None, local_path=None,
                extraction_method="test", extraction_confidence=1.0, created_at="2026-09-25",
                period_type=period.period_type,
            )
        claims = [claim("a", "da", april, "April demand"), claim("j", "dj", july, "July demand")]
        comparison = compare_latest_vs_previous("NVDA", claims)
        self.assertEqual((comparison.from_period, comparison.to_period), ("Q1 FY2027", "Q2 FY2027"))
        state = DeterministicStateSynthesisProvider().synthesize(claims, comparison.changes)
        self.assertEqual(state.period_label, "Q2 FY2027")

    def test_explicit_historical_fiscal_label_beats_calendar_month_and_avoids_collision(self):
        historical = detect_fiscal_period(
            body=("NVIDIA Second Quarter Fiscal 2026 results. "
                  "Results for the quarter ended July 27, 2025."),
            document_type="other_report",
        )
        calendar = detect_fiscal_period(
            body="FORM 10-Q For the quarterly period ended June 30, 2026",
            document_type="quarterly_report",
        )
        self.assertEqual(historical.period_label, "Q2 FY2026")
        self.assertEqual(historical.period_end_date, "2025-07-27")
        self.assertEqual(calendar.period_label, "Q2 2026")
        self.assertNotEqual(historical.period_label, calendar.period_label)

    def test_issuer_registry_labels_unmarked_sec_companion_and_preserves_53_week_fact(self):
        period = detect_fiscal_period(
            metadata={"ticker": "NVDA"},
            body="Form 8-K. The agreement is incorporated into the fiscal quarter ended July 26, 2026.",
            document_type="other_report",
        )
        self.assertEqual(period.period_label, "Q2 FY2027")
        self.assertTrue(period.issuer_fiscal_calendar["week_based"])
        self.assertEqual(period.issuer_fiscal_calendar["weeks_per_year"], 53)

    def test_june_fiscal_year_end_issuer_uses_issuer_sequence(self):
        cases = [
            ("September 30, 2025", "2025-09-30", "Q1 FY2026"),
            ("December 31, 2025", "2025-12-31", "Q2 FY2026"),
            ("March 31, 2026", "2026-03-31", "Q3 FY2026"),
            ("June 30, 2026", "2026-06-30", "Q4 FY2026"),
        ]
        for display_date, end_date, expected in cases:
            with self.subTest(end_date=end_date):
                period = detect_fiscal_period(
                    metadata={"ticker": "RMD"},
                    body=f"FORM 10-Q For the quarterly period ended {display_date}",
                    document_type="quarterly_report",
                )
                self.assertEqual(period.period_label, expected)
                self.assertEqual(period.period_end_date, end_date)

    def test_unknown_publication_availability_is_explicit(self):
        document = build_normalized_document(
            ticker="PLTR", company_name="Palantir", title="Quarterly Report",
            metadata={"category": "reports"},
            text="For the quarterly period ended June 30, 2026",
            local_path="UNKNOWN-DATE_10-Q.pdf",
        )
        self.assertIsNone(document.available_date)
        self.assertEqual(document.availability_status, "UNKNOWN")

    def test_guidance_horizon_is_separate_from_document_period(self):
        document = build_normalized_document(
            ticker="PLTR", company_name="Palantir", title="Q2 2026 results",
            metadata={"category": "reports", "source_url": "https://ir.example/2026%20Q2%20PLTR%2010-Q.pdf"},
            text="For the quarterly period ended June 30, 2026. Management guidance for FY 2026 is maintained.",
            local_path="2026 Q2 PLTR 10-Q.pdf",
        )
        self.assertEqual(document.period_label, "Q2 2026")
        self.assertEqual(document.period_type, "QUARTER_ACTUAL")
        result = extract_document(document)
        guidance = [claim for claim in result.claims if claim.dimension == "guidance"]
        self.assertTrue(guidance)
        self.assertTrue(any(claim.guidance_horizon == "FY 2026" for claim in guidance))

    def test_offline_artifact_ingestion_and_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            company = root / "INCY_Incyte_qualitative analysis_2026-01-01"
            (company / "News Releases").mkdir(parents=True)
            (company / "Reports").mkdir()
            (company / "Events").mkdir()
            (company / "News Releases" / "release.pdf").write_bytes(_pdf("Q2 2026 Incyte announces a new program."))
            (company / "Reports" / "annual.pdf").write_bytes(_pdf("2025 Annual Report\nFinancial outlook."))
            transcript = {
                "schema_version": 1,
                "ticker": "INCY",
                "event": {"title": "Q2 2026 Earnings Conference Call", "date": "2026-07-28", "type": "earnings_call"},
                "source": {"event_url": "https://ir.example.test/event", "method": "official_transcript"},
                "segments": [{"start": 0.0, "end": 2.0, "speaker": "CEO", "text": "Prepared remarks."}],
                "transcript_qa": {"status": "PASS"},
                "transcript_method": "official_transcript",
            }
            (company / "Events" / "call.json").write_text(json.dumps(transcript), encoding="utf-8")
            manifest = {
                "ticker": "INCY", "company_name": "Incyte", "news": [{"title": "Q2 2026 news", "date": "2026-07-01", "source_url": "https://ir.example.test/news", "category": "news", "local_filename": "News Releases/release.pdf", "method": "official_pdf", "provenance": [{"discovery_method": "test"}]}, {"title": "Q2 2026 news duplicate", "date": "2026-07-01", "source_url": "https://ir.example.test/news", "category": "news", "local_filename": "News Releases/release.pdf", "method": "official_pdf"}],
                "reports": [{"title": "2025 Annual Report", "date": "2025", "source_url": "https://ir.example.test/report", "category": "reports", "local_filename": "Reports/annual.pdf", "method": "official_pdf"}],
                "events": [{"title": "Q2 2026 Earnings Conference Call", "date": "2026-07-28", "event_url": "https://ir.example.test/event", "event_type": "earnings_call", "local_json": "Events/call.json", "transcript_method": "official_transcript"}, {"title": "Missing event", "date": "2025-01-01", "event_url": "https://ir.example.test/missing", "event_type": "conference"}],
            }
            (company / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            store = DocumentStore(root / "store")
            result = ingest_company(company, store)
            self.assertEqual(result.documents_normalized, 3)
            self.assertEqual(result.documents_failed, 1)
            self.assertEqual(result.by_type["news_release"], 1)
            self.assertEqual(result.by_type["annual_report"], 1)
            transcript_docs = store.get_by_type("earnings_transcript")
            self.assertEqual(len(transcript_docs), 1)
            self.assertEqual(transcript_docs[0].metadata["segments"][0]["speaker"], "CEO")
            self.assertEqual(transcript_docs[0].metadata["transcription_qa"]["status"], "PASS")
            self.assertEqual(len(store.get_by_ticker("INCY")), 3)
            self.assertEqual(len(store.get_by_fiscal_period(2025)), 1)
            self.assertTrue(store.index_path.exists())

            second = ingest_company(company, store)
            self.assertEqual(second.documents_added, 0)
            self.assertEqual(second.documents_updated, 0)
            self.assertEqual(second.documents_unchanged, 3)

    def test_passwordless_encrypted_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            company = root / "ZM_Zoom_qualitative analysis_2026-01-01"
            (company / "News Releases").mkdir(parents=True)
            (company / "News Releases" / "encrypted.pdf").write_bytes(_pdf("Second Quarter 2026 release", encrypted=True))
            manifest = {"ticker": "ZM", "company_name": "Zoom", "news": [{"title": "Second Quarter 2026 release", "date": "2026-08-01", "source_url": "https://example.test/release", "category": "news", "local_filename": "News Releases/encrypted.pdf"}], "reports": [], "events": []}
            (company / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = ingest_company(company, DocumentStore(root / "store"))
            self.assertEqual(result.documents_normalized, 1)
            self.assertEqual(result.documents_failed, 0)

    def test_invalid_pdf_isolated_as_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            company = root / "EXEL_Exelixis_qualitative analysis_2026-01-01"
            (company / "News Releases").mkdir(parents=True)
            (company / "News Releases" / "bad.pdf").write_bytes(b"not a pdf")
            manifest = {"ticker": "EXEL", "company_name": "Exelixis", "news": [{"title": "bad", "source_url": "https://example.test/bad", "category": "news", "local_filename": "News Releases/bad.pdf"}], "reports": [], "events": []}
            (company / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = ingest_company(company, DocumentStore(root / "store"))
            self.assertEqual(result.documents_normalized, 0)
            self.assertEqual(result.failures[0].failure_type, "TEXT_EXTRACTION_FAILED")

    def test_same_title_different_content_stays_distinct(self):
        kwargs = dict(ticker="PLTR", source_url="https://example.test/release", document_type="news_release", publication_date="2026-01-01", event_date=None)
        self.assertNotEqual(stable_document_id(**kwargs, text="one"), stable_document_id(**kwargs, text="two"))


if __name__ == "__main__":
    unittest.main()
