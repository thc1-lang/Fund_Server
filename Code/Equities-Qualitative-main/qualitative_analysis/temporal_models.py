"""Typed records for evidence-backed comparisons across fiscal periods."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CHANGE_TYPES = (
    "new",
    "newly_mentioned",
    "newly_occurred",
    "removed",
    "reiterated",
    "improving",
    "deteriorating",
    "stable",
    "increasing",
    "decreasing",
    "accelerating",
    "decelerating",
    "raised",
    "lowered",
    "completed",
    "delayed",
    "new_risk",
    "risk_removed",
    "risk_intensified",
    "risk_eased",
    "strategy_changed",
    "strategy_reiterated",
    "mixed",
    "insufficient_evidence",
)

DIRECTION_VALUES = (
    "improving",
    "deteriorating",
    "increasing",
    "decreasing",
    "accelerating",
    "decelerating",
    "raised",
    "lowered",
    "stable",
    "mixed",
    "unknown",
)


@dataclass(frozen=True)
class TemporalEvidence:
    """A claim excerpt retained on one side of a temporal comparison."""

    claim_id: str
    evidence_text: str
    document_id: str
    source_location: dict[str, Any] = field(default_factory=dict)
    period_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TemporalEvidence":
        return cls(
            claim_id=str(value.get("claim_id", "")),
            evidence_text=str(value.get("evidence_text", "")),
            document_id=str(value.get("document_id", "")),
            source_location=dict(value.get("source_location") or {}),
            period_label=value.get("period_label"),
        )


@dataclass
class TemporalChange:
    """A deterministic, provenance-preserving change between two periods."""

    change_id: str
    ticker: str
    company_name: str
    dimension: str
    topic: str | None
    subtopic: str | None
    from_period: str
    to_period: str
    change_type: str
    direction: str
    summary: str
    from_claim_ids: list[str]
    to_claim_ids: list[str]
    from_evidence: list[TemporalEvidence]
    to_evidence: list[TemporalEvidence]
    confidence: float
    comparison_method: str
    comparison_version: str
    created_at: str
    normalized_subject: str | None = None
    metric_unit: str | None = None
    rules_version: str = "temporal-rules-v1"
    provider: str = "deterministic-temporal-v1"
    model_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["from_evidence"] = [item.to_dict() for item in self.from_evidence]
        value["to_evidence"] = [item.to_dict() for item in self.to_evidence]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TemporalChange":
        fields = {field.name for field in cls.__dataclass_fields__.values()}
        data = {key: value[key] for key in fields if key in value}
        data["from_evidence"] = [TemporalEvidence.from_dict(item) for item in value.get("from_evidence", [])]
        data["to_evidence"] = [TemporalEvidence.from_dict(item) for item in value.get("to_evidence", [])]
        data.setdefault("normalized_subject", None)
        data.setdefault("metric_unit", None)
        data.setdefault("rules_version", "temporal-rules-v1")
        data.setdefault("provider", "deterministic-temporal-v1")
        data.setdefault("model_version", None)
        return cls(**data)


@dataclass
class TemporalComparisonResult:
    """Counters and diagnostics returned by a comparison run."""

    ticker: str
    status: str = "OK"
    available_periods: list[str] = field(default_factory=list)
    from_period: str | None = None
    to_period: str | None = None
    previous_claims: int = 0
    current_claims: int = 0
    topics_matched: int = 0
    changes: list[TemporalChange] = field(default_factory=list)
    reused: int = 0
    rejected: list[str] = field(default_factory=list)
    evidence_validation_failures: list[str] = field(default_factory=list)
    comparison_status: str = "OK"
