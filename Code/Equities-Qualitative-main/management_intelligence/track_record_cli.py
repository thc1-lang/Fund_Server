"""CLI/orchestration for Component 6B factual track records."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from typing import Iterable

from insider_intelligence.sec_ingestion import SECSourceProvider

from .capital_allocation import extract_capital_events
from .guidance_track_record import diagnose_guidance_coverage, extract_guidance_commitments, load_qualitative_evidence, match_guidance_to_actuals
from .main import ManagementIntelligenceProvider
from .operating_outcomes import CompanyFactsProvider, build_operating_outcomes, normalize_company_facts
from .sec_sources import SECManagementSourceProvider
from .store import ManagementStore
from .strategic_commitments import extract_strategic_commitments, resolve_strategic_outcomes
from .tenure import build_tenures
from .track_record_models import Attribution, PersonTrackRecordSnapshot, TrackRecordRun
from .track_record_store import TrackRecordStore


class ManagementTrackRecordProvider:
    def __init__(self, *, management_store: ManagementStore | None = None, track_store: TrackRecordStore | None = None, sec: SECManagementSourceProvider | None = None, facts: CompanyFactsProvider | None = None):
        self.management_store = management_store or ManagementStore()
        self.track_store = track_store or TrackRecordStore(self.management_store.root)
        self.sec = sec or SECManagementSourceProvider(SECSourceProvider())
        self.facts = facts or CompanyFactsProvider(self.sec.sec)
        self.last_counts: dict[str, dict[str, int]] = {}
        self.warnings: list[str] = []

    def acquire(self, ticker: str, *, as_of_date: str | None = None, include_prior_companies: bool = False, qualitative_root: str = "artifacts/qualitative_analysis", force: bool = False) -> dict[str, list]:
        ticker = ticker.strip().upper()
        as_of = as_of_date or date.today().isoformat()
        self.warnings = []
        people = self.management_store.list_people(ticker)
        if not people:
            raise LookupError(f"Component 6A has no management people for {ticker}; run management-intelligence first")
        tenures = build_tenures(self.management_store, ticker, as_of_date=as_of, include_prior_companies=include_prior_companies)
        payload: dict = {}
        source_url = ""
        source_path = None
        try:
            payload, source_url, source_path = self.facts.fetch(ticker, force=force)
            facts = normalize_company_facts(payload, ticker, source_url, source_path)
        except Exception as exc:
            facts = []
            self.warnings.append(f"{ticker} Company Facts unavailable: {exc}")
        claims, documents = load_qualitative_evidence(qualitative_root, ticker)
        guidance = extract_guidance_commitments(claims, documents, people, tenures, ticker)
        guidance_outcomes = [match_guidance_to_actuals(item, facts, as_of_date=as_of) for item in guidance]
        guidance_diagnostic = diagnose_guidance_coverage(ticker, claims, documents, guidance)
        strategic = extract_strategic_commitments(claims, documents, people, tenures, ticker)
        strategic_outcomes = resolve_strategic_outcomes(strategic, claims, documents)
        company_name = people[0].company_name
        capital = extract_capital_events(claims, documents, people, tenures, ticker, company_name)
        operating: list = []
        for tenure in tenures:
            if tenure.ticker.upper() == ticker:
                operating.extend(build_operating_outcomes(tenure, facts, as_of_date=as_of))
        snapshots = self._snapshots(ticker, as_of, tenures, guidance, guidance_outcomes, strategic, strategic_outcomes, capital, operating)
        self.last_counts = {
            "tenures": self.track_store.upsert_tenures(tenures),
            "financial_facts": self.track_store.upsert_financial_facts(facts),
            "operating_outcomes": self.track_store.upsert_operating_outcomes(operating),
            "guidance_commitments": self.track_store.upsert_guidance_commitments(guidance),
            "guidance_outcomes": self.track_store.upsert_guidance_outcomes(guidance_outcomes),
            "guidance_diagnostics": self.track_store.upsert_guidance_diagnostics([guidance_diagnostic]),
            "strategic_commitments": self.track_store.upsert_strategic_commitments(strategic),
            "strategic_outcomes": self.track_store.upsert_strategic_outcomes(strategic_outcomes),
            "capital_allocation_events": self.track_store.upsert_capital_events(capital),
            "track_record_snapshots": self.track_store.upsert_snapshots(snapshots),
        }
        return {"tenures": tenures, "facts": facts, "operating": operating, "guidance": guidance, "guidance_outcomes": guidance_outcomes, "guidance_diagnostic": guidance_diagnostic, "strategic": strategic, "strategic_outcomes": strategic_outcomes, "capital": capital, "snapshots": snapshots}

    def acquire_many(self, tickers: Iterable[str], *, as_of_date: str | None = None, include_prior_companies: bool = False, qualitative_root: str = "artifacts/qualitative_analysis", force: bool = False) -> dict[str, dict[str, list]]:
        as_of = as_of_date or date.today().isoformat()
        results = {}
        warnings: list[str] = []
        combined: dict[str, dict[str, int]] = {}
        for ticker in tickers:
            try:
                results[ticker.upper()] = self.acquire(ticker, as_of_date=as_of, include_prior_companies=include_prior_companies, qualitative_root=qualitative_root, force=force)
                for key, value in self.last_counts.items():
                    target = combined.setdefault(key, {"added": 0, "updated": 0, "unchanged": 0, "rejected": 0})
                    for counter, amount in value.items(): target[counter] = target.get(counter, 0) + amount
                warnings.extend(self.warnings)
            except Exception as exc:
                warnings.append(f"{ticker.upper()}: {exc}")
        self.track_store.record_run(TrackRecordRun(
            run_id=f"track-run:{datetime.now(timezone.utc).isoformat(timespec='seconds')}:{','.join(sorted(results))}",
            tickers=sorted(results),
            as_of_date=as_of,
            include_prior_companies=include_prior_companies,
            counts=combined,
            warnings=warnings,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ))
        self.last_counts = combined
        self.warnings = warnings
        return results

    def _snapshots(self, ticker, as_of, tenures, guidance, guidance_outcomes, strategic, strategic_outcomes, capital, operating):
        snapshots = []
        guidance_by_tenure = {}
        for item in guidance:
            guidance_by_tenure.setdefault(item.tenure_id, []).append(item)
        for tenure in tenures:
            if tenure.ticker.upper() != ticker.upper() or tenure.role_category not in {"CEO", "CFO", "COO", "CTO", "president", "executive"}:
                continue
            g = guidance_by_tenure.get(tenure.tenure_id, [])
            g_outcomes = {item.guidance_id: item for item in guidance_outcomes}
            s = [item for item in strategic if item.tenure_id == tenure.tenure_id]
            s_outcomes = {item.commitment_id: item.outcome for item in strategic_outcomes}
            c = [item for item in capital if item.tenure_id == tenure.tenure_id]
            o = [item for item in operating if item.tenure_id == tenure.tenure_id]
            counts = [g_outcomes[item.guidance_id].outcome for item in g if item.guidance_id in g_outcomes]
            metrics = sorted({item.metric for item in o})
            limitations = []
            if not g:
                limitations.append("no safely person-attributed guidance records")
            if not s:
                limitations.append("no person-linked strategic commitments")
            if not c:
                limitations.append("no person-linked capital-allocation events")
            quality = "HIGH" if len(metrics) >= 8 else "MODERATE" if len(metrics) >= 4 else "LOW" if metrics else "INSUFFICIENT"
            tenure_duration = next((item.tenure_duration_days for item in o if item.tenure_duration_days is not None), None)
            snapshot_id = f"snapshot:{tenure.tenure_id}:{as_of}"
            snapshots.append(PersonTrackRecordSnapshot(
                snapshot_id=snapshot_id,
                person_id=tenure.person_id,
                ticker=ticker.upper(),
                role=tenure.role,
                tenure_id=tenure.tenure_id,
                guidance_records=len(g),
                guidance_resolved=sum(value in {"BEAT", "MET", "MISS"} for value in counts),
                guidance_beat=counts.count("BEAT"), guidance_met=counts.count("MET"), guidance_missed=counts.count("MISS"), guidance_withdrawn=counts.count("WITHDRAWN"),
                strategic_commitments=len(s), strategic_achieved=sum(s_outcomes.get(item.commitment_id) == "ACHIEVED" for item in s), strategic_delayed=sum(s_outcomes.get(item.commitment_id) == "DELAYED" for item in s), strategic_missed=sum(s_outcomes.get(item.commitment_id) == "MISSED" for item in s), strategic_unresolved=sum(s_outcomes.get(item.commitment_id) == "UNRESOLVED" for item in s),
                major_acquisitions=sum(item.event_type == "acquisition" for item in c), major_divestitures=sum(item.event_type == "divestiture" for item in c), buybacks=sum(item.event_type.startswith("buyback") for item in c), equity_issuance=sum(item.event_type in {"share_issuance", "equity_compensation"} for item in c), debt_actions=sum(item.event_type in {"debt_issuance", "debt_repayment"} for item in c), operating_outcome_ids=[item.outcome_id for item in o], capital_allocation_event_ids=[item.event_id for item in c],
                direct_attribution_count=sum(item.attribution_level == Attribution.DIRECT for item in c), role_based_count=sum(item.attribution_level == Attribution.ROLE_BASED for item in c), tenure_overlap_count=sum(item.attribution_level == Attribution.TENURE_OVERLAP for item in o), as_of_date=as_of, created_at=as_of,
                team_count=sum(item.attribution_level == Attribution.TEAM for item in c), coverage_quality=quality, coverage_limitations=limitations, operating_metrics_available=metrics, tenure_duration_days=tenure_duration,
            ))
        return snapshots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build official-source management execution and track records")
    parser.add_argument("ticker", nargs="+", help="issuer ticker(s)")
    parser.add_argument("--as-of-date", default=None, help="ISO as-of date, default today")
    parser.add_argument("--include-prior-companies", action="store_true", help="include bounded exact-mapped prior public-company tenures")
    parser.add_argument("--store-root", default="artifacts/management_intelligence")
    parser.add_argument("--qualitative-root", default="artifacts/qualitative_analysis")
    parser.add_argument("--sec-cache-root", default="artifacts/insider_intelligence/sec")
    parser.add_argument("--force", action="store_true", help="refresh the SEC Company Facts cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    management_store = ManagementStore(args.store_root)
    provider = ManagementTrackRecordProvider(
        management_store=management_store,
        track_store=TrackRecordStore(args.store_root),
        sec=SECManagementSourceProvider(SECSourceProvider(cache_root=args.sec_cache_root)),
    )
    results = provider.acquire_many(args.ticker, as_of_date=args.as_of_date, include_prior_companies=args.include_prior_companies, qualitative_root=args.qualitative_root, force=args.force)
    for ticker, result in sorted(results.items()):
        print(f"{ticker}: tenures={len(result['tenures'])} facts={len(result['facts'])} operating={len(result['operating'])} guidance={len(result['guidance'])} guidance_outcomes={len(result['guidance_outcomes'])} strategic={len(result['strategic'])} capital={len(result['capital'])} snapshots={len(result['snapshots'])} guidance_reason={result['guidance_diagnostic'].reason}")
    for warning in provider.warnings:
        print(f"WARNING: {warning}")
    return 0 if results else 1


__all__ = ["ManagementTrackRecordProvider", "build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
