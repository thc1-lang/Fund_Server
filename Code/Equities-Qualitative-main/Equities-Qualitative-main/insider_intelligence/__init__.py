"""Official SEC-backed insider ownership and transaction intelligence."""

from .alignment import build_alignment_snapshot, detect_clusters, is_discretionary_open_market
from .models import (
    ALIGNMENT_VERSION,
    BASELINE_PARSER_VERSION,
    CLASSIFICATION_VERSION,
    PARSER_VERSION,
    InsiderAlignmentSnapshot,
    InsiderCluster,
    InsiderOwnershipPosition,
    InsiderPerson,
    InsiderTransaction,
    OwnershipBaseline,
    OwnershipReconciliation,
    IssuerIdentity,
    SECIngestionResult,
    SECParsedFiling,
    SECProvenance,
    SECFiling,
)
from .ownership import avoid_double_counting_positions, derive_pre_transaction_shares, reconcile_transaction
from .sec_ingestion import SECRequestError, SECResponse, SECSourceProvider, SECTransport, extract_ownership_xml, is_ownership_form, normalize_cik
from .sec_parser import parse_ownership_xml, stable_person_id
from .store import InsiderStore
from .proxy_baseline import ProxyBaselineProvider, ProxyBaselineResult, enrich_people_from_baselines, parse_proxy_html, reconcile_baseline
from .transaction_classification import classify_transaction, detect_10b5_1

__all__ = [
    "ALIGNMENT_VERSION", "CLASSIFICATION_VERSION", "PARSER_VERSION", "BASELINE_PARSER_VERSION", "SECProvenance",
    "SECFiling", "SECParsedFiling", "SECIngestionResult", "IssuerIdentity",
    "InsiderPerson", "InsiderOwnershipPosition", "OwnershipBaseline", "OwnershipReconciliation", "InsiderTransaction", "InsiderCluster",
    "InsiderAlignmentSnapshot", "SECSourceProvider", "SECTransport", "SECResponse",
    "SECRequestError", "extract_ownership_xml", "is_ownership_form", "normalize_cik", "parse_ownership_xml", "stable_person_id",
    "classify_transaction", "detect_10b5_1", "InsiderStore", "reconcile_transaction",
    "derive_pre_transaction_shares", "avoid_double_counting_positions", "detect_clusters",
    "build_alignment_snapshot", "is_discretionary_open_market",
    "ProxyBaselineProvider", "ProxyBaselineResult", "parse_proxy_html", "enrich_people_from_baselines", "reconcile_baseline",
]
