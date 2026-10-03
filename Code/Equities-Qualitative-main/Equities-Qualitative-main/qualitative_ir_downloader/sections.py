"""Layered, scored section discovery inside the validated IR host."""
import logging
from .browser import Browser, links
from .models import Link
from .scoring import section_score, company_tokens, DOMAIN
from urllib.parse import urlsplit
from .urls import Scope, canonicalize_url, public_url, rejected_domain
from .extraction import is_pdf, release_link

log = logging.getLogger(__name__)

async def find_sections(page, base_url: str, category: str, browser: Browser, scope: Scope | None = None, company_name: str = "") -> list[str]:
    scope = scope or Scope([base_url])
    await browser.goto(page, base_url)
    found: dict[str, int] = {}
    queue = [base_url]
    visited = set()
    while queue and len(visited) < browser.config.max_section_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if canonicalize_url(page.url) != url:
            await browser.goto(page, url)
        if not scope.allows(page.url):
            log.warning("Section redirected out of scope: %s", page.url)
            continue
        # Expose accessible navigation menus without following unrelated destinations.
        for name in ("Investors", "Investor Relations", "Financial Information", "News & Events", "Menu"):
            control = page.get_by_role("button", name=name, exact=True)
            if await control.count() and await control.first.is_visible():
                try:
                    await control.first.click(timeout=1500)
                    await browser.settle(page)
                except Exception as exc:
                    log.debug("Menu expansion failed: %s", exc)
        heading = " ".join(await page.locator("h1,h2").all_text_contents())
        own = section_score(Link(canonicalize_url(page.url), await page.title(), heading), category)
        inline_reports = category == "reports" and any(k in heading.lower() for k in ("featured reports", "featured documents", "investor materials", "annual reports"))
        if (own.score >= 14 or inline_reports) and not release_link(Link(page.url, await page.title())):
            found[canonicalize_url(page.url)] = own.score
        for link in await links(page):
            host = urlsplit(link.url).hostname or ""
            if not scope.allows(link.url) and public_url(link.url) and not rejected_domain(link.url):
                if section_score(link, category).score >= 12 and (DOMAIN(link.url).domain.lower().replace("-", "") in company_tokens(company_name)):
                    scope.hosts.add(host)
                    log.info("IR-linked related section host accepted: %s from %s", host, page.url)
            if not scope.allows(link.url) or is_pdf(link) or release_link(link):
                continue
            candidate = section_score(link, category)
            if candidate.score >= 12:
                log.info("%s section candidate URL=%s anchor=%r score=%s reasons=%s", category, link.url, link.text, candidate.score, candidate.reasons)
                found[link.url] = candidate.score
            if candidate.score >= 7 or any(k in link.text.lower() for k in ("investor", "financial", "news", "resources")):
                if link.url not in visited and link.url not in queue:
                    queue.append(link.url)
        # Navigation inspection already yielded direct section candidates.
        if found:
            break
    return sorted(found, key=lambda u: found[u], reverse=True)
