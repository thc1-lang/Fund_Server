from __future__ import annotations

from collections import defaultdict
from typing import Any

from .coverage import weighted_coverage
from .confidence import correlated_factor_confidence
from .models import CompanyScoreSnapshot, FactorScore, PillarScore, ProfileDefinition
from .utils import now, stable_id


def aggregate(ticker: str, as_of: str, profile: ProfileDefinition, factors: list[FactorScore]) -> tuple[list[PillarScore], CompanyScoreSnapshot]:
    pillars: list[PillarScore] = []
    by_pillar: dict[str, list[FactorScore]] = defaultdict(list)
    for factor in factors:
        by_pillar[factor.pillar].append(factor)
    for pillar, configured_weight in profile.pillar_weights.items():
        values = by_pillar.get(pillar, [])
        configured = sum(item.weight for item in values)
        metrics = weighted_coverage(values, configured) if values else None
        evidence_coverage = metrics.evidence_coverage if metrics else 0.0
        scoring_coverage = metrics.scoring_coverage if metrics else 0.0
        eligible = [item for item in values if item.status == "SCORED" and item.score is not None and item.evidence_coverage >= (profile.factor(item.factor_key).min_coverage if profile.factor(item.factor_key) else 0)]
        eligible_weight = sum(item.weight for item in eligible)
        threshold = profile.pillar_scoring_threshold(pillar)
        calculated_score = sum(item.score * item.weight for item in eligible) / eligible_weight if eligible_weight else None
        if not eligible:
            status = "UNSCORED"
        elif scoring_coverage < threshold:
            status = "INSUFFICIENT_SCORING_COVERAGE"
        else:
            status = "SCORED"
        score = calculated_score if status == "SCORED" else None
        effective: dict[str, float] = {}
        if eligible_weight:
            for item in eligible:
                effective[item.factor_key] = item.weight / eligible_weight
                item.effective_weight = effective[item.factor_key]
                item.weighted_contribution = item.score * effective[item.factor_key] if item.score is not None else None
        confidence = correlated_factor_confidence(eligible)
        pillars.append(PillarScore(
            pillar_score_id=f"pillar:{ticker.upper()}:{as_of}:{profile.profile}:{pillar}:{profile.profile_version}:{stable_id(ticker, as_of, profile.profile, pillar, profile.profile_version)}",
            ticker=ticker.upper(), pillar=pillar, score=score, calculated_score=calculated_score,
            weighted_coverage=evidence_coverage, evidence_coverage=evidence_coverage, scoring_coverage=scoring_coverage,
            confidence=confidence,
            factor_scores=[item.factor_score_id for item in values], eligible_weight=eligible_weight,
            configured_weight=configured, coverage_status=status, created_at=now(), effective_factor_weights=effective,
        ))
    pillar_map = {item.pillar: item for item in pillars}
    overall_evidence_coverage = sum(profile.pillar_weights.get(item.pillar, 0.0) * item.evidence_coverage for item in pillars)
    overall_scoring_coverage = sum(profile.pillar_weights.get(item.pillar, 0.0) * item.scoring_coverage for item in pillars)
    scored = [item for item in factors if item.status == "SCORED" and item.score is not None]
    confidence = correlated_factor_confidence(scored)
    critical_missing = [key for key in profile.critical_factor_keys if not any(item.factor_key == key and item.status == "SCORED" and item.scoring_coverage >= (profile.factor(key).min_coverage if profile.factor(key) else 0) for item in factors)]
    business_ok = pillar_map.get("BUSINESS_QUALITY").scoring_coverage >= profile.pillar_scoring_threshold("BUSINESS_QUALITY") if pillar_map.get("BUSINESS_QUALITY") else False
    if critical_missing or overall_scoring_coverage < profile.scoring_coverage_threshold or not business_ok:
        overall_score = None
        status = "INSUFFICIENT_SCORING_COVERAGE"
    else:
        score_parts = [(pillar_map[pillar].score, weight) for pillar, weight in profile.pillar_weights.items() if pillar in pillar_map and pillar_map[pillar].score is not None and pillar_map[pillar].eligible_weight]
        total_weight = sum(weight for score, weight in score_parts)
        overall_score = sum(score * weight for score, weight in score_parts) / total_weight if total_weight else None
        status = "SCORED" if overall_score is not None and overall_scoring_coverage >= profile.scoring_coverage_threshold else "PARTIAL"
    positive = sorted((item for item in scored if item.weighted_contribution is not None and (item.score or 0) > 50), key=lambda item: item.weighted_contribution or 0, reverse=True)
    negative = sorted((item for item in scored if item.weighted_contribution is not None and (item.score or 0) < 50), key=lambda item: item.weighted_contribution or 0)
    limitations = [f"{item.factor_key}: {item.coverage_reason}" for item in factors if item.status != "SCORED"]
    snapshot = CompanyScoreSnapshot(
        snapshot_id=f"company:{ticker.upper()}:{as_of}:{profile.profile}:{profile.profile_version}:{stable_id(ticker, as_of, profile.profile, profile.profile_version)}",
        ticker=ticker.upper(), as_of_date=as_of, profile=profile.profile, overall_score=overall_score,
        overall_coverage=overall_evidence_coverage, overall_evidence_coverage=overall_evidence_coverage,
        overall_scoring_coverage=overall_scoring_coverage, overall_confidence=confidence,
        pillar_scores=[item.pillar_score_id for item in pillars], scored_factor_count=len(scored),
        unscored_factor_count=len(factors) - len(scored), critical_missing_factors=critical_missing,
        score_status=status, scoring_version=profile.scoring_version, rules_version=profile.factor_rules_version,
        profile_version=profile.profile_version, created_at=now(), coverage_version=profile.coverage_version,
        top_positive_contributors=[item.factor_key for item in positive[:5]], top_negative_contributors=[item.factor_key for item in negative[:5]],
        coverage_limitations=limitations,
    )
    return pillars, snapshot
