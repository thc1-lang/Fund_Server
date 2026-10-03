"""Deterministic rules for classifying matched qualitative claims."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .topic_normalization import metric_unit


@dataclass(frozen=True)
class MetricObservation:
    values: tuple[float, ...]
    unit: str
    raw: str

    @property
    def comparable_value(self) -> float:
        return sum(self.values) / len(self.values)


_NUMBER_RE = re.compile(
    r"(?P<prefix>\$)?(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>%|bps|basis points|million|billion|thousand|bn|mm|m|b|k)?\b",
    re.I,
)


def _scale(unit: str | None, prefix: str | None) -> tuple[str, float]:
    value = (unit or "").lower().strip()
    if value in {"%", "percent"}:
        return "percent", 1.0
    if value in {"bps", "basis points"}:
        return "percent", 0.01
    if value in {"billion", "bn", "b"}:
        return "currency" if prefix else "number", 1_000_000_000.0
    if value in {"million", "mm", "m"}:
        return "currency" if prefix else "number", 1_000_000.0
    if value in {"thousand", "k"}:
        return "currency" if prefix else "number", 1_000.0
    if prefix:
        return "currency", 1.0
    return "number", 1.0


def _magnitude_values(magnitude: Any) -> list[tuple[float, str, str]]:
    if not isinstance(magnitude, dict):
        return []
    raw_values = magnitude.get("values") if isinstance(magnitude.get("values"), list) else [magnitude]
    result: list[tuple[float, str, str]] = []
    for item in raw_values:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        raw = str(item.get("raw", item.get("value")))
        unit_text = str(item.get("unit") or "")
        if unit_text.startswith("USD"):
            unit = "currency"
        elif "percent" in unit_text or "bps" in unit_text:
            unit = "percent"
        elif unit_text in {"million", "billion", "thousand"}:
            unit = "number"
        else:
            unit = "number"
        multiplier = 1.0
        lowered = unit_text.lower()
        if "billion" in lowered:
            multiplier = 1_000_000_000.0
        elif "million" in lowered:
            multiplier = 1_000_000.0
        elif "thousand" in lowered:
            multiplier = 1_000.0
        result.append((float(item["value"]) * multiplier, unit, raw))
    return result


def extract_metric(claim: Any) -> MetricObservation | None:
    """Extract explicit comparable values while preserving compatible units."""
    from_magnitude = _magnitude_values(getattr(claim, "magnitude", None))
    if from_magnitude:
        units = {item[1] for item in from_magnitude}
        if len(units) == 1:
            return MetricObservation(tuple(item[0] for item in from_magnitude), from_magnitude[0][1], ", ".join(item[2] for item in from_magnitude))
        # Mixed units in one sentence are disaggregated facts, not one
        # comparable metric. Do not fall back to a misleading average.
        return None

    text = str(getattr(claim, "evidence_text", ""))
    parsed: list[tuple[float, str, str]] = []
    for match in _NUMBER_RE.finditer(text):
        unit, multiplier = _scale(match.group("unit"), match.group("prefix"))
        # Bare years, quarters, and sentence ordinals are not business metrics.
        if match.group("unit") is None and not match.group("prefix"):
            continue
        number = float(match.group("number").replace(",", "")) * multiplier
        parsed.append((number, unit, match.group(0).strip()))
    if not parsed:
        return None
    units = {item[1] for item in parsed}
    if len(units) != 1:
        return None
    return MetricObservation(tuple(item[0] for item in parsed[:3]), parsed[0][1], ", ".join(item[2] for item in parsed[:3]))


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def text_similarity(left: Any, right: Any) -> float:
    a = _tokens(str(getattr(left, "evidence_text", left)))
    b = _tokens(str(getattr(right, "evidence_text", right)))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rank(direction: str) -> int:
    return {
        "deteriorating": -2,
        "decreasing": -2,
        "decelerating": -1,
        "negative": -1,
        "stable": 0,
        "unknown": 0,
        "positive": 1,
        "improving": 2,
        "increasing": 2,
        "accelerating": 3,
        "raised": 3,
        "lowered": -3,
    }.get(direction, 0)


def _explicit_direction(claim: Any) -> str:
    return str(getattr(claim, "direction", "unknown") or "unknown")


def _label(claim: Any) -> str:
    topic = getattr(claim, "topic", None) or getattr(claim, "dimension", "topic")
    return str(topic).replace("_", " ")


def _fiscal_horizon(claim: Any) -> tuple[str, str] | None:
    """Return an explicitly stated comparison horizon when one is present."""
    text = str(getattr(claim, "evidence_text", "")).lower()
    fiscal = re.search(r"\bfy\s*(20\d{2}|19\d{2})\b", text)
    if re.search(r"\b(?:full[- ]year|annual)\b", text) or fiscal:
        return ("annual", fiscal.group(1) if fiscal else "")
    quarter = re.search(r"\bq([1-4])\b", text)
    if quarter:
        return ("quarter", quarter.group(1))
    return None


def _metric_basis(claim: Any) -> set[str]:
    """Identify explicit reporting bases so GAAP and non-GAAP never match."""
    text = str(getattr(claim, "evidence_text", "")).lower()
    values: set[str] = set()
    if re.search(r"\bnon[- ]gaap\b", text):
        values.add("non_gaap")
    elif re.search(r"\bgaap\b", text):
        values.add("gaap")
    return values


def _explicit_metric_terms(claim: Any) -> set[str]:
    """Capture explicit metric names that normalization intentionally groups."""
    text = str(getattr(claim, "evidence_text", "")).lower()
    values: set[str] = set()
    if re.search(r"\b(?:eps|earnings per share)\b", text):
        values.add("eps")
    if re.search(r"\bprofitability\b", text):
        values.add("profitability")
    if re.search(r"\b(?:revenue|sales|top[- ]line)\b", text):
        values.add("revenue")
    if re.search(r"\b(?:gross margin|operating margin|free cash flow margin)\b", text):
        values.add("margin")
    return values


def _completed(claim: Any) -> bool:
    text = str(getattr(claim, "evidence_text", "")).lower()
    return bool(re.search(r"\b(?:approved|launched|acquired|initiated|submitted|authorized|completed|closed)\b", text))


def classify_matched(previous: Any, current: Any) -> dict[str, Any]:
    """Classify two already-matched claims without inventing a direction."""
    prev_metric = extract_metric(previous)
    curr_metric = extract_metric(current)
    unit = prev_metric.unit if prev_metric else (curr_metric.unit if curr_metric else metric_unit(current))
    if prev_metric and curr_metric and prev_metric.unit != curr_metric.unit:
        return {"rejected": "incompatible metric units", "comparison_method": "numeric_unit_check"}

    prev_direction = _explicit_direction(previous)
    curr_direction = _explicit_direction(current)
    dimension = str(getattr(current, "dimension", ""))
    similarity = text_similarity(previous, current)

    # Beat/miss commentary describes realized performance. It is not evidence
    # that management raised or lowered the next guidance range.
    if dimension == "guidance" and getattr(previous, "certainty", "") == "actual" and getattr(current, "certainty", "") == "actual":
        return {"change_type": "insufficient_evidence", "direction": "unknown", "summary": "Actual results changed, but a guidance change was not established.", "confidence": 0.72, "comparison_method": "actual_result_not_guidance", "metric_unit": unit}

    # A like-for-like comparison requires the same explicitly stated fiscal
    # horizon.  Q2 guidance in one transcript and Q3 guidance in the next are
    # different forward periods even when the normalized subject is identical.
    previous_horizon = _fiscal_horizon(previous)
    current_horizon = _fiscal_horizon(current)
    if previous_horizon and current_horizon and previous_horizon != current_horizon:
        return {"rejected": "incompatible fiscal horizon", "comparison_method": "fiscal_horizon_check"}

    previous_basis = _metric_basis(previous)
    current_basis = _metric_basis(current)
    if previous_basis and current_basis and previous_basis != current_basis:
        return {"rejected": "incompatible reporting basis", "comparison_method": "reporting_basis_check"}

    # EPS and broad profitability language are often grouped under one
    # normalized subject, but they are not interchangeable metrics. Preserve
    # the claims as provenance without inventing a reiteration.
    previous_terms = _explicit_metric_terms(previous)
    current_terms = _explicit_metric_terms(current)
    if (("eps" in previous_terms and "profitability" in current_terms and "eps" not in current_terms)
            or ("eps" in current_terms and "profitability" in previous_terms and "eps" not in previous_terms)):
        return {"rejected": "incompatible explicit metric", "comparison_method": "explicit_metric_check"}

    if (prev_metric is None) != (curr_metric is None) and getattr(previous, "magnitude", None) != getattr(current, "magnitude", None):
        return {"change_type": "insufficient_evidence", "direction": "unknown", "summary": f"Insufficient evidence to compare {_label(current)} metrics.", "confidence": 0.45, "comparison_method": "metric_shape_mismatch", "metric_unit": unit}

    if dimension == "risks":
        if re.search(r"\b(?:no longer|resolved|removed|eliminated|discontinued|withdrawn|cancelled|ended|stopped)\b", str(getattr(current, "evidence_text", "")).lower()):
            return {"change_type": "risk_removed", "direction": "improving", "summary": f"{_label(current).capitalize()} risk was explicitly removed or resolved.", "confidence": 0.84, "comparison_method": "explicit_risk_removal", "metric_unit": unit}
        if similarity >= 0.45 and curr_direction == prev_direction:
            return {"change_type": "reiterated", "direction": "stable", "summary": f"{_label(current).capitalize()} risk reiterated.", "confidence": 0.88, "comparison_method": "risk_text_reiteration", "metric_unit": unit}
        if _rank(curr_direction) < _rank(prev_direction):
            return {"change_type": "risk_intensified", "direction": "deteriorating", "summary": f"{_label(current).capitalize()} risk became more prominent.", "confidence": 0.82, "comparison_method": "risk_direction", "metric_unit": unit}
        if _rank(curr_direction) > _rank(prev_direction):
            return {"change_type": "risk_eased", "direction": "improving", "summary": f"{_label(current).capitalize()} risk became less prominent.", "confidence": 0.82, "comparison_method": "risk_direction", "metric_unit": unit}

    if prev_metric and curr_metric:
        # Multiple values are normally regional/product breakouts. Without an
        # explicit mapping of each component, do not compare the controlled
        # direction either: a change in one region can be offset by another.
        if len(prev_metric.values) != 1 or len(curr_metric.values) != 1:
            if prev_metric.values == curr_metric.values and similarity >= 0.45:
                return {"change_type": "reiterated", "direction": "stable", "summary": f"{_label(current).capitalize()} was reiterated with the same disaggregated values.", "confidence": 0.82, "comparison_method": "disaggregated_metric_reiteration", "metric_unit": unit}
            return {"change_type": "insufficient_evidence", "direction": "unknown", "summary": f"Insufficient evidence to compare disaggregated {_label(current)} values.", "confidence": 0.60, "comparison_method": "disaggregated_metric_shape", "metric_unit": unit}

    if prev_metric and curr_metric:
        before, after = prev_metric.comparable_value, curr_metric.comparable_value
        if after == before:
            change_type = "reiterated" if similarity >= 0.25 or dimension == "guidance" else "stable"
            direction = "stable"
        elif after > before:
            change_type = "raised" if dimension == "guidance" else "increasing"
            direction = "raised" if dimension == "guidance" else "increasing"
        else:
            change_type = "lowered" if dimension == "guidance" else "decreasing"
            direction = "lowered" if dimension == "guidance" else "decreasing"
        summary = f"{_label(current).capitalize()} {change_type} from {prev_metric.raw} to {curr_metric.raw}."
        return {"change_type": change_type, "direction": direction, "summary": summary, "confidence": 0.92 if unit else 0.72, "comparison_method": "numeric_metric_comparison", "metric_unit": unit}

    if dimension == "strategy":
        if similarity >= 0.30:
            return {"change_type": "strategy_reiterated", "direction": "stable", "summary": "Strategy was reiterated.", "confidence": 0.78, "comparison_method": "strategy_text_similarity", "metric_unit": unit}
        return {"change_type": "strategy_changed", "direction": "mixed", "summary": "Strategy language changed between periods.", "confidence": 0.68, "comparison_method": "strategy_text_difference", "metric_unit": unit}

    if prev_direction == "unknown" and curr_direction == "unknown":
        if similarity >= 0.30:
            return {"change_type": "reiterated", "direction": "stable", "summary": f"{_label(current).capitalize()} was reiterated.", "confidence": 0.72, "comparison_method": "qualitative_text_similarity", "metric_unit": unit}
        return {"change_type": "insufficient_evidence", "direction": "unknown", "summary": f"Insufficient evidence to classify {_label(current)} change.", "confidence": 0.40, "comparison_method": "insufficient_direction", "metric_unit": unit}

    if prev_direction == curr_direction:
        change_type = "reiterated" if similarity >= 0.25 else "stable"
        return {"change_type": change_type, "direction": "stable", "summary": f"{_label(current).capitalize()} remained {curr_direction}.", "confidence": 0.78 if similarity >= 0.25 else 0.66, "comparison_method": "qualitative_direction_match", "metric_unit": unit}

    previous_rank, current_rank = _rank(prev_direction), _rank(curr_direction)
    if previous_rank == current_rank and previous_rank != 0:
        return {"change_type": "reiterated" if similarity >= 0.20 else "stable", "direction": "stable", "summary": f"{_label(current).capitalize()} remained directionally consistent.", "confidence": 0.70, "comparison_method": "qualitative_direction_equivalence", "metric_unit": unit}
    if previous_rank < current_rank:
        change_type = "improving" if dimension not in {"guidance"} else ("raised" if curr_direction == "raised" else "improving")
        direction = "improving" if change_type == "improving" else "raised"
    elif previous_rank > current_rank:
        change_type = "deteriorating" if dimension not in {"guidance"} else ("lowered" if curr_direction == "lowered" else "deteriorating")
        direction = "deteriorating" if change_type == "deteriorating" else "lowered"
    else:
        change_type, direction = "mixed", "mixed"
    return {"change_type": change_type, "direction": direction, "summary": f"{_label(current).capitalize()} changed from {prev_direction} to {curr_direction}.", "confidence": 0.76, "comparison_method": "qualitative_direction_comparison", "metric_unit": unit}


def classify_new(claim: Any) -> dict[str, Any]:
    dimension = str(getattr(claim, "dimension", ""))
    if dimension == "risks":
        return {"change_type": "new_risk", "direction": "deteriorating", "summary": f"New {_label(claim)} risk was mentioned.", "confidence": 0.78, "comparison_method": "current_period_only", "metric_unit": metric_unit(claim)}
    if _completed(claim):
        return {"change_type": "newly_occurred", "direction": "unknown", "summary": f"{_label(claim).capitalize()} event was newly reported as completed.", "confidence": 0.74, "comparison_method": "current_period_only_completed_event", "metric_unit": metric_unit(claim)}
    return {"change_type": "newly_mentioned", "direction": "unknown", "summary": f"{_label(claim).capitalize()} was newly mentioned; occurrence timing is not established.", "confidence": 0.64, "comparison_method": "current_period_only", "metric_unit": metric_unit(claim)}
