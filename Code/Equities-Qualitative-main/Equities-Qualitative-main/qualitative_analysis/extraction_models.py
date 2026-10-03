"""Typed records and controlled vocabularies for qualitative extraction.

The extraction layer deliberately keeps claims small and source-bound.  A claim
is useful only when a reviewer can follow it back to a normalized document and
to the exact passage that produced it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


DIMENSIONS = (
    "demand", "revenue", "pricing", "volume", "margins", "costs",
    "operating_expenses", "capex", "cash_flow", "guidance", "backlog",
    "bookings", "customer_growth", "customer_retention", "product",
    "geography", "competition", "market_share", "capacity", "supply_chain",
    "inventory", "management", "capital_allocation", "balance_sheet",
    "regulation", "risks", "catalysts", "strategy",
)
_CUSTOM_DIMENSIONS: set[str] = set()
DIRECTION_VALUES = (
    "improving", "deteriorating", "increasing", "decreasing", "raised",
    "lowered", "accelerating", "decelerating", "positive", "negative",
    "stable", "mixed", "not_applicable", "unknown",
)
CERTAINTY_VALUES = (
    "actual", "confirmed", "guidance", "target", "expectation", "estimate",
    "possibility", "risk", "unknown",
)


def register_dimension(name: str) -> str:
    """Register an additional taxonomy value for a downstream component."""
    value = name.strip().lower()
    if not value or not value.replace("_", "").isalnum():
        raise ValueError("dimension names must be non-empty snake-case values")
    _CUSTOM_DIMENSIONS.add(value)
    return value


def is_supported_dimension(name: str) -> bool:
    return name in DIMENSIONS or name in _CUSTOM_DIMENSIONS


@dataclass
class ExtractionChunk:
    """A text window with offsets and inherited provenance."""

    document_id: str
    chunk_id: str
    text: str
    start_char: int
    end_char: int
    page_start: int | None = None
    page_end: int | None = None
    start_timestamp: float | None = None
    end_timestamp: float | None = None
    segment_ids: list[int] = field(default_factory=list)
    section: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClaimCandidate:
    """Provider output before document-aware evidence validation."""

    dimension: str
    topic: str | None
    subtopic: str | None
    claim_text: str
    direction: str = "unknown"
    magnitude: dict[str, Any] | None = None
    certainty: str = "unknown"
    evidence_text: str = ""
    source_location: dict[str, Any] = field(default_factory=dict)
    extraction_method: str = "deterministic_rules_v8"
    extraction_confidence: float = 0.0
    source_start: int | None = None
    source_end: int | None = None


@dataclass
class QualitativeClaim:
    """A persisted, evidence-preserving qualitative claim."""

    claim_id: str
    ticker: str
    company_name: str
    document_id: str
    document_type: str
    fiscal_year: int | None
    fiscal_quarter: int | None
    period_label: str | None
    dimension: str
    topic: str | None
    subtopic: str | None
    claim_text: str
    direction: str
    magnitude: dict[str, Any] | None
    certainty: str
    evidence_text: str
    source_location: dict[str, Any]
    source_url: str | None
    local_path: str | None
    extraction_method: str
    extraction_confidence: float
    created_at: str
    source_content_hash: str = ""
    extraction_version: str = "qualitative-extraction-v8"
    provider_version: str = "deterministic-rules-v8"
    model_version: str | None = None
    rules_version: str = "rules-v8"
    prompt_version: str | None = None
    period_type: str = "UNKNOWN"
    guidance_horizon: str | None = None
    available_date: str | None = None
    availability_status: str = "UNKNOWN"
    period_end_date: str | None = None
    issuer_fiscal_calendar: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self, document):
        """Validate this claim against its normalized source document."""
        from .evidence import validate_claim
        return validate_claim(self, document)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QualitativeClaim":
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        defaults: dict[str, Any] = {
            "magnitude": None, "topic": None, "subtopic": None,
            "source_content_hash": "", "extraction_version": "qualitative-extraction-v8",
            "provider_version": "deterministic-rules-v8", "model_version": None,
            "rules_version": "rules-v8", "prompt_version": None,
            "period_type": "UNKNOWN", "guidance_horizon": None,
            "available_date": None, "availability_status": "UNKNOWN",
            "period_end_date": None, "issuer_fiscal_calendar": {},
        }
        defaults.update({key: value[key] for key in fields if key in value})
        return cls(**defaults)


@dataclass
class ExtractionResult:
    """Counters and claims returned for one document or a CLI run."""

    claims: list[QualitativeClaim] = field(default_factory=list)
    rejected: int = 0
    validation_failures: list[str] = field(default_factory=list)
    chunks_processed: int = 0
    characters_processed: int = 0


