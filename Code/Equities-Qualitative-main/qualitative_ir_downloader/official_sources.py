"""Evidence-driven official-source fallbacks for difficult IR sites.

The primary IR candidate remains authoritative for identity and event
discovery.  This module only admits alternate routes after the page itself is
reachable and company ownership is independently verified.  It intentionally
does not try to defeat access controls: a 401/403/429 is recorded and the
route is skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
import tldextract

from .extraction import extract_date, is_pdf, release_link
from .ir_evidence import ownership
from .models import CollectionError, Document, IRCandidate, IREcosystem, Link, Stock
from .network import ResourceClient
from .structured import archive_next_urls, merge_documents, report_documents, soup_links
from .urls import Scope, canonicalize_url, public_url

log = logging.getLogger(__name__)
DOMAIN = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

SOURCE_IR_FINANCIAL = "OFFICIAL_IR_FINANCIAL_RESULTS"
SOURCE_NEWSROOM = "OFFICIAL_NEWSROOM"
SOURCE_STATIC = "OFFICIAL_STATIC_DOCUMENT"
SOURCE_SEC = "SEC_EDGAR"
SOURCE_EVENTS = "OFFICIAL_IR_EVENTS"

SEC_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A")
SEC_USER_AGENT = "Allen & Cooper Insider Intelligence nicholaslallen1@gmail.com"


@dataclass(frozen=True)
class OfficialRoute:
    url: str
    family: str
    discovered_from: str | None = None
    issuer_verified: bool = False


@dataclass
class RouteObservation:
    route: OfficialRoute
    status: str
    http_status: int | None = None
    documents: list[Document] | None = None
    error: str | None = None
    evidence: list[str] | None = None

    def record(self) -> dict:
        return {
            "url": self.route.url,
            "source_family": self.route.family,
            "discovered_from": self.route.discovered_from,
            "issuer_verified": self.route.issuer_verified,
            "status": self.status,
            "http_status": self.http_status,
            "documents": len(self.documents or []),
            "error": self.error,
            "evidence": list(self.evidence or []),
        }


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme or "https", parsed.netloc, "/", "", ""))


def _candidate_url(origin: str, path: str) -> str:
    return canonicalize_url(urljoin(origin.rstrip("/") + "/", path.lstrip("/")))


def _same_host_scope(url: str) -> Scope:
    return Scope([url])


class OfficialSourceStrategy:
    """Discover and classify official alternate routes for one issuer.

    ``fetcher`` is injectable for deterministic tests.  It receives a URL and
    referer and returns an object with ``status``, ``headers``, ``body`` and
    ``url`` (the same shape as :class:`ResourceResponse`).
    """

    ROUTE_PATHS = (
        (SOURCE_IR_FINANCIAL, ("financial-info/financial-reports", "financial-info", "financial-results", "financials", "reports", "quarterly-earnings/financial-results", "quarterly-earnings", "investor-relations/financial-results")),
        (SOURCE_NEWSROOM, ("news", "newsroom", "press-releases", "press", "news-events/press-releases", "news-events")),
        (SOURCE_EVENTS, ("events", "events-and-presentations", "presentations")),
    )

    def __init__(
        self,
        browser,
        context,
        stock: Stock,
        ecosystem: IREcosystem,
        winner: IRCandidate | None,
        *,
        fetcher=None,
        sec_fetcher=None,
        cache_root: Path | None = None,
        issuer_cik: str | None = None,
    ):
        self.browser = browser
        self.context = context
        self.stock = stock
        self.ecosystem = ecosystem
        self.winner = winner
        self.fetcher = fetcher
        self.sec_fetcher = sec_fetcher
        self.cache_root = Path(cache_root or browser.config.download_root)
        self.issuer_cik = str(issuer_cik or "").zfill(10) if issuer_cik else None
        self.observations: list[RouteObservation] = []
        self.documents: list[Document] = []
        self.statuses: dict[str, str] = {}

    @property
    def primary_url(self) -> str | None:
        return self.winner.url if self.winner else None

    def routes(self) -> list[OfficialRoute]:
        """Build bounded, generic routes from verified identity evidence."""
        routes: list[OfficialRoute] = []
        primary = _origin(self.winner.url) if self.winner else None
        corporate = self.ecosystem.official_corporate_domain
        origins = []
        if primary:
            origins.append((primary, self.winner.url))
        if corporate:
            corporate_origin = f"https://{corporate}/"
            origins.append((corporate_origin, self.ecosystem.official_corporate_url or corporate_origin))
            extracted = DOMAIN(corporate)
            base = f"{extracted.domain}.{extracted.suffix}"
            for subdomain in ("investor", "investors", "ir"):
                origins.append((f"https://{subdomain}.{base}/", self.ecosystem.official_corporate_url or corporate_origin))
        seen: set[str] = set()
        for origin, discovered_from in origins:
            for family, paths in self.ROUTE_PATHS:
                for path in paths:
                    url = _candidate_url(origin, path)
                    if url and url not in seen:
                        seen.add(url)
                        routes.append(OfficialRoute(url, family, discovered_from, False))

        # A separate newsroom is admitted only after its response proves the
        # issuer identity.  These are conventional probes, not trusted URLs.
        if corporate:
            extracted = DOMAIN(corporate)
            base = f"{extracted.domain}.{extracted.suffix}"
            # Keep conventional newsroom subdomains under the verified
            # corporate registrable domain.  A look-alike registrable domain
            # such as ``nvidianews.com`` is never admitted by name alone.
            for host in (f"news.{base}", f"{extracted.domain}news.{base}"):
                url = f"https://{host}/"
                if url not in seen:
                    seen.add(url)
                    routes.append(OfficialRoute(url, SOURCE_NEWSROOM, self.ecosystem.official_corporate_url or f"https://{base}/", False))
        return routes

    async def _fetch(self, url: str, referer: str) -> object:
        if self.fetcher is not None:
            return await self.fetcher(url, referer)
        if not getattr(self.browser, "driver", None):
            raise CollectionError("OFFLINE_FIXTURE", "Official-source transport unavailable in fixture browser")
        # Use the shared bounded client so a legitimate browser session can
        # provide cookies when a linked public resource needs them.  It still
        # records 401/403/429 as blocked and never bypasses the challenge.
        return await ResourceClient(self.browser, self.context).get(url, referer)

    def _verify(self, route: OfficialRoute, response) -> tuple[bool, list[str], str, str]:
        body = bytes(getattr(response, "body", b""))
        content_type = str((getattr(response, "headers", {}) or {}).get("content-type", "")).lower()
        if "html" not in content_type and not body.lstrip().startswith((b"<!", b"<html", b"<HTML")):
            return False, [], "", ""
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = soup.get_text(" ", strip=True)
        corporate = self.ecosystem.official_corporate_domain
        score, evidence = ownership(
            getattr(response, "url", route.url),
            self.stock.company_name,
            self.stock.ticker,
            title=title,
            body=text,
            corporate_domain=corporate,
        )
        host = urlsplit(getattr(response, "url", route.url)).hostname or ""
        primary_host = urlsplit(self.primary_url or "").hostname or ""
        # A path on the already verified IR host inherits the primary
        # ownership relationship, while still requiring a successful page.
        inherited = bool(primary_host and host == primary_host)
        return score >= 90 or inherited, evidence + (["Verified primary IR host"] if inherited else []), title, text

    @staticmethod
    def _doc(
        *,
        link: Link,
        category: str,
        family: str,
        page_url: str,
        company: Stock,
        title: str | None = None,
        date: str | None = None,
    ) -> Document:
        source_url = canonicalize_url(link.url)
        static = family == SOURCE_STATIC
        record = Document(
            title=(title or link.text or source_url.rsplit("/", 1)[-1] or company.ticker).strip(),
            date=date or extract_date(link.context) or extract_date(link.text),
            source_url=source_url,
            category=category,
            pdf_source_url=source_url if is_pdf(link) else None,
            linked_from_url=page_url,
            discovery_method=family.lower(),
            official_ir_url=page_url,
            source_family=family,
            discovered_from=page_url,
            artifact_url=source_url,
            issuer_verified=True,
            provenance=[
                {
                    "source_family": family,
                    "discovered_from": page_url,
                    "artifact_url": source_url,
                    "issuer_verified": "true",
                }
            ],
        )
        if static:
            record.discovery_method = "official_static_document"
        return record

    def _extract(self, route: OfficialRoute, response) -> list[Document]:
        url = canonicalize_url(getattr(response, "url", route.url))
        body = bytes(getattr(response, "body", b""))
        content_type = str((getattr(response, "headers", {}) or {}).get("content-type", "")).lower()
        if "pdf" in content_type or body.lstrip().startswith(b"%PDF-"):
            link = Link(url, url.rsplit("/", 1)[-1], "")
            return [self._doc(link=link, category="reports", family=route.family, page_url=url, company=self.stock)]
        soup = BeautifulSoup(body, "html.parser")
        links = list(soup_links(soup, url))
        docs: list[Document] = []
        # Report pages can expose both reports and release links; keep each
        # family explicit so downstream coverage can describe the limitation.
        if route.family == SOURCE_IR_FINANCIAL:
            docs.extend(report_documents(soup, url))
            # Investor-relations result blocks frequently mix PDF links with
            # HTML transcript/release pages and SEC/XBRL references.  Retain
            # only links whose labels identify an evidence artifact; generic
            # navigation and webcast players remain event inputs.
            artifact_pattern = re.compile(
                r"earnings\s+release|results\s+release|audio\s+transcript|\btranscript\b|"
                r"presentation|\b10-[kq]\b|\bxbrl\b|investor\s+deck",
                re.I,
            )
            for link in links:
                label = f"{link.text} {link.context}"
                if not artifact_pattern.search(label) or re.search(r"webcast|conference\s+call|listen\s+live", link.text, re.I):
                    continue
                if any(canonicalize_url(existing.source_url) == canonicalize_url(link.url) for existing in docs):
                    continue
                docs.append(self._doc(link=link, category="reports", family=route.family, page_url=url, company=self.stock))
                docs[-1].discovery_method = "official_ir_financial_results"
                docs[-1].provenance.append({"source_family": route.family, "discovered_from": url, "artifact_url": link.url, "artifact_type": "financial_result_artifact", "issuer_verified": "true"})
            for document in docs:
                document.source_family = SOURCE_STATIC if urlsplit(document.source_url).hostname != urlsplit(url).hostname else route.family
                document.discovered_from = url
                document.artifact_url = document.source_url
                document.issuer_verified = True
                document.discovery_method = "official_static_document" if document.source_family == SOURCE_STATIC else "official_ir_financial_results"
                document.provenance.append({"source_family": route.family, "discovered_from": url, "artifact_url": document.source_url, "issuer_verified": "true"})
        for link in links:
            text = (link.text + " " + link.context).lower()
            if is_pdf(link) or "/static-files/" in link.url:
                # A PDF linked by a verified page is allowed even when hosted
                # on a CDN whose domain has no independent company branding.
                family = SOURCE_STATIC if urlsplit(link.url).hostname != urlsplit(url).hostname else route.family
                category = "news" if route.family == SOURCE_NEWSROOM and ("release" in text or "press" in text) else "reports"
                docs.append(self._doc(link=link, category=category, family=family, page_url=url, company=self.stock))
                continue
            if release_link(link):
                link_host = urlsplit(link.url).hostname or ""
                page_host = urlsplit(url).hostname or ""
                corporate = self.ecosystem.official_corporate_domain
                same_official_host = link_host == page_host or bool(corporate and DOMAIN(link.url).top_domain_under_public_suffix == corporate)
                # Newsroom pages often link to interviews and coverage on
                # media sites. Those links are not issuer-authored releases.
                if not same_official_host:
                    continue
                family = route.family
                docs.append(self._doc(link=link, category="news", family=family, page_url=url, company=self.stock))
        return merge_documents(docs)

    async def _paginated_documents(self, route: OfficialRoute, response) -> list[Document]:
        """Collect bounded same-host archive pages without trusting mirrors."""
        if route.family not in {SOURCE_NEWSROOM, SOURCE_IR_FINANCIAL}:
            return []
        first_url = canonicalize_url(getattr(response, "url", route.url))
        first_body = bytes(getattr(response, "body", b""))
        if not first_body.lstrip().startswith((b"<!", b"<html", b"<HTML")):
            return []
        first_soup = BeautifulSoup(first_body, "html.parser")
        queue = list(archive_next_urls(first_soup, first_url, "news" if route.family == SOURCE_NEWSROOM else "reports", _same_host_scope(first_url)))
        documents: list[Document] = []
        seen = {first_url}
        while queue and len(seen) < 6:
            next_url = canonicalize_url(queue.pop(0))
            if not next_url or next_url in seen or urlsplit(next_url).hostname != urlsplit(first_url).hostname:
                continue
            seen.add(next_url)
            try:
                page = await asyncio.wait_for(self._fetch(next_url, first_url), timeout=10)
            except Exception:
                continue
            if int(getattr(page, "status", 0) or 0) < 200 or int(getattr(page, "status", 0) or 0) >= 300:
                continue
            verified, _evidence, _title, _body = self._verify(route, page)
            if not verified:
                continue
            documents.extend(self._extract(route, page))
            body = bytes(getattr(page, "body", b""))
            soup = BeautifulSoup(body, "html.parser")
            queue.extend(archive_next_urls(soup, next_url, "news" if route.family == SOURCE_NEWSROOM else "reports", _same_host_scope(first_url)))
        return merge_documents(documents)

    async def _observe_route(self, route: OfficialRoute) -> None:
        try:
            timeout = min(10.0, float(getattr(self.browser.config, "navigation_timeout_ms", 10000)) / 1000.0)
            response = await asyncio.wait_for(
                self._fetch(route.url, route.discovered_from or self.primary_url or route.url),
                timeout=timeout,
            )
            status = int(getattr(response, "status", 0) or 0)
            if not 200 <= status < 300:
                state = "ACCESS_BLOCKED" if status in {401, 403, 429} else "UNUSABLE"
                self.observations.append(RouteObservation(route, state, status, [], f"HTTP {status}"))
                return
            verified, evidence, _title, _body = self._verify(route, response)
            if not verified:
                self.observations.append(RouteObservation(route, "UNVERIFIED", status, [], "Issuer identity not established", evidence))
                return
            documents = self._extract(route, response)
            documents.extend(await self._paginated_documents(route, response))
            documents = merge_documents(documents)
            for document in documents:
                document.official_ir_url = route.url
                document.discovered_from = document.discovered_from or route.url
                document.issuer_verified = True
            self.observations.append(RouteObservation(route, "SUCCESS" if documents else "ACCESSIBLE_EMPTY", status, documents, evidence=evidence))
            self.documents = merge_documents(self.documents + documents)
            self.ecosystem.relationships.append({"source_url": route.discovered_from or route.url, "target_url": route.url, "relation": f"Verified official source route: {route.family}"})
            if route.family in {SOURCE_IR_FINANCIAL, SOURCE_EVENTS} and route.url not in self.ecosystem.verified_ir_urls:
                self.ecosystem.verified_ir_urls.append(route.url)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            state = "ACCESS_BLOCKED" if code in {"SITE_BLOCKED", "ACCESS_BLOCKED"} else "UNUSABLE"
            self.observations.append(RouteObservation(route, state, None, [], str(exc)))

    async def _sec_json(self, url: str) -> dict:
        if self.sec_fetcher is not None:
            return await self.sec_fetcher(url)
        # Lightweight fixture browsers intentionally have no Playwright
        # driver.  Avoid an accidental live SEC request in offline tests.
        if not getattr(self.browser, "driver", None):
            raise RuntimeError("SEC transport unavailable")
        cache = self.cache_root / ".official_sources" / self.stock.ticker.upper() / ("company_tickers.json" if "company_tickers" in url else "submissions.json")
        if cache.exists() and not getattr(self.browser.config, "revalidate_ir", False):
            try:
                value = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except (OSError, ValueError, TypeError):
                pass

        def load() -> dict:
            request = Request(url, headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        value = await asyncio.to_thread(load)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass
        return value

    async def discover_sec(self) -> None:
        """Discover issuer filings using SEC machine-readable endpoints."""
        try:
            if self.issuer_cik:
                cik = self.issuer_cik
            else:
                tickers = await self._sec_json("https://www.sec.gov/files/company_tickers.json")
                values = tickers.values() if isinstance(tickers, dict) else tickers
                match = next((item for item in values if str(item.get("ticker", "")).upper() == self.stock.ticker.upper()), None)
                if not match:
                    self.statuses[SOURCE_SEC] = "UNUSABLE"
                    return
                cik = str(match.get("cik_str") or match.get("cik")).zfill(10)
            submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            payload = await self._sec_json(submissions_url)
            recent = (payload.get("filings") or {}).get("recent") or {}
            length = len(recent.get("accessionNumber", []))
            docs: list[Document] = []
            for index in range(length):
                form = str(recent.get("form", [""] * length)[index]).upper()
                if not any(form == wanted or form.startswith(wanted + "/") for wanted in SEC_FORMS):
                    continue
                accession = str(recent.get("accessionNumber", [""] * length)[index])
                primary = str(recent.get("primaryDocument", [""] * length)[index] or "")
                filing_date = recent.get("filingDate", [None] * length)[index]
                report_date = recent.get("reportDate", [None] * length)[index]
                archive = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary or accession + '.txt'}"
                link = Link(archive, f"{form} {report_date or filing_date}", f"SEC filing {form} filed {filing_date}")
                doc = self._doc(link=link, category="reports", family=SOURCE_SEC, page_url=submissions_url, company=self.stock, title=f"{self.stock.ticker} {form} {report_date or filing_date}", date=filing_date)
                doc.provenance[0].update({"issuer_cik": cik, "accession_number": accession, "form_type": form, "filing_date": str(filing_date or ""), "source_url": archive})
                docs.append(doc)
                # Keep the live fallback bounded; the separate insider
                # component owns the complete Form 3/4/5 history.
                if len(docs) >= 150:
                    break
            self.documents = merge_documents(self.documents + docs)
            self.statuses[SOURCE_SEC] = "SUCCESS" if docs else "ACCESSIBLE_EMPTY"
            if docs:
                self.observations.append(RouteObservation(OfficialRoute(submissions_url, SOURCE_SEC, "https://www.sec.gov/files/company_tickers.json", True), "SUCCESS", 200, docs, evidence=["SEC ticker and submissions endpoints", f"CIK {cik}"]))
        except Exception as exc:
            self.statuses[SOURCE_SEC] = "ACCESS_BLOCKED" if "403" in str(exc) or "forbidden" in str(exc).lower() else "UNUSABLE"
            self.observations.append(RouteObservation(OfficialRoute("https://data.sec.gov/submissions/", SOURCE_SEC, None, True), self.statuses[SOURCE_SEC], None, [], str(exc)))

    async def discover(self) -> dict:
        routes = self.routes()
        # Keep route probing sequential and bounded, matching the rest of the
        # acquisition layer's politeness and retry model.
        for route in routes:
            await self._observe_route(route)
        await self.discover_sec()
        for observation in self.observations:
            self.statuses.setdefault(observation.route.family, observation.status)
            if observation.status == "SUCCESS":
                self.statuses[observation.route.family] = "SUCCESS"
            elif observation.status == "ACCESS_BLOCKED" and self.statuses.get(observation.route.family) not in {"SUCCESS"}:
                self.statuses[observation.route.family] = "ACCESS_BLOCKED"
        primary_blocked = bool(self.winner and self.winner.access.blocked and not (self.winner.access.browser_accessible or self.winner.access.http_accessible))
        limitations = ["PRIMARY_IR_BLOCKED"] if primary_blocked else []
        families = sorted({d.source_family for d in self.documents if d.source_family})
        coverage_status = "ALL_OFFICIAL_SOURCES_UNUSABLE" if not self.documents else "PRIMARY_IR_BLOCKED" if primary_blocked else "OFFICIAL_SOURCES_USABLE"
        if self.winner is None and self.documents:
            limitations.append("IR_DISCOVERY_FAILED")
        if coverage_status == "ALL_OFFICIAL_SOURCES_UNUSABLE" and "ALL_OFFICIAL_SOURCES_UNUSABLE" not in limitations:
            limitations.append("ALL_OFFICIAL_SOURCES_UNUSABLE")
        return {
            "documents": self.documents,
            "documents_by_category": {
                "news": [d for d in self.documents if d.category == "news"],
                "reports": [d for d in self.documents if d.category == "reports"],
            },
            "routes": [observation.record() for observation in self.observations],
            "statuses": self.statuses,
            "limitations": limitations,
            "coverage_status": coverage_status,
            "evidence_families": families,
            "usable": bool(self.documents),
        }


__all__ = [
    "OfficialRoute",
    "OfficialSourceStrategy",
    "RouteObservation",
    "SEC_FORMS",
    "SOURCE_EVENTS",
    "SOURCE_IR_FINANCIAL",
    "SOURCE_NEWSROOM",
    "SOURCE_SEC",
    "SOURCE_STATIC",
]
