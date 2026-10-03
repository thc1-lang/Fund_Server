"""Offline Chromium integration tests; all web requests use in-memory routes."""
import io
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfReader
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.crawler import ArchiveCrawler
from qualitative_ir_downloader.news import find_news_section
from qualitative_ir_downloader.pdf_utils import render_release
from qualitative_ir_downloader.urls import Scope

HOME = '<title>Acme Investor Relations</title><nav><a href="/news">News Releases</a><a href="/reports">Featured Reports</a></nav>'
NEWS = """<title>Acme News Releases</title><main><h1>News Releases</h1>
<select aria-label="Year" onchange="document.querySelector('#items').innerHTML=release(this.value)">
<option value="2026">2026</option><option value="2025">2025</option></select>
<div id="items"><a href="/news-release-details/new">Acme announces new product</a></div>
<button onclick="document.querySelector('#items').insertAdjacentHTML('beforeend',release('more'));this.remove()">Load More</button>
<nav aria-label="Pagination"><a href="/news?page=2">Next</a></nav>
<script>function release(y){return '<a href="/news-release-details/'+y+'">Acme announces results '+y+'</a>'}</script></main>"""
PAGE2 = '<h1>News Releases</h1><a href="/news-release-details/old">Acme announces old results</a><a href="/news">Previous</a>'
REPORTS = '<h1>Featured Reports</h1><a href="https://cdn.acme.test/annual.pdf">2025 Annual Report PDF</a><a href="/reports/archive">Reports Archive</a>'
ARTICLE = "<html><head><title>Acme launches new product</title></head><body><nav>Navigation menu</nav><main><h1>Acme launches new product</h1><time datetime='2026-08-14'>August 14, 2026</time>" + ("<p>This is a complete press release paragraph with important product details and the company's published investor information.</p>" * 40) + "<p>END OF RELEASE - Contact investor relations.</p></main></body></html>"

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser = await Browser(Config(request_delay=0, max_archive_pages=10, max_interactions=20)).__aenter__()
        self.session = self.browser.session()
        self.context = await self.session.__aenter__()
        async def route(request_route):
            url = request_route.request.url
            if "/news?page=2" in url:
                body = PAGE2
            elif url.endswith("/news"):
                body = NEWS
            elif "/reports/archive" in url:
                body = '<a href="https://cdn.acme.test/2024.pdf">2024 Annual Report PDF</a>'
            elif url.endswith("/reports"):
                body = REPORTS
            elif "/article" in url:
                body = ARTICLE
            else:
                body = HOME
            await request_route.fulfill(status=200, content_type="text/html", body=body)
        await self.context.route("**/*", route)

    async def asyncTearDown(self):
        await self.session.__aexit__(None, None, None)
        await self.browser.__aexit__(None, None, None)

    async def test_discovery_year_load_more_pagination(self):
        page = await self.context.new_page()
        sections = await find_news_section(page, "https://ir.acme.test/", self.browser)
        self.assertIn("https://ir.acme.test/news", sections)
        result = await ArchiveCrawler(self.browser, Scope(["https://ir.acme.test/"])).collect(self.context, sections, "news")
        endings = {d.source_url.rsplit("/",1)[-1] for d in result.documents}
        self.assertEqual(endings, {"new", "more", "2025", "2026", "old"})
        self.assertFalse(result.errors)

    async def test_reports_and_cdn(self):
        result = await ArchiveCrawler(self.browser, Scope(["https://ir.acme.test/"])).collect(self.context, ["https://ir.acme.test/reports"], "reports")
        self.assertEqual(len(result.documents), 2)
        self.assertTrue(all(d.pdf_source_url for d in result.documents))

    async def test_print_entire_release(self):
        page = await self.context.new_page()
        await self.browser.goto(page, "https://ir.acme.test/article")
        data = await render_release(page, self.browser)
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(p.extract_text() for p in reader.pages)
        self.assertIn("Acme launches new product", text)
        self.assertIn("August 14, 2026", text)
        self.assertIn("END OF RELEASE", text)
        self.assertGreater(len(reader.pages), 1)
        output = Path("artifacts/pdf-qa")
        output.mkdir(parents=True, exist_ok=True)
        (output / "fixture-release.pdf").write_bytes(data)

    async def test_safety_limit_is_reported(self):
        self.browser.config = Config(request_delay=0, max_archive_pages=1, max_interactions=20)
        result = await ArchiveCrawler(self.browser, Scope(["https://ir.acme.test/"])).collect(self.context, ["https://ir.acme.test/reports"], "reports")
        self.assertTrue(any(e.code == "SAFETY_LIMIT_REACHED" for e in result.errors))

    async def test_tabs_across_years(self):
        html = """<h1>Reports</h1><select onchange="render()"><option>2026</option><option>2025</option></select>
        <button role="tab" onclick="kind='annual';render()">Annual Reports</button>
        <button role="tab" onclick="kind='quarterly';render()">Quarterly Reports</button>
        <div id="items"></div><script>let kind='annual';function render(){let year=document.querySelector('select').value;document.querySelector('#items').innerHTML='<a href="https://cdn.acme.test/'+kind+year+'.pdf">'+year+' '+kind+' report PDF</a>'}render()</script>"""
        await self.context.route("**/combined", lambda route: route.fulfill(status=200,content_type="text/html",body=html))
        result = await ArchiveCrawler(self.browser, Scope(["https://ir.acme.test/"])).collect(self.context, ["https://ir.acme.test/combined"], "reports")
        self.assertEqual(len(result.documents), 4)
        self.assertFalse(result.errors)

    async def test_featured_reports_on_homepage(self):
        from qualitative_ir_downloader.reports import find_reports_section
        html = '<h1>Investor Relations</h1><section><h2>Featured Reports</h2><a href="https://cdn.acme.test/annual.pdf">2025 Annual Report PDF</a></section><section><h2>News</h2><a href="https://cdn.acme.test/news.pdf">PDF</a></section>'
        await self.context.route("https://ir.acme.test/", lambda route: route.fulfill(status=200,content_type="text/html",body=html))
        page = await self.context.new_page()
        sections = await find_reports_section(page, "https://ir.acme.test/", self.browser)
        self.assertIn("https://ir.acme.test/", sections)
        result = await ArchiveCrawler(self.browser, Scope(["https://ir.acme.test/"])).collect(self.context, sections, "reports")
        self.assertEqual(len(result.documents), 1)
        self.assertIn("annual.pdf", result.documents[0].source_url)

    async def test_original_pdf_preferred_and_html_fallback(self):
        from unittest.mock import AsyncMock
        from pypdf import PdfWriter
        from qualitative_ir_downloader.downloader import Downloader
        from qualitative_ir_downloader.models import Document
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        buffer = io.BytesIO()
        writer.write(buffer)
        html = ARTICLE.replace("<main>", '<main><a href="https://cdn.acme.test/release.pdf">PDF Version</a>')
        await self.context.route("**/original", lambda route: route.fulfill(status=200,content_type="text/html",body=html))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "News Releases").mkdir()
            downloader = Downloader(self.browser, self.context, folder, "ACME")
            downloader.fetch_pdf = AsyncMock(return_value=(buffer.getvalue(), "https://cdn.acme.test/release.pdf"))
            original = await downloader.save(Document("Original", None, "https://ir.acme.test/original", "news"))
            self.assertEqual(original.method, "official_pdf")
            self.assertEqual(original.date, "2026-08-14")
            fallback = await downloader.save(Document("HTML", None, "https://ir.acme.test/article", "news"))
            self.assertEqual(fallback.method, "official_html_to_pdf")
            self.assertEqual(downloader.fetch_pdf.await_count, 1)
