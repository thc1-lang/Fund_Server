from __future__ import annotations
from typing import Any
from .models import CompanyResearchDossier, ReportFact

def _pct(v: Any) -> str:
    if v is None: return "Not available"
    try: return f"{float(v)*100:.1f}%" if abs(float(v)) <= 1 else f"{float(v):.1f}%"
    except Exception: return str(v)
def _refs(f: ReportFact|dict) -> str:
    nums=f.source_numbers if isinstance(f,ReportFact) else f.get("source_numbers",[])
    return " " + " ".join(f"[Source {n}]" for n in nums) if nums else ""
def _fact(f: ReportFact|dict) -> str:
    return (f.text if isinstance(f,ReportFact) else f.get("text", "")) + _refs(f)
def _facts(rows: list[Any]) -> str:
    return "\n".join(f"- {_fact(x)}" for x in rows)

def render_markdown(d: CompanyResearchDossier, detail: str="standard", include_evidence: bool=False, explain_scores: bool=False) -> str:
    # Detail only changes presentation breadth; facts and scores remain identical.
    cap = 5 if detail == "concise" else None
    def shown(rows): return rows[:cap] if cap else rows
    s=d.scoring; lines=["# Company Research Dossier","", "## Research Snapshot","",f"- **Ticker:** {d.ticker}",f"- **Company:** {d.company_name}",f"- **As-of:** {d.as_of_date}",f"- **Source cutoff:** {d.source_cutoff}",f"- **Profile:** {s.get('profile')} ({s.get('profile_version')})",f"- **Overall score:** {'Not issued' if s.get('overall_score') is None else s.get('overall_score')}",f"- **Status:** {s.get('status')}",f"- **Evidence coverage:** {_pct(s.get('evidence_coverage'))}",f"- **Scoring coverage:** {_pct(s.get('scoring_coverage'))}",f"- **Confidence:** {_pct(s.get('confidence'))}","", "Business Quality: "+("Not issued — insufficient scoring coverage" if not s.get('pillars',{}).get('BUSINESS_QUALITY',{}).get('score') else str(s['pillars']['BUSINESS_QUALITY']['score'])),"Business Trajectory: "+("Not issued — insufficient scoring coverage" if not s.get('pillars',{}).get('BUSINESS_TRAJECTORY',{}).get('score') else str(s['pillars']['BUSINESS_TRAJECTORY']['score'])),"Management Execution: "+("Not issued — insufficient scoring coverage" if not s.get('pillars',{}).get('MANAGEMENT_EXECUTION',{}).get('score') else str(s['pillars']['MANAGEMENT_EXECUTION']['score'])),"Insider Alignment: "+("Not issued — insufficient scoring coverage" if not s.get('pillars',{}).get('INSIDER_ALIGNMENT',{}).get('score') else str(s['pillars']['INSIDER_ALIGNMENT']['score'])),"Governance: "+("Not issued — insufficient scoring coverage" if not s.get('pillars',{}).get('GOVERNANCE',{}).get('score') else str(s['pillars']['GOVERNANCE']['score'])),"", "## Executive Summary", "", _facts(d.executive_summary), ""]
    bq=d.business_quality; tr=d.business_trajectory
    lines += ["## Business Quality","",_facts(shown(bq.get("facts",[]))) or "- No supported records in the current store.","", "## Business Trajectory","",_facts(shown(tr.get("facts",[]))) or "- No supported records in the current store.",""]
    if d.key_positives: lines += ["## Key Positives","",_facts(shown(d.key_positives)),""]
    if d.key_negatives: lines += ["## Key Negatives","",_facts(shown(d.key_negatives)),""]
    if d.risks.get("current") or d.risks.get("emerging"):
        lines += ["## Risks",""]
        if d.risks.get("current"): lines += ["### Current Risks","",_facts(shown(d.risks.get("current",[]))),""]
        if d.risks.get("emerging"): lines += ["### Emerging Risks","",_facts(shown(d.risks.get("emerging",[]))),""]
    if d.catalysts: lines += ["## Catalysts","",_facts(shown(d.catalysts)),""]
    m=d.management; diag_refs=" ".join(f"[Source {n}]" for n in m.get('guidance',{}).get('diagnostic_source_numbers',[])); lines += ["## Management","",f"- Current management records: {m.get('people_count',0)} people; current roles captured: {len(m.get('current_roles',[]))}.",f"- Guidance diagnostic: {m.get('guidance',{}).get('diagnostic',{}).get('reason','Not established')}.{(' '+diag_refs) if diag_refs else ''}",f"- Guidance commitments captured: {m.get('guidance',{}).get('commitments',0)}; resolved outcomes: {m.get('guidance',{}).get('resolved',0)}.",f"- Strategic commitments: {m.get('strategic_commitments',0)}; capital-allocation events: {m.get('capital_allocation_events',0)}.",f"- {m.get('attribution_note','')}",""]
    if m.get("leaders"):
        for leader in m["leaders"]:
            refs=" ".join(f"[Source {n}]" for n in leader.get("source_numbers",[])); lines.append(f"- {leader.get('name') or 'Unknown person'}: {leader.get('role') or 'role not specified'}."+(f" {refs}" if refs else ""))
    else:
        lines.append("- CEO/CFO identities are not established in the current management store.")
    for c in m.get("leadership_changes",[]): lines.append(f"- {'Future-effective ' if c.get('future') else ''}{c.get('event','change').title()}: {c.get('person') or 'Unknown person'} — {c.get('role') or 'role not specified'} effective {c.get('effective_date') or 'date not established'}."+(" [Source %s]"%c['source_numbers'][0] if c.get('source_numbers') else ""))
    if not m.get("leadership_changes"): lines.append("- No recent leadership changes are captured in the current store.")
    for tr in m.get("track_record",[])[:4]:
        if tr.get("role"):
            lines.append(f"- Track-record snapshot for {tr.get('role')}: {tr.get('guidance_records',0)} guidance records, {tr.get('guidance_resolved',0)} resolved, {tr.get('strategic_unresolved',0)} unresolved strategic commitments, and {tr.get('tenure_overlap_count',0)} tenure-overlap observations. The overlap observations are not person attribution.")
    own_refs=" ".join(f"[Source {n}]" for n in d.insiders.get("ownership",{}).get("source_numbers",[])); tx_refs=" ".join(f"[Source {n}]" for n in d.insiders.get("signal_source_numbers",[])); lines += ["", "## Insider Ownership & Activity","",f"- Ownership records: {d.insiders.get('ownership',{}).get('records',0)}; known insider shares: {d.insiders.get('ownership',{}).get('known_insider_shares','Not disclosed')}. {own_refs}",f"- Transaction counts: {d.insiders.get('transactions',{})}.",f"- {d.insiders.get('signal','')} {tx_refs}",""]
    g=d.governance
    g_refs=" ".join(f"[Source {n}]" for n in g.get("source_numbers",[])); lines += ["## Board & Governance","",f"- Status: {g.get('status','Available from official-source records.')}{(' '+g_refs) if g_refs else ''}"]
    if g.get("available"):
        vrefs=" ".join(f"[Source {n}]" for n in g.get("voting",{}).get("source_numbers",[])); rrefs=" ".join(f"[Source {n}]" for n in g.get("shareholder_rights",{}).get("source_numbers",[])); lines += [f"- Board size: {g.get('board_size','Not established')}; chair structure: {g.get('chair_structure','Not established')}; chair independence: {g.get('chair_is_independent','Not established')}. {g_refs}",f"- Lead independent director: {g.get('lead_independent_director') or 'Not established from available official-source coverage.'}. {g_refs}",f"- Independence: {g.get('independence',{})}. {g_refs}",f"- Committees: {', '.join(c.get('name','') for c in g.get('committees',[])) or 'Not established from available official-source coverage.'}. {g_refs}",*[f"- {c.get('name')}: {c.get('financial_experts',0)} audit-financial-expert record(s); required independence: {c.get('required_independence','unknown')}." for c in g.get('committees',[])],f"- Voting control: {g.get('voting',{}).get('voting_ownership',g.get('voting',{}).get('status','Not established from available official-source coverage.'))}. {vrefs}",f"- Shareholder rights: {g.get('shareholder_rights',{}).get('source_coverage',g.get('shareholder_rights',{}).get('status','Not established from available official-source coverage.'))}. {rrefs}"]
    lines += ["", "## Scoring & Coverage","",f"- Profile: {s.get('profile')} / {s.get('profile_version')}",f"- Calibration: {s.get('calibration')}","- The scoring framework is logically validated but has not yet been empirically calibrated against investment outcomes.",f"- Overall score: {'Not issued' if s.get('overall_score') is None else s.get('overall_score')}; reason/status: {s.get('status')}.","- Evidence coverage reflects available research evidence. Scoring coverage reflects the proportion of the configured scoring model supported by safely scoreable evidence.",""]
    for name,p in sorted(s.get("pillars",{}).items()):
        score=p.get("score") if p.get("score") is not None else "Not issued"
        lines.append(f"- **{name}:** displayed score {score}; evidence coverage {_pct(p.get('evidence_coverage'))}; scoring coverage {_pct(p.get('scoring_coverage'))}; confidence {_pct(p.get('confidence'))}; status {p.get('status')}.")
        if explain_scores and p.get("calculated_score") is not None and p.get("score") is None: lines.append(f"  - Internal calculated score (explain mode): {p.get('calculated_score'):.2f}")
    lines += ["", "## Research Coverage Gaps","",*(f"- {g}" for g in d.coverage_gaps),"", "## Source Index", ""]
    for ref in (d.source_index if detail != "concise" else d.source_index[:10]):
        target=ref.url or ref.local_path or "internal artifact"
        lines.append(f"- **Source {ref.number}:** {ref.title or ref.record_type} ({ref.date or 'date not established'}) — {target}")
    if include_evidence:
        lines += ["", "## Evidence Appendix", ""]
        for ref in d.evidence_appendix:
            lines.append(f"- `{ref.record_id}` ({ref.record_type}): {ref.snippet}")
    return "\n".join(lines).rstrip()+"\n"
