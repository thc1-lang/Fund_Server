"""Corporate-first officiality verification, independent of transport access."""
import base64
import logging
import re
import time
import asyncio
from urllib.parse import quote_plus, parse_qs, urlsplit, urlunsplit
import tldextract
from .browser import Browser, links
from .models import IRCandidate, IREcosystem, Stock, Issue, Link
from .scoring import company_tokens, score_ir
from .urls import canonicalize_url, public_url, rejected_domain
from .rss import discover_rss
from .company_identity import identify,domain_candidates
from .ir_evidence import rank_result,brand_domain,ownership,noise
from .discovery_cache import DiscoveryCache

log = logging.getLogger(__name__)
DOMAIN = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
MAX_SCAN_SEARCH_QUERIES = 4
MAX_SCAN_NETWORK_CANDIDATES = 5
MAX_SCAN_BROWSER_PROBES = 5

INVESTOR_LABEL = re.compile(r"^(?:(?:for|NASDAQ|NYSE|HKEX|SSE|TSX|ASX|LSE)\s+)?(?:investors?(?:\s+(?:relations|information|center|centre|overview))?(?:\s*(?:&|and)\s*(?:news|media))?|shareholders?(?:\s+(?:information|center|centre))?|financial information)$", re.I)

def corporate_domains(company):
    return domain_candidates(company)

def candidate_domain_score(url,company):
    return rank_result(url,company)

def corporate_identity(url: str, title: str, body: str, company: str, search_support: str = '', ticker: str = '') -> bool:
    """Require a branded registrable domain and company identity in title/content."""
    if rejected_domain(url):
        return False
    tokens = company_tokens(company)
    brand = DOMAIN(url).domain.lower().replace("-", "")
    branded = brand_domain(url, company) >= 1
    if not branded:
        return False
    exact = all(t in title.casefold() for t in tokens) and all(t in body.casefold() for t in tokens)
    navigation = sum(bool(re.search(r"\b" + word + r"\b", body, re.I)) for word in ("about", "investors?", "careers", "products?"))
    identity=identify(company)
    supported_brand = (identity.brand_match(title) and identity.brand_match(body) and navigation>=2
        and identity.legal_match(search_support) and bool(re.search(r'investor(?:s| relations)',search_support,re.I)))
    legal_footer = brand_domain(url,company)==2 and identity.legal_match(body) and navigation>=2 and bool(re.search(r'(?:©|copyright).{0,80}'+re.escape(identity.normalized_company_name),body,re.I))
    return exact or (identity.legal_match(body) and identity.brand_match(title) and navigation>=2) or supported_brand or legal_footer

def endorsed_candidate(corporate_url: str, link: Link) -> IRCandidate | None:
    """Direct Investors navigation is ownership evidence even when IR is inaccessible."""
    if not public_url(link.url) or rejected_domain(link.url) or not INVESTOR_LABEL.fullmatch(" ".join(link.text.split())):
        return None
    corporate = DOMAIN(corporate_url).top_domain_under_public_suffix
    host = urlsplit(link.url).hostname or ""
    related = host == corporate or host.endswith("." + corporate)
    evidence = [f"Direct {link.text!r} link from verified corporate page {corporate_url}"]
    evidence.append("IR belongs to corporate registrable domain" if related else "Hosted IR relationship verified by corporate Investors link")
    return IRCandidate(link.url, 100 if related else 95, evidence, True, corporate_url=corporate_url, verified_hosted_ir_domain=None if related else host)

class IRDiscovery:
    def __init__(self, browser: Browser):
        self.browser = browser
        self.candidates: list[IRCandidate] = []
        self.errors: list[Issue] = []
        self.ecosystem = IREcosystem()
        self.rss_feeds: list[str] = []
        self.search_queries=0;self.browser_probes=0;self.corporate_seen=set()
        self.search_evidence={};self.queries=[];self.search_seconds=0.;self.rejected=[];self.seen_result_domains=set()
        self.identity=None;self.ticker='';self.cache=DiscoveryCache(browser.config.download_root)
        self.started=time.perf_counter();self.corporate_candidates=[]
        self.scan_budget=bool(getattr(getattr(browser,"current_company",None),"scan_only",False))

    async def search(self, page, query: str) -> list[str]:
        cached=self.cache.query(query)
        if cached is not None:
            self.search_evidence.update({r['url']:r for r in cached})
            self.queries.append({'query':query,'cached':True,'results':len(cached)})
            return [r['url'] for r in cached]
        if self.scan_budget and self.search_queries>=MAX_SCAN_SEARCH_QUERIES:return []
        self.search_queries+=1;started=time.perf_counter();results=[]
        log.info('Search query: %s',query)
        query_record={'query':query,'cached':False,'results':0,'completed':False};self.queries.append(query_record)
        for engine in ('https://www.bing.com/search?q=','https://html.duckduckgo.com/html/?q='):
            probe_started=time.perf_counter()
            try:
                await asyncio.wait_for(self.browser.goto(page,engine+quote_plus(query)),timeout=12 if self.scan_budget else 30)
                selector='#b_results h2 a' if 'bing.com' in engine else '.result__a'
                records=await page.locator(selector).evaluate_all("els => els.map(e => ({url:e.href,title:e.innerText||'',snippet:(e.closest('.b_algo,.result')?.innerText||'').slice(0,1800)}))")
                for r in records:
                    if isinstance(r,str):r={'url':r,'title':'','snippet':''}
                    item=r['url'];args=parse_qs(urlsplit(item).query);item=args.get('uddg',[item])[0]
                    if 'bing.com/ck/' in item:
                        encoded=args.get('u',[''])[0]
                        if encoded.startswith('a1'):
                            try:item=base64.urlsafe_b64decode(encoded[2:]+'='*(-len(encoded[2:])%4)).decode()
                            except (ValueError,UnicodeDecodeError):continue
                    url=canonicalize_url(item)
                    if public_url(url) and not noise(url):
                        r={**r,'url':url};self.search_evidence[url]=r
                        if not self.identity or rank_result(url,self.identity.raw_company_name,self.ticker,r['title'],r['snippet'])>=0:results.append(r)
                        else:self.rejected.append({'url':url,'score':-100,'reason':'No company relevance in domain/title/snippet'})
                if results:break
                self.errors.append(Issue('SEARCH_RESULTS_IRRELEVANT','No company-relevant results; trying alternate engine/query',engine))
            except Exception as exc:self.errors.append(Issue('SEARCH_FAILED',str(exc),engine))
            finally:self.search_seconds+=time.perf_counter()-probe_started
        results=list({r['url']:r for r in results}.values());self.cache.store_query(query,results)
        domains={DOMAIN(r['url']).top_domain_under_public_suffix for r in results}
        new=domains-self.seen_result_domains;self.seen_result_domains|=domains
        query_record.update(results=len(results),new_domains=sorted(new),completed=True)
        return [r['url'] for r in results]

    @staticmethod
    def relevant_search_results(results,company_name,ticker,evidence=None):
        evidence=evidence or {}
        def score(url):
            r=evidence.get(url,{})
            return rank_result(url,company_name,ticker,r.get('title',''),r.get('snippet',''))
        return sorted((u for u in dict.fromkeys(results) if score(u)>=0),key=score,reverse=True)

    def corporate_search_identity(self,urls,company):
        identity=identify(company)
        for url in sorted(urls,key=lambda u:(DOMAIN(u).suffix=='com',urlsplit(u).path in ('','/')),reverse=True):
            d=DOMAIN(url);r=self.search_evidence.get(url,{})
            title, snippet = r.get('title',''), r.get('snippet','')
            # Search results for investor.resmed.com (and similar official
            # IR hosts) often never expose the corporate homepage.  A
            # dedicated investor subdomain can establish the registrable
            # corporate domain only when its own brand and search evidence
            # agree; an unrelated ``investor.<domain>`` is never accepted.
            dedicated = d.subdomain.lower() in {'ir', 'investor', 'investors'}
            if d.subdomain not in ('','www') and not dedicated:
                continue
            if not brand_domain(url,company):
                continue
            search_identity = identity.brand_match(title) and (
                identity.legal_match(snippet)
                or bool(self.ticker and re.search(r'\b'+re.escape(self.ticker)+r'\b', title+' '+snippet, re.I))
                or bool(re.search(r'investor(?:s| relations)', title, re.I))
            )
            if search_identity:
                self.ecosystem.official_corporate_domain=d.top_domain_under_public_suffix
                self.ecosystem.official_corporate_url='https://'+d.top_domain_under_public_suffix+'/'
                log.info('Corporate identity from exact brand domain plus company-identifying search title/snippet: %s',url)
                return

    def conventional_ir_candidates(self) -> list[IRCandidate]:
        """Return bounded, conventional IR hosts under a verified domain."""
        corporate = self.ecosystem.official_corporate_domain
        if not corporate:
            return []
        extracted = DOMAIN(corporate)
        base = f"{extracted.domain}.{extracted.suffix}"
        corporate_url = self.ecosystem.official_corporate_url or f"https://{base}/"
        candidates = []
        for subdomain in ("investor", "investors", "ir"):
            url = f"https://{subdomain}.{base}/"
            if url in self.ecosystem.verified_ir_urls or any(item.url == url for item in self.candidates):
                continue
            candidates.append(IRCandidate(
                url,
                officiality_score=70,
                evidence=[f"Conventional {subdomain} IR subdomain under verified corporate domain {corporate}"],
                corporate_url=corporate_url,
            ))
        # Some issuers keep IR on the corporate root and expose it only as a
        # conventional path.  These remain untrusted until page-level issuer
        # verification in the normal candidate loop succeeds.
        for path in ("/investors", "/investor-relations", "/investor", "/investors-relations"):
            url = f"https://{base}{path}"
            if url in self.ecosystem.verified_ir_urls or any(item.url == url for item in self.candidates):
                continue
            candidates.append(IRCandidate(
                url,
                officiality_score=55,
                evidence=[f"Conventional IR path {path} under verified corporate domain {corporate}"],
                corporate_url=corporate_url,
            ))
        return candidates

    def accept_public_html(self,candidate,response,company,ticker):
        """Use a successful public response even if the separate browser transport fails."""
        if response is None or response.status!=200:return False
        from bs4 import BeautifulSoup
        from .browser import BLOCK_TEXT
        soup=BeautifulSoup(response.body,'html.parser')
        title=soup.title.get_text(' ',strip=True) if soup.title else ''
        body=soup.get_text(' ',strip=True)
        if BLOCK_TEXT.search(title) or BLOCK_TEXT.search(body[:1500]):return False
        score,reasons=ownership(response.url,company,ticker,title,body,corporate_domain=self.ecosystem.official_corporate_domain)
        candidate.officiality_score=max(candidate.officiality_score,score)
        if score<90:return False
        candidate.official=True;candidate.content_validated=True
        candidate.evidence.extend(['Company identity verified in successful public HTTP HTML']+reasons)
        self.register(candidate,candidate.url,'Public HTTP company/ticker content')
        if self.identity and not brand_domain(response.url,company):
            from dataclasses import replace
            alias=DOMAIN(response.url).domain
            self.identity=replace(self.identity,brand_candidates=tuple(dict.fromkeys(self.identity.brand_candidates+(alias,))))
        return True

    def diagnostics(self):
        accesses=getattr(self.browser,'access_events',[])
        return {**(self.identity.record() if self.identity else {}),'ticker':self.ticker,'queries_attempted':self.queries,
            'search_queries':self.search_queries,'search_seconds':self.search_seconds,
            'http_probes':sum(x.transport=='http' for x in accesses),'browser_probes':self.browser_probes,
            'candidate_domains':sorted({DOMAIN(c.url).top_domain_under_public_suffix for c in self.candidates}|self.seen_result_domains),
            'corporate_candidates':self.corporate_candidates,'ir_candidates':[{'url':c.url,'score':c.officiality_score,'official':c.official,'evidence':c.evidence} for c in self.candidates],
            'rejected_candidates':self.rejected,'best_rejected_candidate':max(self.rejected,key=lambda r:r.get('score',0),default=None),'discovery_seconds':time.perf_counter()-self.started,
            'failure_classification':None if any(c.official for c in self.candidates) else 'IR_DISCOVERY_FAILED'}

    def register(self, candidate: IRCandidate, source: str, relation: str) -> None:
        if not candidate.official:
            return
        if candidate.url not in self.ecosystem.verified_ir_urls:
            self.ecosystem.verified_ir_urls.append(candidate.url)
            self.ecosystem.relationships.append({"source_url": source, "target_url": candidate.url, "relation": relation})
        hosted = candidate.verified_hosted_ir_domain
        if hosted and hosted not in self.ecosystem.verified_hosted_ir_domains:
            self.ecosystem.verified_hosted_ir_domains.append(hosted)

    async def discover_ir_from_corporate_site(self, page, urls: list[str], company: str) -> list[IRCandidate]:
        roots = []
        for url in urls:
            parsed = urlsplit(url)
            domain = DOMAIN(url)
            brand = domain.domain.lower().replace("-", "")
            if rank_result(url,company,self.ticker,**{k:self.search_evidence.get(url,{}).get(k,'') for k in ('title','snippet')})<0:
                continue
            # Only corporate roots, not arbitrary hosted company subdomains.
            host = parsed.hostname or ""
            if domain.subdomain in ("", "www"):
                roots.append(urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")))
            elif domain.subdomain in ("ir", "investor", "investors"):
                roots.append(f"{parsed.scheme}://{domain.top_domain_under_public_suffix}/")
        for url in list(dict.fromkeys(roots))[:2 if self.scan_budget else 5]:
            if url in self.corporate_seen:continue
            self.corporate_seen.add(url)
            if self.scan_budget and self.browser_probes>=MAX_SCAN_BROWSER_PROBES:break
            cached_transport=self.cache.blocked(url)
            if cached_transport and cached_transport.get('failure_type') not in {'TimeoutError','TRANSPORT_FAILED'}:continue
            self.corporate_candidates.append(url)
            try:
                html_page=None
                if self.scan_budget:
                    from .network import ResourceClient
                    from .browser import BLOCK_TEXT
                    from .models import CollectionError
                    from bs4 import BeautifulSoup
                    try:
                        response=await asyncio.wait_for(ResourceClient(self.browser,page.context).http(url,max_bytes=2*1024*1024),timeout=8)
                        if response.status in (401,403,429):
                            raise CollectionError('SITE_BLOCKED',f'HTTP {response.status}: {url}')
                        if response.status==200:
                            parsed=BeautifulSoup(response.body,'html.parser')
                            text=parsed.get_text(' ',strip=True)
                            title=parsed.title.get_text(' ',strip=True) if parsed.title else ''
                            if BLOCK_TEXT.search(title) or BLOCK_TEXT.search(text[:1500]):
                                raise CollectionError('SITE_BLOCKED',f'Public access challenge: {url}')
                            html_page=(canonicalize_url(response.url),title,text,parsed)
                    except CollectionError:raise
                    except Exception:
                        if cached_transport:self.cache.failure(url,'HTTP_TRANSPORT_FAILED')
                if html_page:
                    actual,title,body,soup=html_page
                    from .structured import soup_links
                    corporate_links=soup_links(soup,actual)
                else:
                    if cached_transport:continue
                    self.browser_probes+=1
                    await asyncio.wait_for(self.browser.goto(page,url),timeout=10 if self.scan_budget else 30)
                    actual=canonicalize_url(page.url)
                    title=await page.title();body=await page.locator('body').inner_text()
                    corporate_links=await links(page)
                support=' '.join(r.get('title','')+' '+r.get('snippet','') for u,r in self.search_evidence.items() if DOMAIN(u).top_domain_under_public_suffix==DOMAIN(actual).top_domain_under_public_suffix)
                if not corporate_identity(actual, title, body, company, support, self.ticker):
                    continue
                self.ecosystem.official_corporate_url = actual
                self.ecosystem.official_corporate_domain = DOMAIN(actual).top_domain_under_public_suffix
                log.info("Official corporate domain verified: %s; company identity in title/content and branded registrable domain", self.ecosystem.official_corporate_domain)
                result = []
                for link in corporate_links:
                    candidate = endorsed_candidate(actual, link)
                    if candidate:
                        self.register(candidate, actual, f"Corporate navigation: {link.text}")
                        result.append(candidate)
                        log.info("IR link discovered from official corporate site: %s officiality=%s evidence=%s", candidate.url, candidate.officiality_score, candidate.evidence)
                return result
            except Exception as exc:
                self.cache.failure(url,getattr(exc,"code",type(exc).__name__))
                self.errors.append(Issue(getattr(exc, "code", "CORPORATE_DISCOVERY_FAILED"), str(exc), url))
                log.warning("Corporate candidate failed %s: %s", url, exc)
        return []

    async def discover_investor_relations_site(self, ticker: str, company_name: str, context) -> IRCandidate | None:
        stock = Stock(ticker, company_name, "", 0)
        page = await context.new_page()
        try:
            self.identity=identify(company_name);self.ticker=ticker
            normalized=self.identity.normalized_company_name
            from .company_identity import SECURITY
            legal_query=SECURITY.sub('',company_name).strip(' ,.-')
            variants=[f'"{normalized}" investor relations',f'"{ticker}" "{legal_query}" investor relations',f'"{ticker}" investor relations']
            queue=[];all_results=[]
            for query_index,query in enumerate(variants):
                if query_index==1 and self.ecosystem.official_corporate_domain:
                    query=f'site:{self.ecosystem.official_corporate_domain} investors'
                results=self.relevant_search_results(await self.search(page,query),company_name,ticker,self.search_evidence)
                all_results=list(dict.fromkeys(all_results+results))
                self.corporate_search_identity(all_results,company_name)
                if self.ecosystem.official_corporate_domain:
                    queue.extend(self.conventional_ir_candidates())
                # Known corporate navigation remains the strongest cross-domain endorsement.
                corporate_results=[u for u in results if DOMAIN(u).subdomain in ('','www')]
                queue.extend(await self.discover_ir_from_corporate_site(page,corporate_results,company_name))
                ir_results=[u for u in results if re.search(r'investor|(^|\.)ir\.',urlsplit(u).netloc+urlsplit(u).path,re.I) or re.search(r'investor',self.search_evidence.get(u,{}).get('title',''),re.I)]
                if ir_results and not self.ecosystem.official_corporate_domain:
                    queue.extend(await self.discover_ir_from_corporate_site(page,ir_results,company_name))
                    branded=next((u for u in ir_results if brand_domain(u,company_name)>=1),None)
                    if branded:
                        domain=DOMAIN(branded).top_domain_under_public_suffix
                        supporting=await self.search(page,f'site:{domain} "{normalized}"')
                        self.corporate_search_identity(supporting,company_name)
                queue.extend(IRCandidate(u,evidence=['Search result; evaluating independent ownership evidence']) for u in ir_results)
                if queue:break
            if not queue and not self.ecosystem.official_corporate_domain:
                # Normalized brand guesses remain untrusted until full page identity and navigation verify them.
                guesses=['https://'+d+'/' for d in corporate_domains(company_name)[:2]]
                queue.extend(await self.discover_ir_from_corporate_site(page,guesses,company_name))
            if not queue and self.ecosystem.official_corporate_domain:
                query=f'site:{self.ecosystem.official_corporate_domain} investors'
                queue.extend(IRCandidate(u,evidence=['Site-scoped search result']) for u in self.relevant_search_results(await self.search(page,query),company_name,ticker,self.search_evidence))
            if self.ecosystem.official_corporate_domain:
                queue.extend(self.conventional_ir_candidates())
            seen = set()
            while queue and len(seen) < min(self.browser.config.max_discovery_candidates,MAX_SCAN_NETWORK_CANDIDATES if self.scan_budget else self.browser.config.max_discovery_candidates):
                if self.scan_budget and self.browser_probes>=MAX_SCAN_BROWSER_PROBES:break
                candidate = queue.pop(0)
                if candidate.url in seen or rejected_domain(candidate.url):
                    continue
                seen.add(candidate.url)
                # Independent domain evidence can establish an IR subdomain discovered in search.
                corporate = self.ecosystem.official_corporate_domain
                host = urlsplit(candidate.url).hostname or ""
                if corporate and host.endswith("." + corporate) and host.split(".")[0] in {"ir", "investors", "investor"}:
                    candidate.official = True
                    candidate.officiality_score = max(candidate.officiality_score, 90)
                    candidate.evidence.append(f"IR subdomain of verified corporate domain {corporate}")
                    self.register(candidate, self.ecosystem.official_corporate_url, "Verified corporate-domain relationship")
                r=self.search_evidence.get(candidate.url,{})
                score,evidence=ownership(candidate.url,company_name,ticker,search_title=r.get('title',''),snippet=r.get('snippet',''),corporate_domain=corporate)
                candidate.evidence.extend(evidence)
                candidate.officiality_score=max(candidate.officiality_score,score)
                if score>=90:
                    candidate.official=True;candidate.officiality_score=max(candidate.officiality_score,score)
                    candidate.corporate_url=self.ecosystem.official_corporate_url
                # Preserve ownership evidence before any network access.
                self.candidates.append(candidate)
                if self.cache.blocked(candidate.url):
                    from .models import AccessResult
                    candidate.access=AccessResult(blocked=True,error='Short-lived cached transport failure')
                else:
                    self.browser_probes+=1
                    try:candidate.access=await asyncio.wait_for(self.browser.assess(page,candidate.url),timeout=15 if self.scan_budget else 45)
                    except Exception as exc:
                        from .models import AccessResult
                        candidate.access=AccessResult(error=str(exc))
                    self.accept_public_html(candidate,getattr(self.browser,'last_assessed_public_html',None),company_name,ticker)
                    if not candidate.access.browser_accessible:self.cache.failure(candidate.url,'BLOCKED' if candidate.access.blocked else 'TRANSPORT_FAILED')
                if candidate.access.browser_accessible:
                    final = canonicalize_url(page.url)
                    if rejected_domain(final):
                        candidate.official = False
                        candidate.evidence.append("Rejected aggregator redirect")
                        continue
                    content = await page.locator("body").inner_text()
                    title=await page.title()
                    independent,reasons=ownership(final,company_name,ticker,title,content,corporate_domain=corporate)
                    if independent>=90:
                        candidate.official=True;candidate.officiality_score=max(candidate.officiality_score,independent);candidate.evidence.extend(reasons)
                        if not brand_domain(final,company_name):
                            from dataclasses import replace
                            alias=DOMAIN(final).domain
                            self.identity=replace(self.identity,brand_candidates=tuple(dict.fromkeys(self.identity.brand_candidates+(alias,))))
                            candidate.evidence.append('Domain alias learned only after page company/ticker ownership verification: '+alias)
                    score = score_ir(final,title,content,stock,candidate.official)
                    candidate.officiality_score=max(candidate.officiality_score,independent,score.score)
                    candidate.content_validated = score.score >= 75 or independent>=90
                    if not candidate.official and score.score >= 90:
                        candidate.official = True
                        candidate.officiality_score = score.score
                    candidate.evidence.extend(score.reasons)
                    if candidate.official and candidate.content_validated:
                        # Only a company-identifying IR page can establish a cross-domain redirect alias.
                        if final != candidate.url and score.score >= 75:
                            redirected = IRCandidate(final, candidate.officiality_score, ["Redirect from verified IR URL"], True)
                            if corporate and DOMAIN(final).top_domain_under_public_suffix != corporate:
                                redirected.verified_hosted_ir_domain = urlsplit(final).hostname
                            self.register(redirected, candidate.url, "Verified IR redirect")
                        self.register(candidate, candidate.corporate_url or candidate.url, "Validated IR content")
                        self.rss_feeds.extend(await discover_rss(page))
                        # Explicit Investors links from an already verified IR page can establish aliases.
                        for link in await links(page):
                            alias = endorsed_candidate(candidate.corporate_url or candidate.url, link)
                            if alias and alias.url not in seen and urlsplit(alias.url).hostname != host:
                                alias.evidence = [f"Explicit Investors link from verified IR page {candidate.url}"]
                                self.register(alias, candidate.url, "Verified IR navigation")
                                queue.append(alias)
                elif candidate.access.error:
                    self.errors.append(Issue("SITE_BLOCKED" if candidate.access.blocked else "IR_ACCESS_FAILED", candidate.access.error, candidate.url))
                if not candidate.official:self.rejected.append({'url':candidate.url,'score':candidate.officiality_score,'reason':'Insufficient independent identity evidence','evidence':candidate.evidence})
                log.info("IR candidate url=%s official=%s officiality=%s browser_accessible=%s evidence=%s", candidate.url, candidate.official, candidate.officiality_score, candidate.access.browser_accessible, candidate.evidence)
                if candidate.official:break
            official = sorted((c for c in self.candidates if c.official), key=lambda c: (c.accepted, c.officiality_score), reverse=True)
            self.rss_feeds = list(dict.fromkeys(self.rss_feeds))
            if official:
                winner = official[0]
                self.ecosystem.official_ir_domain = urlsplit(winner.url).hostname
                log.info("IR selected: %s official=%s accessible=%s", winner.url, winner.official, winner.access.browser_accessible)
                return winner
            return None
        finally:
            await page.close()
