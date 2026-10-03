import unittest
from unittest.mock import AsyncMock, patch
import httpx
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.ir_discovery import endorsed_candidate, corporate_identity
from qualitative_ir_downloader.models import Link, Stock
from qualitative_ir_downloader.scoring import score_ir
from qualitative_ir_downloader.rss import parse_feed
from qualitative_ir_downloader.urls import Scope
from qualitative_ir_downloader.network import ResourceClient

class OfficialityTests(unittest.TestCase):
    def test_hosted_link_verified_without_access(self):
        candidate = endorsed_candidate("https://www.acme.com/", Link("https://acme.gcs-web.com/", "Investors & News"))
        self.assertTrue(candidate.official)
        self.assertFalse(candidate.accepted)
        self.assertEqual(candidate.verified_hosted_ir_domain, "acme.gcs-web.com")

    def test_investor_conference_article_is_not_ir_navigation(self):
        self.assertIsNone(endorsed_candidate("https://www.acme.com/", Link("https://ir.acme.com/news-release-details/update", "Acme to Webcast Fireside Chats at Investor Conferences")))

    def test_aggregators_never_become_official(self):
        for domain in ("quartr.com", "alphaspread.com", "finance.yahoo.com"):
            self.assertIsNone(endorsed_candidate("https://www.acme.com/", Link(f"https://{domain}/acme", "Investors")))
            self.assertLess(score_ir(f"https://{domain}/acme", "Acme Investor Relations", "Acme Investors Annual Reports Stock Information", Stock("ACME", "Acme", "Safe", 4)).score, 0)

    def test_subdomain_trap_not_corporate_identity(self):
        self.assertFalse(corporate_identity("https://acme.evil.com/", "Acme", "Acme", "Acme"))
        self.assertTrue(corporate_identity("https://www.acme.co.uk/", "Acme", "Acme", "Acme"))

    def test_rss_preserves_provenance_and_scope(self):
        data = b'<rss><channel><item><title>Acme update</title><link>https://ir.acme.com/news/update</link><pubDate>Sun, 06 Sep 2026 12:00:00 GMT</pubDate></item><item><title>Wrong company</title><link>https://other.com/news</link></item></channel></rss>'
        docs = parse_feed(data, "https://ir.acme.com/rss", Scope(["https://ir.acme.com/"]))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].date, "2026-09-06")
        self.assertEqual(docs[0].linked_from_url, "https://ir.acme.com/rss")

    def test_rss_entities_rejected(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE foo><rss/>', "https://ir.acme.com/rss", Scope(["https://ir.acme.com/"]))

class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_http_denial_uses_existing_context(self):
        browser = Browser(Config(request_delay=0))
        browser.accessible_hosts.add("ir.acme.com")
        context = AsyncMock()
        context.cookies.return_value = []
        api_response = AsyncMock()
        api_response.status = 200
        api_response.headers = {"content-type": "application/pdf"}
        api_response.body.return_value = b"%PDF-example"
        context.request.get.return_value = api_response
        real = httpx.AsyncClient
        with patch("qualitative_ir_downloader.network.httpx.AsyncClient", side_effect=lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(403)), **kw)):
            response = await ResourceClient(browser, context).get("https://cdn.acme.com/file.pdf", "https://ir.acme.com/news")
        self.assertEqual(response.transport, "browser_context")
        self.assertEqual(response.status, 200)
        self.assertFalse(browser.blocked_hosts)
        self.assertEqual([e.status for e in browser.access_events], [403, 200])
        context.request.get.assert_awaited_once()
        api_response.dispose.assert_awaited_once()
