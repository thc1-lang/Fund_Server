import unittest

from qualitative_ir_downloader.models import Document
from qualitative_ir_downloader.report_accounting import (
    classify_failure,
    is_report_candidate,
    reconcile_report_metrics,
    report_document_type,
    report_metrics,
)
from qualitative_ir_downloader.structured import merge_documents


class ReportAccountingTests(unittest.TestCase):
    def test_html_landing_page_is_not_a_report_candidate_or_failure(self):
        landing = Document("Quarterly reports", "2025", "https://ir.example.com/reports", "reports")
        self.assertFalse(is_report_candidate(landing))
        self.assertEqual(classify_failure("HTML_TO_PDF_FAILED", "HTML landing page", document=landing), "HTML_PAGE_NOT_REPORT")

    def test_duplicate_report_url_does_not_count_as_failure(self):
        records = [{"status": "DOWNLOADED", "duplicate_of": "Reports/report.pdf", "cache_hit": False}]
        metrics = report_metrics(records)
        self.assertEqual(metrics["reports_reused"], 1)
        self.assertEqual(metrics["reports_failed"], 0)

    def test_cached_report_is_reused(self):
        metrics = report_metrics([{"status": "CACHED", "cache_hit": True}])
        self.assertEqual(metrics["reports_saved"], 0)
        self.assertEqual(metrics["reports_reused"], 1)
        self.assertTrue(reconcile_report_metrics(metrics))

    def test_canonical_report_with_multiple_urls_is_counted_once(self):
        first = Document("Q4 2025 Report", "2025", "https://ir.example.com/report.pdf?utm_source=ir", "reports", "https://ir.example.com/report.pdf?utm_source=ir")
        second = Document("Q4 2025 Report", "2025", "https://ir.example.com/report.pdf", "reports", "https://ir.example.com/report.pdf")
        merged = merge_documents([first, second])
        self.assertEqual(len(merged), 1)

    def test_inaccessible_genuine_report_is_classified(self):
        report = Document("2020 Annual Report", "2020", "https://cdn.example.com/annual.pdf", "reports", "https://cdn.example.com/annual.pdf")
        self.assertTrue(is_report_candidate(report))
        self.assertEqual(classify_failure("PDF_DOWNLOAD_FAILED", "Invalid PDF structure: Encrypted or empty PDF", http_status=200, document=report), "INVALID_DOCUMENT")
        self.assertEqual(classify_failure("SITE_BLOCKED", "HTTP 403", http_status=403, status="BLOCKED", document=report), "ACCESS_BLOCKED")

    def test_report_totals_reconcile_exactly(self):
        records = [
            {"status": "DOWNLOADED", "cache_hit": False, "duplicate_of": None},
            {"status": "CACHED", "cache_hit": True, "duplicate_of": None},
            {"status": "FAILED", "cache_hit": False, "duplicate_of": None},
        ]
        metrics = report_metrics(records, discovered=3, raw_candidates=4)
        self.assertEqual(metrics["reports_validated"], 3)
        self.assertTrue(reconcile_report_metrics(metrics))
        self.assertEqual(metrics["raw_report_candidates"], 4)
        self.assertEqual(report_document_type(Document("Q3 2024 Earnings Presentation", "2024", "https://ir.example.com/q3.pdf", "reports", "https://ir.example.com/q3.pdf")), "quarterly_report")


if __name__ == "__main__":
    unittest.main()
