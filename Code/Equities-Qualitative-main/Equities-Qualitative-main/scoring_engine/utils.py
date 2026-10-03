from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .confidence import evidence_confidence
from .models import FactorDefinition, FactorScore


def stable_id(*parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def factor_id(ticker: str, as_of: str, profile: str, key: str, version: str) -> str:
    return f"factor:{ticker.upper()}:{as_of}:{profile}:{key}:{version}:{stable_id(ticker, as_of, profile, key, version)}"


def make_factor(*, ticker: str, as_of: str, profile: str, definition: FactorDefinition,
                score: float | None, status: str, coverage: float, confidence: float,
                raw_inputs: dict[str, Any] | None = None, normalized_inputs: dict[str, Any] | None = None,
                supporting_record_ids: list[str] | None = None, supporting_evidence: list[dict[str, Any]] | None = None,
                coverage_reason: str = "", effective_weight: float | None = None,
                evidence_coverage: float | None = None, scoring_coverage: float | None = None) -> FactorScore:
    evidence = max(0.0, min(1.0, float(coverage if evidence_coverage is None else evidence_coverage)))
    if status != "SCORED":
        scoreable = 0.0
    elif scoring_coverage is None:
        scoreable = evidence if score is not None and evidence >= definition.min_coverage else 0.0
    else:
        scoreable = scoring_coverage
    scoreable = max(0.0, min(1.0, float(scoreable)))
    weighted = score * (effective_weight if effective_weight is not None else definition.weight) if score is not None else None
    return FactorScore(
        factor_score_id=factor_id(ticker, as_of, profile, definition.factor_key, definition.rule_version),
        ticker=ticker.upper(), factor_key=definition.factor_key, factor_name=definition.factor_name,
        pillar=definition.pillar, score=score, status=status, coverage=evidence,
        confidence=max(0.0, min(1.0, confidence)), raw_inputs=raw_inputs or {},
        normalized_inputs=normalized_inputs or {}, rule_id=definition.mapping_rule,
        rule_version=definition.rule_version, weight=definition.weight, weighted_contribution=weighted,
        supporting_record_ids=supporting_record_ids or [], supporting_evidence=supporting_evidence or [],
        coverage_reason=coverage_reason, as_of_date=as_of, created_at=now(), effective_weight=effective_weight,
        dependency_group=definition.dependency_group, evidence_coverage=evidence, scoring_coverage=scoreable,
        explanation={"factor": definition.factor_name, "raw_input": raw_inputs or {}, "rule": definition.mapping_rule,
                     "mapped_score": score, "coverage": evidence, "evidence_coverage": evidence,
                     "scoring_coverage": scoreable, "confidence": confidence,
                     "why_scored": coverage_reason if status == "SCORED" else "", 
                     "why_not_scored": coverage_reason if status != "SCORED" else ""},
    )


def confidence_for_rows(rows: list[dict[str, Any]], *, attribution: str | None = None) -> float:
    if not rows:
        return 0.0
    groups = len({str(row.get("source_document_id") or row.get("source_local_path") or row.get("source_url") or row.get("source_accession") or row.get("source_accession_number") or row.get("id", "")) for row in rows})
    return evidence_confidence(source_completeness=1.0, evidence_groups=groups, sample_size=len(rows), attribution=attribution)
