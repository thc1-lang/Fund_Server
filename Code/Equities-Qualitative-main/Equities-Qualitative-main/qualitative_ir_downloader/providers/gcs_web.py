"""GCS/Notified public RSS and HTML routes; no private APIs or invented aliases."""
from collections import deque
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
import logging
import re
from .generic import GenericAdapter
from ..models import ArchiveResult, Document, Issue
from ..rss import parse_feed
from ..structured import merge_documents, soup_links, page_matches, report_documents, archive_next_urls
from ..extraction import release_link, extract_date
from ..urls import canonicalize_url
from ..scoring import company_tokens

log = logging.getLogger(__name__)

def candidate_urls(roots, paths):
    return list(dict.fromkeys(urljoin(root,path) for root in roots for path in paths))

def news_feed_link(url, label=""):
    text = (url + " " + label).lower()
    return not re.search(r"sec[- /]?filing|events?|presentations?", text) and bool(re.search(r"news|press|release", text))

def archive_state_key(url):
    parsed = urlsplit(url)
    query = [(k,v) for k,v in parse_qsl(parsed.query) if k not in {"form_build_id", "form_token"}]
    return parsed._replace(query=urlencode(sorted(query))).geturl()

def matches_verified_domain(ecosystem):
    verified = {urlsplit(u).hostname for u in ecosystem.verified_ir_urls}
    return any(host in verified and host.endswith(".gcs-web.com") for host in ecosystem.verified_hosted_ir_domains)

class GCSWebAdapter(GenericAdapter):
    name = "GCS-Web"
    async def detect(self):
        if matches_verified_domain(self.env.ecosystem):
            self.evidence.append("Hosted gcs-web.com domain present in verified ecosystem")
            return True
        # Public convention probes on the already verified primary IR host only.
        for url in candidate_urls(self.env.roots, ("rss-subscription-links",)):
            response = await self.fetch(url)
            if not response: continue
            soup = BeautifulSoup(response.body.decode("utf-8","replace"),"html.parser")
            text = soup.get_text(" ",strip=True).lower()
            if all(t in text for t in company_tokens(self.env.stock.company_name)) and "nir-" in str(soup) and any("/rss/news-releases.xml" in link.url for link in soup_links(soup,url)):
                self.evidence = ["Verified corporate-to-IR provenance", "Company-identified NIR/Drupal platform markup", "Explicit news-releases RSS route in public subscription page"]
                log.info("IR provider detected: %s evidence=%s", self.name, self.evidence)
                return True
        return False

    async def discover_news_rss(self):
        queue = deque(candidate_urls(self.env.roots, ("rss/news-releases.xml","rss-subscription-links")))
        seen, docs, feeds = set(), [], []
        while queue and len(seen) < self.env.browser.config.max_archive_pages:
            url = queue.popleft()
            if url in seen: continue
            seen.add(url)
            response = await self.fetch(url)
            if not response: continue
            if "xml" not in response.headers.get("content-type","") and not response.body.lstrip().startswith(b"<?xml"):
                soup = BeautifulSoup(response.body.decode("utf-8","replace"),"html.parser")
                for link in soup_links(soup,url):
                    if self.env.scope.allows(link.url) and ("/rss/" in link.url or "rss" in link.text.lower()) and news_feed_link(link.url, link.text):
                        queue.append(link.url)
                continue
            try:
                entries = parse_feed(response.body,url,self.env.scope)
                docs.extend(entries)
                dates = [d.date for d in entries if d.date]
                feeds.append({"url":url,"status":response.status,"entries":len(entries),"oldest":min(dates) if dates else None,"coverage":"incomplete" if entries else "unknown"})
                tree = ET.fromstring(response.body)
                for node in tree.iter():
                    if node.tag.split("}")[-1] == "link" and node.get("rel") == "next":
                        target=canonicalize_url(node.get("href",""),url)
                        if self.env.scope.allows(target) and news_feed_link(target): queue.append(target)
            except (ValueError,ET.ParseError) as exc:
                self.diagnostics.append({"url":url,"error":str(exc)})
        docs = merge_documents(docs)
        dates=[d.date for d in docs if d.date]
        self.rss_stats={"feeds":feeds,"entries":len(docs),"oldest":min(dates) if dates else None,"coverage":"incomplete" if docs else "unknown"}
        log.info("RSS entries discovered: %s; oldest: %s; RSS archive coverage: %s",len(docs),self.rss_stats["oldest"],self.rss_stats["coverage"])
        return docs

    async def crawl_public_archive(self, seeds, category):
        queue,seen,docs,errors=deque(seeds),set(),[],[]
        valid_pages=0
        while queue and len(seen)<self.env.browser.config.max_archive_pages:
            url=queue.popleft()
            state = archive_state_key(url)
            if state in seen: continue
            seen.add(state)
            response=await self.fetch(url)
            if not response:
                errors.append(Issue("PUBLIC_ARCHIVE_UNAVAILABLE","Public archive page unavailable",url));continue
            soup=BeautifulSoup(response.body.decode("utf-8","replace"),"html.parser")
            if not page_matches(soup,self.env.stock.company_name,category):
                continue
            valid_pages+=1
            if category=="reports":
                docs.extend(report_documents(soup,url))
            else:
                for link in soup_links(soup,url):
                    if self.env.scope.allows(link.url) and release_link(link):
                        docs.append(Document(link.text,extract_date(link.context),link.url,"news",linked_from_url=url,official_ir_url=link.url,discovery_method="official_provider_archive"))
            docs=merge_documents(docs)
            if len(docs)>=self.env.browser.config.max_links_per_company:
                errors.append(Issue("SAFETY_LIMIT_REACHED","Provider document limit",url));break
            for target in archive_next_urls(soup,url,category,self.env.scope):
                if archive_state_key(target) not in seen and all(archive_state_key(q) != archive_state_key(target) for q in queue): queue.append(target)
        if queue: errors.append(Issue("SAFETY_LIMIT_REACHED","Provider archive page limit"))
        coverage="exposed_archive_traversed" if valid_pages and not errors else "incomplete" if docs else "unknown"
        return ArchiveResult(docs,errors,len(seen),coverage=coverage)

    async def discover_news(self):
        rss=await self.discover_news_rss()
        sitemap=await self.sitemap_urls()
        seeds=candidate_urls(self.env.roots,("press-releases",))
        result=await self.crawl_public_archive(seeds,"news")
        mapped=[Document(u.rsplit("/",1)[-1],None,u,"news",official_ir_url=u,discovery_method="official_ir_sitemap") for u in sitemap if "news-release-details/" in u]
        result.documents=merge_documents(result.documents+rss+mapped)
        result.rss_feeds=[f["url"] for f in self.rss_stats["feeds"]]
        result.diagnostics={"rss":self.rss_stats,"resources":list(self.diagnostics)}
        return result

    async def discover_reports(self):
        mapped=[u for u in await self.sitemap_urls() if "news-release-details/" not in u]
        seeds=candidate_urls(self.env.roots,("financials/annual-reports",))
        result=await self.crawl_public_archive(list(dict.fromkeys(seeds+mapped)),"reports")
        result.diagnostics={"resources":list(self.diagnostics)}
        return result
