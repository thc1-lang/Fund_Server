from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import FactorScore


@dataclass
class CoverageResult:
    evidence_coverage: float
    scoring_coverage: float
    eligible_weight: float
    configured_weight: float

    @property
    def weighted_coverage(self) -> float:
        """Backward-compatible alias for evidence coverage."""
        return self.evidence_coverage


def weighted_coverage(factors: Iterable[FactorScore], configured_weight: float | None = None) -> CoverageResult:
    values = list(factors)
    configured = float(configured_weight if configured_weight is not None else sum(item.weight for item in values))
    eligible = sum(item.weight for item in values if item.status == "SCORED" and item.scoring_coverage > 0)
    evidence = sum(item.weight * max(0.0, min(1.0, item.evidence_coverage)) for item in values) / configured if configured else 0.0
    scoring = sum(item.weight * max(0.0, min(1.0, item.scoring_coverage)) for item in values) / configured if configured else 0.0
    return CoverageResult(evidence, scoring, eligible, configured)
