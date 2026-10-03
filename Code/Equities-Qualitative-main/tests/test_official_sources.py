import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qualitative_ir_downloader.models import AccessResult, IRCandidate, IREcosystem, Stock
from qualitative_ir_downloader.official_sources import (
    OfficialSourceStrategy,
    SOURCE_IR_FINANCIAL,
    SOURCE_NEWSROOM,
    SOURCE_SEC,
    SOURCE_STATIC,
)
from qualitative_ir_downloader.production_status import aggregate_status


class Response:
    def __init__(self, url, body, status=200, content_type="text/html"):
        self.url = url
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = {"content-type": content_type}


def browser(root):
    return SimpleNamespace(config=SimpleNamespace(download_root=Path(root), revalidate_ir=True))


class OfficialSourceFallbackTests(unittest.IsolatedAsyncioTestCase):
    def stock(self):
        return Stock("ACME", "Acme Corporation", "Safe", 1)

    def winner(self):
        return IRCandidate(
            "https://investor.acme.com/",
            officiality_score=100,
            official=True,
            corporate_url="https://acme.com/",
            access=AccessResult(browser_status=403, blocked=True, error="HTTP 403"),
        )

    def ecosystem(self):
        return IREcosystem(
            official_corporate_domain="acme.com",
            official_corporate_url="https://acme.com/",
            verified_ir_urls=["https://investor.acme.com/"],
        )

    async def test_primary_blocked_financial_and_newsroom_fallbacks_are_usable(self):
        financial = """
        <html><title>Acme Financial Information</title><body>Acme Corporation financial results
        <a href='/news/2026/q2-acme-results'>Q2 Acme results July 30, 2026</a>
        <a href='https://cdn.acme-static.com/q2-2026-report.pdf'>Q2 2026 report PDF</a></body></html>
        """
        newsroom = """
        <html><title>Acme Newsroom</title><body>Acme Corporation newsroom
        <a href='/press-releases/acme-launches-product'>Acme launches product July 1, 2026</a></body></html>
        """

        async def fetch(url, referer):
            if "/financial-info" in url:
                return Response(url, financial)
            if url.startswith("https://news.acme.com") or "/newsroom" in url:
                return Response(url, newsroom)
            return Response(url, "Access denied", 403)

        with tempfile.TemporaryDirectory() as temp:
            result = await OfficialSourceStrategy(
                browser(temp), None, self.stock(), self.ecosystem(), self.winner(), fetcher=fetch
            ).discover()
        self.assertTrue(result["usable"])
        self.assertIn(SOURCE_IR_FINANCIAL, result["evidence_families"])
        self.assertTrue(any(d.source_family == SOURCE_STATIC for d in result["documents"]))
        self.assertTrue(any(d.source_family == SOURCE_NEWSROOM for d in result["documents"]))
        self.assertIn("PRIMARY_IR_BLOCKED", result["limitations"])

    async def test_unverified_cdn_route_is_rejected(self):
        async def fetch(url, referer):
            return Response(url, "<html><title>Unrelated</title><body>Other company</body></html>")

        with tempfile.TemporaryDirectory() as temp:
            result = await OfficialSourceStrategy(
                browser(temp), None, self.stock(), self.ecosystem(), self.winner(), fetcher=fetch
            ).discover()
        self.assertFalse(result["usable"])
        self.assertFalse(any(d.source_family == SOURCE_STATIC for d in result["documents"]))

    async def test_sec_fallback_keeps_accession_and_issuer_provenance(self):
        tickers = {"0": {"ticker": "ACME", "cik_str": 1234, "title": "Acme Corporation"}}
        submissions = {
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K"],
                    "accessionNumber": ["0000000000-26-000001", "0000000000-26-000002"],
                    "primaryDocument": ["q.htm", "current.htm"],
                    "filingDate": ["2026-08-01", "2026-08-15"],
                    "reportDate": ["2026-06-30", "2026-08-10"],
                }
            }
        }

        async def sec_fetch(url):
            return tickers if "company_tickers" in url else submissions

        with tempfile.TemporaryDirectory() as temp:
            strategy = OfficialSourceStrategy(
                browser(temp), None, self.stock(), self.ecosystem(), self.winner(), fetcher=lambda *_: Response("https://investor.acme.com/financial-info", "Access denied", 403), sec_fetcher=sec_fetch
            )
            result = await strategy.discover()
        sec_docs = [d for d in result["documents"] if d.source_family == SOURCE_SEC]
        self.assertEqual(len(sec_docs), 2)
        self.assertEqual(sec_docs[0].provenance[0]["issuer_cik"], "0000001234")
        self.assertEqual(sec_docs[0].provenance[0]["accession_number"], "0000000000-26-000001")

    async def test_duplicate_release_across_verified_routes_is_merged_with_provenance(self):
        html = """
        <html><title>Acme Financial Information</title><body>Acme Corporation financial results
        <a href='https://acme.com/press-releases/acme-q2-results'>Acme Q2 results July 30, 2026</a></body></html>
        """

        async def fetch(url, referer):
            if "/financial-info" in url or "/newsroom" in url:
                return Response(url, html)
            return Response(url, "blocked", 403)

        async def sec_fetch(url):
            return []

        with tempfile.TemporaryDirectory() as temp:
            result = await OfficialSourceStrategy(
                browser(temp), None, self.stock(), self.ecosystem(), self.winner(), fetcher=fetch, sec_fetcher=sec_fetch
            ).discover()
        matches = [d for d in result["documents"] if d.source_url.endswith("acme-q2-results")]
        self.assertEqual(len(matches), 1)
        self.assertGreaterEqual(len(matches[0].provenance), 1)

    async def test_all_official_routes_unusable(self):
        async def fetch(url, referer):
            return Response(url, "blocked", 403)

        async def sec_fetch(url):
            return []

        with tempfile.TemporaryDirectory() as temp:
            result = await OfficialSourceStrategy(
                browser(temp), None, self.stock(), self.ecosystem(), self.winner(), fetcher=fetch, sec_fetcher=sec_fetch
            ).discover()
        self.assertFalse(result["usable"])
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["coverage_status"], "ALL_OFFICIAL_SOURCES_UNUSABLE")

    async def test_sec_fallback_is_independent_when_ir_winner_is_absent(self):
        submissions = {
            "filings": {"recent": {
                "form": ["10-Q", "8-K", "DEF 14A"],
                "accessionNumber": ["0000000000-26-000001", "0000000000-26-000002", "0000000000-26-000003"],
                "primaryDocument": ["q.htm", "current.htm", "proxy.htm"],
                "filingDate": ["2026-08-01", "2026-08-15", "2026-05-01"],
                "reportDate": ["2026-06-30", "2026-08-10", "2026-04-01"],
            }}
        }

        async def fetch(url, referer):
            return Response(url, "blocked", 403)

        async def sec_fetch(url):
            return submissions

        with tempfile.TemporaryDirectory() as temp:
            ecosystem = IREcosystem(official_corporate_domain="acme.com", official_corporate_url="https://acme.com/")
            result = await OfficialSourceStrategy(
                browser(temp), None, self.stock(), ecosystem, None,
                fetcher=fetch, sec_fetcher=sec_fetch, issuer_cik="0000001234",
            ).discover()
        sec_docs = [doc for doc in result["documents"] if doc.source_family == SOURCE_SEC]
        self.assertEqual(len(sec_docs), 3)
        self.assertEqual(result["statuses"][SOURCE_SEC], "SUCCESS")
        self.assertIn("IR_DISCOVERY_FAILED", result["limitations"])

    async def test_rmd_style_financial_results_and_press_pagination_are_classified(self):
        financial_page_1 = """
        <html><title>ResMed Financial Results</title><body>ResMed Inc financial results
        <article><a href='/reports/q1-2026-earnings-release.pdf'>Earnings Release</a>
        <a href='/events/q1-2026'>Earnings Webcast</a>
        <a href='/transcripts/q1-2026'>Audio Transcript</a>
        <a href='/presentations/q1-2026.pdf'>Presentation</a></article>
        <a href='/quarterly-earnings/financial-results?page=2'>2</a></body></html>
        """
        financial_page_2 = """
        <html><title>ResMed Financial Results</title><body>ResMed Inc financial results
        <a href='/reports/q2-2026-10-q.pdf'>10-Q</a></body></html>
        """
        newsroom = """
        <html><title>ResMed Press Releases</title><body>ResMed Inc press releases
        <a href='/news-events/press-releases/r1'>First release Sep 1, 2026</a>
        <a href='/news-events/press-releases?page=2'>2</a></body></html>
        """
        newsroom_page_2 = """
        <html><title>ResMed Press Releases</title><body>ResMed Inc press releases
        <a href='/news-events/press-releases/r2'>Second release Aug 1, 2026</a></body></html>
        """

        async def fetch(url, referer):
            if "quarterly-earnings/financial-results?page=2" in url:
                return Response(url, financial_page_2)
            if "quarterly-earnings/financial-results" in url:
                return Response(url, financial_page_1)
            if "press-releases?page=2" in url:
                return Response(url, newsroom_page_2)
            if "press-releases" in url or "/news-events" in url:
                return Response(url, newsroom)
            return Response(url, "blocked", 403)

        with tempfile.TemporaryDirectory() as temp:
            ecosystem = IREcosystem(official_corporate_domain="resmed.com", official_corporate_url="https://resmed.com/")
            winner = IRCandidate("https://investor.resmed.com/", official=True, corporate_url="https://resmed.com/", access=AccessResult(browser_accessible=True))
            result = await OfficialSourceStrategy(browser(temp), None, Stock("RMD", "RESMED INC", "", 0), ecosystem, winner, fetcher=fetch, sec_fetcher=lambda url: {}) .discover()
        titles = " ".join(document.title for document in result["documents"])
        self.assertIn("Earnings Release", titles)
        self.assertIn("Audio Transcript", titles)
        self.assertIn("10-Q", titles)
        self.assertTrue(any(document.source_url.endswith("/r2") for document in result["documents"]))

    def test_ir_failure_with_sec_evidence_is_success_with_limitations(self):
        status, limitations = aggregate_status(
            ir="DISCOVERY_FAILED", news="NO_DOCUMENTS_FOUND", reports="SUCCESS", events="NOT_SCANNED",
            news_metrics={"newly_saved": 0, "cached": 0}, reports_metrics={"newly_saved": 3, "cached": 0},
            event_metrics={}, official_evidence=True, source_limitations=["IR_DISCOVERY_FAILED"],
        )
        self.assertEqual(status, "SUCCESS_WITH_LIMITATIONS")
        self.assertIn("IR_DISCOVERY_FAILED", limitations)


if __name__ == "__main__":
    unittest.main()
