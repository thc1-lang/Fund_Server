"""HTTP original-PDF downloads with browser-cookie continuity and HTML fallback."""
import logging
import hashlib
import asyncio
from pathlib import Path
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .browser import Browser, links, BLOCK_TEXT
from .extraction import is_pdf, metadata
from .filesystem import document_path
from .models import CollectionError, Document, Link
from .pdf_utils import render_release, validate_pdf
from .urls import canonicalize_url, public_url
from .network import ResourceClient
from .structured import wire_link, WIRE_HOSTS
from .archival_pdf import render_archival

log = logging.getLogger(__name__)

class Downloader:
    def __init__(self, browser: Browser, context, folder: Path, ticker: str, company_name: str = ""):
        self.browser, self.context, self.folder, self.ticker = browser, context, folder, ticker
        self.company_name = company_name or ticker
        self.hashes: dict[str, str] = {}
        self.urls: dict[str, Document] = {}
        self.existing: dict[str, dict] = {}
        self.cache_hits = 0
        self.network_requests_avoided = 0

    def register_existing(self, records: list[dict]) -> None:
        """Register manifest records eligible for identity-validated reuse."""
        for record in records or []:
            key = canonicalize_url(str(record.get("source_url", "")))
            if key:
                self.existing[key] = record

    def _existing_valid(self, document: Document, record: dict) -> bool:
        if canonicalize_url(document.source_url) != canonicalize_url(str(record.get("source_url", ""))):
            return False
        if record.get("category") and record.get("category") != document.category:
            return False
        if record.get("date") and document.date and str(record.get("date")) != str(document.date):
            return False
        local = record.get("local_filename")
        if not local:
            return False
        path = self.folder / str(local)
        try:
            if not path.resolve().is_relative_to(self.folder.resolve()):
                return False
        except (OSError, ValueError):
            return False
        if not path.is_file() or path.stat().st_size == 0:
            return False
        expected = record.get("sha256")
        if expected:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                return False
        return True

    async def fetch_pdf(self, url: str, referer: str) -> tuple[bytes, str]:
        from .network import ResourceClient
        client = ResourceClient(self.browser, self.context)
        current, seen = url, set()
        for _ in range(8):
            if not public_url(current) or current in seen:
                raise CollectionError("PDF_DOWNLOAD_FAILED", f"Invalid viewer URL or cycle: {current}")
            seen.add(current)
            response = await client.get(current, referer)
            data = response.body
            if data.lstrip().startswith(b"%PDF-"):
                validate_pdf(data)
                return data, response.url
            soup = BeautifulSoup(data, "html.parser")
            viewer = soup.select_one('iframe[src], embed[src], object[data]')
            target = (viewer.get("src") or viewer.get("data")) if viewer else None
            if not target:
                target = next((a.get("href") for a in soup.select("a[href]") if is_pdf(Link(str(a.get("href")), a.get_text(" ", strip=True)))), None)
            if not target:
                raise CollectionError("PDF_DOWNLOAD_FAILED", f"Expected PDF, received {response.headers.get('content-type', '')}: {response.url}")
            current = urljoin(response.url, str(target))
        raise CollectionError("PDF_DOWNLOAD_FAILED", "Too many document viewers")

    async def save(self, document: Document) -> Document:
        document.official_ir_url = document.official_ir_url or document.source_url
        document.artifact_url = document.artifact_url or document.pdf_source_url or document.source_url
        document.retrieved_at = document.retrieved_at or datetime.now(timezone.utc).isoformat()
        try:
            saved = await self._save(document)
            saved.status = "CACHED" if saved.cache_hit else "DOWNLOADED"
            saved.download_url = saved.pdf_source_url or saved.wire_source_url or saved.source_url
            return saved
        except Exception as exc:
            document.status = "BLOCKED" if getattr(exc, "code", "") == "SITE_BLOCKED" else "FAILED"
            if document.status == "BLOCKED":
                document.http_status = next((e.status for e in reversed(self.browser.access_events) if e.blocked and e.status), 403)
                log.warning("DISCOVERED_BUT_BLOCKED: %s", document.source_url)
            raise

    async def _save(self, document: Document) -> Document:
        key = canonicalize_url(document.source_url)
        existing = self.existing.get(key)
        if existing and self._existing_valid(document, existing):
            document.local_filename = existing.get("local_filename")
            document.sha256 = existing.get("sha256")
            document.method = existing.get("method") or document.method
            document.pdf_source_url = existing.get("pdf_source_url") or document.pdf_source_url
            document.download_url = existing.get("download_url") or document.download_url
            document.cache_hit = True
            self.cache_hits += 1
            self.network_requests_avoided += 1
            self.urls[key] = document
            log.info("Reused cached artifact: %s", document.local_filename)
            return document
        if key in self.urls:
            previous = self.urls[key]
            document.local_filename, document.sha256 = previous.local_filename, previous.sha256
            document.method, document.pdf_source_url = previous.method, previous.pdf_source_url
            document.duplicate_of = previous.local_filename
            return document
        data, last_error = None, None
        for asset in list(dict.fromkeys(([document.pdf_source_url] if document.pdf_source_url else []) + document.alternate_pdf_urls)):
            try:
                data, actual = await self.fetch_pdf(asset, document.linked_from_url or document.source_url)
                document.pdf_source_url, document.method = actual, "official_pdf"
                break
            except Exception as exc:
                last_error = exc
                log.warning("Original linked PDF unavailable %s: %s", asset, exc)
        if data is None and document.category == "reports" and document.source_family == "SEC_EDGAR":
            data = await self.retrieve_sec(document)
        if data is None and document.category == "reports":
            raise last_error or CollectionError("PDF_DOWNLOAD_FAILED", "No report PDF link")
        if data is None:
            data = await self.retrieve_news(document)
        digest = validate_pdf(data)
        document.sha256 = digest
        if digest in self.hashes:
            document.local_filename = self.hashes[digest]
            document.duplicate_of = document.local_filename
            log.info("Duplicate content retained once: %s", document.source_url)
        else:
            sub = self.folder / ("News Releases" if document.category == "news" else "Reports")
            target = document_path(sub, self.ticker, document.title, document.date)
            temporary = target.with_suffix(".pdf.part")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            document.local_filename = target.relative_to(self.folder).as_posix()
            self.hashes[digest] = document.local_filename
            log.info("%s: %s", "Downloaded original PDF" if document.method == "official_pdf" else "HTML release converted to PDF", document.local_filename)
        self.urls[key] = document
        return document

    async def retrieve_sec(self, document: Document) -> bytes:
        """Fetch an official SEC HTML/text filing without bypassing blocks."""
        from urllib.request import Request, urlopen
        from .official_sources import SEC_USER_AGENT

        def fetch() -> bytes:
            request = Request(
                document.source_url,
                headers={"User-Agent": SEC_USER_AGENT, "Accept": "text/html,text/plain,*/*"},
            )
            with urlopen(request, timeout=30) as response:
                return response.read()

        try:
            body = await asyncio.to_thread(fetch)
        except Exception as exc:
            raise CollectionError("PDF_DOWNLOAD_FAILED", f"SEC filing unavailable: {exc}") from exc
        if not body:
            raise CollectionError("PDF_DOWNLOAD_FAILED", "SEC filing body is empty")
        document.content_html = body.decode("utf-8", "replace")
        document.content_complete = True
        document.method = "official_sec_html"
        return await render_archival(self.context, document, self.ticker, self.company_name, document.content_html)

    async def retrieve_news(self, document: Document) -> bytes:
        page = None
        html, browser_loaded, last_error = None, False, None
        try:
            try:
                if urlsplit(document.source_url).hostname in self.browser.blocked_hosts:
                    raise CollectionError("SITE_BLOCKED", "Browser frontend previously blocked; testing public release HTTP")
                page = await self.context.new_page()
                await self.browser.goto(page, document.source_url)
                html, browser_loaded = await page.content(), True
            except Exception as exc:
                last_error = exc
                try:
                    response = await ResourceClient(self.browser, self.context).get(document.source_url, document.linked_from_url or document.source_url)
                    html = response.body.decode("utf-8", "replace")
                except Exception as http_error:
                    last_error = http_error
            if html:
                check = BeautifulSoup(html, "html.parser")
                if BLOCK_TEXT.search(check.title.get_text() if check.title else ""):
                    last_error = CollectionError("SITE_BLOCKED", "Release HTTP returned an access-challenge page")
                    html = None
            if html:
                soup = BeautifulSoup(html, "html.parser")
                title, date = metadata(html)
                document.title, document.date = title or document.title, date or document.date
                for anchor in soup.select("a[href]"):
                    label = anchor.get_text(" ", strip=True)
                    if not re.fullmatch(r"(?:download |view |printable )?PDF(?: Version)?(?:\s*\([^)]*\))?", label, re.I):
                        continue
                    asset = canonicalize_url(anchor["href"], document.source_url)
                    try:
                        data, actual = await self.fetch_pdf(asset, document.source_url)
                        document.pdf_source_url, document.method = actual, "official_pdf"
                        return data
                    except Exception as exc:
                        last_error = exc
                        log.warning("Official PDF link unavailable %s: %s", asset, exc)
                wire = wire_link(soup, document.source_url)
                if wire:
                    document.wire_source_url, document.wire_provenance_url = wire, document.source_url
                    document.provenance.append({"source_url":document.source_url,"wire_source_url":wire,"relation":"Wire source explicitly linked by official IR"})
                document.method = "official_html_to_pdf"
                if browser_loaded:
                    return await render_release(page, self.browser)
                article = soup.select_one(".node--type-nir-news .node__content, article.node--type-nir-news, article, main")
                if article and len(article.get_text(" ",strip=True)) >= 150:
                    return await render_archival(self.context, document, self.ticker, self.company_name, str(article))
            if document.content_complete and document.content_html and document.feed_url:
                document.method = "official_rss_to_pdf"
                return await render_archival(self.context, document, self.ticker, self.company_name, document.content_html)
            if document.wire_source_url and document.wire_provenance_url and document.wire_provenance_url in (document.source_url, document.feed_url):
                host = urlsplit(document.wire_source_url).hostname or ""
                if any(host == h or host == "www." + h for h in WIRE_HOSTS):
                    if page is None:
                        page = await self.context.new_page()
                    await self.browser.goto(page, document.wire_source_url)
                    document.method = "verified_wire_source_html_to_pdf"
                    return await render_release(page, self.browser)
            raise last_error or CollectionError("PDF_DOWNLOAD_FAILED", "No accessible full official release content")
        finally:
            if page is not None:
                await page.close()
