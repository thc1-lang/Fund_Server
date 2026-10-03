"""Per-event provider observations, never an authorization or exclusion list."""
from pathlib import Path
import json
from .filesystem import atomic_json
from .registration import registrable_domain
from .event_outcomes import outcome

def path(root): return Path(root)/"webcast_provider_capabilities.json"
def load(root):
    try:
        data=json.loads(path(root).read_text(encoding="utf-8"))
        if not isinstance(data,dict):return {}
        normalized={}
        aliases={"media server":"media-server.com","webcasts.com":"webcasts.com","wall street webcasting":"wsw.com"}
        for key,item in data.items():
            if not isinstance(item,dict):continue
            domain=aliases.get(key.casefold(),registrable_domain(key))
            if not domain or "." not in domain:continue
            if domain not in normalized:normalized[domain]=item
            else:
                existing=normalized[domain]
                old=existing.setdefault("legacy_observations",dict(existing.get("observations",{})))
                for kind,count in item.get("observations",{}).items():old[kind]=old.get(kind,0)+count
        return normalized
    except (OSError,ValueError,TypeError): return {}

def observe(root,event):
    domain=registrable_domain(event.registration_destination_domain or event.webcast_url or "")
    if not domain: return
    data=load(root); item=data.setdefault(domain,{"provider":event.provider,"observations":{},"events":{}})
    if "events" not in item:item["legacy_observations"]=dict(item.get("observations",{}))
    records=item.setdefault("events",{})
    keys=[]
    if event.method in {"official_transcript","official_captions"}: keys.append(event.method)
    if event.status=="TRANSCRIBED" and event.method=="generated_transcript": keys.append("generated_transcript_success")
    if event.protected_media_type: keys.append("protected")
    elif event.media_type: keys.append(event.media_type)
    if outcome(event) in {"PLAYER_BLOCKED","PLAYER_HTTP_403","REGISTRATION_APPROVAL_REQUIRED","AUTH_REQUIRED","CAPTCHA_REQUIRED"}: keys.append("blocked")
    key=event.event_url+"|"+(event.date or "")
    prior=records.get(key,[])
    records[key]=list(dict.fromkeys(prior+keys))
    obs=dict(item.get("legacy_observations",{}))
    obs["events_inspected"]=obs.get("events_inspected",0)+len(records)
    for values in records.values():
        for kind in values:obs[kind]=obs.get(kind,0)+1
    item["observations"]=obs;item["generated_transcript_successes"]=obs.get("generated_transcript_success",0)
    atomic_json(path(root),data)

def integration_penalty(root,domain):
    obs=load(root).get(registrable_domain(domain or ""),{}).get("observations",{})
    return min(60,25*obs.get("protected",0)+10*obs.get("blocked",0))-min(60,20*sum(obs.get(k,0) for k in ("audio","video","hls","dash","generated_transcript_success")))
