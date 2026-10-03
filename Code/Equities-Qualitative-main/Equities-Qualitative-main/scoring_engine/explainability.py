from __future__ import annotations

from .models import FactorScore


def explain_factor(factor: FactorScore) -> dict:
    return {
        "Factor": factor.factor_name,
        "Raw input": factor.raw_inputs,
        "Rule": f"{factor.rule_id}@{factor.rule_version}",
        "Score": factor.score,
        "Weight": factor.weight,
        "Effective weight": factor.effective_weight,
        "Contribution": factor.weighted_contribution,
        "Evidence coverage": factor.evidence_coverage,
        "Scoring coverage": factor.scoring_coverage,
        "Confidence": factor.confidence,
        "Supporting records": factor.supporting_record_ids,
        "Why scored": factor.coverage_reason if factor.status == "SCORED" else "",
        "Why not scored": factor.coverage_reason if factor.status != "SCORED" else "",
    }
