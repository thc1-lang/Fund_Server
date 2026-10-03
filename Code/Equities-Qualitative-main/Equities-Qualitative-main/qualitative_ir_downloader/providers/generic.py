"""Generic public sitemap fallback, restricted to verified IR ecosystems."""
from collections import deque
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
import logging
from ..network import ResourceClient
from ..models import ArchiveResult, Document, Issue
from ..urls import canonicalize_url

log = logging.getLogger(__name__)

class GenericAdapter:
    name = "generic"
    def __init__(self, context):
        self.env = context
        self.evidence = []
        self.diagnostics = []
        self._sitemaps = None

    async def fetch(self, url):
        if not self.env.scope.allows(url):
            return None
        if url in self.env.cache:
            return self.env.cache[url]
        try:
            response = await ResourceClient(self.env.browser, self.env.context).get(url, self.env.roots[0])
            if not self.env.scope.allows(response.url):
                return None
            self.diagnostics.append({"url":url,"status":response.status,"content_type":response.headers.get("content-type"),"size":len(response.body)})
        except Exception as exc:
            self.diagnostics.append({"url":url,"error":str(exc)})
            log.warning("Public provider resource unavailable %s: %s", url, exc)
            response = None
        self.env.cache[url] = response
        return response

    async def sitemap_urls(self):
        if self._sitemaps is not None:
            return self._sitemaps
        queue = deque(urljoin(root,path) for root in self.env.roots for path in ("sitemap.xml","sitemap_index.xml"))
        seen, relevant = set(), set()
        while queue and len(seen) < self.env.browser.config.max_archive_pages:
            url = queue.popleft()
            if url in seen: continue
            seen.add(url)
            response = await self.fetch(url)
            if not response: continue
            try:
                if b"<!DOCTYPE" in response.body.upper() or b"<!ENTITY" in response.body.upper(): continue
                tree = ET.fromstring(response.body)
                index = tree.tag.split("}")[-1] == "sitemapindex"
                for node in tree.iter():
                    if node.tag.split("}")[-1] != "loc" or not node.text: continue
                    target = canonicalize_url(node.text, url)
                    if not self.env.scope.allows(target): continue
                    if index:
                        if target not in seen: queue.append(target)
                    elif "news-release-details/" in target or any(x in target for x in ("/annual-reports","/financials","/reports","/sustainability")):
                        relevant.add(target)
            except ET.ParseError:
                log.warning("Public sitemap is not valid XML: %s", url)
        self._sitemaps = relevant
        return relevant

    async def discover_news(self):
        docs = [Document(u.rsplit("/",1)[-1],None,u,"news",discovery_method="official_ir_sitemap",official_ir_url=u) for u in await self.sitemap_urls() if "news-release-details/" in u]
        return ArchiveResult(docs, coverage="unknown", diagnostics={"resources":self.diagnostics})

    async def discover_reports(self):
        return ArchiveResult(coverage="unknown", diagnostics={"resources":self.diagnostics})
