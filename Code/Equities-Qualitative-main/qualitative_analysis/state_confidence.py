"""Explainable deterministic confidence calculation for synthesized states."""

from __future__ import annotations

from typing import Any, Sequence

from .state_rules import change_polarity
from .temporal_models import TemporalChange


def calculate_confidence(
    *,
    evidence_groups: Sequence[Any],
    changes: Sequence[TemporalChange],
    current_state: str,
    trend: str,
    contradiction_count: int,
    topic: str | None,
    period_label: str | None,
    trend_factor: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Return confidence and the factors used to derive it.

    Evidence groups are already deduplicated by the synthesis provider. This
    function therefore rewards independent agreement and penalizes explicit
    contradiction without counting repeated management language mechanically.
    """
    independent = min(1.0, len(evidence_groups) / 3.0)
    source_documents = len({getattr(group.representative, "document_id", "") for group in evidence_groups if getattr(group.representative, "document_id", "")})
    quality = sum(group.quality for group in evidence_groups) / len(evidence_groups) if evidence_groups else 0.0
    agreement = max(0.25, 1.0 - (0.65 * contradiction_count))
    temporal = 1.0 if any(change_polarity(change) != 0 or change.change_type in {"stable", "reiterated"} for change in changes) else (0.72 if trend_factor.get("source") == "comparative_claims" else 0.45)
    specificity = 1.0 if topic else 0.86
    period_certainty = 1.0 if period_label else 0.60
    confidence = 0.15 + 0.20 * independent + 0.22 * quality + 0.20 * agreement + 0.10 * temporal + 0.08 * specificity + 0.05 * period_certainty
    # Several passages in one transcript are useful corroboration, but they
    # are not independent source documents. Keep confidence conservative until
    # a state is corroborated outside the originating document.
    if source_documents <= 1 and len(evidence_groups) > 1:
        confidence = min(confidence, 0.90)
    if current_state == "unknown":
        confidence = min(confidence, 0.58)
    confidence = max(0.0, min(1.0, round(confidence, 4)))
    return confidence, {
        "independent_evidence_groups": len(evidence_groups),
        "source_document_count": source_documents,
        "evidence_quality": round(quality, 4),
        "agreement": round(agreement, 4),
        "temporal_support": round(temporal, 4),
        "topic_specificity": round(specificity, 4),
        "period_certainty": round(period_certainty, 4),
        "contradiction_count": contradiction_count,
        "trend_source": trend_factor.get("source"),
        "state": current_state,
        "trend": trend,
    }


__all__ = ["calculate_confidence"]
