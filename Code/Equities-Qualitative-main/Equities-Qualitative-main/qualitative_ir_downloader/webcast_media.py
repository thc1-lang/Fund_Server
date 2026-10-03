"""Bounded session-preserving media acquisition; FFmpeg only reads local files."""
import copy
import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
import httpx
from .models import CollectionError
from .privacy import redact_url, safe_text
from .urls import public_url
log=logging.getLogger(__name__)

def ffmpeg_path(config):
    path=config.ffmpeg or shutil.which("ffmpeg")
    if not path:
        try:
            import imageio_ffmpeg
            path=imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError,RuntimeError):pass
    if not path or not Path(path).is_file():raise CollectionError("DEPENDENCY_MISSING","FFmpeg missing; install FFmpeg or imageio-ffmpeg; see README")
    return str(path)

def media_info(path):
    import av
    try:
        with av.open(str(path)) as container:
            audio=next((s for s in container.streams if s.type=="audio"),None)
            if audio is None:raise ValueError("No audio stream")
            duration=float(container.duration/av.time_base) if container.duration else float(audio.duration*audio.time_base) if audio.duration else None
            return {"duration":duration,"sample_rate":audio.codec_context.sample_rate,"channels":audio.codec_context.channels,"codec":audio.codec_context.name}
    except Exception as exc:raise CollectionError("MEDIA_INVALID",f"Media cannot be decoded: {type(exc).__name__}") from exc

def validate_duration(duration,config):
    if duration is None or duration<config.min_duration:raise CollectionError("MEDIA_INVALID","Media duration is missing or too short for a webcast")
    if duration>config.max_duration:raise CollectionError("SAFETY_LIMIT_REACHED","Media duration exceeds configured limit")

async def run_ffmpeg(config,args):
    process=await asyncio.create_subprocess_exec(ffmpeg_path(config),"-hide_banner","-nostdin","-y",*args,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    try:
        _,stderr=await asyncio.wait_for(process.communicate(),config.process_timeout)
    except (asyncio.TimeoutError,asyncio.CancelledError):
        process.kill();await process.wait();raise
    if process.returncode:raise CollectionError("MEDIA_DOWNLOAD_FAILED",safe_text(stderr.decode("utf-8","replace")[-1200:]))

class MediaAcquirer:
    def __init__(self,context,config,folder):
        self.context=context;self.config=config;self.folder=Path(folder);self.total_bytes=0;self.count=0
        self.folder.mkdir(parents=True,exist_ok=True)

    async def fetch(self,url,target,referer="",headers=None,limit=None):
        cookies=httpx.Cookies()
        for c in await self.context.cookies():cookies.set(c["name"],c["value"],domain=c["domain"],path=c["path"])
        safe_headers={k:v for k,v in (headers or {}).items() if k.lower() in {"authorization","accept","origin","user-agent"}}
        if getattr(self,"credential_host",urlsplit(url).hostname)!=urlsplit(url).hostname:
            safe_headers={k:v for k,v in safe_headers.items() if k.lower()!="authorization"}
        if referer:safe_headers["Referer"]=referer
        async with httpx.AsyncClient(cookies=cookies,timeout=self.config.media_timeout) as client:
            current=url
            for _ in range(8):
                if not public_url(current):raise CollectionError("MEDIA_DOWNLOAD_FAILED","Media URL is not public HTTP(S)")
                async with client.stream("GET",current,headers=safe_headers) as r:
                    if r.is_redirect:
                        nxt=urljoin(current,r.headers.get("location",""))
                        if urlsplit(nxt).hostname!=urlsplit(current).hostname:safe_headers.pop("authorization",None);safe_headers.pop("Authorization",None)
                        current=nxt;continue
                    if r.status_code in {401,403}:raise CollectionError("AUTH_REQUIRED" if r.status_code==401 else "MEDIA_BLOCKED",f"Media HTTP {r.status_code}: {redact_url(current)}")
                    if r.status_code>=400:raise CollectionError("MEDIA_DOWNLOAD_FAILED",f"Media HTTP {r.status_code}: {redact_url(current)}")
                    written=0
                    with target.open("wb") as output:
                        async for chunk in r.aiter_bytes():
                            written+=len(chunk);self.total_bytes+=len(chunk)
                            if self.total_bytes>self.config.max_media_bytes or limit and written>limit:raise CollectionError("SAFETY_LIMIT_REACHED","Media byte limit exceeded")
                            output.write(chunk)
                    return current,r.headers.get("content-type","")
        raise CollectionError("MEDIA_DOWNLOAD_FAILED","Media redirect limit")

    async def text(self,url,referer="",headers=None):
        self.count+=1;path=self.folder/f"manifest-{self.count}.txt"
        final,ct=await self.fetch(url,path,referer,headers,4*1024*1024)
        return path.read_text(encoding="utf-8-sig"),final

    async def hls(self,url,referer,headers=None,depth=0):
        if depth>4:raise CollectionError("MEDIA_DOWNLOAD_FAILED","HLS nesting limit")
        text,base=await self.text(url,referer,headers)
        if not text.lstrip().startswith("#EXTM3U"):raise CollectionError("MEDIA_INVALID","Invalid HLS manifest")
        if re.search(r'#EXT-X-(?:SESSION-)?KEY:(?![^\n]*METHOD=NONE)',text,re.I):raise CollectionError("DRM_PROTECTED","Encrypted HLS is not processed")
        lines=text.splitlines()
        if "#EXT-X-STREAM-INF" in text:
            audio=next((re.search(r'URI="([^"]+)"',l).group(1) for l in lines if l.startswith("#EXT-X-MEDIA:") and 'TYPE=AUDIO' in l and re.search(r'URI="([^"]+)"',l)),None)
            variants=[lines[i+1].strip() for i,l in enumerate(lines[:-1]) if l.startswith("#EXT-X-STREAM-INF")]
            if not audio and not variants:raise CollectionError("MEDIA_INVALID","No HLS rendition")
            return await self.hls(urljoin(base,audio or variants[0]),base,headers,depth+1)
        if "#EXT-X-ENDLIST" not in text:raise CollectionError("NO_REPLAY_AVAILABLE","HLS is live or incomplete; replay not finalized")
        output=[];segments=0
        for line in lines:
            if line.startswith("#EXT-X-KEY"):continue
            if line.startswith("#EXT-X-MAP"):
                match=re.search(r'URI="([^"]+)"',line)
                if match:
                    local=self.folder/f"hls-init-{segments}.mp4";await self.fetch(urljoin(base,match.group(1)),local,base,headers)
                    line=line.replace(match.group(1),local.name)
            elif line and not line.startswith("#"):
                segments+=1
                if segments>20000:raise CollectionError("SAFETY_LIMIT_REACHED","HLS segment limit")
                suffix=Path(urlsplit(line.strip()).path).suffix.lower()
                local=self.folder/f"hls-{segments:06}.part"
                _,ct=await self.fetch(urljoin(base,line.strip()),local,base,headers)
                if suffix not in {".ts",".m4s",".mp4",".aac",".mp3"}:
                    with local.open("rb") as stream:prefix=stream.read(512)
                    if prefix[:1]==b"G" and (len(prefix)<189 or prefix[188:189]==b"G"):suffix=".ts"
                    elif prefix[4:8] in {b"ftyp",b"styp",b"moof",b"sidx"}:suffix=".m4s"
                    elif prefix[:2] in {b"\xff\xf1",b"\xff\xf9"}:suffix=".aac"
                    elif prefix.startswith(b"ID3") or ct.startswith("audio/mpeg"):suffix=".mp3"
                    else:raise CollectionError("MEDIA_UNSUPPORTED","Unknown HLS segment format")
                final=local.with_suffix(suffix);local.replace(final);line=final.name

                if segments%100==0:log.info("Acquiring HLS: %s segments",segments)
            output.append(line)
        target=self.folder/"replay.m3u8";target.write_text("\n".join(output),encoding="utf-8")
        return target

    async def dash(self,url,referer,headers=None):
        text,base=await self.text(url,referer,headers)
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():raise CollectionError("MEDIA_INVALID","DASH entities are not supported")
        try:root=ET.fromstring(text)
        except ET.ParseError as exc:raise CollectionError("MEDIA_INVALID","Malformed DASH manifest") from exc
        if any(x.tag.split("}")[-1]=="ContentProtection" for x in root.iter()):raise CollectionError("DRM_PROTECTED","Protected DASH is not processed")
        if root.get("type")=="dynamic":raise CollectionError("NO_REPLAY_AVAILABLE","Dynamic DASH is not a finalized replay")
        ns=root.tag.split("}")[0]+"}" if "}" in root.tag else ""
        def child(node,name):return node.find(ns+name)
        def extend(node,current):
            b=child(node,"BaseURL")
            return urljoin(current,b.text.strip()) if b is not None and b.text else current
        current=extend(root,base)
        period=child(root,"Period")
        if period is None:raise CollectionError("MEDIA_INVALID","No DASH period")
        if len(root.findall(ns+"Period"))>1:raise CollectionError("MEDIA_UNSUPPORTED","Multi-period DASH requires an adapter; no truncated transcript created")
        current=extend(period,current)
        adaptations=period.findall(ns+"AdaptationSet")
        adaptation=next((a for a in adaptations if a.get("contentType")=="audio" or "audio" in a.get("mimeType","") or any("audio" in r.get("mimeType","") for r in a.findall(ns+"Representation"))),None)
        if adaptation is None:raise CollectionError("MEDIA_INVALID","No DASH audio adaptation")
        current=extend(adaptation,current)
        reps=adaptation.findall(ns+"Representation")
        if not reps:raise CollectionError("MEDIA_INVALID","No DASH representation")
        rep=max(reps,key=lambda r:int(r.get("bandwidth","0")));current=extend(rep,current)
        template=child(rep,"SegmentTemplate")
        if template is None:template=child(adaptation,"SegmentTemplate")
        segmentlist=child(rep,"SegmentList")
        if segmentlist is None:segmentlist=child(adaptation,"SegmentList")
        if template is None and segmentlist is None:
            target=self.folder/"dash-audio.mp4";await self.fetch(current,target,referer,headers);return target
        urls=[];timescale="1";duration=None;timeline_copy=None
        if segmentlist is not None:
            timescale=segmentlist.get("timescale","1");duration=segmentlist.get("duration")
            init=child(segmentlist,"Initialization")
            initialization=init.get("sourceURL") if init is not None else None
            urls=[n.get("media") for n in segmentlist.findall(ns+"SegmentURL")]
        else:
            timescale=template.get("timescale","1");duration=template.get("duration");start=int(template.get("startNumber","1"))
            def expand(pattern,number,time=0):
                value=pattern.replace("$RepresentationID$",rep.get("id","")).replace("$Bandwidth$",rep.get("bandwidth",""))
                value=re.sub(r'\$Number(?:%0(\d+)d)?\$',lambda m:str(number).zfill(int(m.group(1) or 0)),value)
                return value.replace("$Time$",str(time)).replace("$$","$")
            initialization=expand(template.get("initialization",""),start)
            timeline=child(template,"SegmentTimeline");points=[]
            if timeline is not None:
                timeline_copy=copy.deepcopy(timeline)
                t=0
                for entry in timeline:
                    t=int(entry.get("t",str(t)));d=int(entry.get("d","0"));repeat=int(entry.get("r","0"))
                    if repeat<0:raise CollectionError("MEDIA_UNSUPPORTED","Open-ended DASH timeline requires an adapter")
                    for _ in range(repeat+1):points.append(t);t+=d
            elif duration:
                match=re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",root.get("mediaPresentationDuration",period.get("duration","")))
                if not match:raise CollectionError("MEDIA_INVALID","DASH duration unavailable")
                import math
                seconds=sum(float(v or 0)*w for v,w in zip(match.groups(),[3600,60,1]))
                points=list(range(math.ceil(seconds*int(timescale)/int(duration))))
            if not points or len(points)>20000:raise CollectionError("MEDIA_INVALID","DASH segment count invalid")
            urls=[expand(template.get("media",""),start+i,t) for i,t in enumerate(points)]
        # Produce a local SegmentList; FFmpeg has no network protocols enabled.
        for a in adaptations:
            if a is not adaptation:period.remove(a)
        for r in reps:
            if r is not rep:adaptation.remove(r)
        for node in (root,period,adaptation,rep):
            for c in list(node):
                if c.tag.split("}")[-1] in {"BaseURL","SegmentTemplate","SegmentList"}:node.remove(c)
        local_list=ET.SubElement(rep,ns+"SegmentList",{"timescale":timescale,**({"duration":duration} if duration else {})})
        if timeline_copy is not None:local_list.append(timeline_copy)
        if initialization:
            path=self.folder/"dash-init.mp4";await self.fetch(urljoin(current,initialization),path,referer,headers)
            ET.SubElement(local_list,ns+"Initialization",{"sourceURL":path.name})
        for i,u in enumerate(urls):
            path=self.folder/f"dash-{i:06}.m4s";await self.fetch(urljoin(current,u),path,referer,headers)
            ET.SubElement(local_list,ns+"SegmentURL",{"media":path.name})
        if ns:ET.register_namespace("",ns[1:-1])
        path=self.folder/"replay.mpd";ET.ElementTree(root).write(path,encoding="utf-8",xml_declaration=True);return path

    async def acquire(self,candidate):
        acquisition_started=time.perf_counter()
        log.info("FFmpeg executable: %s",ffmpeg_path(self.config))
        version=await asyncio.create_subprocess_exec(ffmpeg_path(self.config),"-version",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        output,_=await asyncio.wait_for(version.communicate(),30)
        if version.returncode:raise CollectionError("DEPENDENCY_MISSING","FFmpeg version check failed")
        log.info("FFmpeg: %s",output.decode("utf-8",errors="replace").splitlines()[0])
        self.credential_host=urlsplit(candidate.url).hostname
        log.info("Acquiring %s media from %s",candidate.media_type,redact_url(candidate.url))
        if candidate.media_type=="hls":source=await self.hls(candidate.url,candidate.frame_url,candidate.headers)
        elif candidate.media_type=="dash":source=await self.dash(candidate.url,candidate.frame_url,candidate.headers)
        else:
            source=self.folder/"source.media"
            _,ct=await self.fetch(candidate.url,source,candidate.frame_url,candidate.headers)
            if ct.startswith("text/") or "json" in ct:raise CollectionError("MEDIA_INVALID","Media endpoint returned text instead of media")
        normalized=self.folder/"normalized.wav"
        if candidate.media_type not in {"hls","dash"}:
            original=await asyncio.to_thread(media_info,source)
            validate_duration(original["duration"],self.config)
            log.info("Input media duration: %.1f seconds",original["duration"])
        log.info("Normalizing webcast audio to mono 16 kHz PCM WAV")
        acquisition_seconds=time.perf_counter()-acquisition_started
        normalization_started=time.perf_counter()
        args=["-protocol_whitelist","file,pipe"]
        if candidate.media_type=="hls":args += ["-allowed_extensions","ALL"]
        if candidate.media_type=="dash":args += ["-f","dash"]
        args += ["-i",str(source),"-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(normalized)]
        await run_ffmpeg(self.config,args)
        info=await asyncio.to_thread(media_info,normalized);validate_duration(info["duration"],self.config)
        info['timings']={'media_acquisition':acquisition_seconds,'audio_normalize':time.perf_counter()-normalization_started}
        log.info("FFmpeg result: success; normalized duration %.1fs; %s Hz; %s channels; %s",info["duration"],info["sample_rate"],info["channels"],info["codec"])
        return normalized,info
