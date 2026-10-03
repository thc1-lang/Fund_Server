from __future__ import annotations

from typing import Any


def evidence_confidence(*, source_completeness: float = 0.0, evidence_groups: int = 0,
                        sample_size: int = 0, attribution: str | None = None,
                        state_confidence: float | None = None) -> float:
    """Deterministic reliability estimate, independent of direction or score."""
    parts: list[float] = []
    if state_confidence is not None:
        parts.append(max(0.0, min(1.0, float(state_confidence))))
    if source_completeness:
        parts.append(max(0.0, min(1.0, float(source_completeness))))
    if evidence_groups:
        parts.append(min(1.0, 0.5 + 0.15 * min(evidence_groups, 4)))
    if sample_size:
        parts.append(min(1.0, 0.5 + 0.1 * min(sample_size, 5)))
    if attribution:
        parts.append({"DIRECT": 1.0, "ROLE_BASED": .9, "TEAM": .75, "TENURE_OVERLAP": .2}.get(attribution, .45))
    return round(sum(parts) / len(parts), 4) if parts else 0.0


def correlated_factor_confidence(factors: list[Any]) -> float:
    """Aggregate confidence without pretending one source is many independent sources."""
    scored = [item for item in factors if getattr(item, "status", "") == "SCORED" and getattr(item, "score", None) is not None]
    if not scored:
        return 0.0
    total_weight = sum(float(item.weight) for item in scored)
    base = sum(float(item.confidence) * float(item.weight) for item in scored) / total_weight if total_weight else 0.0
    groups: set[str] = set()
    for item in scored:
        raw = getattr(item, "raw_inputs", {}) or {}
        docs = raw.get("source_document_ids") or []
        if docs:
            groups.add(f"document:{docs[0]}")
        elif raw.get("source_group"):
            groups.add(f"source:{raw['source_group']}")
        else:
            groups.add(f"factor:{item.factor_key}")
    independence = min(1.0, len(groups) / len(scored))
    # A single-source evidence set can be reliable, but is not independent evidence.
    return round(base * (0.5 + 0.5 * independence), 4)
