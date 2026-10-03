from __future__ import annotations

import hashlib, json
from pathlib import Path
from typing import Any

from .config import ValidationConfig
from .leakage_audit import LeakageAuditor, audit_rows
from .models import DataAudit, HistoricalObservation
from .survivorship import corporate_action_status, survivorship_status


def _hash_rows(rows: list[HistoricalObservation]) -> str:
    return hashlib.sha256(json.dumps([r.to_dict() for r in rows], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_data_audit(observations: list[HistoricalObservation], config: ValidationConfig, market_status: str = "MARKET_DATA_PROVIDER_NOT_CONFIGURED") -> tuple[DataAudit, Any]:
    leakage=LeakageAuditor().audit(observations)
    synthetic = audit_rows([{"id":"synthetic-future","publication_date":"2030-01-01"}], "2029-12-31")
    leakage.synthetic_detection_passed=bool(synthetic and synthetic[0].status=="FAIL")
    valid=[o for o in observations if o.point_in_time_valid]
    excluded=[o for o in observations if not o.point_in_time_valid]
    dates=sorted({o.as_of_date for o in valid})
    def source_tickers(subdir: str, filename: str) -> set[str]:
        candidates=[Path(config.artifacts_root)/subdir/filename]
        if subdir == "management_intelligence": candidates.append(Path(config.artifacts_root)/"management_intelligence_final8"/filename)
        for path in candidates:
            if path.exists():
                result=set()
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            row=json.loads(line); ticker=row.get("ticker") or row.get("board_ticker") or row.get("employer_ticker")
                            if ticker: result.add(str(ticker).upper())
                        except json.JSONDecodeError: pass
                return result
        return set()
    valid_tickers={o.ticker for o in valid}
    qual=len(valid_tickers & source_tickers("qualitative_analysis","qualitative_states.jsonl"))/len(valid_tickers) if valid_tickers else 0.0
    management=len(valid_tickers & source_tickers("management_intelligence","people.jsonl"))/len(valid_tickers) if valid_tickers else 0.0
    insider=len(valid_tickers & source_tickers("insider_intelligence","alignment_snapshots.jsonl"))/len(valid_tickers) if valid_tickers else 0.0
    governance=len(valid_tickers & source_tickers("management_intelligence","governance_snapshots.jsonl"))/len(valid_tickers) if valid_tickers else 0.0
    score_availability=sum(1 for o in valid if o.overall_score is not None)/len(valid) if valid else 0.0
    reasons={}
    for o in excluded: reasons[o.exclusion_reason or "UNKNOWN"] = reasons.get(o.exclusion_reason or "UNKNOWN",0)+1
    adequate_exploratory=len(valid)>=config.minimum_observations and len({o.ticker for o in valid})>=config.minimum_unique_companies and len(dates)>=2
    audit=DataAudit(config.dataset_version,config.universe_version,config.outcome_version,config.point_in_time_version,len({o.ticker for o in observations}),len(observations),len(valid),len(excluded),reasons,dates[0] if dates else None,dates[-1] if dates else None,len({o.ticker for o in valid}),len(dates),qual,management,insider,governance,score_availability,market_status,0,leakage.status,survivorship_status(),corporate_action_status(),adequate_exploratory,False,False,_hash_rows(observations),[
        "The pilot contains current snapshots only; it is not a historical panel.",
        "No market-price provider is configured, so price outcomes are unavailable.",
        "Survivorship and delisted-company coverage are incomplete.",
        "Calibration and final out-of-sample validation are blocked by minimum-history rules.",
    ])
    return audit, leakage


def audit_markdown(audit: DataAudit, leakage) -> str:
    d=audit.to_dict(); lines=["# Empirical Validation Data Audit","", "## Dataset",f"- Dataset version: {audit.dataset_version}",f"- Companies discovered: {audit.companies_discovered}",f"- Possible observations: {audit.possible_observations}",f"- Valid observations: {audit.valid_observations}",f"- Excluded observations: {audit.excluded_observations}",f"- Date range: {audit.earliest_date or 'None'} to {audit.latest_date or 'None'}",f"- Unique dates: {audit.unique_dates}","", "## Point-in-time audit",f"- Status: {audit.leakage_status}",f"- Checked observations: {leakage.checked_observations}",f"- Failures: {leakage.failed_observations}",f"- Unknowns: {leakage.unknown_observations}",f"- Synthetic leakage detection: {'PASS' if leakage.synthetic_detection_passed else 'FAIL'}", "", "## Coverage",f"- Qualitative: {audit.qualitative_coverage:.1%}",f"- Management: {audit.management_coverage:.1%}",f"- Insider: {audit.insider_coverage:.1%}",f"- Governance: {audit.governance_coverage:.1%}",f"- Overall-score availability: {audit.overall_score_availability:.1%}","", "## Market and outcomes",f"- Market data: {audit.market_data_status}",f"- Outcomes available: {audit.outcomes_available}",f"- Survivorship: {audit.survivorship_status}",f"- Corporate actions: {audit.corporate_action_status}","", "## Decision",f"- Adequate for exploratory diagnostics: {'YES' if audit.adequate_for_exploratory_diagnostics else 'NO'}",f"- Adequate for calibration: {'YES' if audit.adequate_for_calibration else 'NO'}",f"- Adequate for final out-of-sample validation: {'YES' if audit.adequate_for_out_of_sample else 'NO'}", "", "## Limitations",*(f"- {n}" for n in audit.notes)]
    return "\n".join(lines)+"\n"
