from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BusinessOutcome:
    observation_id: str
    target: str
    publication_date: str | None
    period_end: str | None
    value: float | None
    status: str = "UNAVAILABLE"
    exclusion_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class FundamentalsOutcomeProvider:
    """Interface for as-reported fundamentals; no current reconstructed fact is reused."""
    status = "FUNDAMENTALS_OUTCOME_PROVIDER_NOT_CONFIGURED"

    def outcomes(self, *args, **kwargs) -> list[BusinessOutcome]:
        return []
