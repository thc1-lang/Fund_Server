from __future__ import annotations

import hashlib, json, re
from datetime import date
from pathlib import Path
from typing import Any

from .evidence_links import ArtifactReader, SourceCatalog, record_id
from .models import CompanyResearchDossier, ReportFact

REPORT_VERSION = "research-output-v1"
COMPANY_NAMES = {"ZM":"Zoom Communications, Inc.", "PLTR":"Palantir Technologies Inc.", "EXEL":"Exelixis", "INCY":"Incyte Corporation"}
CIKS = {"ZM":"0001585521", "PLTR":"0001321655", "EXEL":"0000939767", "INCY":"0000879169"}

def _norm(s: Any) -> str: return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
def _date(row: dict[str,Any]) -> str:
    for k in ("as_of_fiscal_year","fiscal_year","publication_date","event_date","filing_date","as_of_date","created_at","source_date","effective_date","issued_date","commitment_date"):
        v=row.get(k)
        if v is not None:
            return str(v)
    return ""
def _ticker_match(row: dict[str,Any], ticker: str) -> bool:
    t=ticker.upper(); cik=CIKS.get(t,""); vals=[]
    for k in ("ticker","issuer_ticker","board_ticker","employer_ticker","target_ticker"):
        if row.get(k): vals.append(str(row[k]).upper())
    if t in vals: return True
    for k in ("issuer_cik","board_cik","employer_cik"):
        if row.get(k) and str(row[k]).lstrip("0") == cik.lstrip("0"): return True
    text=" ".join(str(row.get(k,"")) for k in ("company_name","board_company","employer","local_path","local_source_path","source_local_path","source_url"))
    n=_norm(text)
    names={"ZM":["zoomcommunications","zoom"],"PLTR":["palantirtechnologies","palantir"],"EXEL":["exelixis"],"INCY":["incyte"]}.get(t,[t.lower()])
    return any(x in n for x in names)
def _rows(reader: ArtifactReader, subdir: str, file: str, ticker: str) -> list[dict[str,Any]]:
    return [r for r in reader.rows(subdir,file) if _ticker_match(r,ticker)]
def _latest(rows: list[dict[str,Any]], key: str="") -> dict[str,Any] | None:
    if not rows:return None
    return sorted(rows,key=lambda r: (_date(r), str(r.get(key or "record_id",""))), reverse=True)[0]
def _unique(rows: list[dict[str,Any]], id_key: str) -> list[dict[str,Any]]:
    seen=set(); out=[]
    for r in rows:
        i=record_id(r, "row:"+hashlib.sha1(json.dumps(r,sort_keys=True,default=str).encode()).hexdigest()[:12])
        if i not in seen: seen.add(i); out.append(r)
    return out
def _pct(x: Any) -> float|None:
    try:return None if x is None else round(float(x)*100,2) if abs(float(x))<=1 else round(float(x),2)
    except:return None

class Builder:
    def __init__(self, ticker: str, as_of_date: str, profile: str, artifacts_root: str|Path):
        self.ticker=ticker.upper(); self.as_of=as_of_date; self.profile=profile; self.reader=ArtifactReader(artifacts_root); self.catalog=SourceCatalog.create(); self.used=[]
        self.company_name=COMPANY_NAMES.get(self.ticker,self.ticker)
        self.qstates=_rows(self.reader,"qualitative_analysis","qualitative_states.jsonl",self.ticker)
        self.changes=_rows(self.reader,"qualitative_analysis","temporal_changes.jsonl",self.ticker)
        self.claims=_rows(self.reader,"qualitative_analysis","claims.jsonl",self.ticker)
        self.documents=_rows(self.reader,"qualitative_analysis","documents.jsonl",self.ticker)
        self._doc_by_id={r.get("document_id"):r for r in self.documents}
    def cite(self,row:dict[str,Any],typ:str, snippet:str="") -> list[int]:
        if not (row.get("source_url") or row.get("source_urls") or row.get("url")):
            related=(row.get("supporting_claim_ids") or row.get("to_claim_ids") or row.get("from_claim_ids") or row.get("source_claim_ids") or [])
            claim=next((c for c in self.claims if c.get("claim_id") in related),None)
            if claim:
                row=dict(row); row["source_url"]=claim.get("source_url"); row["local_path"]=claim.get("local_path"); row["publication_date"]=claim.get("period_label") or claim.get("created_at")
        rid=record_id(row,"row:"+hashlib.sha1(json.dumps(row,sort_keys=True,default=str).encode()).hexdigest()[:12])
        ref=self.catalog.add(row,rid,typ,snippet or str(row.get("summary") or row.get("evidence_text") or row.get("claim_text") or ""))
        self.used.append(rid); return [ref.number] if ref.number else []
    def state_facts(self, section: str, states: list[dict[str,Any]], limit: int=12) -> list[ReportFact]:
        out=[]
        # parent state rows are the audited display states; child/topic rows are supporting detail
        parents=[s for s in states if not s.get("topic") and not s.get("subtopic")] or states
        parents=sorted(parents,key=lambda s:(-(float(s.get("confidence") or 0)), s.get("dimension","") ,record_id(s,"")))[:limit]
        for s in parents:
            dim=str(s.get("dimension") or s.get("topic") or "unknown").replace("_"," ").title(); state=str(s.get("current_state") or "unknown"); trend=str(s.get("trend") or "unknown")
            summary=s.get("summary") or f"{dim} is {state} with a {trend} trend."
            text=f"{dim}: {summary} Current state: {state}; trend: {trend}."
            ev=self.cite(s,"qualitative_state",str(summary))
            out.append(ReportFact(text,section,[record_id(s,"")],ev,float(s.get("confidence")) if s.get("confidence") is not None else None,trend))
        return out
    def build_business(self) -> tuple[dict[str,Any],dict[str,Any]]:
        material={"demand","revenue","margins","customer_growth","customers","product","costs","cash_flow","guidance","risks","pricing","capex","volume","strategy","catalysts"}
        states=[s for s in self.qstates if str(s.get("dimension") or "").lower() in material and (not s.get("topic") and not s.get("subtopic")) and str(s.get("current_state") or "unknown").lower() != "unknown"]
        quality=self.state_facts("business_quality",states,12)
        traj=[]
        for s in sorted(states,key=lambda x:(x.get("dimension", ""),-float(x.get("confidence") or 0))):
            dim=str(s.get("dimension") or "").replace("_"," ").title(); trend=str(s.get("trend") or "unknown")
            temporal=[c for c in self.changes if str(c.get("dimension"))==str(s.get("dimension")) and c.get("direction") not in (None,"unknown")]
            text=f"{dim}: {trend}. Current state: {s.get('current_state') or 'unknown'}."
            if temporal:
                c=sorted(temporal,key=lambda x:_date(x),reverse=True)[0]; text+=f" Latest directional change: {c.get('change_type') or 'change'} ({c.get('from_period') or 'prior'} to {c.get('to_period') or 'current'})."
                ids=[record_id(s,""),record_id(c,"")]; src=self.cite(s,"qualitative_state"); src+=self.cite(c,"temporal_change")
            else: ids=[record_id(s,"")]; src=self.cite(s,"qualitative_state")
            traj.append(ReportFact(text,"business_trajectory",ids,src,float(s.get("confidence") or 0),trend))
        return {"facts":[f.to_dict() for f in quality],"supported_dimensions":sorted({str(s.get('dimension')) for s in states})},{"facts":[f.to_dict() for f in traj],"supported_dimensions":sorted({str(s.get('dimension')) for s in states})}
    def build_management(self)->dict[str,Any]:
        root="management_intelligence"
        people=_rows(self.reader,root,"people.jsonl",self.ticker); roles=_rows(self.reader,root,"roles.jsonl",self.ticker); changes=_rows(self.reader,root,"role_changes.jsonl",self.ticker)
        diag=_rows(self.reader,root,"guidance_coverage_diagnostics.jsonl",self.ticker); snaps=_rows(self.reader,root,"track_record_snapshots.jsonl",self.ticker)
        guidance=_rows(self.reader,root,"guidance_commitments.jsonl",self.ticker); outcomes=_rows(self.reader,root,"guidance_outcomes.jsonl",self.ticker)
        strategic=_rows(self.reader,root,"strategic_commitments.jsonl",self.ticker); capital=_rows(self.reader,root,"capital_allocation_events.jsonl",self.ticker)
        current_roles=[]; leaders=[]
        people_by_id={p.get("person_id"):p for p in people}
        for r in roles:
            if r.get("is_current"):
                current_roles.append({"title":r.get("title_as_reported") or r.get("role"),"person_id":r.get("person_id"),"source_record_ids":[record_id(r,"")],"source_numbers":self.cite(r,"management_role")})
                person=people_by_id.get(r.get("person_id"))
                if person and any(c in (str(r.get("role_category") or "").upper(),) or c in str(person.get("role_categories") or []) for c in ("CEO","CFO")):
                    leaders.append({"name":person.get("full_name"),"role":r.get("title_as_reported") or r.get("role"),"source_record_ids":[record_id(person,"")],"source_numbers":self.cite(person,"management_person")})
        changes_out=[]
        for c in sorted(changes,key=lambda x:_date(x),reverse=True):
            changes_out.append({"person":c.get("person_name"),"event":c.get("event_type"),"role":c.get("role"),"effective_date":c.get("effective_date"),"future":bool(c.get("effective_date") and c.get("effective_date")>self.as_of),"source_record_ids":[record_id(c,"")],"source_numbers":self.cite(c,"management_role_change")})
        d=_latest(diag) or {}; srows=[]
        for s in snaps[:12]: srows.append({k:s.get(k) for k in ("person_id","role","tenure_duration_days","guidance_records","guidance_resolved","guidance_met","guidance_missed","strategic_commitments","strategic_unresolved","tenure_overlap_count","coverage_limitations")})
        seen_leaders=set(); leaders=[x for x in leaders if not (x["source_record_ids"][0] in seen_leaders or seen_leaders.add(x["source_record_ids"][0]))]
        return {"current_roles":current_roles,"leaders":leaders,"people_count":len(people),"leadership_changes":changes_out,"guidance": {"diagnostic":d,"diagnostic_source_record_ids":[record_id(d,"")] if d else [],"diagnostic_source_numbers":self.cite(d,"guidance_coverage") if d else [],"commitments":len(guidance),"outcomes":len(outcomes),"resolved":sum(1 for x in outcomes if x.get("outcome") not in (None,"UNRESOLVED"))},"strategic_commitments":len(strategic),"capital_allocation_events":len(capital),"track_record":srows,"attribution_note":"Company-level outcomes with TENURE_OVERLAP are not treated as person-level management credit or blame."}
    def build_insiders(self)->dict[str,Any]:
        root="insider_intelligence"; align=_rows(self.reader,root,"alignment_snapshots.jsonl",self.ticker); own=_rows(self.reader,root,"ownership.jsonl",self.ticker); tx=_rows(self.reader,root,"transactions.jsonl",self.ticker); filings=_rows(self.reader,root,"filings.jsonl",self.ticker)
        a=_latest(align) or {}; discretionary=[x for x in tx if x.get("is_open_market") and not x.get("is_automatic_sale") and not x.get("is_10b5_1") and not x.get("is_option_exercise") and not x.get("is_tax_withholding") and not x.get("is_equity_award") and not x.get("is_gift")]
        counts={"total":len(tx),"discretionary_open_market":len(discretionary),"purchases":sum(1 for x in discretionary if x.get("acquired_or_disposed")=="A"),"sales":sum(1 for x in discretionary if x.get("acquired_or_disposed")=="D"),"automatic_sales":sum(1 for x in tx if x.get("is_automatic_sale")),"10b5_1":sum(1 for x in tx if x.get("is_10b5_1")),"option_exercises":sum(1 for x in tx if x.get("is_option_exercise")),"vesting":sum(1 for x in tx if x.get("is_equity_award")),"tax_withholding":sum(1 for x in tx if x.get("is_tax_withholding"))}
        own_source_numbers=[]
        for r in own[:8]: own_source_numbers += self.cite(r,"SEC_ownership")
        for r in discretionary[:8]: self.cite(r,"SEC_transaction")
        return {"ownership":{"records":len(own),"known_insider_shares":a.get("known_insider_shares"),"known_insider_percent":a.get("known_insider_percent"),"ceo_shares":a.get("ceo_shares"),"cfo_shares":a.get("cfo_shares"),"director_shares":a.get("director_shares"),"source_record_ids":[record_id(x,"") for x in own[:8]],"source_numbers":own_source_numbers},"transactions":counts,"signal":"No discretionary insider trading signal identified in the analyzed period." if not discretionary else "Discretionary open-market activity identified; interpretation is limited to eligible transactions.","signal_source_numbers":[self.catalog.number_for(record_id(x,"")) for x in discretionary[:1] if self.catalog.number_for(record_id(x,""))],"filings":len(filings)}
    def build_governance(self)->dict[str,Any]:
        root="management_intelligence"; snap=_latest(_rows(self.reader,root,"governance_snapshots.jsonl",self.ticker))
        if not snap:return {"status":"Not established from available official-source coverage.","available":False}
        committees=_rows(self.reader,root,"governance_committees.jsonl",self.ticker); rights=_latest(_rows(self.reader,root,"governance_shareholder_rights.jsonl",self.ticker)); voting=_latest(_rows(self.reader,root,"governance_voting_control.jsonl",self.ticker)); expertise=_rows(self.reader,root,"governance_expertise.jsonl",self.ticker)
        snap_src=self.cite(snap,"governance_snapshot");
        for r in committees: self.cite(r,"governance_committee")
        rights_src=self.cite(rights,"shareholder_rights") if rights else []
        voting_src=self.cite(voting,"voting_control") if voting else []
        return {"available":True,"source_record_ids":[record_id(snap,"")],"source_numbers":snap_src,"board_size":snap.get("board_size"),"independence":{"known_count":snap.get("known_independence_count"),"known_denominator":snap.get("known_independence_denominator"),"percentage":snap.get("known_status_independent_percentage"),"unknown":snap.get("unknown_independence")},"chair_structure":snap.get("chair_structure"),"chair_is_independent":snap.get("chair_is_independent"),"lead_independent_director":snap.get("lead_independent_director"),"committees":[{"name":c.get("committee_name"),"required_independence":c.get("required_independence"),"financial_experts":len(c.get("financial_expert_person_ids") or []),"member_independence":c.get("member_independence"),"source_record_ids":[record_id(c,"")],"source_numbers":self.cite(c,"governance_committee")} for c in committees],"expertise_records":len(expertise),"tenure":{"average":snap.get("average_tenure"),"median":snap.get("median_tenure")},"voting":dict(voting,source_record_ids=[record_id(voting,"")],source_numbers=voting_src) if voting else {"status":"Not established from available official-source coverage."},"shareholder_rights":dict(rights,source_record_ids=[record_id(rights,"")],source_numbers=rights_src) if rights else {"status":"Not established from available official-source coverage."}}
    def build_scoring(self)->dict[str,Any]:
        rows=self.reader.rows("scoring_engine","company_scores.jsonl"); rows=[r for r in rows if r.get("ticker")==self.ticker and r.get("profile_version")=="core-v1.2"]
        snap=_latest(rows) or {}; profile_version=snap.get("profile_version") or "core-v1.2"
        score_source_numbers=self.cite(snap,"score_snapshot") if snap else []
        pillars=[r for r in self.reader.rows("scoring_engine","pillar_scores.jsonl") if r.get("ticker")==self.ticker and profile_version in str(r.get("pillar_score_id", ""))]
        pillars={r.get("pillar"):r for r in pillars}
        p_out={}
        for name,p in pillars.items():
            p_out[name]={"score":p.get("score"),"calculated_score":p.get("calculated_score"),"evidence_coverage":p.get("evidence_coverage",p.get("weighted_coverage",0.0)),"scoring_coverage":p.get("scoring_coverage",p.get("weighted_coverage",0.0)),"confidence":p.get("confidence"),"status":p.get("coverage_status"),"source_record_ids":[record_id(p,"")],"source_numbers":self.cite(p,"pillar_score")}
        return {"profile":snap.get("profile") or self.profile,"profile_version":profile_version,"scoring_version":snap.get("scoring_version") or "qualitative-scoring-v1.2","coverage_version":snap.get("coverage_version") or "coverage-v3","calibration":"UNCALIBRATED_DESIGN_V1","overall_score":snap.get("overall_score"),"status":snap.get("score_status") or "UNKNOWN","evidence_coverage":snap.get("overall_evidence_coverage",snap.get("overall_coverage")),"scoring_coverage":snap.get("overall_scoring_coverage",snap.get("overall_coverage")),"confidence":snap.get("overall_confidence"),"coverage_limitations":snap.get("coverage_limitations",[]),"pillars":p_out,"critical_missing_factors":snap.get("critical_missing_factors",[]),"source_record_ids":[record_id(snap,"")] if snap else [],"source_numbers":score_source_numbers}
    def build(self)->CompanyResearchDossier:
        bq,traj=self.build_business(); management=self.build_management(); insiders=self.build_insiders(); governance=self.build_governance(); scoring=self.build_scoring()
        # strongest evidence-backed directional facts; unknown states never enter these lists
        allfacts=[ReportFact(**f) for f in traj["facts"]+bq["facts"]]
        # Keep one narrative fact per dimension, preferring the trajectory fact.
        by_dim={}
        for f in allfacts:
            dim=f.text.split(":",1)[0]
            if dim not in by_dim or f.section=="business_trajectory": by_dim[dim]=f
        unique=list(by_dim.values())
        positive=sorted([f for f in unique if (f.direction in ("improving","stable") and f.confidence is not None and f.confidence>=.70 and "risk" not in f.text.lower())],key=lambda f:(-(f.confidence or 0),f.text))[:5]
        negative=sorted([f for f in unique if (f.direction in ("deteriorating","mixed") and f.confidence is not None and f.confidence>=.70)],key=lambda f:(-(f.confidence or 0),f.text))[:5]
        riskfacts=[f for f in unique if "risk" in f.text.lower() or f.direction=="deteriorating"]
        emerging=[]
        for c in self.changes:
            if c.get("change_type") in ("new_risk","deteriorating") or c.get("direction")=="deteriorating":
                txt=c.get("summary") or f"{c.get('dimension','Risk')} change identified."
                if txt.lower() == "new risk risk was mentioned.": txt = "A new risk was mentioned."
                emerging.append(ReportFact(txt,"risks",[record_id(c,"")],self.cite(c,"temporal_change"),float(c.get("confidence") or 0),"deteriorating","emerging_risk"))
        catalysts=[]
        for c in _rows(self.reader,"management_intelligence","capital_allocation_events.jsonl",self.ticker)+_rows(self.reader,"management_intelligence","strategic_commitments.jsonl",self.ticker):
            if c.get("event_status") == "planned" or c.get("target_date"):
                text=c.get("evidence_text") or c.get("commitment_text") or c.get("event_type") or "Strategic development identified."
                catalysts.append(ReportFact(text,"catalysts",[record_id(c,"")],self.cite(c,"management_strategic"),None,"improving","catalyst"))
        gaps=[]
        if not scoring.get("overall_score"): gaps.append("Overall score is not issued because scoring coverage is insufficient; this is a coverage limitation, not a negative finding.")
        for lim in scoring.get("coverage_limitations",[]): gaps.append(str(lim))
        if self.documents and not self.claims: gaps.append(f"{len(self.documents)} source documents are present but no qualitative claims are currently extracted; business quality is not inferred from that extraction gap.")
        if not management.get("current_roles"): gaps.append("Management records are not available in the current store.")
        if not governance.get("available"): gaps.append("Governance records are not available in the current store.")
        summary=[]
        if bq["facts"]: summary.append(ReportFact("The dossier contains evidence-backed business states; these remain distinct from absolute quality scores.","executive_summary",[f["evidence_ids"][0] for f in bq["facts"][:3]],[f["source_numbers"][0] for f in bq["facts"][:3] if f["source_numbers"]]))
        if traj["facts"]: summary.append(ReportFact("The stored trajectory evidence is summarized by directional trends in the Business Trajectory section.","executive_summary",[f["evidence_ids"][0] for f in traj["facts"][:3]],[f["source_numbers"][0] for f in traj["facts"][:3] if f["source_numbers"]]))
        summary.append(ReportFact(f"Overall score: {'Not issued' if scoring.get('overall_score') is None else scoring.get('overall_score')}; status: {scoring.get('status') or 'unknown'}.","executive_summary",list(scoring.get("source_record_ids",[])),list(scoring.get("source_numbers",[]))))
        fp_payload={"ticker":self.ticker,"as_of":self.as_of,"qstates":self.qstates,"changes":self.changes,"claims":self.claims,"documents":[{k:v for k,v in x.items() if k not in ('text','sections')} for x in self.documents],"management":management,"insiders":insiders,"governance":governance,"scoring":scoring}
        fingerprint=hashlib.sha256(json.dumps(fp_payload,sort_keys=True,default=str,separators=(",",":" )).encode()).hexdigest()
        refs=self.catalog.finalize()
        # source numbers in facts were assigned before finalization; remap from record ids
        numbers={r.record_id:r.number for r in refs}
        def remap(f:ReportFact): f.source_numbers=[self.catalog.number_for(x) for x in f.evidence_ids if self.catalog.number_for(x)]; return f
        def remap_obj(obj):
            if isinstance(obj, dict):
                out={k: remap_obj(v) for k,v in obj.items()}
                ids=out.get("source_record_ids") or []
                if ids:
                    out["source_numbers"]=[self.catalog.number_for(x) for x in ids if self.catalog.number_for(x)]
                elif "source_numbers" in out:
                    out["source_numbers"]=[x for x in (out.get("source_numbers") or []) if x]
                return out
            if isinstance(obj, list): return [remap_obj(x) for x in obj]
            return obj
        for f in summary+positive+negative+riskfacts+emerging+catalysts: remap(f)
        for group in (bq["facts"],traj["facts"]):
            for f in group:
                f["source_numbers"]= [self.catalog.number_for(x) for x in f["evidence_ids"] if self.catalog.number_for(x)]
        management=remap_obj(management); insiders=remap_obj(insiders); governance=remap_obj(governance); scoring=remap_obj(scoring)
        dossier_id=f"dossier:{self.ticker}:{self.as_of}:{REPORT_VERSION}:{scoring.get('profile_version','core-v1.2')}:{fingerprint[:16]}"
        return CompanyResearchDossier(dossier_id,self.ticker,self.company_name,self.as_of,self.as_of,[remap(f) for f in summary],bq,traj,management,insiders,governance,scoring,[remap(f) for f in positive],[remap(f) for f in negative],{"current":[remap(f) for f in riskfacts],"emerging":[remap(f) for f in emerging]},[remap(f) for f in catalysts],sorted(set(gaps)),refs,self.as_of+"T00:00:00+00:00",REPORT_VERSION,scoring.get("profile_version","core-v1.2"),scoring.get("scoring_version","qualitative-scoring-v1.2"),scoring.get("coverage_version","coverage-v3"),fingerprint,refs,{"documents":len(self.documents),"claims":len(self.claims),"states":len(self.qstates),"temporal_changes":len(self.changes)})

def assemble_dossier(ticker: str, as_of_date: str, profile: str="core_v1", artifacts_root: str|Path="artifacts") -> CompanyResearchDossier:
    return Builder(ticker,as_of_date,profile,artifacts_root).build()

class DossierAssembler:
    def __init__(self, artifacts_root: str|Path = "artifacts"):
        self.artifacts_root = artifacts_root
    def assemble(self, ticker: str, as_of_date: str, profile: str = "core_v1") -> CompanyResearchDossier:
        return assemble_dossier(ticker, as_of_date, profile, self.artifacts_root)
