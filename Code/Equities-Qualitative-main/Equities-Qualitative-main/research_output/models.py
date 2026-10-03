from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class EvidenceRef:
    number: int | None
    record_id: str
    record_type: str
    title: str = ""
    date: str = ""
    url: str = ""
    local_path: str = ""
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportFact:
    text: str
    section: str
    evidence_ids: list[str] = field(default_factory=list)
    source_numbers: list[int] = field(default_factory=list)
    confidence: float | None = None
    direction: str | None = None
    kind: str = "fact"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompanyResearchDossier:
    dossier_id: str
    ticker: str
    company_name: str
    as_of_date: str
    source_cutoff: str
    executive_summary: list[ReportFact]
    business_quality: dict[str, Any]
    business_trajectory: dict[str, Any]
    management: dict[str, Any]
    insiders: dict[str, Any]
    governance: dict[str, Any]
    scoring: dict[str, Any]
    key_positives: list[ReportFact]
    key_negatives: list[ReportFact]
    risks: dict[str, list[ReportFact]]
    catalysts: list[ReportFact]
    coverage_gaps: list[str]
    source_index: list[EvidenceRef]
    generated_at: str
    report_version: str
    profile_version: str
    scoring_version: str
    coverage_version: str
    input_fingerprint: str
    evidence_appendix: list[EvidenceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_evidence: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not include_evidence:
            d.pop("evidence_appendix", None)
        return d
