from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class HistoricalObservation:
    observation_id: str
    ticker: str
    company_id: str
    as_of_date: str
    information_cutoff: str
    sector: str | None
    industry: str | None
    market_cap_bucket: str | None
    scoring_profile: str
    scoring_version: str
    factor_values: dict[str, float | None]
    factor_statuses: dict[str, str]
    pillar_scores: dict[str, float | None]
    pillar_evidence_coverage: dict[str, float | None]
    pillar_scoring_coverage: dict[str, float | None]
    pillar_confidence: dict[str, float | None]
    overall_score: float | None
    overall_evidence_coverage: float | None
    overall_scoring_coverage: float | None
    overall_confidence: float | None
    critical_missing_factors: list[str]
    source_versions: dict[str, str]
    input_fingerprint: str
    point_in_time_valid: bool
    created_at: str
    exclusion_reason: str | None = None
    source_availability_unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LeakageFinding:
    observation_id: str
    status: str
    rule: str
    detail: str
    timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LeakageAudit:
    status: str
    findings: list[LeakageFinding] = field(default_factory=list)
    checked_observations: int = 0
    failed_observations: int = 0
    unknown_observations: int = 0
    synthetic_detection_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "findings": [x.to_dict() for x in self.findings],
            "checked_observations": self.checked_observations,
            "failed_observations": self.failed_observations,
            "unknown_observations": self.unknown_observations,
            "synthetic_detection_passed": self.synthetic_detection_passed,
        }


@dataclass
class ForwardOutcome:
    observation_id: str
    ticker: str
    horizon_days: int
    execution_lag: str
    execution_date: str | None
    end_date: str | None
    raw_adjusted_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    maximum_drawdown: float | None = None
    realized_volatility: float | None = None
    worst_forward_return: float | None = None
    status: str = "UNAVAILABLE"
    overlap_flag: bool = False
    exclusion_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataAudit:
    dataset_version: str
    universe_version: str
    outcome_version: str
    point_in_time_version: str
    companies_discovered: int
    possible_observations: int
    valid_observations: int
    excluded_observations: int
    exclusion_reasons: dict[str, int]
    earliest_date: str | None
    latest_date: str | None
    unique_companies: int
    unique_dates: int
    qualitative_coverage: float | None
    management_coverage: float | None
    insider_coverage: float | None
    governance_coverage: float | None
    overall_score_availability: float | None
    market_data_status: str
    outcomes_available: int
    leakage_status: str
    survivorship_status: str
    corporate_action_status: str
    adequate_for_exploratory_diagnostics: bool
    adequate_for_calibration: bool
    adequate_for_out_of_sample: bool
    input_fingerprint: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    subject: str
    target: str
    horizon_days: int | None
    sample_count: int
    train_metrics: dict[str, Any] = field(default_factory=dict)
    validation_metrics: dict[str, Any] = field(default_factory=dict)
    test_metrics: dict[str, Any] = field(default_factory=dict)
    subgroup_metrics: dict[str, Any] = field(default_factory=dict)
    stability_metrics: dict[str, Any] = field(default_factory=dict)
    bootstrap_intervals: dict[str, Any] = field(default_factory=dict)
    leakage_status: str = "UNKNOWN"
    survivorship_status: str = "SURVIVORSHIP_COVERAGE_INCOMPLETE"
    calibration_status: str = "INSUFFICIENT_DATA"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalibrationCandidate:
    candidate_id: str
    parent_profile: str
    base_profile: str
    target: str
    horizon_days: int | None
    training_period: dict[str, str | None]
    validation_period: dict[str, str | None]
    factor_set: list[str]
    weights: dict[str, float]
    constraints: dict[str, Any]
    training_metrics: dict[str, Any]
    validation_metrics: dict[str, Any]
    coverage_requirements: dict[str, Any]
    calibration_method: str
    calibration_status: str = "CANDIDATE"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
