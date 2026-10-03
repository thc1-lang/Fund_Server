"""Evidence-backed models for Component 6B.

These records describe facts and comparisons only. They intentionally do not
contain management, credibility, governance, or investment scores.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field
from typing import Any


TRACK_RECORD_VERSION = "management-track-record-v1"
GUIDANCE_MATCH_VERSION = "guidance-match-v1"
ATTRIBUTION_VERSION = "attribution-v1"
FINANCIAL_NORMALIZATION_VERSION = "financial-normalization-v1"


def _from_dict(cls, value: dict[str, Any]):
    fields = cls.__dataclass_fields__
    data = {name: value[name] for name in fields if name in value}
    for name, item in fields.items():
        if name not in data and item.default is not MISSING:
            data[name] = item.default
    return cls(**data)


class Attribution:
    DIRECT = "DIRECT"
    ROLE_BASED = "ROLE_BASED"
    TEAM = "TEAM"
    TENURE_OVERLAP = "TENURE_OVERLAP"
    UNATTRIBUTED = "UNATTRIBUTED"


@dataclass
class ManagementTenure:
    tenure_id: str
    person_id: str
    company_name: str
    ticker: str
    issuer_cik: str | None
    role: str
    role_category: str
    start_date: str | None = None
    end_date: str | None = None
    date_precision: str | None = None
    current_as_of_date: str | None = None
    scheduled_end_date: str | None = None
    prior_company_status: str = "CURRENT_COMPANY"
    source_relationship_ids: list[str] = field(default_factory=list)
    source_evidence_ids: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_local_paths: list[str] = field(default_factory=list)
    source_accession_numbers: list[str] = field(default_factory=list)
    evidence_text: str = ""
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ManagementTenure":
        return _from_dict(cls, value)


@dataclass
class FinancialFact:
    fact_id: str
    ticker: str
    metric: str
    period_start: str | None
    period_end: str
    value: float
    unit: str
    accounting_basis: str
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form: str | None = None
    accession_number: str | None = None
    source_url: str = ""
    source_local_path: str | None = None
    source_fact: dict[str, Any] = field(default_factory=dict)
    value_method: str = "reported"
    derivation: dict[str, Any] = field(default_factory=dict)
    revision_provenance: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    financial_normalization_version: str = FINANCIAL_NORMALIZATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FinancialFact":
        return _from_dict(cls, value)


@dataclass
class OperatingOutcome:
    outcome_id: str
    person_id: str
    tenure_id: str
    ticker: str
    company_name: str
    metric: str
    period_start: str | None
    period_end: str | None
    starting_value: float | None
    ending_value: float | None
    absolute_change: float | None
    percent_change: float | None
    cagr: float | None
    unit: str
    accounting_basis: str
    period_status: str
    source_fact: dict[str, Any]
    source_url: str
    source_accession: str | None = None
    source_local_path: str | None = None
    attribution_level: str = Attribution.TENURE_OVERLAP
    evidence_text: str = ""
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION
    tenure_duration_days: int | None = None
    observation_duration_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OperatingOutcome":
        return _from_dict(cls, value)


@dataclass
class GuidanceCommitment:
    guidance_id: str
    ticker: str
    person_id: str | None
    tenure_id: str | None
    issued_date: str | None
    fiscal_horizon: str
    metric: str
    scope: str
    low_value: float | None
    high_value: float | None
    point_value: float | None
    unit: str | None
    accounting_basis: str | None
    guidance_status: str
    speaker: str | None
    attribution_level: str
    source_claim_id: str | None
    source_document_id: str | None
    evidence_text: str
    source_url: str | None
    source_local_path: str | None
    revision_number: int = 1
    revision_type: str = "initial"
    created_at: str = ""
    guidance_match_version: str = GUIDANCE_MATCH_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GuidanceCommitment":
        return _from_dict(cls, value)


@dataclass
class GuidanceOutcome:
    guidance_outcome_id: str
    guidance_id: str
    ticker: str
    actual_value: float | None
    actual_period: str | None
    outcome: str
    absolute_error: float | None
    percentage_error: float | None
    actual_source: dict[str, Any]
    comparison_method: str
    rejection_reason: str | None = None
    created_at: str = ""
    guidance_match_version: str = GUIDANCE_MATCH_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GuidanceOutcome":
        return _from_dict(cls, value)


@dataclass
class StrategicCommitment:
    commitment_id: str
    person_id: str | None
    ticker: str
    tenure_id: str | None
    commitment_date: str | None
    category: str
    subject: str
    commitment_text: str
    target_date: str | None
    metric: str | None
    value: float | None
    unit: str | None
    attribution_level: str
    source_claim_id: str | None
    source_document_id: str | None
    evidence_text: str
    source_url: str | None
    source_local_path: str | None
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StrategicCommitment":
        return _from_dict(cls, value)


@dataclass
class StrategicOutcome:
    outcome_id: str
    commitment_id: str
    outcome: str
    outcome_date: str | None
    evidence_text: str
    source_claim_id: str | None
    source_document_id: str | None
    source_url: str | None
    source_local_path: str | None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StrategicOutcome":
        return _from_dict(cls, value)


@dataclass
class CapitalAllocationEvent:
    event_id: str
    ticker: str
    company_name: str
    person_id: str | None
    tenure_id: str | None
    event_date: str | None
    event_type: str
    event_status: str
    value: float | None
    currency: str | None
    shares: float | None
    target_or_counterparty: str | None
    stated_rationale: str | None
    source_claim_id: str | None
    source_document_id: str | None
    evidence_text: str
    source_url: str | None
    source_local_path: str | None
    attribution_level: str
    capital_allocation_program_id: str | None = None
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapitalAllocationEvent":
        return _from_dict(cls, value)


@dataclass
class PersonTrackRecordSnapshot:
    snapshot_id: str
    person_id: str
    ticker: str
    role: str
    tenure_id: str
    guidance_records: int
    guidance_resolved: int
    guidance_beat: int
    guidance_met: int
    guidance_missed: int
    guidance_withdrawn: int
    strategic_commitments: int
    strategic_achieved: int
    strategic_delayed: int
    strategic_missed: int
    strategic_unresolved: int
    major_acquisitions: int
    major_divestitures: int
    buybacks: int
    equity_issuance: int
    debt_actions: int
    operating_outcome_ids: list[str]
    capital_allocation_event_ids: list[str]
    direct_attribution_count: int
    role_based_count: int
    tenure_overlap_count: int
    as_of_date: str
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION
    team_count: int = 0
    coverage_quality: str = "INSUFFICIENT"
    coverage_limitations: list[str] = field(default_factory=list)
    operating_metrics_available: list[str] = field(default_factory=list)
    tenure_duration_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PersonTrackRecordSnapshot":
        return _from_dict(cls, value)


@dataclass
class TrackRecordRun:
    run_id: str
    tickers: list[str]
    as_of_date: str
    include_prior_companies: bool
    counts: dict[str, dict[str, int]]
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrackRecordRun":
        return _from_dict(cls, value)


@dataclass
class GuidanceCoverageDiagnostic:
    ticker: str
    reason: str
    document_count: int
    claim_count: int
    guidance_language_claims: int
    numeric_guidance_claims: int
    captured_commitments: int
    examples: list[str] = field(default_factory=list)
    guidance_absence_confirmed: bool = False
    coverage_neutral: bool = True
    created_at: str = ""
    track_record_version: str = TRACK_RECORD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GuidanceCoverageDiagnostic":
        return _from_dict(cls, value)


__all__ = [
    "TRACK_RECORD_VERSION", "GUIDANCE_MATCH_VERSION", "ATTRIBUTION_VERSION",
    "FINANCIAL_NORMALIZATION_VERSION", "Attribution", "ManagementTenure",
    "FinancialFact", "OperatingOutcome", "GuidanceCommitment", "GuidanceOutcome",
    "StrategicCommitment", "StrategicOutcome", "CapitalAllocationEvent",
    "PersonTrackRecordSnapshot", "TrackRecordRun",
    "GuidanceCoverageDiagnostic",
]
