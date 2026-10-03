import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.downloader import Downloader
from qualitative_ir_downloader.models import Document
from qualitative_ir_downloader.production_status import (
    aggregate_status,
    category_status,
    completeness,
    document_metrics,
)


class ProductionStatusTests(unittest.TestCase):
    def test_documents_and_protected_event_are_success_with_limitations(self):
        data = {
            "news": [{"status": "DOWNLOADED", "duplicate_of": None}],
            "reports": [{"status": "DOWNLOADED", "duplicate_of": None}],
            "discovered_news": [{},],
            "discovered_reports": [{},],
        }
        overall, limitations = aggregate_status(
            ir="SUCCESS",
            news="SUCCESS",
            reports="SUCCESS",
            events="CACHED_EVENT_MEDIA_PROTECTED",
            news_metrics=document_metrics(data, "news"),
            reports_metrics=document_metrics(data, "reports"),
            event_metrics={"events_discovered": 1, "media_blocked": 1},
        )
        self.assertEqual(overall, "SUCCESS_WITH_LIMITATIONS")
        self.assertIn("event_media_protected", limitations)

    def test_media_not_found_with_documents_is_not_failed(self):
        metrics = {"discovered": 299, "newly_saved": 299, "cached": 0}
        overall, _ = aggregate_status(
            ir="SUCCESS", news="SUCCESS", reports="SUCCESS", events="MEDIA_NOT_FOUND",
            news_metrics=metrics, reports_metrics={"newly_saved": 31, "cached": 0},
            event_metrics={"events_discovered": 27, "media_not_found": 1},
        )
        self.assertEqual(overall, "SUCCESS_WITH_LIMITATIONS")

    def test_event_status_cannot_replace_overall_status(self):
        overall, _ = aggregate_status(
            ir="SUCCESS", news="SUCCESS", reports="SUCCESS", events="GENERATED_TRANSCRIPT_ALREADY_AVAILABLE",
            news_metrics={"newly_saved": 2, "cached": 0}, reports_metrics={"newly_saved": 1, "cached": 0},
            event_metrics={"events_discovered": 1, "generated_transcript_reused": 1},
        )
        self.assertEqual(overall, "SUCCESS")

    def test_actual_event_failure_with_documents_is_partial(self):
        overall, _ = aggregate_status(
            ir="SUCCESS", news="SUCCESS", reports="SUCCESS", events="FAILED",
            news_metrics={"newly_saved": 2, "cached": 0}, reports_metrics={"newly_saved": 1, "cached": 0},
            event_metrics={"events_discovered": 1, "event_failures": 1},
        )
        self.assertEqual(overall, "PARTIAL")

    def test_total_acquisition_failure_is_failed(self):
        overall, _ = aggregate_status(
            ir="DISCOVERY_FAILED", news="NOT_SCANNED", reports="NOT_SCANNED", events="NOT_SCANNED",
            news_metrics={"newly_saved": 0, "cached": 0}, reports_metrics={"newly_saved": 0, "cached": 0}, event_metrics={},
        )
        self.assertEqual(overall, "FAILED")

    def test_primary_ir_blocked_with_verified_fallback_evidence_is_limited_success(self):
        overall, limitations = aggregate_status(
            ir="ACCESS_BLOCKED", news="SUCCESS", reports="SUCCESS", events="IR_VERIFIED_ACCESS_BLOCKED",
            news_metrics={"newly_saved": 2, "cached": 0}, reports_metrics={"newly_saved": 1, "cached": 0},
            event_metrics={"events_discovered": 0}, official_evidence=True,
            source_limitations=["PRIMARY_IR_BLOCKED"],
        )
        self.assertEqual(overall, "SUCCESS_WITH_LIMITATIONS")
        self.assertIn("PRIMARY_IR_BLOCKED", limitations)

    def test_no_official_evidence_remains_failed(self):
        overall, _ = aggregate_status(
            ir="ACCESS_BLOCKED", news="ACCESS_BLOCKED", reports="ACCESS_BLOCKED", events="IR_VERIFIED_ACCESS_BLOCKED",
            news_metrics={"newly_saved": 0, "cached": 0}, reports_metrics={"newly_saved": 0, "cached": 0},
            event_metrics={"events_discovered": 0}, official_evidence=False,
        )
        self.assertEqual(overall, "FAILED")

    def test_zero_documents_has_explicit_reason(self):
        self.assertEqual(category_status({"news": [], "discovered_news": [], "coverage": {"news": "cached_listing"}}, "news"), "CACHE_HIT_EMPTY")
        self.assertEqual(category_status({"news": [], "discovered_news": [], "archive_errors": {"news": [{"code": "NEWS_SECTION_NOT_FOUND"}]}}, "news"), "DISCOVERY_FAILED")
        self.assertEqual(category_status({}, "news", applicable=False), "NOT_APPLICABLE")

    def test_completeness_keeps_transcript_separate(self):
        result = completeness(
            ir="SUCCESS", news="SUCCESS", reports="SUCCESS", events="CACHED_EVENT_MEDIA_PROTECTED",
            event_metrics={"events_discovered": 1}, limitations=["event_media_protected"],
        )
        self.assertTrue(result["events"])
        self.assertFalse(result["transcript"])
        self.assertEqual(result["limitations"], ["event_media_protected"])


class DocumentCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_document_is_reused_without_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / "News Releases" / "ACME-release.pdf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"cached artifact")
            import hashlib
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            downloader = Downloader(Config(download_root=folder), AsyncMock(), folder, "ACME")
            downloader.register_existing([{
                "category": "news", "source_url": "https://ir.example.com/release", "date": "2025-01-01",
                "local_filename": "News Releases/ACME-release.pdf", "sha256": digest,
            }])
            downloader.fetch_pdf = AsyncMock(side_effect=AssertionError("cache hit performed network I/O"))
            saved = await downloader.save(Document("Release", "2025-01-01", "https://ir.example.com/release", "news"))
            self.assertEqual(saved.status, "CACHED")
            self.assertTrue(saved.cache_hit)
            self.assertEqual(downloader.cache_hits, 1)
            downloader.fetch_pdf.assert_not_awaited()
