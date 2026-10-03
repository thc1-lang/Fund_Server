"""Headless browser lifecycle, bounded settling, and access-block detection."""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
from .config import Config
from .models import CollectionError, Link, AccessEvent, AccessResult
from .urls import canonicalize_url, public_url
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
BLOCK_TEXT = re.compile(r"verify (?:that )?you are human|access denied|unusual traffic|checking your browser|just a moment|automated access.{0,30}(?:denied|blocked)", re.I)

class Browser:
    def __init__(self, config: Config):
        self.config = config
        self.driver = None
        self.browser = None
        self.blocked_hosts: set[str] = set()  # Browser-confirmed restrictions only.
        self.accessible_hosts: set[str] = set()
        self.access_events: list[AccessEvent] = []
        self.navigation_status: dict[str, int | None] = {}

    async def __aenter__(self):
        self.driver = await async_playwright().start()
        return self

    async def __aexit__(self, *args):
        if self.browser:
            await self.browser.close()
        if self.driver:
            await self.driver.stop()

    @asynccontextmanager
    async def session(self):
        self.blocked_hosts.clear()
        self.accessible_hosts.clear()
        self.access_events.clear()
        self.navigation_status.clear()
        if not self.browser or not self.browser.is_connected():
            self.browser = await self.driver.chromium.launch(headless=True)
        context = await self.browser.new_context(accept_downloads=True, service_workers="block")
        context.set_default_timeout(self.config.navigation_timeout_ms)
        context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        async def guard(route):
            if route.request.url.startswith(("http:", "https:")) and not public_url(route.request.url):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", guard)
        try:
            yield context
        finally:
            try:
                await context.close()
            except Exception:
                log.warning("Browser context cleanup failed", exc_info=True)

    async def goto(self, page: Page, url: str) -> None:
        if not public_url(url):
            raise CollectionError("INVALID_URL", f"Not a public HTTP URL: {url}")
        if urlsplit(url).hostname in self.blocked_hosts:
            raise CollectionError("SITE_BLOCKED", f"Previously blocked host: {url}")
        for attempt in range(self.config.retries):
            try:
                await asyncio.sleep(self.config.request_delay)
                response = await page.goto(url, wait_until="domcontentloaded")
                status = response.status if response else None
                self.navigation_status[url] = status
                self.access_events.append(AccessEvent(page.url, "browser", status, status in (401, 403, 429)))
                if response and response.status in (401, 403, 429):
                    self.blocked_hosts.add(urlsplit(page.url).hostname)
                    raise CollectionError("SITE_BLOCKED", f"HTTP {response.status}: {url}")
                if response and response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {url}")
                await self.settle(page)
                await self.check_access(page)
                await self.dismiss_cookies(page)
                self.accessible_hosts.add(urlsplit(page.url).hostname)
                return
            except CollectionError:
                raise
            except Exception as exc:
                if attempt + 1 == self.config.retries:
                    code = "TIMEOUT" if isinstance(exc, PlaywrightTimeout) else "NAVIGATION_FAILED"
                    raise CollectionError(code, str(exc)) from exc
                log.warning("Navigation retry %s: %s (%s)", attempt + 1, url, exc)
                await asyncio.sleep(2 ** attempt)

    async def assess(self, page: Page, url: str) -> AccessResult:
        """Record both transports without making any ownership judgment."""
        from .network import ResourceClient
        result = AccessResult()
        self.last_assessed_public_html = None
        try:
            response = await ResourceClient(self, page.context).http(url, max_bytes=8 * 1024 * 1024)
            result.http_status = response.status
            result.http_accessible = 200 <= response.status < 300
            if result.http_accessible and "html" in response.headers.get("content-type", ""):
                self.last_assessed_public_html = response
        except Exception as exc:
            result.error = f"HTTP: {exc}"
        log.info("Basic HTTP access: %s; trying normal Playwright navigation: %s", result.http_status, url)
        try:
            await self.goto(page, url)
            result.browser_accessible = True
        except CollectionError as exc:
            result.blocked = exc.code == "SITE_BLOCKED"
            result.error = f"{result.error + '; ' if result.error else ''}Browser: {exc}"
        result.browser_status = self.navigation_status.get(url)
        result.final_url = page.url
        try:
            result.page_title = await page.title()
        except Exception:
            result.page_title = None
        log.info("Playwright access: %s status=%s url=%s", "SUCCESS" if result.browser_accessible else "BLOCKED" if result.blocked else "FAILED", result.browser_status, url)
        return result

    async def settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(5000, self.config.navigation_timeout_ms))
        except PlaywrightTimeout:
            log.debug("Network remained active; using DOM stabilization")
        previous = None
        for _ in range(5):
            signature = await page.evaluate("() => [document.body?.innerText.length, document.querySelectorAll('a').length].join(':')")
            if signature == previous:
                break
            previous = signature
            await page.wait_for_timeout(400)

    async def check_access(self, page: Page) -> None:
        title = await page.title()
        body = (await page.locator("body").inner_text())[:12000]
        if BLOCK_TEXT.search(title) or BLOCK_TEXT.search(body[:1500]):
            self.blocked_hosts.add(urlsplit(page.url).hostname)
            self.access_events.append(AccessEvent(page.url, "browser", None, True, "Access challenge"))
            raise CollectionError("SITE_BLOCKED", f"Access challenge at {page.url}")

    async def dismiss_cookies(self, page: Page) -> None:
        for name in ("Reject all", "Reject optional cookies", "Only necessary", "Accept all cookies", "Accept All"):
            control = page.get_by_role("button", name=name, exact=True)
            if await control.count() and await control.first.is_visible():
                try:
                    await control.first.click(timeout=2000)
                    return
                except Exception as exc:
                    log.debug("Cookie dismissal failed: %s", exc)

async def links(page: Page, content_only: bool = False) -> list[Link]:
    records = await page.locator("a[href]").evaluate_all("""(els, contentOnly) => els.filter(e => !contentOnly || (!e.closest('nav, header, footer, [role=navigation]') || e.closest('[aria-label*=agination], [class*=pagination], [class*=pager]'))).map(e => ({
        url:e.href, text:(e.innerText || e.getAttribute('aria-label') || e.title || '').trim(),
        context:(e.closest('article, li, tr, .field-content, .module_item, .views-row, [class*=release-item], [class*=news-item], [class*=report-item]')?.innerText || '').slice(0,900)
    }))""", content_only)
    result = {}
    for record in records:
        url = canonicalize_url(record["url"], page.url)
        if url:
            link = Link(url, record["text"], record["context"])
            if url not in result or len(link.text) > len(result[url].text):
                result[url] = link
    return list(result.values())
