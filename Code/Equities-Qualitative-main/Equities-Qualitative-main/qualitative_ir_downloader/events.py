"""Event collection orchestration with isolated failures and persistent resume."""
import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from .event_discovery import EventDiscovery, event_keys, is_future, event_priority
from .event_models import Event
from .filesystem import atomic_json, document_path
from .models import CollectionError
from .network import ResourceClient
from .privacy import safe_record,safe_text,redact_url
from .providers import ProviderContext,select_provider
from .webcast_providers import select_webcast_provider
from .webcast_providers.generic import candidate,structured_sources
from .webcast_media import MediaAcquirer
from .transcription import transcribe
from .transcript_pdf import transcript_record,create_pdf
from .pdf_utils import validate_pdf
from .provider_capabilities import observe, integration_penalty
from .scoring import company_key
from .event_outcomes import metrics as event_metrics, outcome
from .company_context import CompanyContext, MediaBudget
from .transcription import validate_artifacts
from .captions import parse_captions, validate_completeness
log=logging.getLogger(__name__)


def matching_folder(root,stock):
    matches=[]
    completed=[]
    for p in Path(root).glob("*/manifest.json"):
        try:
            data=json.loads(p.read_text(encoding="utf-8"))
            if data.get("ticker","").casefold()==stock.ticker.casefold() and company_key(data.get("company_name",""))==company_key(stock.company_name) and data.get("worksheet")==stock.worksheet and data.get("spreadsheet_row")==stock.spreadsheet_row:
                matches.append(p)
                if data.get("status") != "IN_PROGRESS":
                    completed.append(p)
        except (OSError,ValueError):continue
    # An interrupted run can leave a newer IN_PROGRESS manifest containing a
    # discovered list but incomplete artifacts.  Prefer the newest completed
    # manifest so the next incremental pass validates/reuses the last known
    # complete artifact set; if none exists, resume the newest partial run.
    candidates = completed or matches
    return max(candidates,key=lambda p:p.stat().st_mtime).parent if candidates else None


def completed_match(event,root,stock,require_json=True,config=None):
    from .event_registry import EventRegistry
    from .events_config import EventConfig
    registry=EventRegistry(root);registry.import_history(stock)
    hit=registry.lookup(stock,event,config or EventConfig())
    return (hit[1],hit[2]) if hit and hit[0]=='artifact' else None


def reuse_completed(event,previous,old_folder,folder):
    for field in Event.__dataclass_fields__:
        if field not in {"event_url","linked_from_url"}:setattr(event,field,getattr(previous,field))
    if old_folder.resolve()!=folder.resolve():
        for field in ("local_pdf","local_json"):
            value=getattr(event,field)
            if value:
                source=old_folder/value
                destination=document_path(folder/"Events", "",source.stem,None).with_suffix(source.suffix)
                shutil.copy2(source,destination);setattr(event,field,destination.relative_to(folder).as_posix())
    event.resumed=True
    event.generated_transcript_created=False
    event.media_attempted=False;event.media_downloads=0;event.whisper_runs=0
    if event.method == "official_transcript":
        event.official_transcript_discovered=True
        event.official_transcript_saved=True
    elif event.method == "official_captions":
        event.official_captions_discovered=True
        event.official_captions_saved=True


class EventProcessor:
    def __init__(self,browser,context,stock,folder,manifest):
        self.browser=browser;self.context=context;self.stock=stock;self.folder=folder;self.manifest=manifest;self.config=browser.config.events

    def persist(self,event):
        self.manifest.event(event)
        registry=getattr(getattr(self.browser,"current_company",None),"registry",None)
        if registry:registry.record(self.stock,event,self.folder)

    async def official_transcript(self,event):
        event.official_transcript_discovered=True
        if self.config.generated_only:
            log.info("Official transcript available; skipping for generated-transcript integration test")
            event.method="official_transcript"; event.transition("SKIPPED_OFFICIAL_TRANSCRIPT"); self.persist(event); return
        response=await ResourceClient(self.browser,self.context).get(event.official_transcript_url,event.webcast_url or event.event_url)
        target=document_path(self.folder/"Events",self.stock.ticker,event.title,event.date)
        event.method="official_transcript";event.transcription_engine=None
        if response.body.lstrip().startswith(b"%PDF-"):
            validate_pdf(response.body)
            temporary=target.with_suffix(".pdf.part");temporary.write_bytes(response.body);temporary.replace(target)
            from pypdf import PdfReader
            segments=[{"start":None,"end":None,"text":p.extract_text()} for p in PdfReader(target).pages]
        else:
            soup=BeautifulSoup(response.body,"html.parser");node=soup.select_one("article,main,[role=main]") or soup
            for x in node.select("script,style,nav,footer,form"):x.decompose()
            text=node.get_text("\n",strip=True)
            if len(text)<200:raise CollectionError("TRANSCRIPT_INVALID","Official transcript body is too short")
            segments=[{"start":None,"end":None,"text":t} for t in text.split("\n") if t.strip()]
            record=transcript_record(self.stock,event,segments);create_pdf(target,record)
        event.local_pdf=target.relative_to(self.folder).as_posix()
        if self.config.transcript_json:
            jp=target.with_suffix(".json");atomic_json(jp,transcript_record(self.stock,event,segments));event.local_json=jp.relative_to(self.folder).as_posix()
        event.official_transcript_saved=True
        event.transition("TRANSCRIBED");self.persist(event)

    async def official_captions(self,event,url):
        event.captions_sources_searched+=1
        response=await ResourceClient(self.browser,self.context).get(url,event.webcast_url or event.event_url)
        result=parse_captions(response.body)
        segments=result.segments
        event.caption_format=result.format
        source=redact_url(getattr(response,"url",None) or url)
        event.caption_diagnostics={**result.diagnostics, "source_url":source, "sha256":hashlib.sha256(response.body).hexdigest()}
        log.info("Caption candidate: format=%s source=%s segments=%s first=%.3fs last=%.3fs characters=%s rolling_duplicate_words_removed=%s",result.format,source,len(segments),result.diagnostics["first_timestamp"],result.diagnostics["last_timestamp"],result.diagnostics["transcript_characters"],result.diagnostics["rolling_duplicate_words_removed"])
        self.persist(event)
        try:
            validate_completeness(result,self.config,event.duration_seconds)
        except CollectionError as exc:
            event.caption_diagnostics["rejection_code"]=exc.code
            log.info("Caption candidate rejected: %s (%s)",exc.code,exc)
            self.persist(event)
            raise
        event.official_captions_discovered=True
        event.official_captions_url=source
        event.caption_diagnostics.update(result.diagnostics)
        log.info("Caption completeness: %s; caption coverage=%.1f%%; media duration=%s",result.diagnostics["completeness_check"],100*result.diagnostics["caption_time_coverage"],event.duration_seconds)
        if self.config.generated_only:
            log.info("Official captions available; skipping for generated-transcript integration test")
            event.method="official_captions"; event.transition("SKIPPED_OFFICIAL_CAPTIONS"); self.persist(event); return
        target=document_path(self.folder/"Events",self.stock.ticker,event.title,event.date)
        event.method="official_captions"; event.transcription_engine=None; event.transcription_model=None; event.device=None
        record=transcript_record(self.stock,event,segments); jp=target.with_suffix(".json"); atomic_json(jp,record); event.local_json=jp.relative_to(self.folder).as_posix()
        await asyncio.to_thread(create_pdf,target,record)
        validate_artifacts(target,jp,self.config)
        event.local_pdf=target.relative_to(self.folder).as_posix();event.official_captions_saved=True
        event.transition("TRANSCRIBED"); self.persist(event)

    async def caption_duration(self,page,event):
        """Use the duration exposed by the normal player, never caption end time."""
        durations=[]
        for frame in page.frames:
            durations.extend(await frame.locator("audio,video").evaluate_all("els=>els.map(e=>e.duration).filter(d=>Number.isFinite(d)&&d>0)"))
        if durations:
            event.duration_seconds=max(durations)

    async def search_official_transcript(self,event):
        """Search only verified IR roots and the event's related first-party pages."""
        terms=re.compile(r"\b(?:transcript|prepared remarks|earnings remarks)\b",re.I)
        roots=list(dict.fromkeys([event.event_url]+self.manifest.data.get("ecosystem",{}).get("verified_ir_urls",[])))
        urls=[]
        client=ResourceClient(self.browser,self.context)
        for root in roots[:8]:
            try:
                response=await client.get(root,event.event_url); event.transcript_candidates_searched+=1
                soup=BeautifulSoup(response.body,"html.parser")
                for anchor in soup.select("a[href]"):
                    label=anchor.get_text(" ",strip=True); href=anchor.get("href","")
                    absolute=__import__("urllib.parse",fromlist=["urljoin"]).urljoin(root,href)
                    if terms.search(label+" "+absolute) and urlsplit(absolute).hostname in {urlsplit(r).hostname for r in roots}:
                        urls.append(absolute)
            except Exception: continue
        for url in list(dict.fromkeys(urls))[:12]:
            if url==event.webcast_url: continue
            try:
                response=await client.get(url,event.event_url); event.transcript_candidates_searched+=1
                text=response.body.decode("utf-8",errors="replace")
                if "xml" in response.headers.get("content-type", ""): continue
                if terms.search(text[:5000]) and any(t in text.casefold() for t in (event.title.casefold(), (event.date or "")[:10]) if t):
                    event.official_transcript_url=url; return url
            except Exception: continue
        return None

    async def process(self,event):
        process_started=time.perf_counter()
        if not self.config.force and not self.config.discover_registration_destinations:
            runtime=getattr(self.browser,'current_company',None)
            registry=getattr(runtime,'registry',None)
            hit=registry.lookup(self.stock,event,self.config) if registry else None
            match=None
            if hit:
                kind,previous,old_folder=hit
                if kind=='artifact':match=(previous,old_folder)
                else:
                    event.resumed=True
                    event.protected_media_type=previous.protected_media_type
                    event.official_transcript_url=previous.official_transcript_url
                    event.transition('CACHED_EVENT_MEDIA_PROTECTED' if kind=='protected' else 'SKIPPED_OFFICIAL_TRANSCRIPT')
            elif not registry:
                match=completed_match(event,self.browser.config.download_root,self.stock,self.config.transcript_json,self.config)
            if match:
                reuse_completed(event,*match,self.folder)
                if self.config.generated_only:
                    event.transition({'official_transcript':'SKIPPED_OFFICIAL_TRANSCRIPT','official_captions':'SKIPPED_OFFICIAL_CAPTIONS'}.get(event.method,'GENERATED_TRANSCRIPT_ALREADY_AVAILABLE'))
            if hit or match:
                budget=getattr(self.browser,'media_budget',None)
                if budget:budget.skipped_cached+=1
                event.timings={'resume_validation':time.perf_counter()-process_started}
                self.persist(event);log.info('Completed/cached event skipped: %s; media_attempt=false Whisper=false',event.title);return
        if event.official_transcript_url and not self.config.discover_registration_destinations:
            try:await self.official_transcript(event);return
            except Exception as exc:
                event.errors.append({"stage":"official_transcript","message":safe_text(exc)})
                if not event.webcast_url:raise
        if not event.webcast_url:
            event.transition("FUTURE_EVENT_NO_REPLAY" if is_future(event) else "NO_WEBCAST");self.persist(event);return
        page=await self.context.new_page();observed=[];tasks=set();submitted=set();observed_captions=[]
        async def observe(response):
            event.provider_responses_inspected+=1
            ct=response.headers.get("content-type","")
            if "text/vtt" in ct or re.search(r"\.(?:vtt|srt)(?:[?]|$)",response.url,re.I):
                observed_captions.append(response.url)
            try:frame=response.request.frame.url
            except Exception:frame=page.url
            c=candidate(response.url,ct,"network_response",frame,size=int(response.headers.get("content-length","0")) or None)
            if c:
                c.headers=await response.request.all_headers();observed.append(c)
            elif "json" in ct and int(response.headers.get("content-length","0"))<2*1024*1024:
                try:
                    payload=await response.json()
                    from .webcast_providers.zoom import is_zoom_recording,exposed_sources
                    observed.extend(exposed_sources(payload,frame) if is_zoom_recording(page.url) else structured_sources(payload,frame))
                except Exception:pass
        def schedule(response):
            task=asyncio.create_task(observe(response));tasks.add(task);task.add_done_callback(tasks.discard)
        page.on("response",schedule)
        try:
            if not event.official_transcript_url and not self.config.discover_registration_destinations:
                transcript=await self.search_official_transcript(event)
                if transcript:
                    try: await self.official_transcript(event); return
                    except Exception as exc: event.errors.append({"stage":"official_transcript_search","message":safe_text(exc)})
            budget = getattr(self.browser,"media_budget",None)
            if budget is None:
                budget = self.browser.media_budget = MediaBudget(self.config.max_media_attempts)
            if not self.config.discover_registration_destinations and not budget.claim():
                event.transition("MEDIA_ATTEMPT_LIMIT"); self.persist(event); return
            event.media_attempted=True
            event.processing_stage = "webcast_page"
            try:
                await self.browser.goto(page,event.webcast_url)
            except CollectionError as exc:
                code = "PLAYER_HTTP_403" if getattr(self.browser,"navigation_status",{}).get(event.webcast_url)==403 else "PLAYER_BLOCKED" if exc.code=="SITE_BLOCKED" else exc.code
                raise CollectionError(code, "Webcast page could not be loaded") from exc
            adapter=await select_webcast_provider(page);event.provider=adapter.name
            log.info("Webcast provider: %s",event.provider)
            if self.config.discover_registration_destinations:
                from .registration import discover_destination
                destination=await discover_destination(page,event)
                if destination:
                    log.info("Registration destination discovered (submission disabled): %s",redact_url(destination['page_url']))
                else:
                    event.registration_status="NOT_REQUIRED"
                    event.transition("REGISTRATION_NOT_REQUIRED")
                self.persist(event); return
            if self.config.skip_registration:
                if await page.locator('input[type="email"]:visible, input[type="password"]:visible').count():
                    raise CollectionError("REGISTRATION_REQUIRED","Registration skipped: no personal data submission allowed for this search")
            else:
                event.processing_stage="registration"
                self.context._approved_registration_domains=self.config.approved_registration_domains
                await adapter.handle_registration(page,self.config.profile,event,submitted)
            event.processing_stage="player"
            await self.browser.settle(page)
            await self.browser.check_access(page)
            # Guarded second inspection catches email/CAPTCHA/login and prevents repeat submits.
            if event.registration_required:
                from .registration import access_state
                state=access_state((await page.locator("body").inner_text())[:12000])
                if state:raise CollectionError(state,"Registration needs an additional access step")
                # Visible registration submit indicates the gate has not cleared.
                if await page.locator('input[type="email"]:visible').count():
                    raise CollectionError("REGISTRATION_REQUIRED","Registration form remains visible after one submission")
                event.registration_status="COMPLETED";event.transition("REGISTRATION_COMPLETED")
            event.player_loaded=True
            log.info("Registration status=%s; post-submit=%s; player_loaded=True",event.registration_status,event.post_submit_url or redact_url(page.url))
            event.transition("WEBCAST_ACCESSIBLE");self.persist(event)
            await self.caption_duration(page,event)
            transcript=await adapter.find_official_transcript(page)
            if transcript and not self.config.discover_registration_destinations:
                event.official_transcript_url=transcript
                try:await self.official_transcript(event);return
                except Exception as exc:event.errors.append({"stage":"official_transcript","message":safe_text(exc)})
            caption_urls=[]
            for frame in page.frames:
                caption_urls.extend(await frame.locator('track[kind="captions"],track[kind="subtitles"]').evaluate_all("els => els.map(e => e.src || e.getAttribute('src')).filter(Boolean)"))
            if not caption_urls:
                caption_urls.extend(await page.locator('a[href*=".vtt"],a[href*=".srt"],a[href*="caption"],a[href*="subtitle"]').evaluate_all("els => els.map(e => e.href)"))
            for caption_url in dict.fromkeys(caption_urls):
                try: await self.official_captions(event,caption_url); return
                except Exception as exc: event.errors.append({"stage":"official_captions","message":safe_text(exc)})
            for frame in page.frames:
                controls=frame.get_by_role("button",name=re.compile(r"^(play|play video|play audio|play webcast|watch now|listen now|launch webcast)$",re.I))
                if await controls.count() and await controls.first.is_visible():
                    await controls.first.click(timeout=5000)
            await page.wait_for_timeout(4000)
            if tasks:await asyncio.gather(*list(tasks),return_exceptions=True)
            await self.caption_duration(page,event)
            for frame in page.frames:
                observed_captions.extend(await frame.locator('track[kind="captions"],track[kind="subtitles"]').evaluate_all("els => els.map(e => e.src).filter(Boolean)"))
            for caption_url in dict.fromkeys(observed_captions):
                if caption_url in caption_urls:continue
                try:await self.official_captions(event,caption_url);return
                except Exception as exc:event.errors.append({"stage":"official_captions","message":safe_text(exc)})
            observed.extend(await adapter.discover_media(page))
            unique={}
            for c in observed:
                if c.url not in unique or c.score>unique[c.url].score:unique[c.url]=c
            candidates=sorted(unique.values(),key=lambda c:c.score,reverse=True)
            event.timings['player_media']=time.perf_counter()-process_started
            if any(c.media_type in {"hls","dash"} for c in candidates):
                candidates=[c for c in candidates if not re.search(r"\.(?:ts|m4s|aac)(?:[?]|$)",c.url,re.I)]
            log.info("Media candidates found: %s",len(candidates))
            event.media_provenance=[{"url":redact_url(c.url),"type":c.media_type,"content_type":c.content_type,"source":c.source,"frame":redact_url(c.frame_url),"score":c.score} for c in candidates]
            if not candidates:raise CollectionError("MEDIA_NOT_FOUND","Player did not expose an accessible media candidate")
            event.transition("MEDIA_DISCOVERED");self.persist(event)
            event_id=hashlib.sha256(event.event_url.encode()).hexdigest()[:16]
            temporary=self.folder/"Events"/".temp"/event_id
            audio=None;failures=[]
            event.processing_stage="media"
            for i,c in enumerate(candidates[:self.config.max_media_candidates]):
                try:
                    event.media_downloads+=1
                    acquirer=MediaAcquirer(self.context,self.config,temporary/str(i))
                    audio,info=await acquirer.acquire(c);event.media_type=c.media_type;event.media_url=redact_url(c.url);event.duration_seconds=info["duration"];event.timings.update(info.get('timings',{}));break
                except Exception as exc:
                    failure=exc if isinstance(exc,CollectionError) else CollectionError("MEDIA_DOWNLOAD_FAILED",safe_text(exc))
                    if failure.code in {"DRM_PROTECTED","HLS_ENCRYPTED"}:
                        event.protected_media_type = "encrypted " + c.media_type.upper()
                        event.media_type=c.media_type; event.media_url=redact_url(c.url)
                    event.media_provenance[i]["outcome"]=failure.code
                    failures.append(failure);log.warning("Media candidate rejected: %s",failure.code)
            if audio is None:
                protected=next((e for e in failures if e.code in {"DRM_PROTECTED","HLS_ENCRYPTED"}),None)
                if protected: event.transition("EVENT_MEDIA_PROTECTED")
                raise protected or (failures[-1] if failures else CollectionError("MEDIA_DOWNLOAD_FAILED","No usable media"))
            event.retained_media=temporary.relative_to(self.folder).as_posix();event.method="generated_transcript";event.transition("TRANSCRIBING");self.persist(event)
            import threading
            stop=threading.Event()
            # The worker checkpoints and stops at the next yielded segment. It
            # never writes final artifacts after coroutine cancellation.
            event.whisper_runs+=1
            worker=asyncio.create_task(asyncio.to_thread(transcribe,audio,self.config,event,temporary/"transcript-checkpoint.json",stop))
            try:segments=await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop.set()
                try:await asyncio.shield(worker)
                except (Exception,asyncio.CancelledError):pass
                event.transition('INTERRUPTED');self.persist(event);raise
            except Exception as exc:raise CollectionError("TRANSCRIPTION_FAILED",safe_text(exc)) from exc
            artifact_started=time.perf_counter()
            target=document_path(self.folder/"Events",self.stock.ticker,event.title,event.date)
            record=transcript_record(self.stock,event,segments);jp=target.with_suffix(".json")
            # Always retain JSON until PDF succeeds, even if final JSON output is disabled.
            atomic_json(jp,record);event.local_json=jp.relative_to(self.folder).as_posix();self.persist(event)
            try:await asyncio.to_thread(create_pdf,target,record)
            except Exception as exc:raise CollectionError("PDF_FAILED",safe_text(exc)) from exc
            validate_artifacts(target,jp,self.config)
            event.timings['artifact_write']=time.perf_counter()-artifact_started
            event.generated_transcript_created=True
            event.local_pdf=target.relative_to(self.folder).as_posix();event.transition("TRANSCRIBED");self.persist(event)
            if not self.config.keep_media:
                resolved=temporary.resolve();root=(self.folder/"Events"/".temp").resolve()
                if resolved==root or not resolved.is_relative_to(root):raise RuntimeError("Unsafe media cleanup path")
                try:
                    shutil.rmtree(resolved);event.retained_media=None;log.info("Temporary event media deleted")
                except OSError:
                    log.warning("Transcript succeeded; temporary media cleanup must be retried")
            if not self.config.transcript_json:jp.unlink();event.local_json=None
            self.persist(event)
        finally:
            page.remove_listener("response",schedule)
            for task in list(tasks):task.cancel()
            if tasks:await asyncio.gather(*list(tasks),return_exceptions=True)
            await page.close()


def select_candidates(events, limit=1, config=None, root=None):
    candidates=[]
    for event in events:
        if (is_future(event) or event.date and event.date[:10]==date.today().isoformat()) and not event.replay_available:
            event.transition("FUTURE_EVENT_NO_REPLAY")
        elif not event.webcast_url and not event.official_transcript_url:
            event.transition("NO_WEBCAST")
        else:
            candidates.append(event)
    # Reconciliation marks durable generated/protected outcomes before selection.
    # A new-event search skips completed generated artifacts, but falls back to a
    # cached terminal event when no other usable candidate exists.
    if config and config.require_new_generated_event:
        fresh=[e for e in candidates if not (getattr(e,'cached_outcome',None) == 'artifact' and getattr(e,'cached_method',None) == 'generated_transcript')]
        if fresh:candidates=fresh
    candidates.sort(key=event_priority,reverse=True)
    return candidates[:limit] if limit else candidates

def candidate_priority(event, root):
    from .webcast_providers.generic import media_type
    kind=media_type(event.webcast_url or "")
    if getattr(event,'already_completed',False):return 10000
    if event.resumed or getattr(event,'cached_outcome',None):return -10000
    return ({"audio":100,"video":90,"hls":60,"dash":50}.get(kind,20)
            - (100 if event.official_transcript_url else 0)
            - integration_penalty(root,event.webcast_url or ""))

async def collect_events(browser,context,stock,ecosystem,folder,manifest):
    discovery_started=time.perf_counter()
    config=browser.config.events
    (folder/"Events").mkdir(exist_ok=True)
    runtime=getattr(browser,"current_company",None) or CompanyContext(stock,ecosystem=ecosystem)
    provider=await runtime.resolve_provider(browser,context)
    manifest.data["provider"]={"name":provider.name,"evidence":provider.evidence}
    if not runtime.scanned:
        discovery=EventDiscovery(runtime.provider_context,provider)
        registry=getattr(runtime,'registry',None)
        cached=registry.listing(stock,ecosystem.verified_ir_urls[0],config) if registry and not config.force else None
        fresh_events=cached if cached is not None else await discovery.discover()
        # A listing is an observation. Reconcile it with durable enriched event
        # records before selection or processing can inspect it.
        runtime.events=registry.reconcile_events(stock,fresh_events) if registry else fresh_events
        if cached is not None:
            discovery.coverage='cached_listing';log.info('[%s] event listing cache hit',stock.ticker)
        elif registry:registry.save_listing(stock,ecosystem.verified_ir_urls[0],runtime.events)
        runtime.discovery=discovery
        for e in runtime.events:
            e.resumed=False;e.generated_transcript_created=False;e.media_attempted=False;e.media_downloads=0;e.whisper_runs=0
        if registry and not config.force:
            for event in runtime.events:
                hit=registry.lookup(stock,event,config)
                if hit:
                    kind,previous,_=hit
                    event.cached_outcome=kind
                    event.cached_method=previous.method
                    event.already_completed=kind=='artifact' and previous.method=='generated_transcript'
                    event.cached_terminal_status=previous.status
        runtime.selected=select_candidates(runtime.events,config.limit,config,browser.config.download_root)
        runtime.scanned=True
    discovery_seconds=time.perf_counter()-discovery_started
    discovery=runtime.discovery;events=runtime.events;selected=runtime.selected
    manifest.data["events_sections"]=discovery.sections
    manifest.data.setdefault("coverage",{})["events"]=discovery.coverage
    manifest.data["events_discovery_errors"]=[asdict(x) for x in discovery.errors]
    manifest.data.setdefault("archive_pages",{})["events"]=discovery.pages
    manifest.data["event_limit"]=config.limit
    manifest.data["events"]=[e.record() for e in events]
    processor=EventProcessor(browser,context,stock,folder,manifest)
    for event in selected:
        from .webcast_providers import provider_name
        event.provider=provider_name(event.webcast_url)
        if runtime.scan_only:
            hit=runtime.registry.lookup(stock,event,config) if runtime.registry else None
            if hit:
                kind,previous,old_folder=hit
                event.already_completed=kind=='artifact' and previous.method=='generated_transcript'
                # Stage A is read-only classification; Stage B performs reuse/copy.
                event.cached_outcome=kind
                event.resumed=True
                event.method=previous.method
                event.transcript_qa=previous.transcript_qa
                event.official_transcript_url=previous.official_transcript_url
                event.protected_media_type=previous.protected_media_type
                event.transition('GENERATED_TRANSCRIPT_ALREADY_AVAILABLE' if event.already_completed else 'CACHED_EVENT_MEDIA_PROTECTED' if kind=='protected' else 'SKIPPED_OFFICIAL_TRANSCRIPT' if previous.method!='official_captions' else 'SKIPPED_OFFICIAL_CAPTIONS')
            else:event.transition("CANDIDATE_SCANNED")
        else:
            log.info("[%s] Selected: %s (%s)",stock.ticker,event.title,event.date)
            try:
                await processor.process(event)
            except asyncio.CancelledError:
                event.transition('INTERRUPTED');manifest.event(event);manifest.data['status']='INTERRUPTED';manifest.write();raise
            except Exception as exc:
                code=getattr(exc,"code","FAILED")
                if code=="SITE_BLOCKED":
                    code="REGISTRATION_FAILED" if event.processing_stage=="registration" else "PLAYER_BLOCKED"
                if not event.protected_media_type and event.status!="TRANSCRIBED": event.transition(code)
                if event.registration_required and event.registration_status!="COMPLETED":event.registration_status=code
                event.errors.append({"code":code,"stage":event.processing_stage,"message":safe_text(exc)})
                log.error("Event stopped: stage=%s outcome=%s",event.processing_stage,outcome(event))
            if not event.resumed: observe(browser.config.download_root,event)
        manifest.event(event)
        if runtime.registry and not runtime.scan_only:runtime.registry.record(stock,event,folder)
    metrics=event_metrics(events,selected,config.generated_only)
    metrics.update(events_listing_unique=getattr(discovery,'listing_count',len(events)),event_details_inspected=getattr(discovery,'details_inspected',len(events)),
                   webcast_count_complete=getattr(discovery,'details_inspected',len(events))>=len(events),
                   events_future=sum(is_future(e) for e in events),events_undated=sum(not e.date for e in events))
    metrics['timings']={'event_discovery':discovery_seconds,**(selected[0].timings if selected else {})}
    if selected:
        metrics.update(transcription_model=selected[0].transcription_model,transcript_qa=selected[0].transcript_qa,transcription_performance=selected[0].performance)
    metrics.update(events_historical=sum(bool(e.date and e.date[:10]<date.today().isoformat()) for e in events),events_intentionally_limited=len(selected)<len(events),
                   ir_provider=provider.name, selected_event=selected[0].title if selected else None,
                   selected_event_url=selected[0].event_url if selected else None,
                   selected_webcast_url=selected[0].webcast_url if selected else None,
                   webcast_provider=selected[0].provider if selected else None,
                   selected_media=(selected[0].protected_media_type or selected[0].media_type) if selected else None,
                   candidate_priority=candidate_priority(selected[0],browser.config.download_root) if selected else -10000)
    manifest.data["selected_events"]=[e.event_url for e in selected]
    manifest.data["event_metrics"]=metrics;manifest.write()
    log.info("[%s] IR=%s; events=%s; provider=%s; outcome=%s",stock.ticker,ecosystem.verified_ir_urls,len(events),provider.name,metrics["event_outcome"])
    return metrics
