"""Typed records for Component 6A.

Every fact record carries a source reference.  Dates are ISO dates where the
official source provides a precise date; otherwise they remain ``None`` and
the original context is retained in ``evidence_text``.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field as dc_field
from typing import Any


PARSER_VERSION = "management-person-career-v2"


def _from_dict(cls, value: dict[str, Any]):
    fields = {item.name for item in cls.__dataclass_fields__.values()}
    data = {key: value[key] for key in fields if key in value}
    for item in cls.__dataclass_fields__.values():
        if item.name not in data and item.default is not MISSING:
            data[item.name] = item.default
    return cls(**data)


@dataclass(frozen=True)
class OfficialSource:
    """A stable pointer to an official SEC or issuer source."""

    source_id: str
    source_authority: str  # sec_official_filing or issuer_official
    source_type: str  # DEF 14A, 10-K, 8-K, leadership_page, etc.
    url: str
    local_path: str | None = None
    issuer_cik: str | None = None
    accession_number: str | None = None
    filing_date: str | None = None
    report_date: str | None = None
    retrieved_at: str = ""
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OfficialSource":
        return _from_dict(cls, value)


@dataclass
class RoleAssertion:
    assertion_id: str
    person_id: str
    ticker: str
    role: str
    role_category: str
    is_current: bool
    issuer_cik: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    date_precision: str | None = None
    title_as_reported: str | None = None
    responsibilities: list[str] = dc_field(default_factory=list)
    evidence_text: str = ""
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    source_form_types: list[str] = dc_field(default_factory=list)
    source_url: str | None = None
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str | None = None
    confidence: str = "reported"
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoleAssertion":
        return _from_dict(cls, value)


@dataclass
class CareerEntry:
    career_id: str
    person_id: str
    ticker: str
    employer: str
    issuer_cik: str | None = None
    employer_ticker: str | None = None
    employer_cik: str | None = None
    employer_classification: str = "UNRESOLVED"
    event_type: str = "employment"
    date_precision: str | None = None
    title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    responsibilities: list[str] = dc_field(default_factory=list)
    functional_areas: list[str] = dc_field(default_factory=list)
    industry_categories: list[str] = dc_field(default_factory=list)
    evidence_text: str = ""
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    source_form_types: list[str] = dc_field(default_factory=list)
    source_url: str | None = None
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str | None = None
    confidence: str = "reported"
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CareerEntry":
        return _from_dict(cls, value)


@dataclass
class BoardMembership:
    board_id: str
    person_id: str
    ticker: str
    board_company: str
    issuer_cik: str | None = None
    person_company_id: str | None = None
    board_ticker: str | None = None
    board_cik: str | None = None
    board_classification: str = "UNRESOLVED"
    date_precision: str | None = None
    board_role: str = "director"
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = True
    evidence_text: str = ""
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    source_form_types: list[str] = dc_field(default_factory=list)
    source_url: str | None = None
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str | None = None
    confidence: str = "reported"
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoardMembership":
        return _from_dict(cls, value)


@dataclass
class ManagementPerson:
    person_id: str
    # ``ticker`` and ``company_name`` are retained as compatibility aliases
    # for the primary relationship. Canonical identity is global and is not
    # derived from either field.
    ticker: str
    company_name: str
    full_name: str
    normalized_name: str
    issuer_cik: str | None = None
    current_roles: list[str] = dc_field(default_factory=list)
    role_categories: list[str] = dc_field(default_factory=list)
    role_start_dates: dict[str, str] = dc_field(default_factory=dict)
    current_since: str | None = None
    is_founder: bool = False
    founder_scope: str = "unknown"  # issuer, other_company, or unknown
    founder_context: str | None = None
    relevant_industries: list[str] = dc_field(default_factory=list)
    relevant_responsibilities: list[str] = dc_field(default_factory=list)
    biography: str | None = None
    aliases: list[str] = dc_field(default_factory=list)
    issuer_relationship_ids: list[str] = dc_field(default_factory=list)
    insider_person_ids: list[str] = dc_field(default_factory=list)
    reporting_owner_ciks: list[str] = dc_field(default_factory=list)
    identity_evidence: list[str] = dc_field(default_factory=list)
    identity_confidence: str = "name_only"
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    source_form_types: list[str] = dc_field(default_factory=list)
    source_url: str | None = None
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str | None = None
    identity_status: str = "resolved"
    created_at: str = ""
    updated_at: str = ""
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ManagementPerson":
        return _from_dict(cls, value)


@dataclass
class ManagementRun:
    run_id: str
    tickers: list[str]
    sources: list[str]
    people_count: int
    roles_count: int
    career_count: int
    boards_count: int
    warnings: list[str] = dc_field(default_factory=list)
    created_at: str = ""
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ManagementRun":
        return _from_dict(cls, value)


@dataclass
class PersonCompanyRelationship:
    """The issuer-specific relationship of a global person."""

    person_company_id: str
    person_id: str
    issuer_cik: str
    ticker: str
    company_name: str
    current_roles: list[str] = dc_field(default_factory=list)
    role_categories: list[str] = dc_field(default_factory=list)
    relationship_type: str = "management_or_board"
    status: str = "current"
    start_date: str | None = None
    end_date: str | None = None
    date_precision: str | None = None
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    source_form_types: list[str] = dc_field(default_factory=list)
    evidence_text: str = ""
    confidence: str = "reported"
    source_url: str | None = None
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str | None = None
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PersonCompanyRelationship":
        return _from_dict(cls, value)


@dataclass
class EducationEntry:
    education_id: str
    person_id: str
    ticker: str
    institution: str
    degree: str | None = None
    field: str | None = None
    year: str | None = None
    date_precision: str | None = None
    evidence_text: str = ""
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    source_form_types: list[str] = dc_field(default_factory=list)
    source_url: str | None = None
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str | None = None
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EducationEntry":
        return _from_dict(cls, value)


@dataclass
class InsiderIdentityLink:
    link_id: str
    management_person_id: str
    insider_person_id: str
    ticker: str
    issuer_cik: str | None = None
    reporting_owner_cik: str | None = None
    match_method: str = ""
    confidence: str = "probable"
    evidence_text: str = ""
    source_ids: list[str] = dc_field(default_factory=list)
    source_urls: list[str] = dc_field(default_factory=list)
    source_local_paths: list[str] = dc_field(default_factory=list)
    source_accession_numbers: list[str] = dc_field(default_factory=list)
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InsiderIdentityLink":
        return _from_dict(cls, value)


@dataclass
class RoleChangeEvent:
    event_id: str
    person_id: str | None
    ticker: str
    issuer_cik: str
    event_type: str  # appointment, departure, resignation, retirement, interim_appointment
    person_name: str | None = None
    role: str | None = None
    effective_date: str | None = None
    source_id: str = ""
    source_url: str = ""
    local_source_path: str | None = None
    accession_number: str | None = None
    form_type: str = "8-K"
    filing_date: str | None = None
    evidence_text: str = ""
    confidence: str = "reported"
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoleChangeEvent":
        return _from_dict(cls, value)


__all__ = [
    "PARSER_VERSION",
    "OfficialSource",
    "SourceProvenance",
    "RoleAssertion",
    "CareerEntry",
    "BoardMembership",
    "ManagementPerson",
    "ManagementRun",
    "PersonCompanyRelationship",
    "EducationEntry",
    "InsiderIdentityLink",
    "RoleChangeEvent",
]

# Descriptive alias for callers that use the provenance terminology from the
# frozen SEC component.
SourceProvenance = OfficialSource
