"""Inspect media exposed by the page/frames and normal browser responses."""
import json
import re
from urllib.parse import urlsplit
from .base import WebcastProviderAdapter
from ..registration import register
from ..event_models import MediaCandidate
from ..urls import public_url

MEDIA_TYPES={".mp3":"audio",".m4a":"audio",".aac":"audio",".wav":"audio",".ogg":"audio",".mp4":"video",".webm":"video",".m3u8":"hls",".mpd":"dash"}
NEGATIVE=re.compile(r"tracking|beacon|pixel|advert|sound.effect|analytics",re.I)

def media_type(url, content_type=""):
    ct=content_type.lower()
    if "mpegurl" in ct:return "hls"
    if "dash+xml" in ct:return "dash"
    if ct.startswith("audio/"):return "audio"
    if ct.startswith("video/"):return "video"
    path=urlsplit(url).path.lower()
    return next((t for ext,t in MEDIA_TYPES.items() if path.endswith(ext)),None)

def candidate(url,content_type="",source="player",frame_url="",duration=None,size=None):
    if content_type.lower().startswith("video/mp2t") or re.search(r"\.(?:ts|m4s)(?:[?]|$)",url,re.I):return None
    kind=media_type(url,content_type)
    if not kind or not public_url(url) or NEGATIVE.search(url):return None
    if duration is not None and duration<30:return None
    score={"audio":130,"hls":75,"dash":70,"video":15}[kind]
    score+=10 if re.search(r"audio|webcast|archive|replay|event|stream|presentation",url,re.I) else 0
    score+=20 if source=="media_element" else 5
    score+=20 if duration and duration>=60 else 0
    return MediaCandidate(url,kind,content_type,source,frame_url,duration,size,score)

def structured_sources(value, base=""):
    from urllib.parse import urljoin
    results=[]
    if isinstance(value,dict):
        for k,v in value.items():
            if isinstance(v,str) and (k.lower() in {"src","source","url","file","contenturl","streamurl","audiourl","videourl","hls","dash"}):
                c=candidate(urljoin(base,v),source="player_configuration",frame_url=base)
                if c:results.append(c)
            elif isinstance(v,(dict,list)):results.extend(structured_sources(v,base))
    elif isinstance(value,list):
        for v in value:results.extend(structured_sources(v,base))
    return results

class GenericWebcastAdapter:
    name="generic"
    async def matches(self,page):return True
    async def handle_registration(self,page,profile,event,submitted):return await register(page,profile,event,submitted)
    async def find_official_transcript(self,page):
        for frame in page.frames:
            records=await frame.locator("a[href]").evaluate_all("els=>els.map(e=>({url:e.href,label:[e.innerText,e.title,e.getAttribute('aria-label')].join(' ')}))")
            for r in records:
                if re.search(r"transcript",r["label"],re.I) and public_url(r["url"]):return r["url"]
        return None
    async def discover_media(self,page):
        results=[]
        for frame in page.frames:
            values=await frame.locator("audio,video,source").evaluate_all("els=>els.map(e=>({url:e.currentSrc||e.src,type:e.type||'',duration:Number.isFinite(e.duration)?e.duration:null}))")
            for v in values:
                c=candidate(v["url"],v["type"],"media_element",frame.url,v["duration"])
                if c:results.append(c)
            scripts=await frame.locator('script[type="application/ld+json"],script[type="application/json"]').all_text_contents()
            for script in scripts:
                try:results.extend(structured_sources(json.loads(script),frame.url))
                except (ValueError,TypeError):pass
            # Reading own-page state is ordinary player inspection; no code strings evaluated.
            state=await frame.evaluate("() => {try{return JSON.parse(JSON.stringify(window.__INITIAL_STATE__||window.playerConfig||{}))}catch{return {}}}")
            results.extend(structured_sources(state,frame.url))
            attributes=await frame.locator("[data-src],[data-config],[data-sources]").evaluate_all("els=>els.flatMap(e=>[e.dataset.src,e.dataset.config,e.dataset.sources].filter(Boolean))")
            for value in attributes:
                try:results.extend(structured_sources(json.loads(value),frame.url))
                except ValueError:
                    c=candidate(value,source="player_configuration",frame_url=frame.url)
                    if c:results.append(c)
        return results
