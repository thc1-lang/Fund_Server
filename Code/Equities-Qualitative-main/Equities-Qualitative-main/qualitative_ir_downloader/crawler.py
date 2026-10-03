"""Targeted archive traversal with bounded dynamic-state exploration."""
import asyncio
import logging
import re
from collections import deque
from urllib.parse import urlsplit
from .browser import Browser, links
from .extraction import extract_date, is_pdf, release_link
from .models import ArchiveResult, Document, Issue
from .scoring import section_score
from .urls import Scope, canonicalize_url
from .rss import discover_rss

log = logging.getLogger(__name__)
PAGING = re.compile(r"^(?:next(?: page)?|older(?: (?:releases|news|posts))?|[>\u00bb\u203a]+|page \d+|\d{1,4}|view all(?: news| reports| releases)?|see all|(?:news |reports? |press release )?archive)$", re.I)
MORE = re.compile(r"^(?:load|show|view) more(?: .*?)?$|^more (?:releases|reports)$|^older (?:releases|news)$|^next(?: page)?$|^[>\u00bb\u203a]+$", re.I)
PROVIDERS = ("businesswire.com", "globenewswire.com", "prnewswire.com")

class ArchiveCrawler:
    def __init__(self, browser: Browser, scope: Scope):
        self.browser = browser
        self.scope = scope

    async def collect(self, context, seeds: list[str], category: str, remaining_documents: int | None = None, extractor=None) -> ArchiveResult:
        result = ArchiveResult()
        document_limit = self.browser.config.max_links_per_company if remaining_documents is None else max(0, remaining_documents)
        queue = deque(dict.fromkeys(seeds))
        seen_pages, seen_docs = set(), set()
        interactions = 0
        page = await context.new_page()

        def issue(code, message, url=None):
            log.warning("%s %s %s", code, message, url or "")
            result.errors.append(Issue(code, message, url))

        async def harvest():
            nonlocal queue
            if not self.scope.allows(page.url):
                raise RuntimeError(f"Archive navigation left allowed hosts: {page.url}")
            if category == "news":
                for feed in await discover_rss(page):
                    if feed not in result.rss_feeds:
                        result.rss_feeds.append(feed)
            if extractor:
                for item in extractor(await page.content(), page.url):
                    if item.source_url not in seen_docs:
                        if len(seen_docs) >= document_limit:
                            issue("SAFETY_LIMIT_REACHED", "Event link limit", page.url)
                            return False
                        seen_docs.add(item.source_url)
                        result.documents.append(item)
                for link in await links(page, content_only=True):
                    if self.scope.allows(link.url) and link.url not in seen_pages and link.url not in queue and (PAGING.fullmatch(link.text.strip()) or re.search(r"past events|archived events|previous events|view all events", link.text, re.I)):
                        queue.append(link.url)
                return True
            heading = " ".join(await page.locator("h1").all_text_contents()).lower()
            generic_landing = category == "reports" and not re.search(r"report|financial|resource|result|presentation", heading + " " + urlsplit(page.url).path, re.I)
            for link in await links(page, content_only=True):
                asset = is_pdf(link)
                host = urlsplit(link.url).hostname or ""
                syndicated = any(host == h or host.endswith("." + h) for h in PROVIDERS)
                if category == "news":
                    document = (release_link(link) or asset) and (self.scope.allows(link.url) or asset or syndicated)
                    if asset and re.search(r"annual report|presentation|sustainability|privacy", link.text, re.I):
                        document = False
                else:
                    # Extensionless CDN download/viewer links explicitly labelled as reports.
                    if not self.scope.allows(link.url) and re.search(r"annual report|quarterly report|presentation|financial report|sustainability report|esg report", link.text, re.I):
                        asset = True
                    document = asset
                    if generic_landing and not re.search(r"report|presentation|financial|investor material", link.text + " " + link.context + " " + link.url, re.I):
                        document = False
                if document:
                    if link.url not in seen_docs:
                        if len(seen_docs) >= document_limit:
                            issue("SAFETY_LIMIT_REACHED", "Document link limit", page.url)
                            return False
                        seen_docs.add(link.url)
                        title = link.text or link.url.rsplit("/", 1)[-1]
                        if len(title) < 12 and link.context:
                            title = link.context.replace("\n", " ")[:220]
                        date = extract_date(link.context) or extract_date(link.text)
                        if not date and category == "reports":
                            year = re.search(r"\b(?:19|20)\d{2}\b", link.text + " " + link.context)
                            date = year.group() if year else None
                        result.documents.append(Document(title, date, link.url, category, link.url if asset else None, linked_from_url=page.url))
                    continue
                if self.scope.allows(link.url) and link.url not in seen_pages and link.url not in queue:
                    score = section_score(link, category).score
                    if score >= 12 or PAGING.fullmatch(link.text.strip()):
                        # Do not enqueue unrelated root navigation simply because its label is a year.
                        queue.append(link.url)
            return True

        async def expand():
            nonlocal interactions
            signatures = set()
            while True:
                if not await harvest():
                    return
                signature = tuple(sorted(l.url for l in await links(page)))
                if signature in signatures:
                    return
                signatures.add(signature)
                controls = page.locator("button, a[role=button], a[href='#'], a:not([href]), input[type=button], input[type=submit]")
                clicked = False
                for index in range(await controls.count()):
                    control = controls.nth(index)
                    label = (await control.inner_text() or await control.get_attribute("value") or await control.get_attribute("aria-label") or "").strip()
                    if MORE.fullmatch(label) and await control.is_visible() and await control.is_enabled() and await control.get_attribute("aria-disabled") != "true":
                        if interactions >= self.browser.config.max_interactions:
                            issue("SAFETY_LIMIT_REACHED", "Dynamic interaction limit", page.url)
                            return
                        interactions += 1
                        before = await page.locator("body").inner_text()
                        await asyncio.sleep(self.browser.config.request_delay)
                        await control.click(timeout=5000)
                        await self.browser.settle(page)
                        await self.browser.check_access(page)
                        after = await page.locator("body").inner_text()
                        if before == after:
                            issue("ARCHIVE_STALLED", f"Control {label!r} produced no content change", page.url)
                            return
                        clicked = True
                        break
                if not clicked:
                    return

        async def expand_panels(url):
            nonlocal interactions
            controls = page.locator("button, [role=tab], summary, [role=option]")
            labels = []
            for index in range(await controls.count()):
                c = controls.nth(index)
                label = (await c.inner_text()).strip()
                expanded = await c.get_attribute("aria-expanded")
                if re.fullmatch(r"(?:19|20)\d{2}", label) or await c.get_attribute("role") == "tab" or expanded == "false" or await c.evaluate("e => e.tagName === 'SUMMARY'"):
                    if label and len(label) < 100 and not re.search(r"cookie|menu|search|subscribe", label, re.I):
                        labels.append(label)
            for label in dict.fromkeys(labels):
                if interactions >= self.browser.config.max_interactions:
                    issue("SAFETY_LIMIT_REACHED", "Tab/accordion limit", url)
                    break
                try:
                    # Keep previously expanded accordions; reload only if navigation changed.
                    if not self.scope.allows(page.url):
                        raise RuntimeError("Panel navigation left IR scope")
                    control = page.locator("button, [role=tab], summary, [role=option]").filter(has_text=re.compile("^" + re.escape(label) + "$")).first
                    if await control.count() and await control.is_visible():
                        interactions += 1
                        await asyncio.sleep(self.browser.config.request_delay)
                        await control.click(timeout=3000)
                        await self.browser.settle(page)
                        await self.browser.check_access(page)
                        await expand()
                except Exception as exc:
                    issue(getattr(exc, "code", "ARCHIVE_CONTROL_FAILED"), str(exc), url)

        try:
            while queue:
                if len(seen_pages) >= self.browser.config.max_archive_pages:
                    issue("SAFETY_LIMIT_REACHED", "Archive page limit")
                    break
                if len(seen_docs) >= document_limit or interactions >= self.browser.config.max_interactions:
                    issue("SAFETY_LIMIT_REACHED", "Archive document/interaction limit")
                    break
                url = queue.popleft()
                if url in seen_pages:
                    continue
                seen_pages.add(url)
                result.pages += 1
                try:
                    await self.browser.goto(page, url)
                    if not self.scope.allows(page.url):
                        issue("OUT_OF_SCOPE_REDIRECT", "Archive redirect left IR ecosystem", page.url)
                        continue
                    log.info("Traversing %s archive %s", category, page.url)
                    # Capture filter definitions before load-more mutates the DOM.
                    selects = await page.locator("select").evaluate_all("""els => els.map((e,i) => ({
                        index:i, options:[...e.options].filter(o => /^(19|20)\\d{2}$/.test(o.textContent.trim())).map(o => ({value:o.value,label:o.textContent.trim()}))
                    })).filter(e => e.options.length)""")
                    variants = [(s["index"], o["value"]) for s in selects for o in s["options"]]
                    await expand()
                    await expand_panels(url)
                    for index, value in variants:
                        if interactions >= self.browser.config.max_interactions:
                            issue("SAFETY_LIMIT_REACHED", "Year selection limit", url)
                            break
                        try:
                            await self.browser.goto(page, url)
                            interactions += 1
                            await page.locator("select").nth(index).select_option(value)
                            # Support forms with an explicit year-filter submit.
                            for label in ("Apply", "Filter", "Go", "Search"):
                                button = page.get_by_role("button", name=label, exact=True)
                                if await button.count() and await button.first.is_visible():
                                    await button.first.click(timeout=2000)
                                    break
                            await self.browser.settle(page)
                            await self.browser.check_access(page)
                            await expand()
                            await expand_panels(url)
                        except Exception as exc:
                            issue(getattr(exc, "code", "ARCHIVE_CONTROL_FAILED"), str(exc), url)
                except Exception as exc:
                    issue(getattr(exc, "code", "ARCHIVE_PAGE_FAILED"), str(exc), url)
            if not result.documents:
                issue("NO_DOCUMENTS_FOUND", f"No {category} documents recognized in selected archive")
            log.info("Found %s unique %s documents across %s archive pages", len(result.documents), category, result.pages)
            return result
        finally:
            await page.close()
