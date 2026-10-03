"""Conservative subject and metric normalization for temporal matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "our", "that", "the", "their", "this", "to", "was", "were", "we", "with",
    "would", "will", "year", "over", "more", "less", "some", "all", "also", "very", "than", "into",
}

_ALIASES = {
    "net": "revenue",
    "sales": "revenue",
    "topline": "revenue",
    "top-line": "revenue",
    "outlook": "guidance",
    "forecast": "guidance",
    "margin": "margins",
    "expense": "costs",
    "expenses": "costs",
    "buyback": "share_repurchase",
    "repurchase": "share_repurchase",
    "repurchases": "share_repurchase",
}


@dataclass(frozen=True)
class TopicIdentity:
    dimension: str
    topic: str
    normalized_subject: str
    metric_unit: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str | None]:
        return (self.dimension, self.topic, self.normalized_subject, self.metric_unit)


def _clean(text: str) -> str:
    text = re.sub(r"[^a-z0-9%$]+", " ", str(text).lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [token for token in _clean(text).split() if token not in _STOPWORDS and len(token) > 1]


def _metric_unit(claim: Any) -> str | None:
    magnitude = getattr(claim, "magnitude", None)
    values: list[dict[str, Any]] = []
    if isinstance(magnitude, dict):
        if isinstance(magnitude.get("values"), list):
            values.extend(item for item in magnitude["values"] if isinstance(item, dict))
        elif "unit" in magnitude:
            values.append(magnitude)
    units = {str(item.get("unit", "")).lower() for item in values}
    has_percent = any("percent" in unit or unit in {"bps", "basis_points"} for unit in units)
    has_currency = any("usd" in unit or unit in {"currency", "million", "billion", "thousand"} for unit in units)
    if has_percent and has_currency:
        return None
    if has_percent:
        return "percent"
    if has_currency:
        return "currency"
    if values:
        return "number"
    text = _clean(getattr(claim, "evidence_text", claim if isinstance(claim, str) else ""))
    if "%" in text or re.search(r"\b(?:bps|basis points)\b", text):
        return "percent"
    if "$" in text or re.search(r"\b(?:million|billion|thousand)\b", text):
        return "currency"
    return None


def _subject_from_text(text: str, dimension: str, topic: str | None = None) -> str:
    value = _clean(text)
    # These distinctions are deliberately explicit: they avoid comparing a
    # revenue claim with an unrelated margin, capex, or customer metric.
    patterns: tuple[tuple[str, str], ...] = (
        (r"\bdeferred revenue\b", "deferred_revenue"),
        (r"\b(?:eps|earnings per share|profitability|income from operations)\b", "profitability"),
        (r"\bfree cash flow margin\b", "free_cash_flow_margin"),
        (r"\boperating margin\b", "operating_margin"),
        (r"\bgross margin\b", "gross_margin"),
        (r"\b(?:ndr|net dollar retention|dollar based net retention)\b", "net_dollar_retention"),
        (r"\b(?:international|americas|emea|apac|apec|europe|asia)\b", "international"),
        (r"\b(?:enterprise|large deals)\b", "enterprise"),
        (r"\b(?:revenue|net sales|sales|top line|topline)\b", "revenue"),
        (r"\b(?:capex|capital expenditure|capital expenditures)\b", "capex"),
        (r"\b(?:share repurchase|share repurchases|buyback|buybacks)\b", "share_repurchase"),
        (r"\b(?:acquisition|acquisitions|acquired)\b", "acquisition"),
        (r"\b(?:churn|retention|renewal)\b", "retention"),
        (r"\b(?:online)\b", "online"),
        (r"\b(?:rpo|bookings?)\b", "bookings"),
        (r"\b(?:ai|ai first|ai powered|ai products?)\b", "ai"),
        (r"\b(?:customer experience|zoom cx|contact center)\b", "customer_experience"),
        (r"\b(?:workvivo|zoommate|zoom ai services|virtual agent)\b", "product_named"),
    )
    for pattern, subject in patterns:
        if re.search(pattern, value):
            return subject

    if dimension == "risks":
        words = [word for word in _tokens(value) if word not in {"risk", "risks", "headwind", "headwinds", "uncertain", "uncertainty", "uncertainties"}]
        for marker in ("remains", "remain", "has", "have", "been", "is", "was", "became", "becomes", "resolved", "intensified", "eased"):
            if marker in words:
                words = words[:words.index(marker)]
                break
        return " ".join(words[:5]) or "risk"
    if dimension == "demand":
        return "general_demand"
    if dimension == "margins":
        return "general_margin"
    if dimension in {"guidance", "revenue"}:
        if topic:
            topic_value = _ALIASES.get(_clean(topic), _clean(topic))
            if topic_value and topic_value not in {"outlook", "guidance", "revenue"}:
                return topic_value.replace(" ", "_")
        return "general_revenue" if dimension == "revenue" else "general_guidance"
    words = _tokens(value)
    return " ".join(words[:5]) or _clean(topic or dimension).replace(" ", "_")


def normalize_topic(claim_or_text: Any, dimension: str | None = None, topic: str | None = None) -> str:
    """Return a stable, conservative subject key.

    The function accepts either a claim-like object or raw text, which keeps
    the normalization rules easy to test without coupling them to storage.
    """
    if isinstance(claim_or_text, str):
        text = claim_or_text
        dim = dimension or ""
        top = topic
    else:
        text = getattr(claim_or_text, "evidence_text", "")
        dim = dimension or getattr(claim_or_text, "dimension", "")
        top = topic if topic is not None else getattr(claim_or_text, "topic", None)
    subject = _subject_from_text(text, _clean(dim).replace(" ", "_"), top)
    return subject


def topic_identity(claim: Any) -> TopicIdentity:
    dimension = _clean(getattr(claim, "dimension", "")).replace(" ", "_")
    topic = _clean(getattr(claim, "topic", None) or dimension).replace(" ", "_")
    subject = normalize_topic(claim, dimension=dimension, topic=topic)
    return TopicIdentity(dimension=dimension, topic=topic, normalized_subject=subject, metric_unit=_metric_unit(claim))


def metric_unit(claim: Any) -> str | None:
    return _metric_unit(claim)
