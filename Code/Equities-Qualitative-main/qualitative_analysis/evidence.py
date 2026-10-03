"""Evidence and provenance validation for qualitative claims."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extraction_models import CERTAINTY_VALUES, DIRECTION_VALUES, QualitativeClaim, is_supported_dimension
from .models import NormalizedDocument


class EvidenceValidationError(ValueError):
    """Raised when a claim cannot be traced to its normalized source."""


@dataclass
class EvidenceValidation:
    valid: bool
    errors: list[str]


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def validate_claim(claim: QualitativeClaim, document: NormalizedDocument) -> EvidenceValidation:
    """Validate identity, taxonomy, evidence containment, and location.

    Validation is intentionally strict: the store must never contain a claim
    whose evidence cannot be found verbatim in the normalized document.
    """
    errors: list[str] = []
    if not claim.claim_id:
        errors.append("claim_id is required")
    if claim.document_id != document.document_id:
        errors.append("document_id does not match normalized document")
    if claim.ticker.upper() != document.ticker.upper():
        errors.append("ticker does not match normalized document")
    if claim.document_type != document.document_type:
        errors.append("document_type does not match normalized document")
    if claim.fiscal_year != document.fiscal_year or claim.fiscal_quarter != document.fiscal_quarter:
        errors.append("fiscal period does not match normalized document")
    if claim.period_label != document.period_label:
        errors.append("period_label does not match normalized document")
    if not is_supported_dimension(claim.dimension):
        errors.append(f"unsupported dimension: {claim.dimension}")
    if claim.direction not in DIRECTION_VALUES:
        errors.append(f"unsupported direction: {claim.direction}")
    if claim.certainty not in CERTAINTY_VALUES:
        errors.append(f"unsupported certainty: {claim.certainty}")
    if not claim.claim_text or not claim.claim_text.strip():
        errors.append("claim_text is required")
    evidence = claim.evidence_text
    text = document.text or ""
    if not evidence or not evidence.strip():
        errors.append("evidence_text is required")
    else:
        location = claim.source_location
        if not isinstance(location, dict) or not location:
            errors.append("source_location is required")
        start = location.get("start_char") if isinstance(location, dict) else None
        end = location.get("end_char") if isinstance(location, dict) else None
        if start is not None or end is not None:
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or end > len(text):
                errors.append("source character offsets are invalid")
            elif text[start:end] != evidence:
                errors.append("evidence_text does not match source character offsets")
        elif evidence not in text:
            errors.append("evidence_text is not contained in normalized document")
        elif not isinstance(location, dict):
            errors.append("source_location must be a mapping")

        if isinstance(location, dict):
            for key in ("start_timestamp", "end_timestamp"):
                if key in location and _number(location[key]) is None:
                    errors.append(f"{key} must be numeric")
            if _number(location.get("start_timestamp")) is not None and _number(location.get("end_timestamp")) is not None:
                if location["end_timestamp"] < location["start_timestamp"]:
                    errors.append("end_timestamp precedes start_timestamp")
            if "segment_ids" in location:
                segment_ids = location["segment_ids"]
                if not isinstance(segment_ids, list) or any(not isinstance(i, int) or i < 0 for i in segment_ids):
                    errors.append("segment_ids must be non-negative integers")
            if "page" in location and (not isinstance(location["page"], int) or location["page"] < 1):
                errors.append("page must be a positive integer")
            if "page_start" in location and (not isinstance(location["page_start"], int) or location["page_start"] < 1):
                errors.append("page_start must be a positive integer")
            if "page_end" in location and (not isinstance(location["page_end"], int) or location["page_end"] < 1):
                errors.append("page_end must be a positive integer")

            if document.document_type in {"earnings_transcript", "event_transcript"}:
                if "segment_ids" not in location:
                    errors.append("transcript evidence requires segment_ids")
                if "start_timestamp" not in location or "end_timestamp" not in location:
                    errors.append("transcript evidence requires timestamps")
            if document.document_type in {"news_release", "annual_report", "quarterly_report", "investor_presentation", "other_report"}:
                if "start_char" not in location or "end_char" not in location:
                    errors.append("document evidence requires character offsets")

    if claim.source_content_hash and claim.source_content_hash != document.content_hash:
        errors.append("source content hash does not match normalized document")
    return EvidenceValidation(not errors, errors)


def assert_valid_claim(claim: QualitativeClaim, document: NormalizedDocument) -> None:
    result = validate_claim(claim, document)
    if not result.valid:
        raise EvidenceValidationError("; ".join(result.errors))


validate_evidence = validate_claim


def find_evidence_offset(text: str, evidence: str, start: int = 0) -> tuple[int, int] | None:
    """Return the first exact contiguous evidence span at or after ``start``."""
    offset = text.find(evidence, max(0, start))
    return None if offset < 0 else (offset, offset + len(evidence))
