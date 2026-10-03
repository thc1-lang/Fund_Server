"""Deterministic, evidence-preserving qualitative extraction."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Sequence

from .evidence import EvidenceValidationError, assert_valid_claim
from .extraction_models import ClaimCandidate, ExtractionChunk, ExtractionResult, QualitativeClaim
from .extraction_rules import candidate_for_sentence, matching_rules, primary_rule, split_sentences
from .models import NormalizedDocument


def _segment_ranges(document: NormalizedDocument) -> list[tuple[int, int, int, float | None, float | None]]:
    segments = document.metadata.get("segments") if isinstance(document.metadata, dict) else None
    if not isinstance(segments, list):
        return []
    ranges: list[tuple[int, int, int, float | None, float | None]] = []
    cursor = 0
    text = document.text or ""
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            continue
        value = segment["text"]
        found = text.find(value, cursor)
        if found < 0:
            # The normalized transcript uses the same segment text but can have
            # whitespace normalization.  A bounded fallback still preserves a
            # useful segment id and timestamp.
            found = cursor
            end = min(len(text), found + len(value))
        else:
            end = found + len(value)
        ranges.append((found, end, index, _as_float(segment.get("start")), _as_float(segment.get("end"))))
        cursor = end
    return ranges


def _as_float(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _page_for_offset(document: NormalizedDocument, start: int, end: int) -> tuple[int | None, int | None]:
    offsets = document.metadata.get("page_offsets") if isinstance(document.metadata, dict) else None
    if not isinstance(offsets, list):
        return None, None
    pages: list[int] = []
    for item in offsets:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        left, right = item.get("start_char"), item.get("end_char")
        if isinstance(page, int) and isinstance(left, int) and isinstance(right, int) and start < right and end > left:
            pages.append(page)
    return (min(pages), max(pages)) if pages else (None, None)


def _section_for_offset(document: NormalizedDocument, start: int, end: int) -> str | None:
    for section in document.sections or []:
        if not isinstance(section, dict):
            continue
        left, right = section.get("start_char"), section.get("end_char")
        if isinstance(left, int) and isinstance(right, int) and start < right and end > left:
            return str(section.get("heading") or "") or None
    return None


def chunk_document(document: NormalizedDocument, max_chars: int = 1800, overlap_chars: int = 240) -> list[ExtractionChunk]:
    """Create deterministic windows while retaining exact document offsets."""
    text = document.text or ""
    if not text:
        return []
    max_chars = max(200, max_chars)
    overlap_chars = max(0, min(overlap_chars, max_chars // 2))
    ranges = _segment_ranges(document)
    chunks: list[ExtractionChunk] = []
    units = split_sentences(text)
    if not units:
        units = [(text, 0, len(text))]
    unit_index = 0
    chunk_number = 0
    while unit_index < len(units):
        start = units[unit_index][1]
        end = units[unit_index][2]
        next_index = unit_index + 1
        while next_index < len(units) and units[next_index][2] - start <= max_chars:
            end = units[next_index][2]
            next_index += 1
        # Keep an unusually long sentence intact instead of creating partial
        # evidence spans at a chunk boundary.
        chunk_text = text[start:end]
        segment_ids: list[int] = []
        starts: list[float] = []
        ends: list[float] = []
        for left, right, index, seg_start, seg_end in ranges:
            if start < right and end > left:
                segment_ids.append(index)
                if seg_start is not None:
                    starts.append(seg_start)
                if seg_end is not None:
                    ends.append(seg_end)
        page_start, page_end = _page_for_offset(document, start, end)
        section = _section_for_offset(document, start, end)
        chunks.append(ExtractionChunk(
            document_id=document.document_id,
            chunk_id=f"{document.document_id}:chunk-{chunk_number:05d}",
            text=chunk_text,
            start_char=start,
            end_char=end,
            page_start=page_start,
            page_end=page_end,
            start_timestamp=min(starts) if starts else None,
            end_timestamp=max(ends) if ends else None,
            segment_ids=segment_ids,
            section=section,
        ))
        if end >= len(text):
            break
        overlap_start = max(start, end - overlap_chars)
        overlap_index = next_index
        while overlap_index > unit_index and units[overlap_index - 1][1] >= overlap_start:
            overlap_index -= 1
        unit_index = max(unit_index + 1, overlap_index)
        chunk_number += 1
    return chunks


class ExtractionProvider(ABC):
    """Provider contract; no external model or credential is required."""

    @abstractmethod
    def extract(self, document: NormalizedDocument, chunks: Sequence[ExtractionChunk]) -> list[ClaimCandidate]:
        raise NotImplementedError


_SEMANTIC_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "of", "on", "or", "the", "to", "was", "were", "with", "this", "that", "our", "we", "year", "over",
}


def _semantic_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return {token for token in tokens if token not in _SEMANTIC_STOPWORDS and len(token) > 2}


class DeterministicExtractionProvider(ExtractionProvider):
    """Rule-assisted provider whose every output can be reproduced offline."""

    provider_version = "deterministic-rules-v8"
    rules_version = "rules-v8"

    def extract(self, document: NormalizedDocument, chunks: Sequence[ExtractionChunk]) -> list[ClaimCandidate]:
        candidates: list[ClaimCandidate] = []
        seen: set[tuple[str, str]] = set()
        seen_candidates: list[ClaimCandidate] = []
        for chunk in chunks:
            for sentence, sentence_start, _ in split_sentences(chunk.text):
                rules = matching_rules(sentence)
                if not rules:
                    continue
                rule = primary_rule(sentence, rules)
                candidate = candidate_for_sentence(sentence, rule)
                absolute_start = chunk.start_char + sentence_start
                # Exact repeated wording in one document is treated as a
                # conservative semantic duplicate. Different wording or a
                # different proposition remains eligible.
                key = (candidate.dimension, re.sub(r"\s+", " ", sentence).strip().lower().rstrip(".!?"))
                if key in seen:
                    continue
                # A conservative semantic duplicate check is limited to one
                # normalized document and requires both the same primary
                # dimension and the same explicit magnitude.  Token overlap
                # prevents merging distinct metrics that merely share a value.
                candidate_tokens = _semantic_tokens(sentence)
                duplicate = False
                if candidate.magnitude is not None and candidate_tokens:
                    for previous in seen_candidates:
                        if previous.dimension != candidate.dimension or previous.magnitude != candidate.magnitude:
                            continue
                        previous_tokens = _semantic_tokens(previous.evidence_text)
                        if previous_tokens and len(candidate_tokens & previous_tokens) / min(len(candidate_tokens), len(previous_tokens)) >= 0.70:
                            duplicate = True
                            break
                if duplicate:
                    continue
                seen.add(key)
                seen_candidates.append(candidate)
                candidate.source_start = absolute_start
                candidate.source_end = absolute_start + len(sentence) if absolute_start is not None else None
                candidates.append(candidate)
        return candidates


def _candidate_location(document: NormalizedDocument, chunk: ExtractionChunk, evidence: str, relative_start: int) -> dict:
    start = chunk.start_char + relative_start
    end = start + len(evidence)
    location: dict = {"start_char": start, "end_char": end}
    page_start, page_end = _page_for_offset(document, start, end)
    if page_start is not None:
        location.update({"page_start": page_start, "page_end": page_end})
        if page_start == page_end:
            location["page"] = page_start
    ranges = _segment_ranges(document)
    if ranges:
        ids: list[int] = []
        timestamps_start: list[float] = []
        timestamps_end: list[float] = []
        for left, right, index, seg_start, seg_end in ranges:
            if start < right and end > left:
                ids.append(index)
                if seg_start is not None:
                    timestamps_start.append(seg_start)
                if seg_end is not None:
                    timestamps_end.append(seg_end)
        if ids:
            location["segment_ids"] = ids
            if timestamps_start:
                location["start_timestamp"] = min(timestamps_start)
            if timestamps_end:
                location["end_timestamp"] = max(timestamps_end)
    return location


def stable_claim_id(document: NormalizedDocument, candidate: ClaimCandidate, location: dict) -> str:
    identity = "|".join((document.document_id, candidate.dimension, candidate.evidence_text, str(location.get("start_char")), str(location.get("end_char"))))
    return "clm_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


_claim_id = stable_claim_id


def _guidance_horizon(candidate: ClaimCandidate) -> str | None:
    """Capture a claim's forward horizon without relabeling its document."""
    if candidate.dimension != "guidance" and candidate.certainty not in {"guidance", "target", "expectation", "estimate"}:
        return None
    match = re.search(r"\b(?:FY|FISCAL\s+YEAR|FULL[- ]YEAR)\s*(20\d{2}|19\d{2})\b", candidate.evidence_text, re.I)
    return f"FY {match.group(1)}" if match else None


def extract_document(
    document: NormalizedDocument,
    provider: ExtractionProvider | None = None,
    *,
    extraction_version: str = "qualitative-extraction-v8",
    dimension: str | None = None,
) -> ExtractionResult:
    provider = provider or DeterministicExtractionProvider()
    chunks = chunk_document(document)
    candidates = provider.extract(document, chunks)
    result = ExtractionResult(chunks_processed=len(chunks), characters_processed=sum(len(c.text) for c in chunks))
    for candidate in candidates:
        if dimension and candidate.dimension != dimension:
            continue
        for chunk in chunks:
            if candidate.source_start is not None and not (chunk.start_char <= candidate.source_start and candidate.source_end is not None and candidate.source_end <= chunk.end_char):
                continue
            relative_start = (candidate.source_start - chunk.start_char) if candidate.source_start is not None else chunk.text.find(candidate.evidence_text)
            if relative_start < 0:
                continue
            location = _candidate_location(document, chunk, candidate.evidence_text, relative_start)
            claim = QualitativeClaim(
                claim_id=stable_claim_id(document, candidate, location),
                ticker=document.ticker,
                company_name=document.company_name,
                document_id=document.document_id,
                document_type=document.document_type,
                fiscal_year=document.fiscal_year,
                fiscal_quarter=document.fiscal_quarter,
                period_label=document.period_label,
                dimension=candidate.dimension,
                topic=candidate.topic,
                subtopic=candidate.subtopic,
                claim_text=candidate.claim_text,
                direction=candidate.direction,
                magnitude=candidate.magnitude,
                certainty=candidate.certainty,
                evidence_text=candidate.evidence_text,
                source_location=location,
                source_url=document.source_url,
                local_path=document.local_path,
                extraction_method=candidate.extraction_method,
                extraction_confidence=candidate.extraction_confidence,
                created_at=datetime.now(timezone.utc).isoformat(),
                source_content_hash=document.content_hash,
                extraction_version=extraction_version,
                provider_version=getattr(provider, "provider_version", provider.__class__.__name__),
                model_version=None,
                rules_version=getattr(provider, "rules_version", "unknown"),
                period_type=document.period_type,
                guidance_horizon=_guidance_horizon(candidate),
                available_date=document.available_date,
                availability_status=document.availability_status,
                period_end_date=document.period_end_date,
                issuer_fiscal_calendar=dict(document.issuer_fiscal_calendar),
            )
            try:
                assert_valid_claim(claim, document)
            except EvidenceValidationError as exc:
                result.rejected += 1
                result.validation_failures.append(f"{claim.claim_id}: {exc}")
            else:
                result.claims.append(claim)
            break
    # Chunk overlap can surface the same evidence more than once.  Keep the
    # stable first claim and report no duplicate persisted records.
    unique: dict[str, QualitativeClaim] = {claim.claim_id: claim for claim in result.claims}
    result.claims = list(unique.values())
    return result


def extract_claims(document: NormalizedDocument, provider: ExtractionProvider | None = None, **kwargs) -> list[QualitativeClaim]:
    """Convenience API matching the conceptual Component 2 interface."""
    return extract_document(document, provider, **kwargs).claims


__all__ = ["ExtractionProvider", "DeterministicExtractionProvider", "chunk_document", "extract_document", "extract_claims", "stable_claim_id"]

