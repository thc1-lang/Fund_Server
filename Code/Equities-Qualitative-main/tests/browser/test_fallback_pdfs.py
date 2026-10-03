import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from pypdf import PdfReader
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.downloader import Downloader
from qualitative_ir_downloader.models import Document, CollectionError
from qualitative_ir_downloader.rss import parse_feed
from qualitative_ir_downloader.urls import Scope

class FallbackPDFTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        (self.folder/"News Releases").mkdir()
        self.browser=await Browser(Config(request_delay=0)).__aenter__()
        self.session=self.browser.session()
        self.context=await self.session.__aenter__()

    async def asyncTearDown(self):
        await self.session.__aexit__(None,None,None)
        await self.browser.__aexit__(None,None,None)
        self.temp.cleanup()

    async def test_full_official_rss_body_pdf(self):
        body=("<p>Acme announces its latest operational update with detailed published facts for investors.</p>"*25)+"<p>Contacts: Acme Investor Relations. END OF RELEASE.</p>"
        feed=('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>Acme release</title><link>https://ir.acme.com/release</link><pubDate>Tue, 01 Sep 2026 00:00:00 GMT</pubDate><content:encoded><![CDATA['+body+']]></content:encoded></item></channel></rss>').encode()
        doc=parse_feed(feed,"https://ir.acme.com/rss.xml",Scope(["https://ir.acme.com/"]))[0]
        self.assertTrue(doc.content_complete)
        d=Downloader(self.browser,self.context,self.folder,"ACME","Acme")
        with patch.object(self.browser,"goto",AsyncMock(side_effect=CollectionError("SITE_BLOCKED","403"))), patch("qualitative_ir_downloader.downloader.ResourceClient.get",AsyncMock(side_effect=CollectionError("SITE_BLOCKED","403"))):
            saved=await d.save(doc)
        self.assertEqual(saved.method,"official_rss_to_pdf")
        data=(self.folder/saved.local_filename).read_bytes()
        text=" ".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
        self.assertIn("END OF RELEASE",text)
        self.assertIn("https://ir.acme.com/rss.xml",text)
        self.assertIn("Acme",text)
        Path("artifacts/pdf-qa").mkdir(parents=True,exist_ok=True)
        Path("artifacts/pdf-qa/rss-release.pdf").write_bytes(data)

    async def test_verified_wire_fallback(self):
        doc=Document("Acme release",None,"https://ir.acme.com/release","news",wire_source_url="https://www.businesswire.com/news/home/123/en/",wire_provenance_url="https://ir.acme.com/release")
        async def goto(page,url):
            if "businesswire.com" not in url: raise CollectionError("SITE_BLOCKED","403")
            await page.set_content("<h1>Acme release</h1><p>"+("Acme published a full press release. "*30)+"</p>")
        with patch.object(self.browser,"goto",goto),patch("qualitative_ir_downloader.downloader.ResourceClient.get",AsyncMock(side_effect=CollectionError("SITE_BLOCKED","403"))):
            saved=await Downloader(self.browser,self.context,self.folder,"ACME","Acme").save(doc)
        self.assertEqual(saved.method,"verified_wire_source_html_to_pdf")

    async def test_unproven_wire_source_rejected(self):
        doc=Document("Acme release",None,"https://ir.acme.com/release","news",wire_source_url="https://www.businesswire.com/news/home/123/en/")
        with patch.object(self.browser,"goto",AsyncMock(side_effect=CollectionError("SITE_BLOCKED","403"))) as goto,patch("qualitative_ir_downloader.downloader.ResourceClient.get",AsyncMock(side_effect=CollectionError("SITE_BLOCKED","403"))):
            with self.assertRaises(CollectionError):
                await Downloader(self.browser,self.context,self.folder,"ACME","Acme").save(doc)
        self.assertEqual(goto.await_count,1)
