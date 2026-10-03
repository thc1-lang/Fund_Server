from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ValidationConfig:
    dataset_version: str = "empirical-validation-dataset-v1"
    universe_version: str = "pilot-universe-v1"
    outcome_version: str = "outcomes-v1"
    point_in_time_version: str = "point-in-time-v1"
    profile: str = "core_v1"
    profile_version: str = "core-v1.2"
    as_of_date: str = "2026-09-24"
    observation_schedule: str = "quarterly"
    execution_lag: str = "next_trading_day_open"
    horizons_days: list[int] = field(default_factory=lambda: [21, 63, 126, 252])
    tickers: list[str] = field(default_factory=lambda: ["ZM", "PLTR", "EXEL", "INCY"])
    benchmark: str = "SPY"
    minimum_observations: int = 30
    minimum_unique_companies: int = 10
    minimum_unique_dates: int = 4
    minimum_quantile_size: int = 10
    holdout_start: str | None = None
    holdout_end: str | None = None
    bootstrap_iterations: int = 1000
    random_seed: int = 20260924
    artifacts_root: str = "artifacts"
    output_root: str = "artifacts/empirical_validation"
    market_data_provider: str | None = None
    survivorship_status: str = "SURVIVORSHIP_COVERAGE_INCOMPLETE"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ValidationConfig":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in allowed})


def load_config(path: str | Path) -> ValidationConfig:
    p = Path(path)
    if not p.exists():
        return ValidationConfig()
    return ValidationConfig.from_dict(json.loads(p.read_text(encoding="utf-8")))
