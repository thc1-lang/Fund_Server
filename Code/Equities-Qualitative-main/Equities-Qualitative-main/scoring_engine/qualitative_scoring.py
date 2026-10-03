from __future__ import annotations

from typing import Any

from .confidence import evidence_confidence
from .factor_registry import load_mappings
from .factor_rules import map_ordinal
from .models import FactorDefinition, FactorScore, ProfileDefinition
from .sources import EvidenceSources
from .utils import make_factor


def _latest_state(states: list[dict[str, Any]], dimension: str) -> dict[str, Any] | None:
    values = [row for row in states if str(row.get("dimension", "")).lower() == dimension]
    if not values:
        return None
    parents = [row for row in values if not row.get("topic")]
    values = parents or values
    return sorted(values, key=lambda row: (int(row.get("as_of_fiscal_year") or 0), int(row.get("as_of_fiscal_quarter") or 0), str(row.get("period_label") or ""), str(row.get("created_at") or "")))[-1]


def _state_factor(*, ticker: str, as_of: str, profile: str, definition: FactorDefinition,
                  states: list[dict[str, Any]], mappings: dict[str, Any], field: str) -> FactorScore:
    dimension = definition.mapping_rule.split(":", 1)[1]
    state = _latest_state(states, dimension)
    if state is None:
        return make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                           status="INSUFFICIENT_COVERAGE", coverage=0.0, confidence=0.0,
                           coverage_reason=f"NO_AUDITED_{dimension.upper()}_STATE")
    raw = {"state_id": state.get("state_id"), "dimension": dimension, "current_state": state.get("current_state"),
           "trend": state.get("trend"), "evidence_count": state.get("evidence_count"),
           "evidence_groups_considered": state.get("evidence_groups_considered"),
           "evidence_groups_retained": state.get("evidence_groups_retained"),
           "state_confidence": state.get("confidence"),
           "state_confidence_factors": state.get("confidence_factors", {}),
           "supporting_claim_ids": state.get("supporting_claim_ids", []), "supporting_change_ids": state.get("supporting_change_ids", []),
           "supporting_document_ids": state.get("supporting_document_ids", []), "source_document_ids": state.get("supporting_document_ids", []),
           "source_document_count": len(state.get("supporting_document_ids", []) or []), "source_urls": state.get("source_urls", []),
           "semantic_class": definition.semantic_class}
    label = str(state.get(field) or "").lower()
    score = map_ordinal(label, mappings.get("state", {}).get(dimension, {}) if field == "current_state" else mappings.get("trend", {}).get(dimension, {}))
    considered = int(state.get("evidence_groups_considered") or state.get("evidence_count") or 0)
    retained = int(state.get("evidence_groups_retained") or state.get("evidence_count") or 0)
    coverage = min(1.0, retained / considered) if considered else (1.0 if state.get("supporting_document_ids") else 0.0)
    confidence = evidence_confidence(source_completeness=1.0 if state.get("source_local_paths") or state.get("source_urls") else .5,
                                      evidence_groups=retained, state_confidence=state.get("confidence"))
    if definition.semantic_class == "NOT_SAFELY_SCORABLE" and field == "current_state":
        status = "UNSCORED"
        reason = "UPSTREAM_STATE_NOT_PROVEN_ABSOLUTE_QUALITY"
        score = None
    else:
        status = "SCORED" if score is not None and coverage >= definition.min_coverage else ("UNKNOWN" if score is None else "INSUFFICIENT_COVERAGE")
        reason = "AUDITED_STATE_MAPPED" if status == "SCORED" else ("STATE_LABEL_NOT_CONFIGURED" if score is None else "STATE_COVERAGE_BELOW_MINIMUM")
    evidence = [{"state_id": state.get("state_id"), "summary": state.get("summary"), "source_urls": state.get("source_urls", [])}]
    ids = [str(state.get("state_id"))] + [str(item) for item in state.get("supporting_claim_ids", [])] + [str(item) for item in state.get("supporting_change_ids", [])] + [str(item) for item in state.get("supporting_document_ids", [])]
    return make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score if status == "SCORED" else None,
                       status=status, coverage=coverage, confidence=confidence, raw_inputs=raw,
                       normalized_inputs={"dimension": dimension, "field": field, "label": label},
                       supporting_record_ids=[item for item in ids if item and item != "None"], supporting_evidence=evidence,
                       coverage_reason=reason)


def score_qualitative(ticker: str, as_of: str, profile: str, definitions: list[FactorDefinition],
                      sources: EvidenceSources, config_root: str = "config/scoring") -> list[FactorScore]:
    states = sources.states(ticker)
    mappings = load_mappings(config_root)
    results: list[FactorScore] = []
    for definition in definitions:
        if definition.input_source != "qualitative_states":
            continue
        field = definition.input_field
        results.append(_state_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition,
                                     states=states, mappings=mappings, field=field))
    return results
