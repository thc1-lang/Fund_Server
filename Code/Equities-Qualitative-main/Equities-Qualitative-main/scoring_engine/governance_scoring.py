from __future__ import annotations

import re
from typing import Any

from .confidence import evidence_confidence
from .factor_registry import load_mappings
from .models import FactorDefinition, FactorScore
from .sources import EvidenceSources
from .utils import make_factor


def _committee_explicitly_independent(row: dict[str, Any]) -> bool:
    text = str(row.get("evidence_text", "")).lower()
    return str(row.get("required_independence", "")).upper() == "YES" and ("all independent" in text or bool(row.get("member_independence")) and all(str(v).upper() in {"INDEPENDENT", "YES", "TRUE"} for v in row.get("member_independence", {}).values()))


def score_governance(ticker: str, as_of: str, profile: str, definitions: list[FactorDefinition], sources: EvidenceSources,
                    config_root: str = "config/scoring") -> list[FactorScore]:
    mappings = load_mappings(config_root)
    snapshot = sources.governance_snapshot(ticker, as_of)
    committees = sources.governance("governance_committees", ticker)
    results: list[FactorScore] = []
    for definition in definitions:
        if definition.pillar != "GOVERNANCE":
            continue
        if snapshot is None:
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None,
                                       status="INSUFFICIENT_COVERAGE", coverage=0.0, confidence=0.0,
                                       coverage_reason="NO_GOVERNANCE_SNAPSHOT"))
            continue
        sid = str(snapshot.get("snapshot_id", ""))
        if definition.input_source == "governance_snapshots" and definition.mapping_rule == "board_independence":
            total = int(snapshot.get("independent_denominator") or snapshot.get("board_size") or 0)
            known = int(snapshot.get("known_independence_denominator") or 0)
            independent = int(snapshot.get("independent_count") or 0)
            coverage = min(1.0, known / total) if total else 0.0
            score = 100.0 * independent / total if total else None
            status = "SCORED" if score is not None and coverage >= definition.min_coverage else "INSUFFICIENT_COVERAGE"
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score if status == "SCORED" else None,
                                       status=status, coverage=coverage, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=1),
                                       raw_inputs={"independent_count": independent, "board_size": total, "known_independence_count": known},
                                       normalized_inputs={"independent_percent_of_total": score}, supporting_record_ids=[sid],
                                       coverage_reason="KNOWN_INDEPENDENCE_OVER_TOTAL_BOARD" if status == "SCORED" else "UNKNOWN_INDEPENDENCE_REDUCES_COVERAGE"))
        elif definition.mapping_rule == "leadership_structure":
            value = str(snapshot.get("chair_structure") or "")
            score = mappings.get("leadership_structure", {}).get(value)
            status = "SCORED" if score is not None else "UNKNOWN"
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score,
                                       status=status, coverage=1.0 if score is not None else 0.0, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=1),
                                       raw_inputs={"chair_structure": value, "chair_is_independent": snapshot.get("chair_is_independent")},
                                       supporting_record_ids=[sid], coverage_reason="EXPLICIT_CONFIGURED_LEADERSHIP_MAPPING" if score is not None else "LEADERSHIP_STRUCTURE_UNKNOWN"))
        elif definition.input_source == "governance_committees" and definition.mapping_rule == "committee_independence":
            required = [row for row in committees if str(row.get("required_independence", "")).upper() == "YES"]
            known = [row for row in required if _committee_explicitly_independent(row)]
            coverage = len(known) / len(required) if required else 0.0
            if not required:
                status, score = "INSUFFICIENT_COVERAGE", None
            elif coverage < definition.min_coverage:
                status, score = "INSUFFICIENT_COVERAGE", None
            else:
                status, score = "SCORED", 100.0 * len(known) / len(required)
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score,
                                       status=status, coverage=coverage, confidence=evidence_confidence(source_completeness=1.0, evidence_groups=len(known)),
                                       raw_inputs={"required_committees": len(required), "explicitly_independent": len(known)},
                                       supporting_record_ids=[str(row.get("committee_id")) for row in required], coverage_reason="UNKNOWN_COMMITTEE_INDEPENDENCE_REDUCES_COVERAGE" if status != "SCORED" else "EXPLICIT_COMMITTEE_INDEPENDENCE"))
        elif definition.input_source == "governance_committees" and definition.mapping_rule == "audit_expertise":
            audits = [row for row in committees if str(row.get("committee_type", "")).lower() == "audit"]
            experts = sum(len(row.get("financial_expert_person_ids", [])) for row in audits)
            score = 100.0 if audits and experts > 0 else None
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score,
                                       status="SCORED" if score is not None else "UNKNOWN", coverage=1.0 if audits else 0.0,
                                       confidence=evidence_confidence(source_completeness=1.0, evidence_groups=len(audits)),
                                       raw_inputs={"audit_committees": len(audits), "explicit_financial_experts": experts},
                                       supporting_record_ids=[str(row.get("committee_id")) for row in audits], coverage_reason="EXPLICIT_AUDIT_FINANCIAL_EXPERTS" if score is not None else "NO_EXPLICIT_AUDIT_EXPERT_EVIDENCE"))
        elif definition.mapping_rule == "outside_boards":
            rows = sources.governance("governance_overboarding", ticker)
            if not rows or any(row.get("outside_public_board_count") is None for row in rows):
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None, status="UNSCORED", coverage=0.0,
                                           confidence=0.0, raw_inputs={"confirmed_records": len(rows), "unknown_records": sum(row.get("outside_public_board_count") is None for row in rows)},
                                           supporting_record_ids=[str(row.get("overboarding_id")) for row in rows], coverage_reason="UNKNOWN_OUTSIDE_BOARD_COUNTS_NOT_SCORED"))
            else:
                average = sum(float(row.get("outside_public_board_count", 0)) for row in rows) / len(rows)
                score = max(0.0, 100.0 - average * 25.0)
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score, status="SCORED", coverage=1.0,
                                           confidence=evidence_confidence(source_completeness=1.0, evidence_groups=len(rows)), raw_inputs={"average_outside_public_boards": average},
                                           supporting_record_ids=[str(row.get("overboarding_id")) for row in rows], coverage_reason="CONFIRMED_CURRENT_PUBLIC_BOARD_ROLES"))
        elif definition.mapping_rule == "related_party":
            rows = sources.governance("governance_related_parties", ticker)
            # An empty materialized view is not equivalent to an audited explicit-none result.
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None, status="UNSCORED", coverage=0.0,
                                       confidence=0.0, raw_inputs={"relationships": len(rows)}, supporting_record_ids=[str(row.get("relationship_id")) for row in rows],
                                       coverage_reason="RELATED_PARTY_COVERAGE_NOT_EXPLICITLY_COMPLETE"))
        elif definition.mapping_rule == "voting_control":
            founder = snapshot.get("founder_voting_control") or {}
            pct = founder.get("voting_percentage")
            if pct is None:
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None, status="UNSCORED", coverage=0.0,
                                           confidence=0.0, raw_inputs={"voting_percentage": None, "economic_percentage": founder.get("economic_percentage")},
                                           supporting_record_ids=[sid], coverage_reason="VOTING_CONTROL_UNKNOWN"))
            else:
                label = "low" if float(pct) < 20 else "moderate" if float(pct) < 50 else "high"
                score = mappings.get("voting_control", {}).get(label)
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=score, status="SCORED", coverage=1.0,
                                           confidence=evidence_confidence(source_completeness=1.0, evidence_groups=1), raw_inputs={"voting_percentage": pct, "economic_percentage": founder.get("economic_percentage")},
                                           normalized_inputs={"concentration_band": label}, supporting_record_ids=[sid], coverage_reason="VOTING_CONTROL_SEPARATE_FROM_ECONOMIC_OWNERSHIP"))
        elif definition.mapping_rule == "shareholder_rights":
            rights = snapshot.get("shareholder_rights") or {}
            core = ["advance_notice", "cumulative_voting", "special_meeting", "written_consent", "supermajority"]
            known = [key for key in core if str(rights.get(key, "UNKNOWN")).upper() not in {"UNKNOWN", "NONE", ""}]
            coverage = len(known) / len(core)
            if coverage < 1.0:
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None, status="INSUFFICIENT_COVERAGE", coverage=coverage,
                                           confidence=evidence_confidence(source_completeness=coverage, evidence_groups=1), raw_inputs={"rights": rights, "known_core_rights": known},
                                           supporting_record_ids=[sid, str(snapshot.get("shareholder_rights_id", ""))], coverage_reason="UNKNOWN_SHAREHOLDER_RIGHTS_DO_NOT_IMPLY_ADVERSE_RESULT"))
            else:
                positive = sum(str(rights[key]).upper() in {"YES", "MAJORITY", "ANNUAL"} for key in core)
                results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=100.0 * positive / len(core), status="SCORED", coverage=1.0,
                                           confidence=evidence_confidence(source_completeness=1.0, evidence_groups=1), raw_inputs={"rights": rights}, supporting_record_ids=[sid], coverage_reason="ALL_CORE_RIGHTS_KNOWN"))
        elif definition.mapping_rule == "class_structure":
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None, status="UNSCORED", coverage=0.0,
                                       confidence=0.0, raw_inputs={"share_class_structure": snapshot.get("share_class_structure")}, supporting_record_ids=[sid],
                                       coverage_reason="FACTUAL_CLASS_STRUCTURE_REQUIRES_EXPLICIT_PROFILE_EFFECT"))
        elif definition.input_source == "governance_refreshment":
            results.append(make_factor(ticker=ticker, as_of=as_of, profile=profile, definition=definition, score=None, status="UNSCORED", coverage=0.0, confidence=0.0,
                                       supporting_record_ids=[sid], coverage_reason="REFRESHMENT_QUALITY_RULE_NOT_ESTABLISHED"))
    if snapshot is not None:
        source_group = snapshot.get("coverage_id") or snapshot.get("snapshot_id")
        for item in results:
            item.raw_inputs.setdefault("source_group", source_group)
    return results
