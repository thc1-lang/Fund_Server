from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


STATUSES = {
    "SCORED",
    "INSUFFICIENT_COVERAGE",
    "INSUFFICIENT_SCORING_COVERAGE",
    "INSUFFICIENT_SAMPLE",
    "UNKNOWN",
    "NOT_APPLICABLE",
    "UNSCORED",
}


@dataclass
class FactorDefinition:
    factor_key: str
    factor_name: str
    pillar: str
    description: str
    input_source: str
    input_field: str
    mapping_rule: str
    weight: float
    critical: bool = False
    min_coverage: float = 0.0
    min_sample: int = 0
    rule_version: str = ""
    dependency_group: str = ""
    semantic_class: str = "UNKNOWN"
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FactorDefinition":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProfileDefinition:
    profile: str
    profile_version: str
    scoring_version: str
    factor_rules_version: str
    coverage_version: str
    pillar_weights: dict[str, float]
    factors: list[FactorDefinition]
    minimum_overall_coverage: float
    minimum_pillar_coverage: dict[str, float]
    critical_factor_keys: list[str] = field(default_factory=list)
    dependency_groups: dict[str, list[str]] = field(default_factory=dict)
    created_date: str = ""
    calibration_status: str = ""
    description: str = ""
    # New explicit names. The legacy fields above remain accepted for callers
    # constructing profiles directly; the registry populates both from the
    # versioned configuration.
    minimum_overall_scoring_coverage: float | None = None
    minimum_pillar_scoring_coverage: dict[str, float] | None = None

    def factor(self, key: str) -> FactorDefinition | None:
        return next((item for item in self.factors if item.factor_key == key), None)

    @property
    def scoring_coverage_threshold(self) -> float:
        return float(self.minimum_overall_scoring_coverage if self.minimum_overall_scoring_coverage is not None else self.minimum_overall_coverage)

    def pillar_scoring_threshold(self, pillar: str) -> float:
        values = self.minimum_pillar_scoring_coverage if self.minimum_pillar_scoring_coverage is not None else self.minimum_pillar_coverage
        return float(values.get(pillar, 0.0))


@dataclass
class FactorScore:
    factor_score_id: str
    ticker: str
    factor_key: str
    factor_name: str
    pillar: str
    score: float | None
    status: str
    coverage: float
    confidence: float
    raw_inputs: dict[str, Any]
    normalized_inputs: dict[str, Any]
    rule_id: str
    rule_version: str
    weight: float
    weighted_contribution: float | None
    supporting_record_ids: list[str]
    supporting_evidence: list[dict[str, Any]]
    coverage_reason: str
    as_of_date: str
    created_at: str
    effective_weight: float | None = None
    dependency_group: str = ""
    explanation: dict[str, Any] = field(default_factory=dict)
    # `coverage` is retained as a backwards-compatible alias for evidence
    # coverage. New consumers should use the explicit fields below.
    evidence_coverage: float = 0.0
    scoring_coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PillarScore:
    pillar_score_id: str
    ticker: str
    pillar: str
    score: float | None
    calculated_score: float | None
    weighted_coverage: float
    confidence: float
    factor_scores: list[str]
    eligible_weight: float
    configured_weight: float
    coverage_status: str
    created_at: str
    effective_factor_weights: dict[str, float] = field(default_factory=dict)
    # `weighted_coverage` remains an evidence-coverage alias for old stores.
    evidence_coverage: float = 0.0
    scoring_coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompanyScoreSnapshot:
    snapshot_id: str
    ticker: str
    as_of_date: str
    profile: str
    overall_score: float | None
    overall_coverage: float
    overall_confidence: float
    pillar_scores: list[str]
    scored_factor_count: int
    unscored_factor_count: int
    critical_missing_factors: list[str]
    score_status: str
    scoring_version: str
    rules_version: str
    profile_version: str
    created_at: str
    coverage_version: str = ""
    top_positive_contributors: list[str] = field(default_factory=list)
    top_negative_contributors: list[str] = field(default_factory=list)
    coverage_limitations: list[str] = field(default_factory=list)
    # `overall_coverage` remains an evidence-coverage alias for old stores.
    overall_evidence_coverage: float = 0.0
    overall_scoring_coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreRun:
    run_id: str
    profile: str
    as_of_date: str
    tickers: list[str]
    scoring_version: str
    factor_rules_version: str
    profile_version: str
    coverage_version: str
    created_at: str
    status: str = "COMPLETED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
