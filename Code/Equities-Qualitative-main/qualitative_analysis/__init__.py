"""Offline normalization, provenance, and deterministic qualitative extraction."""

from .models import FiscalPeriod, IngestionFailure, NormalizedDocument
from .document_store import DocumentStore
from .extraction_models import ClaimCandidate, ExtractionChunk, QualitativeClaim, register_dimension
from .extraction_store import ExtractionStore
from .qualitative_extraction import DeterministicExtractionProvider, ExtractionProvider, chunk_document, extract_claims, extract_document, stable_claim_id
from .temporal_models import TemporalChange, TemporalComparisonResult, TemporalEvidence
from .temporal_comparison import DeterministicTemporalComparisonProvider, TemporalComparisonProvider, compare_latest_vs_previous
from .temporal_store import TemporalStore
from .state_models import QualitativeState, StateSynthesisResult
from .state_synthesis import DeterministicStateSynthesisProvider, StateSynthesisProvider, stable_state_id
from .state_store import StateStore
from .state_confidence import calculate_confidence

__all__ = [
    "DocumentStore", "FiscalPeriod", "IngestionFailure", "NormalizedDocument",
    "ClaimCandidate", "ExtractionChunk", "QualitativeClaim", "ExtractionStore", "register_dimension",
    "ExtractionProvider", "DeterministicExtractionProvider", "chunk_document", "extract_document", "extract_claims", "stable_claim_id",
    "TemporalChange", "TemporalComparisonResult", "TemporalEvidence", "TemporalComparisonProvider",
    "DeterministicTemporalComparisonProvider", "compare_latest_vs_previous", "TemporalStore",
    "QualitativeState", "StateSynthesisResult", "StateSynthesisProvider",
    "DeterministicStateSynthesisProvider", "stable_state_id", "StateStore", "calculate_confidence",
]
