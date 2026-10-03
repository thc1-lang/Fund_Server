"""Official IR event parsing, identity, and public archive discovery."""
import logging
import asyncio
import json
import re
from collections import deque
from datetime import date
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .event_models import Event
from .extraction import extract_date, is_pdf
from .models import Link, Issue
from .structured import archive_next_urls, soup_links
from .providers.gcs_web import archive_state_key
from .scoring import section_score, company_tokens
from .urls import canonicalize_url, public_url
from .sections import find_sections
from .crawler import ArchiveCrawler

log = logging.getLogger(__name__)
DETAIL = re.compile(r"/events?/event-details/|/event-details/|/events?/[^/?]{8,}|/webcasts?/[^/?]{8,}", re.I)
WEBCAST = re.compile(r"webcast|replay|listen|audio|watch|conference call", re.I)
UNRELATED = re.compile(r"career|recruit|community|charity|job fair|employee|training|volunteer", re.I)


def event_type(title):
    if re.search(r"earnings|financial results|quarter.*results|quarterly results|FY\s?\d.*results", title, re.I): return "earnings_call"
    if re.search(r"investor day|capital markets day|analyst day",title,re.I): return "investor_day"
    if re.search(r"annual.*meeting|shareholder.*meeting",title,re.I): return "annual_meeting"
    if re.search(r"fireside",title,re.I): return "fireside_chat"
    if re.search(r"conference|forum|summit",title,re.I): return "conference"
    return "presentation" if re.search(r"presentation",title,re.I) else "other"

def event_priority(event):
    """Rank undated fiscal archive links by their stated year and quarter.

    This is selection metadata only, never an invented calendar event date.
    """
    year=re.search(r'\b(20\d{2})\b',event.title)
    quarter=re.search(r'\b(?:Q([1-4])|(first|second|third|fourth)\s+quarter)\b',event.title,re.I)
    number=int(quarter[1]) if quarter and quarter[1] else {'first':1,'second':2,'third':3,'fourth':4}.get(quarter[2].lower(),0) if quarter else 0
    period=int(year[1]) if year and number else int(event.date[:4]) if event.date else 0
    return (event.event_type=='earnings_call',period,number,event.date or '',event.event_url)


def event_keys(e):
    keys = set() if "#event-" in e.event_url else {("url",canonicalize_url(e.event_url))}
    if e.webcast_url: keys.add(("webcast",canonicalize_url(e.webcast_url)))
    if e.title and e.date: keys.add(("title_date",re.sub(r"\W+","",e.title.casefold()),e.date))
    if e.provider_event_id: keys.add(("provider",e.provider,e.provider_event_id))
    return keys


def merge_events(events):
    result=[]; identities={}
    for event in events:
        def compatible(previous):
            shared=event_keys(event)&event_keys(previous)
            # Shared generic/expired player links do not establish that two
            # differently dated events are the same corporate event.
            return not (all(k[0]=='webcast' for k in shared) and event.date and previous.date and event.date!=previous.date)
        prior=next((identities[k] for k in event_keys(event) if k in identities and compatible(identities[k])),None)
        if prior is None and re.search(r'\b20\d{2}\b',event.title):
            title_key=re.sub(r'\W+','',event.title.casefold())
            prior=next((p for p in result if re.sub(r'\W+','',p.title.casefold())==title_key and (not p.date or not event.date) and not (p.webcast_url and event.webcast_url and p.webcast_url!=event.webcast_url)),None)
        if prior is None:
            prior=event;result.append(prior)
        else:
            if '#event-' in prior.event_url and '#event-' not in event.event_url:
                prior.event_url=event.event_url
            for field in ("webcast_url","official_transcript_url","date","provider_event_id"):
                if not getattr(prior,field): setattr(prior,field,getattr(event,field))
            prior.presentation_urls=list(dict.fromkeys(prior.presentation_urls+event.presentation_urls))
            prior.replay_available |= event.replay_available
        for key in event_keys(event): identities[key]=prior
    return result


def attach_links(event, node, base):
    for a in node.select("a[href]"):
        u=canonicalize_url(a["href"],base)
        if not public_url(u): continue
        label=" ".join([a.get_text(" ",strip=True),a.get("aria-label",""),a.get("title",""),a.get("class",[]) and " ".join(a.get("class",[])) or ""])
        if re.search(r"transcript",label,re.I): event.official_transcript_url=u
        elif is_pdf(Link(u,label)) or re.search(r"slides|presentation materials",label,re.I):
            if u not in event.presentation_urls: event.presentation_urls.append(u)
        elif WEBCAST.search(label) and not (urlsplit(u).hostname==urlsplit(base).hostname and DETAIL.search(u)) and not re.search(r"calendar|mailto:|news-release",u,re.I):
            event.webcast_url=u
            event.replay_available |= bool(re.search(r"replay|archive|on.demand",label,re.I))
    for frame in node.select("iframe[src]"):
        u=canonicalize_url(frame["src"],base)
        if public_url(u) and not re.search(r"youtube.*embed/videoseries|captcha|advert|analytics",u,re.I): event.webcast_url=event.webcast_url or u
    if node.select_one("audio,video"):
        event.webcast_url=event.webcast_url or base
        event.replay_available=True
    return event


def parse_events(html, base):
    soup=BeautifulSoup(html,"html.parser");events=[]
    for a in soup.select("a[href]"):
        u=canonicalize_url(a["href"],base);title=a.get_text(" ",strip=True) or a.get("aria-label","")
        # A provider's /webcast/... path is not an IR event-detail URL.
        # Direct-provider cards are parsed separately with their IR heading.
        if urlsplit(u).hostname!=urlsplit(base).hostname:continue
        if not DETAIL.search(u) or not title or UNRELATED.search(title) or a.find_parent(["nav","header","footer"]): continue
        card=a.find_parent(class_=re.compile(r"node--type-nir-event|views-row|event.item|module_item")) or a.find_parent(["article","li","tr"]) or a.parent
        when=card.select_one("time[datetime]")
        d=extract_date(when.get("datetime", "")) if when else None
        d=d or extract_date(card.get_text(" ",strip=True))
        e=Event(title,d,u,event_type(title),linked_from_url=base)
        events.append(attach_links(e,card,base))
    # Cards linking directly to a provider still require an IR-hosted archive record.
    for card in soup.select("article, .event-item, .event-card, .module_item"):
        if card.select_one('a[href*="event-details"]'): continue
        heading=card.select_one('.card-title') or card.select_one("h2,h3,h4")
        if not heading: continue
        title=heading.get_text(" ",strip=True);d=extract_date(card.get_text(" ",strip=True))
        if not d or UNRELATED.search(title): continue
        e=attach_links(Event(title,d,base+"#event-"+re.sub(r"\W+","-",title.lower()),event_type(title),linked_from_url=base),card,base)
        if e.webcast_url or e.official_transcript_url: events.append(e)
    # Financial-results cards often use paragraphs instead of headings.
    for card in soup.select('.report, .event, .module_item, [itemtype*=Event]'):
        heading=card.select_one('.report-name, .event-name, .module_headline, [itemprop=name], h2, h3, h4')
        if not heading:continue
        title=heading.get_text(' ',strip=True)
        dt=card.select_one('.report-date, .event-date, time, [itemprop=startDate]')
        raw=(dt.get('datetime') or dt.get('content') or dt.get_text(' ',strip=True)) if dt else card.get_text(' ',strip=True)
        date_value=extract_date(raw)
        if not date_value and dt:
            match=re.fullmatch(r'\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2})\s*',raw)
            if match:
                try:date_value=date(2000+int(match[3]),int(match[1]),int(match[2])).isoformat()
                except ValueError:pass
        if not date_value or UNRELATED.search(title):continue
        e=attach_links(Event(title,date_value,base+'#event-'+re.sub(r'\W+','-',title.lower()),event_type(title),linked_from_url=base),card,base)
        if e.webcast_url or e.official_transcript_url:events.append(e)
    for script in soup.select('script[type="application/ld+json"], script[type="application/json"]'):
        try:events.extend(parse_event_json(json.loads(script.get_text()),base))
        except (ValueError,TypeError):pass
    return merge_events(events)


def parse_event_json(payload,base):
    result=[]
    def walk(node,depth=0):
        if depth>15:return
        if isinstance(node,list):
            for item in node[:2000]:walk(item,depth+1)
        elif isinstance(node,dict):
            title=node.get('EventName') or node.get('EventTitle') or (node.get('name') if 'Event' in str(node.get('@type','')) else None)
            dt=node.get('EventDate') or node.get('StartDate') or node.get('startDate')
            if title and dt:
                target=node.get('EventUrl') or node.get('LinkToDetailPage') or node.get('url') or base+'#event-'+re.sub(r'\W+','-',str(title).lower())
                webcast=node.get('WebcastLink') or node.get('WebcastUrl') or node.get('webcastUrl')
                target=urljoin(base,target)
                if public_url(target):
                    result.append(Event(str(title),extract_date(str(dt)),target,event_type(str(title)),webcast_url=urljoin(base,webcast) if isinstance(webcast,str) and public_url(urljoin(base,webcast)) else None,linked_from_url=base))
            for value in node.values():
                if isinstance(value,(dict,list)):walk(value,depth+1)
    walk(payload)
    return result


def parse_detail(event, html):
    soup=BeautifulSoup(html,"html.parser")
    node=soup.select_one("article.node--type-nir-event, [itemtype*='Event'], .event-detail, main") or soup
    heading=node.select_one(".field-nir-event-title, h1, h2")
    if heading and heading.get_text(" ",strip=True).lower() not in {"event details","events","webcast"}: event.title=heading.get_text(" ",strip=True)
    when=node.select_one("time[datetime]")
    event.date=(extract_date(when.get("datetime","")) if when else None) or extract_date(node.get_text(" ",strip=True)) or event.date
    event.event_type=event_type(event.title)
    return attach_links(event,node,event.event_url)


def is_future(event, today=None):
    return bool(event.date and event.date[:10] > (today or date.today()).isoformat())

async def find_events_section(page, base_url, browser, scope=None, company_name=""):
    return await find_sections(page,base_url,"events",browser,scope,company_name)

class EventDiscovery:
    def __init__(self,env,provider):
        self.env=env;self.provider=provider;self.errors=[];self.sections=[];self.pages=0;self.coverage="unknown"
        self.details_inspected=0;self.rendered_pages=0;self.json_endpoints=[];self.resources=[];self.links_detected=0

    async def render_events(self,url):
        if self.rendered_pages>=2:return []
        self.rendered_pages+=1;page=await self.env.context.new_page();tasks=[];found=[]
        async def capture(response):
            if not self.env.scope.allows(response.url) or 'json' not in response.headers.get('content-type',''):return
            if len(self.json_endpoints)>=10:return
            self.json_endpoints.append(response.url)
            try:
                body=await response.body()
                if len(body)<=2*1024*1024:found.extend(parse_event_json(json.loads(body),url))
            except (ValueError,TypeError):pass
        def schedule(response):tasks.append(asyncio.create_task(capture(response)))
        page.on('response',schedule)
        try:
            await asyncio.wait_for(self.env.browser.goto(page,url),timeout=20)
            found.extend(parse_events(await page.content(),url))
        except Exception as exc:
            self.resources.append({'url':url,'transport':'browser','error':str(exc)})
        finally:
            page.remove_listener('response',schedule)
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            await page.close()
        return found

    def diagnostics(self):
        blocked=any(getattr(x,'code','') in {'SITE_BLOCKED','HTTP_403','ACCESS_BLOCKED'} or re.search(r'\b(?:401|403|429)\b|SITE_BLOCKED',str(getattr(x,'message','')),re.I) for x in self.errors)
        blocked=blocked or any(re.search(r'\b(?:401|403|429)\b|SITE_BLOCKED',str(x),re.I) for x in self.resources)
        return {'official_ir':self.env.roots,'provider':self.provider.name,'sections_attempted':self.sections,
            'resources':getattr(self.provider,'diagnostics',[])+self.resources,'candidate_event_links':self.links_detected,
            'json_endpoints_detected':self.json_endpoints,'browser_pages':self.rendered_pages,
            'reason_no_events':None if getattr(self,'listing_count',0) else 'No dated event records recognized in accessible HTML/rendered data',
            'access_blocked':blocked}

    async def discover(self):
        events=[];page=await self.env.context.new_page()
        try:
            seeds=[] if self.env.browser.config.events.only else await find_events_section(page,self.env.roots[0],self.env.browser,self.env.scope,self.env.stock.company_name)
            if seeds:
                archive=await ArchiveCrawler(self.env.browser,self.env.scope).collect(self.env.context,seeds,"events",extractor=parse_events)
                events.extend(archive.documents);self.errors.extend(archive.errors);self.pages+=archive.pages;self.sections.extend(seeds)
        except Exception as exc:
            log.info("Browser event archive unavailable; testing public official resources: %s",getattr(exc,"code",type(exc).__name__))
        finally: await page.close()
        from .access_strategy import load,save
        known=load(self.env.browser.config.download_root,self.env.stock,self.env.roots[0])
        queue=deque(self.sections+[u for u in known.get('sections',[]) if self.env.scope.allows(u)])
        for root in ([] if queue else self.env.roots):
            response=await self.provider.fetch(root)
            if response:
                for link in soup_links(BeautifulSoup(response.body,"html.parser"),root):
                    if self.env.scope.allows(link.url) and section_score(link,"events").score>=12 and not DETAIL.search(link.url): queue.append(link.url)
        if not queue:
            paths=("event-calendar","events-and-presentations","events","webcasts")
            queue.extend(urljoin(root,p) for root in self.env.roots for p in paths)
        seen=set();valid=0
        while queue and len(seen)<self.env.browser.config.max_archive_pages:
            url=queue.popleft();key=archive_state_key(url)
            if key in seen or not self.env.scope.allows(url): continue
            seen.add(key);response=await self.provider.fetch(url)
            if not response:
                self.resources.append({"url":url,"error":"Public resource unavailable"});continue
            soup=BeautifulSoup(response.body,"html.parser")
            title=soup.title.get_text(" ",strip=True) if soup.title else ""
            content=soup.get_text(" ",strip=True)
            from .company_identity import identify
            identity=identify(self.env.stock.company_name)
            relevant=re.search(r'event|webcast|presentation|conference|results',title+' '+url,re.I)
            if not relevant or not identity.brand_match(content+' '+title):continue
            valid+=1;self.sections.append(url)
            parsed=parse_events(response.body,url);self.links_detected+=len(parsed)
            if not parsed and soup.select_one('script[src], #root, #app, .module_container'):
                parsed=await self.render_events(url)
            events.extend(parsed)
            for target in archive_next_urls(soup,url,"events",self.env.scope):
                if not DETAIL.search(target) and archive_state_key(target) not in seen: queue.append(target)
            for link in soup_links(soup,url):
                if self.env.scope.allows(link.url) and not DETAIL.search(link.url) and re.search(r"past events|archived events|previous events|view all events|past webcasts",link.text,re.I):queue.append(link.url)
        self.pages+=valid
        if queue:self.errors.append(Issue("SAFETY_LIMIT_REACHED","Event archive page limit"))
        self.sections=list(dict.fromkeys(self.sections))
        if not events and self.env.browser.config.events.only:
            fallback_page=await self.env.context.new_page()
            try:
                seeds=await find_events_section(fallback_page,self.env.roots[0],self.env.browser,self.env.scope,self.env.stock.company_name)
                if seeds:
                    archive=await ArchiveCrawler(self.env.browser,self.env.scope).collect(self.env.context,seeds,'events',extractor=parse_events)
                    events.extend(archive.documents);self.sections.extend(seeds);self.pages+=archive.pages;self.errors.extend(archive.errors)
            except Exception as exc:log.info('Public and browser event discovery unavailable: %s',getattr(exc,'code',type(exc).__name__))
            finally:await fallback_page.close()
        events=merge_events(events)
        if len(events)>self.env.browser.config.max_links_per_company:
            events=events[:self.env.browser.config.max_links_per_company];self.errors.append(Issue("SAFETY_LIMIT_REACHED","Event link limit"))
        self.coverage="exposed_archive_traversed" if events and not self.errors else "incomplete" if events else "unknown"
        log.info("Found %s unique events across %s exposed archive pages",len(events),self.pages)
        self.listing_count=len(events)
        if events:
            save(self.env.browser.config.download_root,self.env.stock,self.env.roots[0],{'provider':self.provider.name,'sections':self.sections,'transport':'public_http','browser_root_required':False})
        # Detail inspection is discovery only; it never opens/registers a webcast.
        inspect=events
        targeted=self.env.browser.config.events.only and self.env.browser.config.events.limit
        if targeted:
            inspect=sorted((e for e in events if not is_future(e)),key=event_priority,reverse=True)
        selected_count=0
        for event in inspect:
            runtime=getattr(self.env.browser,'current_company',None)
            registry=getattr(runtime,'registry',None)
            cached=registry.lookup(self.env.stock,event,self.env.browser.config.events) if registry else None
            if cached:
                selected_count+=1
                if targeted and selected_count>=targeted:break
                continue
            if self.env.scope.allows(event.event_url) and '#event-' not in event.event_url:
                response=await self.provider.fetch(event.event_url.split("#")[0])
                self.details_inspected+=1
                if response: parse_detail(event,response.body)
            if not is_future(event) and (event.webcast_url or event.official_transcript_url):selected_count+=1
            if targeted and selected_count>=targeted:break
        merged=merge_events(events)
        log.info('Event counts: listing_unique=%s final_unique=%s historical=%s future=%s undated=%s detail_merge_duplicates=%s',self.listing_count,len(merged),sum(bool(e.date) and not is_future(e) for e in merged),sum(is_future(e) for e in merged),sum(not e.date for e in merged),self.listing_count-len(merged))
        return merged
