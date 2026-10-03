"""Deterministic rules used by current qualitative-state synthesis."""

from __future__ import annotations

import re
from typing import Any

from .extraction_models import QualitativeClaim
from .temporal_models import TemporalChange


POSITIVE_DIRECTIONS = {"improving", "increasing", "accelerating", "raised", "positive"}
NEGATIVE_DIRECTIONS = {"deteriorating", "decreasing", "decelerating", "lowered", "negative"}
STABLE_DIRECTIONS = {"stable", "unknown", "not_applicable"}

_QUALITY = {
    "actual": 1.00,
    "guidance": 0.90,
    "confirmed": 0.86,
    "expectation": 0.78,
    "target": 0.76,
    "estimate": 0.68,
    "unknown": 0.60,
    "possibility": 0.38,
    "risk": 0.42,
}

_ABSOLUTE_POSITIVE = re.compile(
    r"\b(?:strong|robust|solid|healthy|durable|resilient|broad|clear|significant|material|leading|record|well positioned)\b",
    re.I,
)
_ABSOLUTE_NEGATIVE = re.compile(
    r"\b(?:weak|soft|stressed|poor|headwind|declin\w*|deteriorat\w*|compress\w*|materially lower|significant risk)\b",
    re.I,
)
_RETENTION_NEGATIVE = re.compile(r"\b(?:high|rising|increased|increasing|elevated|worsen\w*|worse)\b[^.]{0,40}\bchurn\b|\bchurn\b[^.]{0,40}\b(?:higher|increased|increasing|rising|elevated|worsen\w*)\b", re.I)
_RISK_IMPACT = re.compile(r"\b(?:impact|bps|basis points|material|significant|substantial|exposure|headwind)\b", re.I)
_COST_REDUCTION = re.compile(r"\b(?:reduce|reducing|reduced|lower|lowered|decrease|decreased|saving|savings|efficien)\w*\b[^.]{0,80}\b(?:cost|costs|expense|expenses)\b|\b(?:cost|costs|expense|expenses)\b[^.]{0,80}\b(?:reduce|reducing|reduced|lower|lowered|decrease|decreased|saving|savings|efficien)\w*\b", re.I)
_COST_INCREASE = re.compile(r"\b(?:increase|increased|increasing|raise|raised|raising|higher|growth|grew|up)\w*\b[^.]{0,80}\b(?:cost|costs|expense|expenses|r&d|sg&a)\b|\b(?:cost|costs|expense|expenses|r&d|sg&a)\b[^.]{0,80}\b(?:increase|increased|increasing|raise|raised|raising|higher|growth|grew|up)\w*\b", re.I)


def evidence_quality(claim: QualitativeClaim) -> float:
    """Return a deterministic quality weight without treating frequency as quality."""
    return _QUALITY.get(claim.certainty, _QUALITY["unknown"])


def evidence_key(text: str) -> str:
    """Canonicalize an evidence sentence for duplicate-group detection."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def claim_polarity(claim: QualitativeClaim) -> int:
    """Map a claim to positive, negative, or neutral evidence."""
    direction = str(claim.direction or "unknown").lower()
    text = claim.evidence_text.lower()
    dimension = str(claim.dimension or "").lower()
    if dimension in {"costs", "operating_expenses"}:
        if _COST_REDUCTION.search(text):
            return 1
        if _COST_INCREASE.search(text):
            return -1
    if direction in POSITIVE_DIRECTIONS:
        return 1
    if direction in NEGATIVE_DIRECTIONS:
        return -1
    if re.search(r"\b(?:improv|accelerat|grew|grow|growth|higher|strong|robust|solid|healthy|increased|upward|surpass|on track|momentum)\w*\b", text):
        return 1
    if re.search(r"\b(?:declin|decreas|deteriorat|weaker|lower|headwind|downward|compress)\w*\b", text) or _RETENTION_NEGATIVE.search(text):
        return -1
    return 0


def absolute_signal(claim: QualitativeClaim) -> int:
    """Return explicit current-state strength evidence, separate from trend."""
    text = str(claim.evidence_text or "")
    dimension = str(claim.dimension or "").lower()
    if dimension == "risks":
        return -1 if _RISK_IMPACT.search(text) else 0
    if _RETENTION_NEGATIVE.search(text):
        return -1
    if _ABSOLUTE_NEGATIVE.search(text):
        return -1
    if _ABSOLUTE_POSITIVE.search(text):
        return 1
    return 0


def change_polarity(change: TemporalChange) -> int:
    """Map an audited temporal change to directional evidence."""
    if change.change_type in {"new_risk", "risk_intensified"}:
        return -1
    if change.change_type in {"risk_removed", "risk_eased"}:
        return 1
    direction = str(change.direction or "unknown").lower()
    if direction in POSITIVE_DIRECTIONS:
        return 1
    if direction in NEGATIVE_DIRECTIONS:
        return -1
    return 0


def explicit_comparative_claim(claim: QualitativeClaim) -> bool:
    """Whether one-period evidence explicitly describes a comparison or change."""
    text = claim.evidence_text.lower()
    return bool(re.search(
        r"\b(?:year[- ]over[- ]year|year on year|versus|compared with|compared to|from .* to|continued|accelerat|decelerat|grew|growth|increased|decreased|higher|lower|improv|deteriorat|up \d|down \d)\b",
        text,
    ))


def state_for_signals(
    dimension: str,
    weighted_score: float,
    positive: int,
    negative: int,
    total: int,
    neutral: int = 0,
    *,
    absolute_positive: int = 0,
    absolute_negative: int = 0,
) -> str:
    """Map weighted current evidence to the dimension's controlled state vocabulary."""
    if positive and negative:
        # A strong actual result may outweigh a weak possibility, while close
        # quality/weight remains explicitly mixed. Contradicting IDs are still
        # retained by the synthesis layer in either case.
        if weighted_score >= 0.45:
            positive, negative = positive, 0
        elif weighted_score <= -0.45:
            positive, negative = 0, negative
        else:
            return "mixed"
    if total == 0 or (not positive and not negative and not neutral):
        return "unknown"
    dimension = dimension.lower()
    if dimension == "guidance":
        if positive:
            return "raised"
        if negative:
            return "lowered"
        return "maintained" if neutral else "unknown"
    if dimension == "margins":
        if positive:
            return "expanding"
        if negative:
            return "compressing"
        return "stable" if neutral else "unknown"
    if dimension == "risks":
        if negative:
            if weighted_score < -0.70 and (absolute_negative or negative >= 2):
                return "increasing"
            return "elevated" if absolute_negative or negative >= 2 else "unknown"
        if positive:
            return "low" if absolute_positive or positive >= 2 else "unknown"
        return "moderate" if neutral else "unknown"
    if dimension == "product":
        if positive:
            return "progressing"
        if negative:
            return "delayed"
        return "stable" if neutral else "unknown"
    if dimension == "capital_allocation":
        if positive:
            return "expansionary"
        if negative:
            return "conservative"
        return "neutral" if neutral else "unknown"
    if weighted_score >= 0.65:
        # Directional improvement is not an absolute quality judgement. A
        # single positive fact remains healthy until there is an explicit
        # strength anchor or repeated corroboration.
        return "strong" if absolute_positive or positive >= 3 else "healthy"
    if weighted_score > 0.0:
        return "healthy"
    if weighted_score <= -0.65:
        return "stressed" if absolute_negative or negative >= 3 else "weak"
    if weighted_score < 0.0:
        return "weak"
    return "neutral" if neutral else "unknown"


def trend_for_changes(changes: list[TemporalChange], claims: list[QualitativeClaim]) -> tuple[str, dict[str, Any]]:
    """Prefer audited period-over-period changes and fall back conservatively."""
    directional = [change_polarity(change) for change in changes]
    positive = sum(item > 0 for item in directional)
    negative = sum(item < 0 for item in directional)
    if positive and negative:
        return "mixed", {"source": "temporal_changes", "positive_changes": positive, "negative_changes": negative}
    if positive:
        accelerating = any(change.change_type in {"accelerating", "raised"} for change in changes if change_polarity(change) > 0)
        return ("accelerating" if accelerating else "improving"), {"source": "temporal_changes", "positive_changes": positive, "negative_changes": 0}
    if negative:
        decelerating = any(change.change_type in {"decelerating", "lowered"} for change in changes if change_polarity(change) < 0)
        return ("decelerating" if decelerating else "deteriorating"), {"source": "temporal_changes", "positive_changes": 0, "negative_changes": negative}
    stable = any(change.change_type in {"stable", "reiterated"} for change in changes)
    if stable:
        return "stable", {"source": "temporal_changes", "positive_changes": 0, "negative_changes": 0, "stable_changes": 1}

    comparative = [claim for claim in claims if explicit_comparative_claim(claim)]
    signs = [claim_polarity(claim) for claim in comparative]
    if any(sign > 0 for sign in signs) and any(sign < 0 for sign in signs):
        return "mixed", {"source": "comparative_claims", "positive_claims": sum(sign > 0 for sign in signs), "negative_claims": sum(sign < 0 for sign in signs)}
    if any(sign > 0 for sign in signs):
        return "improving", {"source": "comparative_claims", "positive_claims": sum(sign > 0 for sign in signs), "negative_claims": 0}
    if any(sign < 0 for sign in signs):
        return "deteriorating", {"source": "comparative_claims", "positive_claims": 0, "negative_claims": sum(sign < 0 for sign in signs)}
    return "unknown", {"source": "no_period_comparison"}


def state_label(dimension: str) -> str:
    return dimension.replace("_", " ")
