"""Typed, provenance-preserving records for insider intelligence.

The models in this package intentionally contain facts and classifications only.
They do not contain an investment score or a bullish/bearish conclusion.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field
from typing import Any


PARSER_VERSION = "sec-parser-v1"
CLASSIFICATION_VERSION = "transaction-classification-v1"
ALIGNMENT_VERSION = "insider-alignment-v1"
BASELINE_PARSER_VERSION = "proxy-baseline-v1"


def _from_dict(cls, value: dict[str, Any]):
    fields = {item.name for item in cls.__dataclass_fields__.values()}
    data = {key: value[key] for key in fields if key in value}
    for item in cls.__dataclass_fields__.values():
        if item.name not in data and item.default is not MISSING:
            data[item.name] = item.default
    return cls(**data)


@dataclass(frozen=True)
class SECProvenance:
    issuer_cik: str
    reporting_owner_cik: str | None
    accession_number: str
    form_type: str
    filing_date: str | None
    source_url: str
    local_source_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IssuerIdentity:
    ticker: str
    company_name: str
    issuer_cik: str
    source_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SECFiling:
    ticker: str
    issuer_cik: str
    form_type: str
    accession_number: str
    filing_date: str | None
    report_date: str | None
    primary_document: str
    source_url: str
    local_source_path: str | None = None
    downloaded: bool = False
    cache_hit: bool = False
    source_authority: str = "sec_structured"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SECFiling":
        return _from_dict(cls, value)


@dataclass
class InsiderPerson:
    person_id: str
    ticker: str
    company_name: str
    full_name: str
    roles: list[str] = field(default_factory=list)
    primary_role: str | None = None
    officer_title: str | None = None
    other_relationship: str | None = None
    is_director: bool | None = None
    is_officer: bool | None = None
    is_ceo: bool | None = None
    is_cfo: bool | None = None
    is_founder: bool | None = None
    is_ten_percent_owner: bool | None = None
    reporting_owner_cik: str | None = None
    aliases: list[str] = field(default_factory=list)
    appointment_date: str | None = None
    departure_date: str | None = None
    source_urls: list[str] = field(default_factory=list)
    source_filing_ids: list[str] = field(default_factory=list)
    source_form_types: list[str] = field(default_factory=list)
    source_filing_dates: list[str] = field(default_factory=list)
    source_local_paths: list[str] = field(default_factory=list)
    issuer_cik: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # Direct aliases make the exact source of the current identity record
    # available without discarding the complete multi-filing provenance lists.
    source_url: str | None = None
    filing_id: str | None = None
    filing_date: str | None = None
    form_type: str | None = None
    local_source_path: str | None = None
    source_authority: str = "sec_structured"
    role_source_urls: list[str] = field(default_factory=list)
    role_source_filing_ids: list[str] = field(default_factory=list)
    role_source_form_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InsiderPerson":
        return _from_dict(cls, value)


@dataclass
class InsiderOwnershipPosition:
    ownership_id: str
    ticker: str
    person_id: str
    as_of_date: str | None
    shares_owned_direct: float | None = None
    shares_owned_indirect: float | None = None
    shares_beneficially_owned: float | None = None
    options_or_derivatives: float | None = None
    other_equity_interests: float | None = None
    percent_shares_outstanding: float | None = None
    estimated_market_value: float | None = None
    source_url: str = ""
    filing_id: str = ""
    filing_date: str | None = None
    issuer_cik: str = ""
    reporting_owner_cik: str | None = None
    form_type: str = ""
    local_source_path: str = ""
    ownership_method: str = "reported"
    direct_indirect: str | None = None
    nature_of_indirect_ownership: str | None = None
    security_title: str | None = None
    security_type: str = "common_stock"
    source_row_index: int | None = None
    source_authority: str = "sec_structured"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InsiderOwnershipPosition":
        return _from_dict(cls, value)


@dataclass
class OwnershipBaseline:
    """Issuer-reported ownership from a proxy or other official filing.

    A baseline is deliberately separate from Section 16 positions.  Proxy
    tables commonly include beneficial, voting, derivative, and indirect
    interests that cannot safely be added to transaction-derived holdings.
    """

    baseline_id: str
    ticker: str
    company_name: str
    person_name: str
    issuer_cik: str
    source_accession: str
    source_url: str
    local_source_path: str
    # Explicit SEC provenance aliases mirror SECProvenance and make the
    # baseline schema self-describing to downstream consumers.
    accession_number: str = ""
    form_type: str = "DEF 14A"
    reporting_owner_cik: str | None = None
    source_form: str = "DEF 14A"
    filing_date: str | None = None
    as_of_date: str | None = None
    person_id: str | None = None
    role: str | None = None
    beneficial_shares: float | None = None
    beneficial_percent: float | None = None
    beneficial_percent_display: str | None = None
    direct_shares: float | None = None
    indirect_shares: float | None = None
    shares_acquirable_within_60_days: float | None = None
    options_included: bool | None = None
    other_derivative_or_rights_included: bool | None = None
    security_class: str | None = None
    class_breakdown: dict[str, float | None] = field(default_factory=dict)
    voting_power_percent: float | None = None
    voting_power_display: str | None = None
    voting_power_notes: str | None = None
    dispositive_power_notes: str | None = None
    ownership_notes: str | None = None
    footnotes: list[str] = field(default_factory=list)
    source_context: str | None = None
    source_method: str = "proxy_table"
    source_authority: str = "sec_official_filing"
    is_major_beneficial_owner: bool = False
    is_group_record: bool = False
    current_ownership_method: str = "proxy_only"
    reconstructed_current_shares: float | None = None
    reconstructed_as_of_date: str | None = None
    section16_position_ids: list[str] = field(default_factory=list)
    reconciliation_warning: str | None = None
    parser_version: str = BASELINE_PARSER_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OwnershipBaseline":
        return _from_dict(cls, value)


@dataclass
class OwnershipReconciliation:
    """Audit result joining one proxy baseline to Section 16 evidence."""

    reconciliation_id: str
    ticker: str
    baseline_id: str
    person_id: str | None
    current_ownership_method: str
    proxy_beneficial_shares: float | None = None
    section16_reported_shares: float | None = None
    reconstructed_current_shares: float | None = None
    as_of_date: str | None = None
    section16_position_ids: list[str] = field(default_factory=list)
    warning: str | None = None
    source_filing_ids: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OwnershipReconciliation":
        return _from_dict(cls, value)


@dataclass
class InsiderTransaction:
    transaction_id: str
    ticker: str
    person_id: str
    transaction_date: str | None
    filing_date: str | None
    security_type: str | None
    transaction_code: str | None
    transaction_type: str
    shares: float | None
    price: float | None
    transaction_value: float | None
    acquired_or_disposed: str | None
    shares_owned_after: float | None
    ownership_form: str | None
    is_open_market: bool | None
    is_option_exercise: bool | None
    is_equity_award: bool | None
    is_tax_withholding: bool | None
    is_gift: bool | None
    is_automatic_sale: bool | None
    is_10b5_1: bool | None
    footnotes: list[str]
    source_url: str
    filing_id: str
    filing_date_reported: str | None = None
    issuer_cik: str = ""
    reporting_owner_cik: str | None = None
    form_type: str = ""
    local_source_path: str = ""
    issuer_name: str | None = None
    issuer_ticker: str | None = None
    security_title: str | None = None
    deemed_execution_date: str | None = None
    transaction_form_type: str | None = None
    nature_of_indirect_ownership: str | None = None
    derivative_security: bool = False
    exercise_price: float | None = None
    expiration_date: str | None = None
    underlying_security: str | None = None
    underlying_shares: float | None = None
    source_row_index: int | None = None
    plan_adoption_date: str | None = None
    pre_transaction_shares: float | None = None
    pre_holdings_method: str = "unknown"
    percent_of_pre_transaction_holdings: float | None = None
    percent_of_post_transaction_holdings: float | None = None
    change_in_direct_holdings: float | None = None
    change_in_economic_exposure: float | None = None
    classification_confidence: float = 0.0
    source_authority: str = "sec_structured"
    parser_version: str = PARSER_VERSION
    classification_version: str = CLASSIFICATION_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InsiderTransaction":
        return _from_dict(cls, value)


@dataclass
class InsiderCluster:
    cluster_id: str
    ticker: str
    transaction_type: str
    start_date: str
    end_date: str
    people: list[str]
    transaction_ids: list[str]
    combined_value: float
    window_days: int = 30
    created_at: str = ""
    alignment_version: str = ALIGNMENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InsiderCluster":
        return _from_dict(cls, value)


@dataclass
class InsiderAlignmentSnapshot:
    ticker: str
    as_of_date: str
    ceo_shares: float | None
    cfo_shares: float | None
    founder_shares: float | None
    director_shares: float | None
    known_insider_shares: float | None
    known_insider_percent: float | None
    open_market_purchases_30d: int
    open_market_purchases_90d: int
    open_market_purchases_365d: int
    open_market_sales_30d: int
    open_market_sales_90d: int
    open_market_sales_365d: int
    purchase_value_365d: float
    sale_value_365d: float
    buyers_365d: int
    sellers_365d: int
    cluster_purchase_count: int
    cluster_sale_count: int
    source_filing_ids: list[str] = field(default_factory=list)
    alignment_version: str = ALIGNMENT_VERSION
    created_at: str = ""
    net_open_market_value_365d: float = 0.0
    automatic_sale_value_365d: float = 0.0
    planned_sale_value_365d: float = 0.0
    proxy_ceo_shares: float | None = None
    proxy_cfo_shares: float | None = None
    proxy_founder_shares: float | None = None
    proxy_director_shares: float | None = None
    proxy_group_shares: float | None = None
    proxy_group_percent: float | None = None
    section16_reported_shares: float | None = None
    reconstructed_current_shares: float | None = None
    current_ownership_method: str = "section16_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InsiderAlignmentSnapshot":
        return _from_dict(cls, value)


@dataclass
class SECParsedFiling:
    filing: SECFiling
    people: list[InsiderPerson] = field(default_factory=list)
    ownership_positions: list[InsiderOwnershipPosition] = field(default_factory=list)
    transactions: list[InsiderTransaction] = field(default_factory=list)
    parser_version: str = PARSER_VERSION
    warnings: list[str] = field(default_factory=list)


@dataclass
class SECIngestionResult:
    ticker: str
    issuer_cik: str | None
    filings_discovered: int = 0
    filings_processed: int = 0
    filings_reused: int = 0
    people_added: int = 0
    people_updated: int = 0
    transactions_added: int = 0
    transactions_updated: int = 0
    transactions_unchanged: int = 0
    ownership_positions_added: int = 0
    ownership_positions_updated: int = 0
    ownership_positions_unchanged: int = 0
    filings: list[SECFiling] = field(default_factory=list)
    parsed_filings: list[SECParsedFiling] = field(default_factory=list)
    classification_failures: list[str] = field(default_factory=list)
    provenance_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


__all__ = [
    "ALIGNMENT_VERSION", "CLASSIFICATION_VERSION", "PARSER_VERSION", "BASELINE_PARSER_VERSION", "SECProvenance",
    "SECFiling", "SECParsedFiling", "SECIngestionResult", "IssuerIdentity", "InsiderPerson",
    "InsiderOwnershipPosition", "OwnershipBaseline", "OwnershipReconciliation", "InsiderTransaction", "InsiderCluster",
    "InsiderAlignmentSnapshot",
]
