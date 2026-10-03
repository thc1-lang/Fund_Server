"""Regression cases use real Chromium with in-memory pages and mocked basic HTTP."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.ir_discovery import IRDiscovery
from qualitative_ir_downloader.network import ResourceResponse
from qualitative_ir_downloader.main import process_company
from qualitative_ir_downloader.models import Stock, ArchiveResult

CORPORATE = '<title>Acme Corporation</title><h1>Acme</h1><nav><a href="https://ir.acme.com/">Investors &amp; News</a></nav>'
IR = '<title>Acme Investors &amp; News</title><h1>Acme Investor Relations</h1><a href="/press-releases">All News</a><a href="/annual-reports">View All Reports</a><p>Annual Reports Stock Information Financial Information</p><link rel="alternate" type="application/rss+xml" href="/rss.xml">'

class OfficialityBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = await Browser(Config(events=EventConfig(enabled=False), request_delay=0, download_root=Path(self.temp.name))).__aenter__()
        self.session = self.browser.session()
        self.context = await self.session.__aenter__()

    async def asyncTearDown(self):
        await self.session.__aexit__(None, None, None)
        await self.browser.__aexit__(None, None, None)
        self.temp.cleanup()

    async def route_pages(self, ir_status=200, hosted=False):
        target = "https://acme.gcs-web.com/" if hosted else "https://ir.acme.com/"
        corporate = CORPORATE.replace("https://ir.acme.com/", target)
        async def route(r):
            body, status = (corporate, 200) if r.request.url == "https://www.acme.com/" else (IR if ir_status == 200 else "<title>Access denied</title>", ir_status)
            await r.fulfill(status=status, content_type="text/html", body=body)
        await self.context.route("**/*", route)

    async def discover(self):
        with patch.object(IRDiscovery, "search", AsyncMock(return_value=["https://www.acme.com/"])), patch("qualitative_ir_downloader.network.ResourceClient.http", AsyncMock(return_value=ResourceResponse("https://ir.acme.com/", 403, {}, b"Denied", "http"))):
            discovery = IRDiscovery(self.browser)
            winner = await discovery.discover_investor_relations_site("ACME", "Acme", self.context)
        return discovery, winner

    async def test_http_403_browser_success_is_accepted(self):
        await self.route_pages()
        discovery, winner = await self.discover()
        self.assertTrue(winner.official)
        self.assertTrue(winner.accepted)
        self.assertEqual(winner.access.http_status, 403)
        self.assertEqual(winner.access.browser_status, 200)
        self.assertIn("https://ir.acme.com/rss.xml", discovery.rss_feeds)
        self.assertEqual(discovery.ecosystem.official_corporate_domain, "acme.com")

    async def test_both_403_remain_official(self):
        await self.route_pages(ir_status=403)
        _, winner = await self.discover()
        self.assertTrue(winner.official)
        self.assertFalse(winner.accepted)
        self.assertTrue(winner.access.blocked)
        self.assertEqual(winner.access.browser_status, 403)

    async def test_corporate_hosted_relationship(self):
        await self.route_pages(hosted=True)
        discovery, winner = await self.discover()
        self.assertTrue(winner.accepted)
        self.assertIn("acme.gcs-web.com", discovery.ecosystem.verified_hosted_ir_domains)

    async def test_blocked_official_summary_status(self):
        await self.route_pages(ir_status=403)
        discovery, winner = await self.discover()
        # Keep this same context for the orchestration test.
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def session():
            yield self.context
        adapter = AsyncMock()
        adapter.name, adapter.evidence = "generic", []
        adapter.discover_news.return_value = ArchiveResult()
        adapter.discover_reports.return_value = ArchiveResult()
        with patch("qualitative_ir_downloader.main.select_provider", AsyncMock(return_value=adapter)), patch.object(self.browser, "session", session), patch("qualitative_ir_downloader.main.IRDiscovery", return_value=discovery), patch.object(discovery, "discover_investor_relations_site", AsyncMock(return_value=winner)):
            result = await process_company(self.browser, Stock("ACME", "Acme", "Safe", 4), "2026-09-06_12-00-00")
        # A blocked primary IR site is retained as a source-level diagnostic.
        # Once the verified official fallbacks have also produced no usable
        # evidence, the company-level outcome is FAILED.
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["overall_status"], "FAILED")
        self.assertEqual(result["official_source_coverage_status"], "ALL_OFFICIAL_SOURCES_UNUSABLE")
        self.assertIn("PRIMARY_IR_BLOCKED", result["official_source_limitations"])
        self.assertIn("ALL_OFFICIAL_SOURCES_UNUSABLE", result["official_source_limitations"])
        self.assertTrue(result["ir_official"])
        self.assertFalse(result["ir_accessible"])
        self.assertEqual(result["document_failures"], 0)
        self.assertGreaterEqual(result["access_blocks"], 1)
