"""Explicitly linked RSS/Atom feeds supplement (never replace) archive coverage."""
import logging
import re
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree
from .models import Document, Issue
from .urls import canonicalize_url, public_url, rejected_domain
from .network import ResourceClient
from .structured import merge_documents, wire_link
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

async def discover_rss(page) -> list[str]:
    values = await page.locator('link[rel~="alternate"], a[href]').evaluate_all(r"""els => els.filter(e =>
        /application\/(rss|atom)\+xml/i.test(e.type || '') ||
        /\brss\b|news feed|press release feed/i.test(e.innerText || e.getAttribute('aria-label') || '')
    ).map(e => e.href)""")
    feeds = list(dict.fromkeys(canonicalize_url(u, page.url) for u in values))
    feeds = [u for u in feeds if public_url(u) and not rejected_domain(u)]
    for url in feeds:
        log.info("RSS discovered from official page %s: %s (supplemental coverage)", page.url, url)
    return feeds

def parse_feed(data: bytes, feed_url: str, scope) -> list[Document]:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Feed DTD/entities are not supported")
    root = ElementTree.fromstring(data)
    channel_title = root.findtext("./channel/title", "")
    if re.search(r"sec filings?|events?|presentations?", channel_title, re.I) and not re.search(r"news|press releases?", channel_title, re.I):
        return []
    documents = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        fields = {child.tag.split("}")[-1]: child for child in item}
        link = fields.get("link")
        url = canonicalize_url((link.get("href") or link.text or "") if link is not None else "", feed_url)
        if not scope.allows(url) or re.search(r"/(?:sec-filings?|events?|presentations?)(?:/|$)", url, re.I):
            continue
        title_node = fields.get("title")
        title = "".join(title_node.itertext()).strip() if title_node is not None else url
        date = None
        for key in ("pubDate", "published", "updated"):
            value = fields.get(key)
            if value is not None and value.text:
                try:
                    date = parsedate_to_datetime(value.text).date().isoformat()
                except (TypeError, ValueError):
                    match = re.search(r"\d{4}-\d{2}-\d{2}", value.text)
                    date = match.group() if match else None
                if date:
                    break
        def value(name):
            node = fields.get(name)
            return "".join(node.itertext()).strip() if node is not None else None
        body = value("encoded") or value("content")
        description = value("description") or value("summary")
        text = BeautifulSoup(body or "", "html.parser").get_text(" ", strip=True)
        complete = bool(body and len(text) >= 1000 and re.search(r"contacts?|investor relations|media inquiries|source version", text[-1200:], re.I) and not re.search(r"read more|continue reading|\.\.\.$", text[-100:], re.I))
        wire = wire_link(BeautifulSoup(body or description or "", "html.parser"), url)
        documents.append(Document(title, date, url, "news", linked_from_url=feed_url,
            discovery_method="official_ir_rss", official_ir_url=url, feed_url=feed_url,
            guid=value("guid") or value("id"), description=description, content_html=body,
            content_complete=complete, wire_source_url=wire, wire_provenance_url=feed_url if wire else None,
            provenance=[{"discovery_method":"official_ir_rss", "feed_url":feed_url, "release_url":url}]))
    return merge_documents(documents)

async def collect_feeds(browser, context, feeds: list[str], scope, referer: str):
    documents, errors, seen = [], [], set()
    for feed in dict.fromkeys(feeds):
        try:
            response = await ResourceClient(browser, context).get(feed, referer)
            for document in parse_feed(response.body, feed, scope):
                if document.source_url not in seen:
                    if len(documents) >= browser.config.max_links_per_company:
                        errors.append(Issue("SAFETY_LIMIT_REACHED", "RSS document budget", feed))
                        break
                    seen.add(document.source_url)
                    documents.append(document)
        except Exception as exc:
            log.warning("RSS collection failed %s: %s", feed, exc)
            errors.append(Issue(getattr(exc, "code", "RSS_FAILED"), str(exc), feed))
    return documents, errors
