"""Durable, monotonic storage for verified Investor Relations provenance."""
from datetime import datetime, timezone
from pathlib import Path
import json
from .filesystem import atomic_json
from .models import IRCandidate, Stock
from .urls import public_url, rejected_domain
from .scoring import company_key

def cache_path(root: Path) -> Path:
    return Path(root) / "verified_ir.json"

def read_store(root):
    path = cache_path(root)
    if not path.exists(): return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict): raise ValueError("Invalid verified IR store; file preserved")
    return data

def candidate_from(item, stock):
    if not isinstance(item, dict) or item.get("official") is not True or item.get("invalidated_explicitly"): return None
    if company_key(item.get("company_name", "")) != company_key(stock.company_name): return None
    url = item.get("official_ir_url") or item.get("url")
    try:
        score = int(item.get("officiality_score", item.get("officiality",0)))
        if not url or not public_url(url) or rejected_domain(url) or score < 90: return None
    except (ValueError, TypeError): return None
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e,str) for e in evidence): return None
    return IRCandidate(url, score, evidence, True, corporate_url=item.get("official_corporate_url") or item.get("corporate_url"))

def load(root, stock):
    try: return candidate_from(read_store(root).get(stock.ticker.upper()), stock)
    except (OSError, ValueError, TypeError): return None

def invalidated(root, ticker):
    try: return read_store(root).get(ticker.upper(), {}).get("invalidated_explicitly") is True
    except (OSError, ValueError, TypeError, AttributeError): return False

def upsert(root, stock, candidate, *, source="discovery", confirmed=False, verified_at=None):
    item = {"company_name":stock.company_name, "url":candidate.url, "official":candidate.official,
            "officiality_score":candidate.officiality_score, "evidence":candidate.evidence}
    if not candidate_from(item, stock): return
    data = read_store(root)  # Never silently replace a damaged store.
    key=stock.ticker.upper(); old=data.get(key,{})
    previous=candidate_from(old,stock)
    if old.get("official") and not previous: raise ValueError("Conflicting cached identity requires explicit repair")
    if previous and (previous.officiality_score > candidate.officiality_score or previous.url != candidate.url): return
    now=datetime.now(timezone.utc).isoformat()
    first=old.get("first_verified_at") or old.get("verified_at") or verified_at or (now if source=="discovery" else None)
    data[key]={**old, "ticker":stock.ticker,"company_name":stock.company_name,"official_ir_url":candidate.url,
        "official_corporate_url":candidate.corporate_url or old.get("official_corporate_url"),
        "official":True,"invalidated_explicitly":False,"officiality_score":candidate.officiality_score,
        "evidence":list(dict.fromkeys(old.get("evidence",[])+candidate.evidence)),
        "verification_source":old.get("verification_source") or source,
        "first_verified_at":first,"verified_at":first,"first_recorded_at":old.get("first_recorded_at",now),
        "last_confirmed_at":now if confirmed else old.get("last_confirmed_at")}
    atomic_json(cache_path(root),data)

def recover(root, stock, seed_root=None):
    existing=load(root,stock)
    if existing or invalidated(root,stock.ticker): return existing
    records=[]
    if seed_root:
        seed=load(seed_root,stock)
        if seed:
            old=read_store(seed_root)[stock.ticker.upper()]
            records.append((seed.officiality_score,0,seed,"verified_seed",old.get("first_verified_at") or old.get("verified_at")))
    for path in Path(root).rglob("manifest.json"):
        try:
            data=json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data,dict) or str(data.get("ticker","")).casefold()!=stock.ticker.casefold(): continue
            items=list(data.get("discovery",[]))
            if data.get("ir_official"):
                items.append({"url":data.get("investor_relations_url"),"official":True,
                              "officiality_score":data.get("officiality_score",0),"evidence":data.get("ir_evidence",[])})
            for item in items:
                if not isinstance(item,dict): continue
                candidate=candidate_from({**item,"company_name":data.get("company_name","")},stock)
                if candidate:
                    candidate.corporate_url=candidate.corporate_url or data.get("ecosystem",{}).get("official_corporate_url")
                    records.append((candidate.officiality_score,path.stat().st_mtime,candidate,f"historical_manifest:{path}",data.get("run_timestamp")))
        except (OSError,ValueError,TypeError,AttributeError): continue
    if not records: return None
    _,_,candidate,source,first=max(records,key=lambda r:r[:2])
    try:
        upsert(root,stock,candidate,source=source,verified_at=first)
    except (OSError,ValueError) as exc:
        __import__("logging").getLogger(__name__).warning("Verified identity recovered; cache write failed and existing file preserved: %s",exc)
    return candidate

def remove(root,ticker):
    data=read_store(root); old=data.get(ticker.upper(),{})
    data[ticker.upper()]={**old,"official":False,"invalidated_explicitly":True}
    atomic_json(cache_path(root),data)

if __name__ == "__main__":
    import argparse
    from .models import AccessResult
    parser = argparse.ArgumentParser(description="Manage verified IR provenance")
    parser.add_argument("command", choices=("set", "remove")); parser.add_argument("--root", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--ticker", required=True); parser.add_argument("--company"); parser.add_argument("--ir-url"); parser.add_argument("--corporate-url"); parser.add_argument("--officiality", type=int, default=100)
    args = parser.parse_args()
    if args.command == "remove": remove(args.root, args.ticker)
    else:
        if not args.company or not args.ir_url: parser.error("set requires --company and --ir-url")
        stock = Stock(args.ticker, args.company, "", 0)
        upsert(args.root, stock, IRCandidate(args.ir_url, args.officiality, ["Explicit operator verification"], True, corporate_url=args.corporate_url, content_validated=True))
