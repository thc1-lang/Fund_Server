"""Factual and semantic audit helpers for Component 6C.

The audit is deliberately diagnostic.  It checks that normalized records are
complete enough to trace back to their official sources, while leaving an
unsupported fact as UNKNOWN rather than treating it as a defect.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable, Mapping


ISSUE_TAXONOMY = (
    "WRONG_BOARD_MEMBER", "WRONG_CURRENT_STATUS", "WRONG_INDEPENDENCE",
    "UNSUPPORTED_INDEPENDENCE", "WRONG_COMMITTEE", "WRONG_FINANCIAL_EXPERT",
    "FALSE_TENURE_PRECISION", "WRONG_EXPERTISE", "STALE_OUTSIDE_BOARD",
    "WRONG_RELATED_PARTY_STATUS", "WRONG_SHARE_CLASS", "WRONG_VOTES_PER_SHARE",
    "WRONG_VOTING_CONTROL", "ECONOMIC_VOTING_CONFLATION", "WRONG_CLASSIFIED_BOARD_STATUS",
    "WRONG_SHAREHOLDER_RIGHT", "RIGHT_CONDITION_LOST", "UNJUSTIFIED_COVERAGE_COMPLETE",
    "MISSING_PROVENANCE",
)


@dataclass(frozen=True)
class DirectorAuditFinding:
    ticker: str
    person_id: str
    name: str
    current: bool
    board_role: str
    independent: str
    independence_basis: str
    start_date: str | None
    tenure_precision: str
    committee_ids: list[str]
    committee_chairs: list[str]
    audit_financial_expert: bool
    expertise_categories: list[str]
    expertise_career_ids: list[str]
    expertise_evidence: str
    outside_public_board_count: int | None
    ownership_link: dict[str, Any]
    related_party_evidence: list[str]
    source_records: list[dict[str, Any]]
    issues: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_current_board(result: Mapping[str, Any], people: Mapping[str, Any] | None = None) -> list[DirectorAuditFinding]:
    """Audit every materialized current director in one ``build_governance`` result."""
    people = people or {}
    board = list(result.get("board", []))
    committees = list(result.get("committees", []))
    tenure_by_person = {item.person_id: item for item in result.get("tenure", [])}
    expertise_by_person = {item.person_id: item for item in result.get("expertise", [])}
    outside_by_person = {item.person_id: item for item in result.get("overboarding", [])}
    related = list(result.get("related", []))
    seen: set[str] = set()
    findings: list[DirectorAuditFinding] = []
    for member in board:
        person = people.get(member.person_id)
        tenure = tenure_by_person.get(member.person_id)
        expertise = expertise_by_person.get(member.person_id)
        outside = outside_by_person.get(member.person_id)
        member_committees = [item for item in committees if member.person_id in item.members]
        chair_committees = [item.committee_id for item in member_committees if item.chair_person_id == member.person_id]
        is_audit_expert = any(member.person_id in item.financial_expert_person_ids for item in member_committees)
        related_evidence = [item.evidence_text for item in related if item.person_id == member.person_id]
        issues: list[str] = []
        if member.person_id in seen:
            issues.append("WRONG_BOARD_MEMBER")
        seen.add(member.person_id)
        if not member.is_current:
            issues.append("WRONG_CURRENT_STATUS")
        if member.is_independent not in {"INDEPENDENT", "NOT_INDEPENDENT", "UNKNOWN"}:
            issues.append("WRONG_INDEPENDENCE")
        if member.is_independent != "UNKNOWN" and (not member.independence_basis or not member.independence_source):
            issues.append("UNSUPPORTED_INDEPENDENCE")
        if tenure and tenure.tenure_precision in {"YEAR", "MONTH"} and tenure.tenure_years_exact is not None:
            issues.append("FALSE_TENURE_PRECISION")
        if expertise and expertise.categories and not expertise.career_ids:
            issues.append("WRONG_EXPERTISE")
        provenance_values = (member.source_url, member.source_accession, member.source_form, member.source_date, member.local_source_path, member.evidence_text)
        if not all(provenance_values):
            issues.append("MISSING_PROVENANCE")
        source_records = [{
            "source_url": member.source_url, "source_accession": member.source_accession,
            "source_form": member.source_form, "source_date": member.source_date,
            "local_source_path": member.local_source_path,
            "evidence_text": member.evidence_text,
        }]
        findings.append(DirectorAuditFinding(
            ticker=member.ticker, person_id=member.person_id,
            name=getattr(person, "full_name", member.person_id), current=member.is_current,
            board_role=member.board_role, independent=member.is_independent,
            independence_basis=member.independence_basis, start_date=member.start_date,
            tenure_precision=tenure.tenure_precision if tenure else "UNKNOWN",
            committee_ids=[item.committee_id for item in member_committees],
            committee_chairs=chair_committees, audit_financial_expert=is_audit_expert,
            expertise_categories=list(expertise.categories) if expertise else [],
            expertise_career_ids=list(expertise.career_ids) if expertise else [],
            expertise_evidence=getattr(expertise, "evidence_text", "") if expertise else "",
            outside_public_board_count=outside.outside_public_board_count if outside else None,
            ownership_link=dict(member.ownership_link), related_party_evidence=related_evidence,
            source_records=source_records, issues=sorted(set(issues)),
        ))
    return sorted(findings, key=lambda item: (item.ticker, item.name, item.person_id))


def render_audit_report(results: Iterable[Mapping[str, Any]], people_by_id: Mapping[str, Any] | None = None) -> str:
    people_by_id = people_by_id or {}
    lines = ["# Component 6C factual and semantic audit", "", "This report contains source-backed facts and coverage diagnostics only.", ""]
    all_findings: list[DirectorAuditFinding] = []
    for result in results:
        findings = audit_current_board(result, people_by_id)
        all_findings.extend(findings)
        ticker = str(result.get("ticker", "")).upper()
        snapshot = result.get("snapshot")
        coverage = result.get("coverage")
        lines += [f"## {ticker}", "", f"Current directors audited: {len(findings)}", ""]
        if snapshot:
            lines.append(f"Snapshot: board {snapshot.board_size}; independent {snapshot.independent_count}/{snapshot.independent_denominator}; unknown independence {snapshot.unknown_independence}; tenure statistics {snapshot.tenure_statistics_precision}.")
        if coverage:
            lines.append(f"Coverage: {coverage.to_dict()}")
        lines += ["", "| Name | Person ID | Current | Role | Independent | Start | Tenure precision | Committees | Committee chair | Audit expert | Expertise | Outside boards | Issues |", "|---|---|---|---|---|---|---|---|---|---|---|---:|---|"]
        for finding in findings:
            lines.append("| " + " | ".join([
                finding.name, finding.person_id, str(finding.current), finding.board_role,
                finding.independent, finding.start_date or "UNKNOWN", finding.tenure_precision,
                ", ".join(finding.committee_ids) or "UNKNOWN",
                ", ".join(finding.committee_chairs) or "NO", "YES" if finding.audit_financial_expert else "NO",
                ", ".join(finding.expertise_categories) or "UNKNOWN",
                str(finding.outside_public_board_count) if finding.outside_public_board_count is not None else "UNKNOWN",
                ", ".join(finding.issues) or "VALID",
            ]) + " |")
        lines.append("")
        for finding in findings:
            lines += [f"### {finding.name}", "", f"- Source records: {finding.source_records}", f"- Committee chair: {finding.committee_chairs or 'NO'}", f"- Audit financial expert: {'YES' if finding.audit_financial_expert else 'NO'}", f"- Ownership linkage: {finding.ownership_link}", f"- Related-party evidence: {finding.related_party_evidence or 'UNKNOWN / no person-linked normalized record'}", f"- Expertise evidence IDs: {finding.expertise_career_ids or 'UNKNOWN'}", f"- Expertise evidence: {finding.expertise_evidence or 'UNKNOWN'}", ""]
    invalid = [item for item in all_findings if item.issues]
    lines += ["## Audit result", "", f"Profiles audited: {len(all_findings)}", f"Profiles with classified issues: {len(invalid)}", ""]
    return "\n".join(lines)


__all__ = ["ISSUE_TAXONOMY", "DirectorAuditFinding", "audit_current_board", "render_audit_report"]
