import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from bs4 import BeautifulSoup
from qualitative_ir_downloader.models import Document, IREcosystem, CollectionError, Stock
from qualitative_ir_downloader.urls import Scope
from qualitative_ir_downloader.rss import parse_feed
from qualitative_ir_downloader.structured import merge_documents, report_documents, wire_link, archive_next_urls
from qualitative_ir_downloader.providers.gcs_web import matches_verified_domain, candidate_urls, GCSWebAdapter, news_feed_link, archive_state_key
from qualitative_ir_downloader.providers.base import ProviderContext
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.network import ResourceResponse
from qualitative_ir_downloader.downloader import Downloader

FEED = b'<rss><channel><title>Acme News</title><item><title>Acme update</title><link>https://ir.acme.com/news-release-details/update</link><guid>123</guid><description>Short summary</description><pubDate>Tue, 01 Sep 2026 16:05:00 -0400</pubDate></item></channel></rss>'

class ProviderTests(unittest.TestCase):
    def test_provider_release_title_over_generic_page_heading(self):
        from qualitative_ir_downloader.extraction import metadata
        title, date = metadata('<main><h1>Press Release</h1><h2><div class="field--name-field-nir-news-title">Acme announces an update</div></h2><time datetime="2026-09-01">September 1, 2026</time></main>')
        self.assertEqual(title, "Acme announces an update")
        self.assertEqual(date, "2026-09-01")

    def test_other_subscription_feeds_are_excluded(self):
        self.assertFalse(news_feed_link("https://ir.acme.com/rss/sec-filings.xml", "SEC Filings"))
        self.assertFalse(news_feed_link("https://ir.acme.com/rss/events.xml", "Events"))
        self.assertTrue(news_feed_link("https://ir.acme.com/rss/news-releases.xml", "All Press Releases"))

    def test_archive_state_ignores_volatile_form_identity(self):
        self.assertEqual(archive_state_key("https://ir.acme.com/news?year=2025&form_build_id=old"), archive_state_key("https://ir.acme.com/news?year=2025&form_build_id=new"))
        self.assertNotEqual(archive_state_key("https://ir.acme.com/news?year=2025"), archive_state_key("https://ir.acme.com/news?year=2024"))

    def test_rss_conversion_and_dedup(self):
        scope=Scope(["https://ir.acme.com/"])
        docs=parse_feed(FEED,"https://ir.acme.com/rss",scope)
        self.assertEqual(docs[0].guid,"123")
        self.assertEqual(docs[0].description,"Short summary")
        self.assertFalse(docs[0].content_complete)
        self.assertEqual(docs[0].discovery_method,"official_ir_rss")
        self.assertEqual(len(merge_documents(docs+parse_feed(FEED,"https://ir.acme.com/rss2",scope))),1)

    def test_title_date_alias_dedup(self):
        a=Document("Title","2026-09-01","https://ir.acme.com/a","news")
        b=Document("Title","2026-09-01","https://acme.gcs-web.com/a","news")
        self.assertEqual(len(merge_documents([a,b])),1)

    def test_verified_provider_recognition(self):
        eco=IREcosystem(verified_ir_urls=["https://acme.gcs-web.com/"],verified_hosted_ir_domains=["acme.gcs-web.com"])
        self.assertTrue(matches_verified_domain(eco))
        eco.verified_ir_urls=[]
        self.assertFalse(matches_verified_domain(eco))

    def test_unverified_provider_rejected(self):
        self.assertFalse(matches_verified_domain(IREcosystem(verified_ir_urls=["https://ir.acme.com/"])))

    def test_alias_candidates_only_from_verified_roots(self):
        urls=candidate_urls(["https://ir.acme.com/","https://acme.gcs-web.com/"],["rss/news-releases.xml"])
        self.assertEqual(len(urls),2)
        self.assertNotIn("https://other.gcs-web.com/rss/news-releases.xml",urls)

    def test_report_metadata_extensionless(self):
        soup=BeautifulSoup('<article><a href="/static-files/abc">2025 Sustainability Report</a> 2.4 MB</article>',"html.parser")
        doc=report_documents(soup,"https://ir.acme.com/reports")[0]
        self.assertEqual(doc.date,"2025")
        self.assertEqual(doc.file_size,"2.4 MB")
        self.assertEqual(doc.pdf_source_url,"https://ir.acme.com/static-files/abc")

    def test_wire_requires_actual_source_link(self):
        soup=BeautifulSoup('<a href="https://www.businesswire.com/news/home/123/en/">View source version on businesswire.com</a>',"html.parser")
        self.assertTrue(wire_link(soup,"https://ir.acme.com/release"))
        self.assertIsNone(wire_link(BeautifulSoup("Business Wire has a matching title","html.parser"),"https://ir.acme.com/release"))

    def test_year_get_parameters_derived_from_form(self):
        soup=BeautifulSoup('<form method="get" action="/press-releases"><select name="provider_year[value]"><option value="2025">2025</option></select></form>',"html.parser")
        urls=archive_next_urls(soup,"https://ir.acme.com/press-releases","news",Scope(["https://ir.acme.com/"]))
        self.assertIn("provider_year%5Bvalue%5D=2025",urls[0])

    def test_year_form_replaces_existing_query_controls(self):
        from urllib.parse import parse_qsl, urlsplit
        soup=BeautifulSoup('<form method="get"><input type="hidden" name="widget_id" value="news"><input type="hidden" name="form_build_id" value="new"><select name="year"><option value="2025">2025</option></select></form>', "html.parser")
        scope=Scope(["https://ir.acme.com/"])
        original="https://ir.acme.com/press-releases?widget_id=news&form_build_id=old&year=2026"
        first=archive_next_urls(soup,original,"news",scope)[0]
        second=archive_next_urls(soup,first,"news",scope)[0]
        self.assertEqual(first,second)
        self.assertEqual(dict(parse_qsl(urlsplit(first).query)), {"widget_id":"news","form_build_id":"new","year":"2025"})
        self.assertEqual(len(parse_qsl(urlsplit(first).query)),3)

class ProviderAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_feed_coverage_incomplete(self):
        browser=Browser(Config(request_delay=0))
        env=ProviderContext(browser,AsyncMock(),Stock("ACME","Acme","Safe",4),IREcosystem(verified_ir_urls=["https://ir.acme.com/"]))
        adapter=GCSWebAdapter(env)
        adapter.fetch=AsyncMock(side_effect=lambda url: ResourceResponse(url,200,{"content-type":"application/rss+xml"},FEED,"http") if url.endswith(".xml") else None)
        docs=await adapter.discover_news_rss()
        self.assertEqual(len(docs),1)
        self.assertEqual(adapter.rss_stats["oldest"],"2026-09-01")
        self.assertEqual(adapter.rss_stats["coverage"],"incomplete")

    async def test_discovered_asset_remains_blocked(self):
        browser=Browser(Config())
        d=Downloader(browser,AsyncMock(),Path("."),"ACME")
        d.fetch_pdf=AsyncMock(side_effect=CollectionError("SITE_BLOCKED","403"))
        doc=Document("Annual","2025","https://ir.acme.com/static-files/abc","reports","https://ir.acme.com/static-files/abc")
        with self.assertRaises(CollectionError):
            await d.save(doc)
        self.assertEqual(doc.status,"BLOCKED")
        self.assertEqual(doc.http_status,403)
        self.assertEqual(doc.title,"Annual")

    async def test_extensionless_static_asset_pdf_magic(self):
        import io, tempfile
        from pypdf import PdfWriter
        from unittest.mock import patch
        writer=PdfWriter();writer.add_blank_page(width=600,height=800)
        buffer=io.BytesIO();writer.write(buffer)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/"Reports").mkdir()
            response=ResourceResponse("https://ir.acme.com/static-files/abc",200,{"content-type":"application/pdf"},buffer.getvalue(),"http")
            with patch("qualitative_ir_downloader.network.ResourceClient.get",AsyncMock(return_value=response)):
                doc=await Downloader(Browser(Config()),AsyncMock(),folder,"ACME").save(Document("Annual Report","2025",response.url,"reports",response.url))
            self.assertEqual(doc.status,"DOWNLOADED")
            self.assertTrue(doc.local_filename.endswith(".pdf"))
