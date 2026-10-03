"""Evidence-backed factual models for Component 6C.

The models deliberately contain facts and coverage metadata only.  They do not
contain governance scores or recommendations.  Every record is versioned and
has a stable id so a refresh can safely upsert cached evidence.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field
from typing import Any


GOVERNANCE_VERSION = "board-governance-v1"
INDEPENDENCE_VERSION = "board-independence-v1"
COMMITTEE_VERSION = "board-committees-v1"
VOTING_STRUCTURE_VERSION = "voting-structure-v1"
RIGHTS_VERSION = "shareholder-rights-v1"


def _from_dict(cls, value: dict[str, Any]):
    fields = {item.name for item in cls.__dataclass_fields__.values()}
    data = {key: value[key] for key in fields if key in value}
    for item in cls.__dataclass_fields__.values():
        if item.name not in data and item.default is not MISSING:
            data[item.name] = item.default
        elif item.name not in data and item.default_factory is not MISSING:
            data[item.name] = item.default_factory()
    return cls(**data)


class DictRecord:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]):
        return _from_dict(cls, value)


@dataclass
class GovernanceBoardMembership(DictRecord):
    board_membership_id: str
    person_id: str
    ticker: str
    issuer_cik: str | None = None
    board_role: str = "director"
    is_current: bool = True
    start_date: str | None = None
    end_date: str | None = None
    date_precision: str | None = None
    is_independent: str = "UNKNOWN"  # INDEPENDENT, NOT_INDEPENDENT, UNKNOWN
    independence_basis: str = "unknown"
    independence_source: str | None = None
    committee_memberships: list[str] = field(default_factory=list)
    chair_status: str | None = None
    lead_independent_status: bool = False
    age: int | None = None
    ownership_link: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    local_source_path: str | None = None
    evidence_text: str = ""
    created_at: str = ""
    governance_version: str = GOVERNANCE_VERSION
    independence_version: str = INDEPENDENCE_VERSION


@dataclass
class BoardCommittee(DictRecord):
    committee_id: str
    ticker: str
    committee_type: str
    committee_name: str
    members: list[str] = field(default_factory=list)
    chair_person_id: str | None = None
    financial_expert_person_ids: list[str] = field(default_factory=list)
    financial_background: dict[str, str] = field(default_factory=dict)
    required_independence: str = "UNKNOWN"
    member_independence: dict[str, str] = field(default_factory=dict)
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    local_source_path: str | None = None
    evidence_text: str = ""
    created_at: str = ""
    committee_version: str = COMMITTEE_VERSION


@dataclass
class BoardTenure(DictRecord):
    tenure_id: str
    ticker: str
    person_id: str
    start_date: str | None = None
    as_of_date: str = ""
    date_precision: str | None = None
    tenure_days: int | None = None
    tenure_years: float | None = None
    # ``tenure_years`` is retained for compatibility with the first 6C
    # materialization.  These fields make the evidentiary precision explicit:
    # year/month starts produce an estimate, never a falsely exact duration.
    tenure_years_exact: float | None = None
    tenure_years_estimate: float | None = None
    tenure_precision: str = "UNKNOWN"  # DAY, MONTH, YEAR, UNKNOWN
    tenure_bucket: str = "unknown"
    age: int | None = None
    source_url: str | None = None
    source_accession: str | None = None
    evidence_text: str = ""
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class BoardRefreshment(DictRecord):
    refreshment_id: str
    ticker: str
    as_of_date: str
    directors_added_1y: int = 0
    directors_added_3y: int = 0
    directors_departed_1y: int = 0
    directors_departed_3y: int = 0
    added_person_ids: list[str] = field(default_factory=list)
    departed_person_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class BoardExpertise(DictRecord):
    expertise_id: str
    ticker: str
    person_id: str
    categories: list[str] = field(default_factory=list)
    career_ids: list[str] = field(default_factory=list)
    evidence_text: str = ""
    source_urls: list[str] = field(default_factory=list)
    source_accessions: list[str] = field(default_factory=list)
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class OverboardingRecord(DictRecord):
    overboarding_id: str
    ticker: str
    person_id: str
    current_public_company_board_count: int | None = None
    current_public_board_roles: list[dict[str, Any]] = field(default_factory=list)
    current_executive_role: str | None = None
    is_current_public_company_ceo: bool = False
    outside_public_board_count: int | None = None
    unknown_board_count: int = 0
    source_ids: list[str] = field(default_factory=list)
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class RelatedPartyRelationship(DictRecord):
    relationship_id: str
    ticker: str
    person_id: str | None = None
    relationship_type: str = "other"
    counterparty: str | None = None
    description: str = ""
    transaction_value: float | None = None
    currency: str | None = None
    period: str | None = None
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    local_source_path: str | None = None
    evidence_text: str = ""
    created_at: str = ""
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class VotingClass(DictRecord):
    voting_class_id: str
    ticker: str
    class_name: str
    shares_outstanding: int | None = None
    votes_per_share: float | None = None
    conversion_rights: str | None = None
    transfer_restrictions: str | None = None
    sunset_provisions: str | None = None
    holder_restrictions: str | None = None
    structural_notes: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    evidence_text: str = ""
    voting_structure_version: str = VOTING_STRUCTURE_VERSION


@dataclass
class VotingControlSnapshot(DictRecord):
    voting_control_id: str
    ticker: str
    share_class_structure: str = "unknown"  # single_class, dual_class, multi_class, unknown
    economic_ownership: dict[str, Any] = field(default_factory=dict)
    voting_ownership: dict[str, Any] = field(default_factory=dict)
    founder_control: dict[str, Any] = field(default_factory=dict)
    voting_agreements: list[dict[str, Any]] = field(default_factory=list)
    structural_notes: dict[str, Any] = field(default_factory=dict)
    control_as_of_date: str | None = None
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    evidence_text: str = ""
    voting_structure_version: str = VOTING_STRUCTURE_VERSION


@dataclass
class ShareholderRights(DictRecord):
    rights_id: str
    ticker: str
    classified_board_status: str = "UNKNOWN"  # CLASSIFIED, ANNUAL_ELECTION, UNKNOWN
    number_of_classes: int | None = None
    approximate_class_terms: str | None = None
    director_election_standard: str = "UNKNOWN"
    contested_election_standard: str = "UNKNOWN"
    uncontested_election_standard: str = "UNKNOWN"
    resignation_policy: str = "UNKNOWN"
    special_meeting_right: str = "UNKNOWN"
    written_consent_right: str = "UNKNOWN"
    proxy_access: str = "UNKNOWN"
    advance_notice_requirements: str = "UNKNOWN"
    advance_notice_scope: str | None = None
    special_meeting_scope: str | None = None
    cumulative_voting: str = "UNKNOWN"
    supermajority_requirements: str = "UNKNOWN"
    poison_pill: str = "UNKNOWN"
    equity_ownership_guidelines: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    local_source_path: str | None = None
    source_coverage: str = "UNKNOWN"
    evidence_text: str = ""
    rights_version: str = RIGHTS_VERSION


@dataclass
class GovernanceEvent(DictRecord):
    event_id: str
    ticker: str
    event_type: str
    effective_date: str | None = None
    person_id: str | None = None
    description: str = ""
    source_url: str | None = None
    source_accession: str | None = None
    source_form: str | None = None
    source_date: str | None = None
    local_source_path: str | None = None
    evidence_text: str = ""
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class GovernanceCoverage(DictRecord):
    coverage_id: str
    ticker: str
    as_of_date: str
    board_composition: str = "UNKNOWN"
    independence: str = "UNKNOWN"
    tenure: str = "UNKNOWN"
    committees: str = "UNKNOWN"
    expertise: str = "UNKNOWN"
    outside_boards: str = "UNKNOWN"
    related_parties: str = "UNKNOWN"
    voting_structure: str = "UNKNOWN"
    shareholder_rights: str = "UNKNOWN"
    related_party_diagnostics: dict[str, str] = field(default_factory=dict)
    expertise_diagnostics: dict[str, str] = field(default_factory=dict)
    rights_source_coverage: str = "UNKNOWN"
    limitations: list[str] = field(default_factory=list)
    governance_version: str = GOVERNANCE_VERSION


@dataclass
class GovernanceSnapshot(DictRecord):
    snapshot_id: str
    ticker: str
    as_of_date: str
    board_size: int = 0
    independent_count: int = 0
    independent_denominator: int | None = None
    known_independence_count: int = 0
    known_independence_denominator: int = 0
    known_status_independent_percentage: float | None = None
    independence_percentage_basis: str = "known-independent/total-board"
    independent_percentage: float | None = None
    unknown_independence: int = 0
    chair_structure: str = "UNKNOWN"
    chair_person_id: str | None = None
    chair_is_independent: str = "UNKNOWN"
    ceo_is_chair: bool | None = None
    lead_independent_director: str | None = None
    average_tenure: float | None = None
    median_tenure: float | None = None
    tenure_statistics_precision: str = "UNKNOWN"
    tenure_buckets: dict[str, int] = field(default_factory=dict)
    committee_structure: list[str] = field(default_factory=list)
    board_membership_ids: list[str] = field(default_factory=list)
    committee_ids: list[str] = field(default_factory=list)
    tenure_ids: list[str] = field(default_factory=list)
    expertise_ids: list[str] = field(default_factory=list)
    overboarding_ids: list[str] = field(default_factory=list)
    related_party_ids: list[str] = field(default_factory=list)
    voting_class_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    public_board_counts: dict[str, int | None] = field(default_factory=dict)
    related_party_relationship_count: int = 0
    share_class_structure: str = "unknown"
    founder_voting_control: dict[str, Any] = field(default_factory=dict)
    classified_board_status: str = "UNKNOWN"
    shareholder_rights: dict[str, str] = field(default_factory=dict)
    shareholder_rights_id: str | None = None
    coverage_id: str | None = None
    governance_version: str = GOVERNANCE_VERSION
    independence_version: str = INDEPENDENCE_VERSION
    committee_version: str = COMMITTEE_VERSION
    voting_structure_version: str = VOTING_STRUCTURE_VERSION
    rights_version: str = RIGHTS_VERSION
    created_at: str = ""


@dataclass
class GovernanceRun(DictRecord):
    run_id: str
    tickers: list[str]
    as_of_date: str
    records: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    network_requests: int = 0
    created_at: str = ""
    governance_version: str = GOVERNANCE_VERSION


# Component 6C callers may use the concise relationship name from the
# specification; Component 6A's ``models.BoardMembership`` remains unchanged.
BoardMembership = GovernanceBoardMembership


__all__ = [
    "GOVERNANCE_VERSION", "INDEPENDENCE_VERSION", "COMMITTEE_VERSION",
    "VOTING_STRUCTURE_VERSION", "RIGHTS_VERSION", "GovernanceBoardMembership", "BoardMembership",
    "BoardCommittee", "BoardTenure", "BoardRefreshment", "BoardExpertise",
    "OverboardingRecord", "RelatedPartyRelationship", "VotingClass",
    "VotingControlSnapshot", "ShareholderRights", "GovernanceEvent",
    "GovernanceCoverage", "GovernanceSnapshot", "GovernanceRun",
]
