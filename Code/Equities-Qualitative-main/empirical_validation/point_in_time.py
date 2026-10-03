from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def available_date(row: dict[str, Any]) -> date | None:
    """Return the public-availability date, never a fiscal period end."""
    for key in (
        "filing_date", "publication_date", "source_date", "retrieved_at",
        "event_date", "issued_date", "commitment_date", "created_at",
    ):
        d = parse_date(row.get(key))
        if d:
            return d
    return None


@dataclass(frozen=True)
class PointInTimePolicy:
    version: str = "point-in-time-v1"
    unknown_timestamp_status: str = "UNKNOWN"

    def is_available(self, row: dict[str, Any], cutoff: str) -> str:
        available = available_date(row)
        cutoff_date = parse_date(cutoff)
        if not cutoff_date or not available:
            return self.unknown_timestamp_status
        return "PASS" if available <= cutoff_date else "FAIL"

    def filter_rows(self, rows: Iterable[dict[str, Any]], cutoff: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        passed, future, unknown = [], [], []
        for row in rows:
            status = self.is_available(row, cutoff)
            (passed if status == "PASS" else future if status == "FAIL" else unknown).append(row)
        return passed, future, unknown


def conservative_cutoff(observation_date: str, execution_lag: str) -> str:
    """The observation date is the last date on which inputs may be known.

    A provider-specific trading calendar is required to turn this into an
    executable timestamp. Until one is configured, the date is retained and
    outcomes remain unavailable rather than being guessed.
    """
    return observation_date
