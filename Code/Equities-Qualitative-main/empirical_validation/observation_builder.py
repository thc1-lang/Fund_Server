from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ValidationConfig
from .models import HistoricalObservation
from .point_in_time import PointInTimePolicy, available_date
from .universe import PilotUniverse


def _read_jsonl(root: Path, subdir: str, name: str) -> list[dict[str, Any]]:
    paths = [root / subdir / name]
    if subdir == "management_intelligence":
        paths.append(root / "management_intelligence_final8" / name)
    for path in paths:
        if path.exists():
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try: rows.append(json.loads(line))
                    except json.JSONDecodeError: pass
            return rows
    return []


def _ticker(row: dict[str, Any]) -> str | None:
    for key in ("ticker", "issuer_ticker", "board_ticker", "employer_ticker"):
        if row.get(key): return str(row[key]).upper()
    return None


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _latest(rows: list[dict[str, Any]], key: str = "created_at") -> dict[str, Any] | None:
    return sorted(rows, key=lambda r: (str(r.get(key) or r.get("as_of_date") or ""), str(r.get("snapshot_id") or r.get("factor_score_id") or "")), reverse=True)[0] if rows else None


def build_observations(config: ValidationConfig, universe: PilotUniverse | None = None) -> list[HistoricalObservation]:
    root = Path(config.artifacts_root)
    universe = universe or PilotUniverse.from_tickers(config.tickers, config.universe_version)
    scores = _read_jsonl(root, "scoring_engine", "company_scores.jsonl")
    factors = _read_jsonl(root, "scoring_engine", "factor_scores.jsonl")
    pillars = _read_jsonl(root, "scoring_engine", "pillar_scores.jsonl")
    states = _read_jsonl(root, "qualitative_analysis", "qualitative_states.jsonl")
    people = _read_jsonl(root, "management_intelligence", "people.jsonl")
    management_snapshots = _read_jsonl(root, "management_intelligence", "track_record_snapshots.jsonl")
    alignments = _read_jsonl(root, "insider_intelligence", "alignment_snapshots.jsonl")
    governance = _read_jsonl(root, "management_intelligence", "governance_snapshots.jsonl")
    policy = PointInTimePolicy(config.point_in_time_version)
    observations: list[HistoricalObservation] = []
    for member in universe.members:
        ticker = member.ticker
        candidates = [r for r in scores if _ticker(r) == ticker and r.get("profile_version") == config.profile_version]
        score = _latest(candidates)
        if not score: continue
        as_of = str(score.get("as_of_date") or config.as_of_date)
        cutoff = as_of
        factor_rows = [r for r in factors if _ticker(r) == ticker and config.profile_version in str(r.get("factor_score_id", "")) and str(r.get("as_of_date")) == as_of]
        pillar_rows = [r for r in pillars if _ticker(r) == ticker and config.profile_version in str(r.get("pillar_score_id", ""))]
        source_rows = [score, *factor_rows, *pillar_rows]
        future = [r for r in source_rows if policy.is_available(r, cutoff) == "FAIL"]
        unknown = [r for r in source_rows if policy.is_available(r, cutoff) == "UNKNOWN"]
        input_payload = {"score": score, "factors": factor_rows, "pillars": pillar_rows}
        factor_values = {str(r.get("factor_key")): r.get("score") for r in factor_rows}
        factor_statuses = {str(r.get("factor_key")): str(r.get("status") or "UNKNOWN") for r in factor_rows}
        pillar_score = {str(r.get("pillar")): r.get("score") for r in pillar_rows}
        pillar_ev = {str(r.get("pillar")): r.get("evidence_coverage", r.get("weighted_coverage")) for r in pillar_rows}
        pillar_sc = {str(r.get("pillar")): r.get("scoring_coverage", r.get("weighted_coverage")) for r in pillar_rows}
        pillar_conf = {str(r.get("pillar")): r.get("confidence") for r in pillar_rows}
        observation_id = f"obs:{ticker}:{as_of}:{config.profile_version}:{_canonical_hash(input_payload)[:16]}"
        valid = not future
        observations.append(HistoricalObservation(
            observation_id=observation_id,
            ticker=ticker,
            company_id=member.company_id,
            as_of_date=as_of,
            information_cutoff=cutoff,
            sector=member.sector,
            industry=member.industry,
            market_cap_bucket=None,
            scoring_profile=str(score.get("profile") or config.profile),
            scoring_version=str(score.get("scoring_version") or "qualitative-scoring-v1.2"),
            factor_values=factor_values,
            factor_statuses=factor_statuses,
            pillar_scores=pillar_score,
            pillar_evidence_coverage=pillar_ev,
            pillar_scoring_coverage=pillar_sc,
            pillar_confidence=pillar_conf,
            overall_score=score.get("overall_score"),
            overall_evidence_coverage=score.get("overall_evidence_coverage", score.get("overall_coverage")),
            overall_scoring_coverage=score.get("overall_scoring_coverage", score.get("overall_coverage")),
            overall_confidence=score.get("overall_confidence"),
            critical_missing_factors=list(score.get("critical_missing_factors") or []),
            source_versions={"profile_version": str(score.get("profile_version") or config.profile_version), "scoring_version": str(score.get("scoring_version") or "qualitative-scoring-v1.2"), "coverage_version": str(score.get("coverage_version") or "coverage-v3")},
            input_fingerprint=_canonical_hash(input_payload),
            point_in_time_valid=valid,
            created_at=str(score.get("created_at") or as_of),
            exclusion_reason="LEAKAGE_DETECTED" if future else None,
            source_availability_unknown=bool(unknown),
        ))
    return observations


def deduplicate_observations(observations: list[HistoricalObservation]) -> tuple[list[HistoricalObservation], int]:
    seen=set(); unique=[]; duplicates=0
    for row in sorted(observations,key=lambda x:x.observation_id):
        if row.observation_id in seen:
            duplicates += 1
        else:
            seen.add(row.observation_id); unique.append(row)
    return unique, duplicates
