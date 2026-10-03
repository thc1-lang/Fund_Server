"""Durable event state independent of timestamped run folders.

Records are hints: artifacts and event identity are validated at every reuse.
Historical manifests are incrementally imported before orchestration mutates them.
"""
import json
import time
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from .event_models import Event
from .event_discovery import event_keys
from .filesystem import atomic_json
from .privacy import redact_url
from .scoring import company_key
from .urls import canonicalize_url

PROTECTED={'DRM_PROTECTED','HLS_ENCRYPTED','EVENT_MEDIA_PROTECTED','CACHED_EVENT_MEDIA_PROTECTED'}

def stable_event_url(url):
    """Return the durable event URL identity, excluding volatile query/fragment data."""
    canonical=canonicalize_url(url)
    if not canonical or '#event-' in (url or ''):
        return ''
    parts=urlsplit(canonical)
    return urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))

def normalized_title(title):
    return re.sub(r'\W+','',str(title or '').casefold())

def stable_media_url(url):
    """Keep the provider path, discard replay signatures and temporary queries."""
    canonical=canonicalize_url(url)
    if not canonical:return ''
    parts=urlsplit(canonical)
    return urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))

def identity(stock,event):
    url=stable_event_url(event.event_url)
    return '|'.join((stock.ticker.upper(),url,event.date or '',normalized_title(event.title)))

def event_match_method(fresh,cached):
    same_url=bool(stable_event_url(fresh.event_url) and stable_event_url(fresh.event_url)==stable_event_url(cached.event_url))
    if same_url:
        if fresh.date and cached.date and fresh.date!=cached.date:
            return None
        if fresh.webcast_url and cached.webcast_url and stable_media_url(fresh.webcast_url)!=stable_media_url(cached.webcast_url):
            return None
        if fresh.media_url and cached.media_url and stable_media_url(fresh.media_url)!=stable_media_url(cached.media_url):
            return None
        return 'canonical_url'
    if fresh.provider_event_id and cached.provider_event_id and fresh.provider_event_id==cached.provider_event_id:
        return 'provider_event_id'
    if not same_url and fresh.date and cached.date and fresh.date==cached.date and normalized_title(fresh.title)==normalized_title(cached.title):
        return 'date_title'
    return None

def same_event(a,b):
    return bool(event_match_method(a,b))

_ENRICHED_FIELDS=(
    'official_transcript_url','official_transcript_discovered','official_transcript_saved',
    'official_captions_url','official_captions_discovered','official_captions_saved',
    'generated_transcript_created','local_pdf','local_json','transcript_qa',
    'transcription_config','transcription_engine','transcription_model','language','device',
    'performance','protected_media_type','media_type','media_url','media_provenance',
    'provider','registration_required','registration_status','registration_acceptances',
    'registration_page_url','registration_form_action','registration_destination_domain',
    'registration_fields','submission_attempted','post_submit_url','player_loaded',
    'replay_available','duration_seconds','method','retained_media','last_checked',
)

def merge_event_state(fresh,cached,method=None):
    """Merge a listing observation with durable state without resetting enrichment."""
    method=method or event_match_method(fresh,cached)
    if not method:return fresh
    # Listing metadata is allowed to improve; durable processing state is sticky.
    for field in _ENRICHED_FIELDS:
        old=getattr(cached,field,None)
        new=getattr(fresh,field,None)
        if old not in (None,False,'',[],{}):
            setattr(fresh,field,old)
        elif new not in (None,False,'',[],{}):
            setattr(fresh,field,new)
    # A fresh valid webcast link is preferred; a sparse listing must retain the
    # durable link so selection can reconnect to the enriched event.
    if not getattr(fresh,'webcast_url',None) and getattr(cached,'webcast_url',None):
        fresh.webcast_url=cached.webcast_url
    fresh.presentation_urls=list(dict.fromkeys((fresh.presentation_urls or [])+(cached.presentation_urls or [])))
    if cached.status not in {'DISCOVERED','CANDIDATE_SCANNED','NO_WEBCAST','FUTURE_EVENT_NO_REPLAY'}:
        fresh.status=cached.status
    fresh.states=list(dict.fromkeys((cached.states or [])+(fresh.states or [])))
    fresh.errors=list(cached.errors or [])+[e for e in (fresh.errors or []) if e not in (cached.errors or [])]
    # These runtime markers are intentionally outside Event.record().
    fresh.cached_match_method=method
    fresh.cached_record=cached
    return fresh

class EventRegistry:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'event_artifacts.json'
        self.data=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'version':1,'events':{},'manifests':{},'listings':{}}
        if not isinstance(self.data,dict) or self.data.get('version')!=1:raise ValueError('Invalid event registry; preserved without overwrite')

    def save(self):atomic_json(self.path,self.data)

    def record(self,stock,event,folder,checked=None,save=True):
        terminal=bool(event.local_pdf) or bool(event.protected_media_type) or event.status in PROTECTED or event.status in {'SKIPPED_OFFICIAL_TRANSCRIPT','SKIPPED_OFFICIAL_CAPTIONS'}
        if not terminal:return
        key=identity(stock,event);previous=self.data['events'].get(key,{})
        if event.resumed and previous and (not event.local_pdf or previous.get('folder')==str(Path(folder).resolve())):return
        if previous.get('event',{}).get('local_pdf') and not event.local_pdf:return
        now=checked or time.time()
        if previous.get('last_checked',0)>now:return
        history=list(previous.get('previous_artifacts',[]))
        if previous.get('event',{}).get('local_pdf') and previous.get('folder')!=str(Path(folder).resolve()):
            history.append({k:v for k,v in previous.items() if k!='previous_artifacts'})
        self.data['events'][key]={'previous_artifacts':history,'ticker':stock.ticker.upper(),'company_name':stock.company_name,'event':event.record(),
            'folder':str(Path(folder).resolve()),'last_checked':now,'status':event.status,'transcript_method':event.method,
            'pdf_path':str((Path(folder)/event.local_pdf).resolve()) if event.local_pdf else None,
            'json_path':str((Path(folder)/event.local_json).resolve()) if event.local_json else None,
            'media_identity':redact_url(event.media_url or event.webcast_url or ''),'model':event.transcription_model,'validated':False}
        if save:self.save()

    def import_history(self,stock):
        paths=list(self.root.glob('*/manifest.json'))
        if (self.root/'manifest.json').exists():paths.append(self.root/'manifest.json')
        changed=False
        for p in paths:
            stamp=p.stat().st_mtime_ns;name=str(p.resolve())
            if self.data['manifests'].get(name)==stamp:continue
            try:
                data=json.loads(p.read_text(encoding='utf-8'))
                if data.get('ticker','').upper()!=stock.ticker.upper() or company_key(data.get('company_name',''))!=company_key(stock.company_name):continue
                runs=data.get('run_history',[])+[data]
                for run in runs:
                    try:checked=datetime.strptime(run.get('run_timestamp',''),'%Y-%m-%d_%H-%M-%S').timestamp()
                    except ValueError:checked=p.stat().st_mtime
                    for raw in run.get('events',[]):
                        e=Event(**{k:v for k,v in raw.items() if k in Event.__dataclass_fields__})
                        self.record(stock,e,p.parent,checked,save=False)
                url=data.get('investor_relations_url')
                old_listing=self.data['listings'].get(stock.ticker.upper(),{})
                if url and data.get('events') and checked>old_listing.get('checked',0):
                    self.data['listings'][stock.ticker.upper()]={'company_name':company_key(stock.company_name),'ir_url':canonicalize_url(url),'checked':checked,'events':data['events']}
                self.data['manifests'][name]=stamp;changed=True
            except (OSError,ValueError,TypeError):continue
        if changed:self.save()

    def lookup(self,stock,event,config):
        if config.force:return None
        from .transcription import validate_artifacts
        from .pdf_utils import validate_pdf
        from .models import CollectionError
        entries=[entry for item in self.data['events'].values() for entry in [item]+item.get('previous_artifacts',[])]
        for item in sorted(entries,key=lambda x:x['last_checked'],reverse=True):
            if item['ticker']!=stock.ticker.upper() or company_key(item['company_name'])!=company_key(stock.company_name):continue
            previous=Event(**{k:v for k,v in item['event'].items() if k in Event.__dataclass_fields__})
            if not same_event(event,previous):continue
            age=time.time()-item['last_checked']
            if previous.status in PROTECTED or previous.protected_media_type:
                if 0<=age<=config.protected_cache_hours*3600:return 'protected',previous,Path(item['folder'])
                continue
            if previous.local_pdf:
                try:
                    folder=Path(item['folder']).resolve();pdf=(folder/previous.local_pdf).resolve()
                    if not pdf.is_relative_to(folder):continue
                    validate_pdf(pdf.read_bytes())
                    if previous.method in {'generated_transcript','official_captions'}:
                        jp=(folder/(previous.local_json or '')).resolve()
                        if not jp.is_relative_to(folder):continue
                        validate_artifacts(pdf,jp,config)
                        saved=json.loads(jp.read_text(encoding='utf-8'))
                        source=saved.get('source',{});meta=saved.get('event',{})
                        artifact=Event(meta.get('title',''),meta.get('date'),source.get('event_url',''),webcast_url=source.get('webcast_url'))
                        if saved.get('ticker','').upper()!=stock.ticker.upper() or not same_event(event,artifact):continue
                        if previous.method=='generated_transcript':
                            from .transcript_qa import assess
                            previous.transcript_qa=assess(saved['segments'],previous.duration_seconds)
                        elif not previous.caption_diagnostics.get('completeness_check'):continue
                    previous.status='TRANSCRIBED';item['validated']=True;self.save()
                    return 'artifact',previous,folder
                except (OSError,ValueError,TypeError,KeyError,CollectionError):continue
            elif previous.official_transcript_url and config.generated_only and 0<=age<=config.event_cache_hours*3600:
                return 'official',previous,Path(item['folder'])
        return None

    def cached_events(self,stock):
        """Load the newest durable event record for a company for reconciliation."""
        result=[];seen={}
        entries=[item for item in self.data['events'].values() if item.get('ticker')==stock.ticker.upper() and company_key(item.get('company_name',''))==company_key(stock.company_name)]
        for item in sorted(entries,key=lambda x:x.get('last_checked',0),reverse=True):
            try:event=Event(**{k:v for k,v in item.get('event',{}).items() if k in Event.__dataclass_fields__})
            except (TypeError,ValueError):continue
            key=(stable_event_url(event.event_url),event.date,normalized_title(event.title))
            if key not in seen:
                seen[key]=len(result);result.append(event)
            else:
                index=seen[key]
                result[index]=merge_event_state(result[index],event)
        return result

    def reconcile_events(self,stock,fresh_events):
        """Canonical reconciliation stage: fresh observations never replace enrichment."""
        cached=self.cached_events(stock)
        merged=[];matched=set()
        for fresh in fresh_events:
            match=next(((i,old,event_match_method(fresh,old)) for i,old in enumerate(cached) if i not in matched and event_match_method(fresh,old)),None)
            if match:
                i,old,method=match;matched.add(i)
                log_match=__import__('logging').getLogger(__name__)
                log_match.info('event_registry_match ticker=%s method=%s cached_key=%s fresh_event=%s',stock.ticker,method,identity(stock,old),fresh.title)
                merged.append(merge_event_state(fresh,old,method))
            else: merged.append(fresh)
        for i,old in enumerate(cached):
            if i not in matched:merged.append(old)
        return merged

    def listing(self,stock,ir_url,config):
        item=self.data['listings'].get(stock.ticker.upper())
        if not item or not item.get('events') or item['company_name']!=company_key(stock.company_name) or item['ir_url']!=canonicalize_url(ir_url):return None
        if not 0<=time.time()-item['checked']<=config.event_cache_hours*3600:return None
        return [Event(**{k:v for k,v in raw.items() if k in Event.__dataclass_fields__}) for raw in item['events']]

    def save_listing(self,stock,ir_url,events):
        self.data['listings'][stock.ticker.upper()]={'company_name':company_key(stock.company_name),'ir_url':canonicalize_url(ir_url),
            'checked':time.time(),'events':[e.record() for e in events]};self.save()
