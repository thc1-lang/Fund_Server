from __future__ import annotations

from collections import defaultdict
from typing import Any

from .confidence import evidence_confidence
from .factor_registry import load_mappings
from .factor_rules import clamp
from .models import FactorDefinition, FactorScore
from .sources import EvidenceSources
from .utils import make_factor


def _outcome_factor(ticker: str, as_of: str, profile: str, definition: FactorDefinition,
                    rows: list[dict[str, Any]], mapping: dict[str, float], *, min_sample: int,
                    value_key: str = "outcome", attribution_key: str | None = None) -> FactorScore:
    mapping = {str(key).upper(): value for key, value in mapping.items()}
    eligible = [row for row in rows if str(row.get(value_key, "")).upper() in mapping]
    ids = [str(value) for row in eligible for key, value in row.items() if key.endswith("_id") and value]
    if len(eligible) < min_sample:
        return make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                           status="INSUFFICIENT_SAMPLE", coverage=min(1.0, len(eligible) / min_sample) if min_sample else 0.0,
                           confidence=evidence_confidence(sample_size=len(eligible)) if eligible else 0.0,
                           raw_inputs={"eligible_observations": len(eligible), "minimum_sample": min_sample,
                                       "outcomes": [row.get(value_key) for row in rows]},
                           supporting_record_ids=ids,
                           coverage_reason="INSUFFICIENT_RESOLVED_SAMPLE")
    values = [float(mapping[str(row[value_key]).upper()]) for row in eligible]
    score = sum(values) / len(values)
    confidence = evidence_confidence(source_completeness=1.0, evidence_groups=len(eligible), sample_size=len(eligible),
                                      attribution=attribution_key)
    return make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score,
                       status="SCORED", coverage=1.0, confidence=confidence,
                       raw_inputs={"eligible_observations": len(eligible), "outcomes": [row.get(value_key) for row in eligible]},
                       normalized_inputs={"mapped_values": values},
                       supporting_record_ids=ids,
                       supporting_evidence=[{"outcome": row.get(value_key), "evidence": row.get("evidence_text", ""),
                                            "source_url": row.get("source_url")} for row in eligible],
                       coverage_reason="RESOLVED_COMPARABLE_OUTCOMES")


def score_management(ticker: str, as_of: str, profile: str, definitions: list[FactorDefinition],
                     sources: EvidenceSources, config_root: str = "config/scoring") -> list[FactorScore]:
    mappings = load_mappings(config_root)
    results: list[FactorScore] = []
    for definition in definitions:
        if definition.input_source == "guidance_outcomes":
            outcomes = sources.guidance_outcomes(ticker)
            commitments = {str(row.get("guidance_id")): row for row in sources.guidance_commitments(ticker)}
            resolved = [row for row in outcomes if str(row.get("outcome", "")).upper() in mappings.get("guidance_outcome", {})]
            for row in resolved:
                commitment = commitments.get(str(row.get("guidance_id")), {})
                row["initial_guidance"] = {"low": commitment.get("low_value"), "high": commitment.get("high_value"), "point": commitment.get("point_value"), "revision_type": commitment.get("revision_type")}
                row["guidance_source_record_ids"] = [str(row.get("guidance_outcome_id")), str(row.get("guidance_id")), str(commitment.get("source_claim_id")), str(commitment.get("source_document_id"))]
            result = _outcome_factor(ticker, as_of, profile, definition, outcomes, mappings.get("guidance_outcome", {}), min_sample=definition.min_sample)
            result.raw_inputs["initial_and_latest_guidance"] = [{"guidance_id": row.get("guidance_id"), "initial_guidance": row.get("initial_guidance")} for row in resolved]
            result.supporting_record_ids = [record_id for row in resolved for record_id in row.get("guidance_source_record_ids", []) if record_id and record_id != "None"]
            results.append(result)
        elif definition.input_source == "strategic_outcomes":
            results.append(_outcome_factor(ticker, as_of, profile, definition, sources.strategic_outcomes(ticker), mappings.get("commitment_outcome", {}), min_sample=definition.min_sample))
        elif definition.input_source == "operating_outcomes":
            rows = [row for row in sources.operating_outcomes(ticker) if str(row.get("attribution_level", "")).upper() in {"DIRECT", "ROLE_BASED", "TEAM"} and row.get("percent_change") is not None]
            if len(rows) < definition.min_sample:
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                                           status="INSUFFICIENT_SAMPLE", coverage=min(1.0, len(rows)/definition.min_sample) if definition.min_sample else 0.0,
                                           confidence=evidence_confidence(sample_size=len(rows)),
                                           raw_inputs={"eligible_observations": len(rows), "excluded_tenure_overlap": len(sources.operating_outcomes(ticker))-len(rows)},
                                           coverage_reason="TENURE_OVERLAP_NOT_MANAGEMENT_ATTRIBUTION" if not rows else "INSUFFICIENT_ATTRIBUTED_SAMPLE"))
            else:
                positives = sum(float(row.get("percent_change")) > 0 for row in rows)
                score = 50.0 + 50.0 * ((positives / len(rows)) * 2 - 1)
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=clamp(score), status="SCORED", coverage=1.0,
                                           confidence=evidence_confidence(source_completeness=1.0, evidence_groups=len(rows), sample_size=len(rows)),
                                           raw_inputs={"eligible_observations": len(rows), "excluded_tenure_overlap": len(sources.operating_outcomes(ticker))-len(rows)},
                                           normalized_inputs={"positive_count": positives, "negative_count": len(rows)-positives},
                                           supporting_record_ids=[str(row.get("outcome_id")) for row in rows], coverage_reason="EXPLICIT_ATTRIBUTED_OPERATING_OUTCOMES"))
        elif definition.input_source == "capital_allocation_events":
            rows = [row for row in sources.capital_events(ticker) if str(row.get("event_status", "")).lower() in mappings.get("capital_outcome", {})]
            results.append(_outcome_factor(ticker, as_of, profile, definition, rows, mappings.get("capital_outcome", {}), min_sample=definition.min_sample, value_key="event_status"))
    return results
