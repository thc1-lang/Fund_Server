import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from pypdf import PdfWriter
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.downloader import Downloader
from qualitative_ir_downloader.models import Document, CollectionError

def pdf_bytes():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.write(output)
    return output.getvalue()

class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_dedup_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "Reports").mkdir()
            d = Downloader(Browser(Config()), AsyncMock(), folder, "ACME")
            d.fetch_pdf = AsyncMock(return_value=(pdf_bytes(), "https://cdn.example.com/final.pdf"))
            first = await d.save(Document("Annual", "2025", "https://ir.example.com/one.pdf", "reports", "https://ir.example.com/one.pdf"))
            second = await d.save(Document("Annual duplicate", "2025", "https://ir.example.com/two.pdf", "reports", "https://ir.example.com/two.pdf"))
            self.assertEqual(first.local_filename, second.local_filename)
            self.assertIsNotNone(second.duplicate_of)
            self.assertNotEqual(first.source_url, second.source_url)
            self.assertEqual(len(list((folder / "Reports").glob("*.pdf"))), 1)

    async def test_viewer_redirect_and_invalid_response(self):
        context = AsyncMock()
        context.cookies.return_value = []
        downloader = Downloader(Browser(Config(request_delay=0)), context, Path("."), "ACME")
        async def handle(request):
            if request.url.path == "/viewer":
                return httpx.Response(200, text='<iframe src="/file.pdf"></iframe>')
            if request.url.path == "/file.pdf":
                return httpx.Response(200, content=pdf_bytes(), headers={"content-type":"application/octet-stream"})
            return httpx.Response(200, text="<html>Error</html>")
        real_client = httpx.AsyncClient
        with patch("qualitative_ir_downloader.network.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)):
            data, url = await downloader.fetch_pdf("https://cdn.example.com/viewer", "https://ir.example.com/")
            self.assertTrue(data.startswith(b"%PDF"))
            self.assertEqual(url, "https://cdn.example.com/file.pdf")
            with self.assertRaises(CollectionError):
                await downloader.fetch_pdf("https://cdn.example.com/error", "https://ir.example.com/")

    async def test_blocked_response_is_not_retried(self):
        context = AsyncMock()
        context.cookies.return_value = []
        downloader = Downloader(Browser(Config(request_delay=0)), context, Path("."), "ACME")
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(403, text="Access denied")
        real_client = httpx.AsyncClient
        with patch("qualitative_ir_downloader.network.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)):
            with self.assertRaises(CollectionError) as error:
                await downloader.fetch_pdf("https://cdn.example.com/file", "https://ir.example.com/")
        self.assertEqual(error.exception.code, "SITE_BLOCKED")
        self.assertEqual(len(calls), 1)
