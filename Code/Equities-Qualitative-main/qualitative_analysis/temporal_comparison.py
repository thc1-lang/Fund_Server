"""Deterministic comparison of evidence-backed claims across periods."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Sequence

from .extraction_models import QualitativeClaim
from .temporal_models import TemporalChange, TemporalComparisonResult, TemporalEvidence
from .temporal_rules import classify_matched, classify_new, extract_metric, text_similarity
from .topic_normalization import TopicIdentity, topic_identity


COMPARISON_VERSION = "temporal-comparison-v2"
RULES_VERSION = "temporal-rules-v2"
PROVIDER_VERSION = "deterministic-temporal-v2"

ACTUAL_PERIOD_TYPES = {"QUARTER_ACTUAL", "ANNUAL_ACTUAL", "YTD_ACTUAL"}


def period_type(value: Any) -> str:
    explicit = str(getattr(value, "period_type", "") or "").upper()
    if explicit and explicit != "UNKNOWN":
        return explicit
    label = str(getattr(value, "period_label", value) or "").upper()
    if re.search(r"\bQ[1-4]\b", label):
        return "QUARTER_ACTUAL"
    if re.search(r"\bFY\s*(?:19|20)\d{2}\b", label) or re.search(r"\b(?:19|20)\d{2}\b", label) and not re.search(r"\bQ[1-4]\b", label):
        return "ANNUAL_ACTUAL"
    return "UNKNOWN"


def compatible_period_types(left: Any, right: Any) -> bool:
    left_type, right_type = period_type(left), period_type(right)
    return left_type == right_type and left_type in ACTUAL_PERIOD_TYPES


def claims_available_as_of(claims: Sequence[QualitativeClaim], as_of_date: str | None) -> list[QualitativeClaim]:
    """Admit known evidence only when its availability is on/before cutoff.

    Unknown availability is retained with its explicit UNKNOWN provenance so
    callers can apply the repository's conservative unknown-date policy rather
    than silently assigning a pre-cutoff date.
    """
    if not as_of_date:
        return list(claims)
    return [claim for claim in claims if not claim.available_date or str(claim.available_date)[:10] <= as_of_date]


def period_sort_key(claim_or_period: Any) -> tuple[int, int, str]:
    """Order fiscal periods before publication dates, including non-calendar years."""
    if isinstance(claim_or_period, str):
        value = claim_or_period.upper()
        year_match = re.search(r"(?:FY\s*)?(20\d{2}|19\d{2})", value)
        quarter_match = re.search(r"Q([1-4])", value)
        return (int(year_match.group(1)) if year_match else -1, int(quarter_match.group(1)) if quarter_match else 5, value)
    year = getattr(claim_or_period, "fiscal_year", None)
    quarter = getattr(claim_or_period, "fiscal_quarter", None)
    label = getattr(claim_or_period, "period_label", None) or ""
    if year is None:
        return period_sort_key(label)
    return (int(year), int(quarter) if quarter in {1, 2, 3, 4} else 5, str(label))


def period_groups(claims: Sequence[QualitativeClaim]) -> dict[tuple[int, int, str], list[QualitativeClaim]]:
    grouped: dict[tuple[int, int, str], list[QualitativeClaim]] = {}
    for claim in claims:
        key = period_sort_key(claim)
        grouped.setdefault(key, []).append(claim)
    return grouped


def period_label(claims: Sequence[QualitativeClaim]) -> str:
    if not claims:
        return "UNKNOWN"
    label = getattr(claims[0], "period_label", None)
    if label:
        return str(label)
    year = getattr(claims[0], "fiscal_year", None)
    quarter = getattr(claims[0], "fiscal_quarter", None)
    if year is not None and quarter:
        return f"Q{quarter} {year}"
    if year is not None:
        return f"FY {year}"
    return "UNKNOWN"


def select_latest_vs_previous(claims: Sequence[QualitativeClaim]) -> tuple[list[QualitativeClaim], list[QualitativeClaim], list[str]] | None:
    groups = period_groups(claims)
    # Guidance horizons and unknown references are never ordinary sequential
    # reporting periods. If quarters exist, compare quarters with quarters;
    # otherwise compare completed annual/YTD groups of the same type.
    eligible = {key: values for key, values in groups.items() if period_type(values[0]) in ACTUAL_PERIOD_TYPES}
    quarter_groups = {key: values for key, values in eligible.items() if period_type(values[0]) == "QUARTER_ACTUAL"}
    if quarter_groups:
        groups = quarter_groups
    else:
        annual_groups = {key: values for key, values in eligible.items() if period_type(values[0]) == "ANNUAL_ACTUAL"}
        groups = annual_groups or eligible
    ordered = sorted(groups.items(), key=lambda item: item[0])
    available = [period_label(value) for _, value in ordered]
    if len(ordered) < 2:
        return None
    return ordered[-2][1], ordered[-1][1], available


def _base_key(claim: QualitativeClaim) -> tuple[str, str, str]:
    identity = topic_identity(claim)
    # A capex outlook can be extracted under ``guidance`` in one transcript
    # and under ``capex`` in another. Treat that one explicit subject as the
    # same comparison family while keeping revenue guidance separate from
    # realized revenue.
    if identity.normalized_subject == "capex":
        return ("capex", "capex", "capex")
    return (identity.dimension, identity.topic, identity.normalized_subject)


def _claim_evidence(claim: QualitativeClaim) -> TemporalEvidence:
    return TemporalEvidence(
        claim_id=claim.claim_id,
        evidence_text=claim.evidence_text,
        document_id=claim.document_id,
        source_location=dict(claim.source_location),
        period_label=claim.period_label,
    )


def stable_change_id(
    ticker: str,
    normalized_subject: str,
    from_period: str,
    to_period: str,
    from_claim_ids: Sequence[str],
    to_claim_ids: Sequence[str],
    comparison_version: str = COMPARISON_VERSION,
) -> str:
    identity = "|".join((ticker.upper(), normalized_subject, from_period, to_period, ",".join(sorted(from_claim_ids)), ",".join(sorted(to_claim_ids)), comparison_version))
    return "tch_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _match_score(previous: QualitativeClaim, current: QualitativeClaim) -> float:
    previous_identity, current_identity = topic_identity(previous), topic_identity(current)
    if _base_key(previous) != _base_key(current):
        return -1.0
    if previous_identity.metric_unit and current_identity.metric_unit and previous_identity.metric_unit != current_identity.metric_unit:
        return -1.0
    score = 0.35
    if previous_identity.metric_unit == current_identity.metric_unit and previous_identity.metric_unit is not None:
        score += 0.25
    score += min(0.40, text_similarity(previous, current))
    return score


def _explicit_removal(claim: QualitativeClaim) -> bool:
    return bool(re.search(r"\b(?:no longer|resolved|removed|eliminated|discontinued|withdrawn|cancelled|ended|stopped)\b", claim.evidence_text.lower()))


def _low_value_current_only(claim: QualitativeClaim) -> bool:
    """Exclude generic promotional language that supplies no temporal fact."""
    if claim.dimension != "catalysts":
        return False
    if getattr(claim, "direction", "unknown") != "unknown" or extract_metric(claim) is not None:
        return False
    return not re.search(r"\b(?:launched|approved|closed|completed|acquired|revised|raised|lowered)\b", claim.evidence_text.lower())


def _make_change(previous: list[QualitativeClaim], current: list[QualitativeClaim], classification: dict[str, Any], from_period: str, to_period: str) -> TemporalChange:
    anchor = current[0] if current else previous[0]
    identity = topic_identity(anchor)
    from_ids = [claim.claim_id for claim in previous]
    to_ids = [claim.claim_id for claim in current]
    return TemporalChange(
        change_id=stable_change_id(anchor.ticker, identity.normalized_subject, from_period, to_period, from_ids, to_ids),
        ticker=anchor.ticker,
        company_name=anchor.company_name,
        dimension=anchor.dimension,
        topic=anchor.topic,
        subtopic=anchor.subtopic,
        from_period=from_period,
        to_period=to_period,
        change_type=classification["change_type"],
        direction=classification.get("direction", "unknown"),
        summary=classification["summary"],
        from_claim_ids=from_ids,
        to_claim_ids=to_ids,
        from_evidence=[_claim_evidence(claim) for claim in previous],
        to_evidence=[_claim_evidence(claim) for claim in current],
        confidence=float(classification.get("confidence", 0.0)),
        comparison_method=classification.get("comparison_method", "deterministic"),
        comparison_version=COMPARISON_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        normalized_subject=identity.normalized_subject,
        metric_unit=classification.get("metric_unit") or identity.metric_unit,
        rules_version=RULES_VERSION,
        provider=PROVIDER_VERSION,
    )


class TemporalComparisonProvider(ABC):
    """Provider contract for deterministic or future model-assisted comparison."""

    provider_version = PROVIDER_VERSION
    comparison_version = COMPARISON_VERSION

    @abstractmethod
    def compare(self, previous_claims: Sequence[QualitativeClaim], current_claims: Sequence[QualitativeClaim], from_period: str, to_period: str) -> TemporalComparisonResult:
        raise NotImplementedError


class DeterministicTemporalComparisonProvider(TemporalComparisonProvider):
    """Conservative provider that never compares unrelated normalized subjects."""

    def compare(self, previous_claims: Sequence[QualitativeClaim], current_claims: Sequence[QualitativeClaim], from_period: str, to_period: str) -> TemporalComparisonResult:
        result = TemporalComparisonResult(
            ticker=(current_claims[0].ticker if current_claims else previous_claims[0].ticker if previous_claims else ""),
            from_period=from_period,
            to_period=to_period,
            previous_claims=len(previous_claims),
            current_claims=len(current_claims),
        )
        previous_by_key: dict[tuple[str, str, str], list[QualitativeClaim]] = {}
        current_by_key: dict[tuple[str, str, str], list[QualitativeClaim]] = {}
        for claim in previous_claims:
            previous_by_key.setdefault(_base_key(claim), []).append(claim)
        for claim in current_claims:
            current_by_key.setdefault(_base_key(claim), []).append(claim)

        used_previous: set[str] = set()
        for key, current_group in current_by_key.items():
            previous_group = previous_by_key.get(key, [])
            previous_signs = {1 if getattr(claim, "direction", "unknown") in {"increasing", "improving", "accelerating", "raised", "positive"} else -1 if getattr(claim, "direction", "unknown") in {"decreasing", "deteriorating", "decelerating", "lowered", "negative"} else 0 for claim in previous_group}
            current_signs = {1 if getattr(claim, "direction", "unknown") in {"increasing", "improving", "accelerating", "raised", "positive"} else -1 if getattr(claim, "direction", "unknown") in {"decreasing", "deteriorating", "decelerating", "lowered", "negative"} else 0 for claim in current_group}
            if previous_group and ({-1, 1} <= previous_signs or {-1, 1} <= current_signs):
                used_previous.update(claim.claim_id for claim in previous_group)
                result.topics_matched += 1
                result.changes.append(_make_change(previous_group, current_group, {"change_type": "mixed", "direction": "mixed", "summary": f"{current_group[0].topic or current_group[0].dimension} evidence was mixed within the compared periods.", "confidence": 0.78, "comparison_method": "within_period_conflict", "metric_unit": topic_identity(current_group[0]).metric_unit}, from_period, to_period))
                continue
            unmatched_current: list[QualitativeClaim] = []
            for current in sorted(current_group, key=lambda item: item.claim_id):
                candidates = [claim for claim in previous_group if claim.claim_id not in used_previous]
                scored = sorted(((claim, _match_score(claim, current)) for claim in candidates), key=lambda item: item[1], reverse=True)
                current_identity = topic_identity(current)
                threshold = 0.42 if current.dimension in {"demand", "customer_growth", "customer_retention"} and current_identity.normalized_subject in {"ai", "enterprise", "retention", "bookings"} else 0.52
                if scored and scored[0][1] >= threshold:
                    previous, score = scored[0]
                    used_previous.add(previous.claim_id)
                    result.topics_matched += 1
                    classification = classify_matched(previous, current)
                    if classification.get("rejected"):
                        result.rejected.append(f"{current.claim_id}: {classification['rejected']}")
                        continue
                    if classification.get("change_type") == "insufficient_evidence":
                        result.rejected.append(f"{current.claim_id}: insufficient evidence")
                        continue
                    result.changes.append(_make_change([previous], [current], classification, from_period, to_period))
                else:
                    # A same-subject pair with explicit but incompatible units is
                    # rejected instead of being silently treated as a new topic.
                    incompatible = next((claim for claim in candidates if topic_identity(claim).metric_unit and topic_identity(current).metric_unit and topic_identity(claim).metric_unit != topic_identity(current).metric_unit), None)
                    if incompatible:
                        result.rejected.append(f"{current.claim_id}: incompatible metric units")
                        continue
                    unmatched_current.append(current)
            if unmatched_current:
                anchor = next((claim for claim in unmatched_current if re.search(r"\b(?:approved|launched|acquired|initiated|submitted|authorized|completed|closed)\b", claim.evidence_text.lower())), unmatched_current[0])
                if _low_value_current_only(anchor):
                    result.rejected.append(f"{anchor.claim_id}: low-value generic current-only mention")
                    continue
                result.changes.append(_make_change([], unmatched_current, classify_new(anchor), from_period, to_period))

        # Silence is intentionally not converted into removed/risk_removed. A
        # removal requires an explicit current-period statement.
        for current in current_claims:
            if _explicit_removal(current) and current.claim_id not in {claim_id for change in result.changes for claim_id in change.to_claim_ids}:
                result.changes.append(_make_change([], [current], classify_new(current), from_period, to_period))

        result.changes.sort(key=lambda change: change.change_id)
        result.evidence_validation_failures.extend(validate_temporal_changes(result.changes, previous_claims, current_claims))
        return result


def validate_temporal_changes(changes: Sequence[TemporalChange], previous_claims: Sequence[QualitativeClaim], current_claims: Sequence[QualitativeClaim]) -> list[str]:
    previous = {claim.claim_id: claim for claim in previous_claims}
    current = {claim.claim_id: claim for claim in current_claims}
    errors: list[str] = []
    for change in changes:
        if not change.from_claim_ids and not change.to_claim_ids:
            errors.append(f"{change.change_id}: no claim provenance")
            continue
        for evidence in change.from_evidence:
            claim = previous.get(evidence.claim_id)
            if claim is None or claim.evidence_text != evidence.evidence_text:
                errors.append(f"{change.change_id}: invalid from evidence {evidence.claim_id}")
        for evidence in change.to_evidence:
            claim = current.get(evidence.claim_id)
            if claim is None or claim.evidence_text != evidence.evidence_text:
                errors.append(f"{change.change_id}: invalid to evidence {evidence.claim_id}")
    return errors


def compare_latest_vs_previous(ticker: str, claims: Sequence[QualitativeClaim], dimension: str | None = None, provider: TemporalComparisonProvider | None = None) -> TemporalComparisonResult:
    scoped = [claim for claim in claims if claim.ticker.upper() == ticker.upper() and (dimension is None or claim.dimension == dimension)]
    selected = select_latest_vs_previous(scoped)
    if selected is None:
        groups = period_groups(scoped)
        return TemporalComparisonResult(ticker=ticker.upper(), status="INSUFFICIENT_PERIODS", available_periods=[period_label(value) for _, value in sorted(groups.items())], previous_claims=0, current_claims=0)
    previous, current, available = selected
    if not compatible_period_types(previous[0], current[0]):
        return TemporalComparisonResult(ticker=ticker.upper(), status="NOT_COMPARABLE_PERIOD_TYPES", comparison_status="NOT_COMPARABLE_PERIOD_TYPES", available_periods=available, from_period=period_label(previous), to_period=period_label(current))
    result = (provider or DeterministicTemporalComparisonProvider()).compare(previous, current, period_label(previous), period_label(current))
    result.available_periods = available
    return result


__all__ = [
    "TemporalComparisonProvider", "DeterministicTemporalComparisonProvider", "compare_latest_vs_previous",
    "period_sort_key", "period_groups", "select_latest_vs_previous", "stable_change_id", "validate_temporal_changes", "claims_available_as_of",
]
