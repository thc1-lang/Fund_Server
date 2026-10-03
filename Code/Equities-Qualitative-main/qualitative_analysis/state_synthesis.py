"""Deterministic synthesis of current qualitative states."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .extraction_models import QualitativeClaim
from .state_confidence import calculate_confidence
from .state_models import PROVIDER_VERSION, RULES_VERSION, STATE_VERSION, QualitativeState, StateSynthesisResult
from .state_rules import (
    absolute_signal,
    claim_polarity,
    change_polarity,
    evidence_key,
    evidence_quality,
    state_for_signals,
    state_label,
    trend_for_changes,
)
from .temporal_comparison import ACTUAL_PERIOD_TYPES, period_sort_key, period_type
from .temporal_models import TemporalChange
from .topic_normalization import topic_identity


_GENERIC_SUBJECTS = {
    "general_demand", "general_revenue", "general_guidance", "general_margin",
}

# Topic states are deliberately sparse. The extraction layer can produce
# sentence fragments as fallback subjects; those are useful provenance for a
# claim but are not stable analytical topics. Keep only recurring, domain-
# meaningful subjects and omit exact dimension mirrors.
_USEFUL_TOPIC_SUBJECTS = {
    "acquisition", "ai", "bookings", "capex", "customer_experience",
    "deferred_revenue", "enterprise", "free_cash_flow_margin", "gross_margin",
    "international", "net_dollar_retention", "online", "operating_margin",
    "product_named", "profitability", "revenue", "retention", "share_repurchase",
}
_TOPIC_SUBJECTS_BY_DIMENSION = {
    "balance_sheet": set(),
    "capital_allocation": {"acquisition", "share_repurchase"},
    "costs": {"ai"},
    "customer_growth": {"ai", "enterprise", "customer_experience"},
    "customer_retention": {"retention"},
    "demand": {"ai", "bookings", "enterprise", "customer_experience", "international"},
    "geography": {"international"},
    "guidance": {"acquisition", "capex", "deferred_revenue", "enterprise", "profitability", "revenue"},
    "inventory": {"revenue"},
    "margins": {"free_cash_flow_margin", "gross_margin", "operating_margin", "revenue"},
    "pricing": {"online", "retention", "revenue"},
    "product": {"ai", "international", "product_named"},
    "regulation": {"international"},
    "revenue": {"enterprise", "international", "net_dollar_retention", "product_named", "profitability"},
    "risks": {"revenue"},
}


def stable_state_id(
    ticker: str,
    dimension: str,
    topic: str | None,
    subtopic: str | None,
    period_label: str | None,
    state_version: str = STATE_VERSION,
) -> str:
    identity = "|".join((ticker.upper(), dimension, topic or "", subtopic or "", period_label or "", state_version))
    return "state_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


class StateSynthesisProvider(ABC):
    """Provider contract for deterministic or future model-assisted synthesis."""

    provider_version = PROVIDER_VERSION
    state_version = STATE_VERSION
    rules_version = RULES_VERSION

    @abstractmethod
    def synthesize(
        self,
        claims: Sequence[QualitativeClaim],
        temporal_changes: Sequence[TemporalChange],
    ) -> StateSynthesisResult:
        raise NotImplementedError


@dataclass
class _EvidenceGroup:
    representative: QualitativeClaim
    claims: list[QualitativeClaim]
    polarity: int
    quality: float


class DeterministicStateSynthesisProvider(StateSynthesisProvider):
    """Conservative state provider that preserves conflicting evidence."""

    def synthesize(
        self,
        claims: Sequence[QualitativeClaim],
        temporal_changes: Sequence[TemporalChange],
    ) -> StateSynthesisResult:
        if not claims:
            return StateSynthesisResult(ticker="", period_label=None)
        ticker = claims[0].ticker.upper()
        # Current state is based on the latest issuer reporting period. A FY
        # guidance reference or an unclassified annual phrase cannot outrank a
        # completed quarter merely because a scalar sort key is larger.
        actual_claims = [claim for claim in claims if period_type(claim) in ACTUAL_PERIOD_TYPES]
        quarter_claims = [claim for claim in actual_claims if period_type(claim) == "QUARTER_ACTUAL"]
        eligible_claims = quarter_claims or [claim for claim in actual_claims if period_type(claim) == "ANNUAL_ACTUAL"] or actual_claims
        if not eligible_claims:
            eligible_claims = list(claims)
        latest_key = max(period_sort_key(claim) for claim in eligible_claims)
        current_claims = [claim for claim in eligible_claims if period_sort_key(claim) == latest_key]
        period_label = next((claim.period_label for claim in current_claims if claim.period_label), None)
        current_changes = [change for change in temporal_changes if change.to_period == period_label]
        dimensions = sorted({claim.dimension for claim in current_claims})
        states: list[QualitativeState] = []
        rejected: list[str] = []

        for dimension in dimensions:
            dimension_claims = [claim for claim in current_claims if claim.dimension == dimension]
            dimension_changes = [change for change in current_changes if change.dimension == dimension]
            subjects = sorted({topic_identity(claim).normalized_subject for claim in dimension_claims})
            topic_states: list[QualitativeState] = []
            for subject in subjects:
                if not self._is_useful_topic(dimension, subject):
                    if subject not in _GENERIC_SUBJECTS:
                        rejected.append(f"{dimension}:{subject}: low-value topic omitted")
                    continue
                topic_claims = [claim for claim in dimension_claims if topic_identity(claim).normalized_subject == subject]
                topic_changes = [change for change in dimension_changes if (change.normalized_subject or "") == subject]
                topic_state, errors = self._build_state(topic_claims, topic_changes, period_label, topic=subject, topic_names=[subject])
                if topic_state.current_state == "unknown" and topic_state.trend == "unknown" and topic_state.unknown_reason == "no_directional_evidence":
                    rejected.append(f"{dimension}:{subject}: unknown topic omitted without directional evidence")
                    rejected.extend(errors)
                    continue
                topic_states.append(topic_state)
                rejected.extend(errors)
            topic_names = [state.topic for state in topic_states if state.topic]
            state, errors = self._build_state(
                dimension_claims,
                dimension_changes,
                period_label,
                topic=None,
                topic_names=topic_names,
                child_state_ids=[item.state_id for item in topic_states],
            )
            states.append(state)
            states.extend(topic_states)
            rejected.extend(errors)

        states.sort(key=lambda state: (state.dimension, state.topic or "", state.state_id))
        validation_failures = self._validate(states, claims, temporal_changes)
        return StateSynthesisResult(
            ticker=ticker,
            period_label=period_label,
            claims_considered=len(current_claims),
            temporal_changes_considered=len(current_changes),
            states=states,
            rejected_groups=rejected,
            validation_failures=validation_failures,
        )

    def _build_state(
        self,
        claims: list[QualitativeClaim],
        changes: list[TemporalChange],
        period_label: str | None,
        *,
        topic: str | None,
        topic_names: list[str],
        child_state_ids: list[str] | None = None,
    ) -> tuple[QualitativeState, list[str]]:
        first = claims[0]
        groups: dict[str, list[QualitativeClaim]] = {}
        for claim in claims:
            groups.setdefault(evidence_key(claim.evidence_text), []).append(claim)
        evidence_groups: list[_EvidenceGroup] = []
        for values in groups.values():
            representative = max(values, key=lambda claim: (evidence_quality(claim), bool(claim.magnitude), claim.claim_id))
            evidence_groups.append(_EvidenceGroup(representative, values, claim_polarity(representative), evidence_quality(representative)))
        evidence_groups.sort(key=lambda group: group.representative.claim_id)
        duplicate_groups_removed = max(0, len(claims) - len(evidence_groups))

        positive_groups = [group for group in evidence_groups if group.polarity > 0]
        negative_groups = [group for group in evidence_groups if group.polarity < 0]
        neutral_groups = [group for group in evidence_groups if group.polarity == 0 and re.search(r"\b(?:stable|maintain|unchanged|consistent|low churn)\b", group.representative.evidence_text.lower())]
        weighted_score = sum(group.quality * group.polarity for group in evidence_groups)
        positive_weight = sum(group.quality for group in positive_groups)
        negative_weight = sum(group.quality for group in negative_groups)
        absolute_positive = sum(absolute_signal(claim) > 0 for group in positive_groups for claim in group.claims)
        absolute_negative = sum(absolute_signal(claim) < 0 for group in negative_groups for claim in group.claims)
        current_state = state_for_signals(
            first.dimension,
            weighted_score,
            len(positive_groups),
            len(negative_groups),
            len(evidence_groups),
            len(neutral_groups),
            absolute_positive=absolute_positive,
            absolute_negative=absolute_negative,
        )
        trend, trend_factor = trend_for_changes(changes, claims)

        all_claim_ids = [claim.claim_id for claim in claims]
        positive_ids = [claim.claim_id for group in positive_groups for claim in group.claims]
        negative_ids = [claim.claim_id for group in negative_groups for claim in group.claims]
        neutral_ids = [claim.claim_id for group in neutral_groups for claim in group.claims]
        supporting_changes = [change.change_id for change in changes]
        positive_changes = [change.change_id for change in changes if change_polarity(change) > 0]
        negative_changes = [change.change_id for change in changes if change_polarity(change) < 0]

        if positive_groups and negative_groups:
            supporting_claim_ids = all_claim_ids
            contradicting_claim_ids = negative_ids if positive_weight >= negative_weight else positive_ids
            supporting_change_ids = supporting_changes or [change.change_id for change in changes]
            contradicting_change_ids = negative_changes if positive_weight >= negative_weight else positive_changes
        elif positive_groups:
            supporting_claim_ids = positive_ids + neutral_ids
            contradicting_claim_ids = negative_ids
            supporting_change_ids = supporting_changes
            contradicting_change_ids = negative_changes
        elif negative_groups:
            supporting_claim_ids = negative_ids + neutral_ids
            contradicting_claim_ids = positive_ids
            supporting_change_ids = supporting_changes
            contradicting_change_ids = positive_changes
        else:
            supporting_claim_ids = all_claim_ids
            contradicting_claim_ids = []
            supporting_change_ids = supporting_changes
            contradicting_change_ids = []

        contradiction_groups = min(len(positive_groups), len(negative_groups)) if positive_groups and negative_groups else 0
        confidence, factors = calculate_confidence(
            evidence_groups=evidence_groups,
            changes=changes,
            current_state=current_state,
            trend=trend,
            contradiction_count=contradiction_groups,
            topic=topic,
            period_label=period_label,
            trend_factor=trend_factor,
        )
        summary = self._summary(first.dimension, current_state, trend, period_label, len(evidence_groups), topic, topic_names)
        unknown_reason = self._unknown_reason(current_state, positive_groups, negative_groups, neutral_groups)
        if current_state == "unknown" and unknown_reason:
            summary = self._summary(first.dimension, current_state, trend, period_label, len(evidence_groups), topic, topic_names, unknown_reason)
        document_ids = sorted({claim.document_id for claim in claims})
        source_urls = sorted({claim.source_url for claim in claims if claim.source_url})
        source_local_paths = sorted({claim.local_path for claim in claims if claim.local_path})
        state = QualitativeState(
            state_id=stable_state_id(first.ticker, first.dimension, topic, first.subtopic, period_label),
            ticker=first.ticker.upper(),
            company_name=first.company_name,
            dimension=first.dimension,
            topic=topic,
            subtopic=first.subtopic,
            as_of_fiscal_year=first.fiscal_year,
            as_of_fiscal_quarter=first.fiscal_quarter,
            period_label=period_label,
            current_state=current_state,
            trend=trend,
            summary=summary,
            supporting_claim_ids=sorted(set(supporting_claim_ids)),
            supporting_change_ids=sorted(set(supporting_change_ids)),
            contradicting_claim_ids=sorted(set(contradicting_claim_ids)),
            contradicting_change_ids=sorted(set(contradicting_change_ids)),
            confidence=confidence,
            evidence_count=len(evidence_groups),
            contradiction_count=factors.get("contradiction_count", 0),
            state_method="deterministic_evidence_hierarchy",
            state_version=STATE_VERSION,
            rules_version=RULES_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(),
            evidence_groups_considered=len(groups),
            evidence_groups_retained=len(evidence_groups),
            evidence_groups_rejected=0,
            duplicate_groups_removed=duplicate_groups_removed,
            confidence_factors=factors,
            supporting_document_ids=document_ids,
            source_urls=source_urls,
            source_local_paths=source_local_paths,
            child_state_ids=sorted(child_state_ids or []),
            unknown_reason=unknown_reason,
            provider=PROVIDER_VERSION,
            period_type=period_type(first),
            period_end_date=getattr(first, "period_end_date", None),
            issuer_fiscal_calendar=dict(getattr(first, "issuer_fiscal_calendar", {}) or {}),
        )
        return state, []

    @staticmethod
    def _is_useful_topic(dimension: str, subject: str) -> bool:
        if subject in _GENERIC_SUBJECTS or subject not in _USEFUL_TOPIC_SUBJECTS:
            return False
        allowed = _TOPIC_SUBJECTS_BY_DIMENSION.get(dimension)
        if allowed is not None and subject not in allowed:
            return False
        # A topic equal to its parent dimension is an atomic mirror, not an
        # additional analytical state.
        if subject == dimension:
            return False
        if dimension == "revenue" and subject == "revenue":
            return False
        return True

    @staticmethod
    def _unknown_reason(current_state: str, positive_groups: list[_EvidenceGroup], negative_groups: list[_EvidenceGroup], neutral_groups: list[_EvidenceGroup]) -> str | None:
        if current_state != "unknown":
            return None
        if positive_groups and negative_groups:
            return "conflicting_evidence"
        if positive_groups or negative_groups:
            return "direction_without_absolute_anchor"
        if neutral_groups:
            return "neutral_only"
        return "no_directional_evidence"

    @staticmethod
    def _summary(
        dimension: str,
        current_state: str,
        trend: str,
        period_label: str | None,
        evidence_count: int,
        topic: str | None,
        topic_names: list[str],
        unknown_reason: str | None = None,
    ) -> str:
        label = state_label(topic or dimension)
        if topic and dimension == "risks":
            label = f"{label} risk"
        if current_state == "mixed":
            subjects = ", ".join(state_label(item) for item in topic_names[:4])
            detail = f" across {subjects}" if subjects else ""
            return f"{label.capitalize()} evidence is mixed{detail}."
        if current_state == "unknown":
            detail = f" ({unknown_reason})" if unknown_reason else ""
            return f"Current {label} evidence is insufficient to classify the state{detail}."
        period = f" in {period_label}" if period_label else ""
        trend_text = f" Sequential trend is {trend}." if trend != "unknown" else ""
        verb = "are" if label.lower() in {"margins", "risks", "costs", "catalysts", "bookings"} else "is"
        return f"{label.capitalize()} {verb} {current_state}{period}, based on {evidence_count} deduplicated evidence group(s)." + trend_text

    @staticmethod
    def _validate(states: list[QualitativeState], claims: Sequence[QualitativeClaim], changes: Sequence[TemporalChange]) -> list[str]:
        claim_ids = {claim.claim_id for claim in claims}
        change_ids = {change.change_id for change in changes}
        errors: list[str] = []
        for state in states:
            if not state.supporting_claim_ids and state.current_state != "unknown":
                errors.append(f"{state.state_id}: non-unknown state has no claim provenance")
            for claim_id in state.supporting_claim_ids + state.contradicting_claim_ids:
                if claim_id not in claim_ids:
                    errors.append(f"{state.state_id}: unknown claim {claim_id}")
            for change_id in state.supporting_change_ids + state.contradicting_change_ids:
                if change_id not in change_ids:
                    errors.append(f"{state.state_id}: unknown change {change_id}")
        return errors


__all__ = ["StateSynthesisProvider", "DeterministicStateSynthesisProvider", "stable_state_id"]
