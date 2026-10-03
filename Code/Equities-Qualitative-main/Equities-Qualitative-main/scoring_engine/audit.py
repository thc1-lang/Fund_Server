from __future__ import annotations

from collections import defaultdict
from typing import Any

from .confidence import correlated_factor_confidence
from .engine import score_company
from .models import FactorScore, ProfileDefinition


def classify_factor(factor: FactorScore) -> str:
    reason = factor.coverage_reason.upper()
    if factor.status == "SCORED":
        return "VALID"
    if "NOT_PROVEN_ABSOLUTE" in reason or "NON_DIRECTIONAL" in reason:
        return "SEMANTICALLY_NON_DIRECTIONAL"
    if "UNKNOWN" in reason or "NO_SIGNAL" in reason or "NOT_DISCLOSED" in reason or "NOT_CONFIGURED" in reason:
        return "INSUFFICIENT_EVIDENCE"
    if factor.status in {"INSUFFICIENT_COVERAGE", "INSUFFICIENT_SCORING_COVERAGE", "INSUFFICIENT_SAMPLE", "UNSCORED", "UNKNOWN"}:
        return "INSUFFICIENT_EVIDENCE"
    return "VALID"


def factor_audit_rows(factors: list[FactorScore], profile: ProfileDefinition) -> list[dict[str, Any]]:
    definitions = {item.factor_key: item for item in profile.factors}
    rows = []
    for factor in factors:
        definition = definitions[factor.factor_key]
        rows.append({
            "factor_key": factor.factor_key,
            "pillar": factor.pillar,
            "raw_input": factor.raw_inputs,
            "input_record_ids": factor.supporting_record_ids,
            "status": factor.status,
            "configured_weight": factor.weight,
            "eligible_weight": factor.weight if factor.status == "SCORED" else 0.0,
            "effective_weight": factor.effective_weight,
            "mapping_rule": f"{factor.rule_id}@{factor.rule_version}",
            "mapped_score": factor.score,
            "coverage": factor.evidence_coverage,
            "evidence_coverage": factor.evidence_coverage,
            "scoring_coverage": factor.scoring_coverage,
            "confidence": factor.confidence,
            "weighted_contribution": factor.weighted_contribution,
            "dependency_group": definition.dependency_group,
            "semantic_class": definition.semantic_class,
            "classification": classify_factor(factor),
            "reason": factor.coverage_reason,
        })
    return rows


def manual_evidence_coverage(profile: ProfileDefinition, pillars: list[Any]) -> float:
    return sum(float(profile.pillar_weights.get(pillar.pillar, 0.0)) * float(pillar.evidence_coverage) for pillar in pillars)


def manual_scoring_coverage(profile: ProfileDefinition, pillars: list[Any]) -> float:
    return sum(float(profile.pillar_weights.get(pillar.pillar, 0.0)) * float(pillar.scoring_coverage) for pillar in pillars)


def manual_coverage(profile: ProfileDefinition, pillars: list[Any]) -> float:
    """Backward-compatible alias for manual evidence coverage."""
    return manual_evidence_coverage(profile, pillars)


def manual_pillar_score(factors: list[FactorScore], pillar: str) -> float | None:
    eligible = [item for item in factors if item.pillar == pillar and item.status == "SCORED" and item.score is not None]
    total = sum(item.weight for item in eligible)
    return sum(float(item.score) * item.weight for item in eligible) / total if total else None


def dependency_audit(factors: list[FactorScore]) -> list[dict[str, Any]]:
    groups: dict[str, list[FactorScore]] = defaultdict(list)
    for factor in factors:
        groups[factor.dependency_group].append(factor)
    result = []
    for group, values in groups.items():
        if not group:
            continue
        shared = sorted({record for factor in values for record in factor.supporting_record_ids})
        result.append({"dependency_group": group, "factor_keys": [factor.factor_key for factor in values], "shared_record_ids": shared})
    return result


def audit_company(ticker: str, as_of: str, profile_name: str = "core_v1", *, artifacts_root: str = "artifacts", config_root: str = "config/scoring") -> dict[str, Any]:
    profile, factors, pillars, snapshot = score_company(ticker, as_of, profile_name, artifacts_root=artifacts_root, config_root=config_root)
    return {
        "profile": {"name": profile.profile, "version": profile.profile_version, "rules_version": profile.factor_rules_version,
                     "coverage_version": profile.coverage_version, "calibration_status": profile.calibration_status},
        "factors": factor_audit_rows(factors, profile),
        "pillars": [pillar.to_dict() for pillar in pillars],
        "snapshot": snapshot.to_dict(),
        "manual_coverage": manual_evidence_coverage(profile, pillars),
        "manual_evidence_coverage": manual_evidence_coverage(profile, pillars),
        "manual_scoring_coverage": manual_scoring_coverage(profile, pillars),
        "manual_confidence": correlated_factor_confidence([factor for factor in factors if factor.status == "SCORED"]),
        "dependencies": dependency_audit(factors),
    }
