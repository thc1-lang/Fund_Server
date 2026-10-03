"""Stable JSONL persistence for Component 6B records."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, TypeVar

from .track_record_models import (
    CapitalAllocationEvent, FinancialFact, GuidanceCommitment, GuidanceOutcome,
    ManagementTenure, OperatingOutcome, PersonTrackRecordSnapshot,
    StrategicCommitment, StrategicOutcome, TrackRecordRun, GuidanceCoverageDiagnostic,
)

T = TypeVar("T")


class TrackRecordStore:
    def __init__(self, root: str | Path = "artifacts/management_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def _read(self, name: str, cls: type[T]) -> list[T]:
        path = self._path(name)
        if not path.exists():
            return []
        values: list[T] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                values.append(cls.from_dict(json.loads(line)))  # type: ignore[attr-defined]
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return values

    def _write(self, name: str, values: Iterable[Any], key: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f"track-record-{name}-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(temporary)
        try:
            ordered = sorted(values, key=lambda value: str(getattr(value, key)))
            path.write_text("".join(json.dumps(value.to_dict(), sort_keys=True) + "\n" for value in ordered), encoding="utf-8")
            os.replace(path, self._path(name))
        finally:
            if path.exists():
                path.unlink()

    def _upsert(self, name: str, values: Iterable[Any], key: str, cls: type[Any]) -> dict[str, int]:
        existing = {str(getattr(item, key)): item for item in self._read(name, cls)}
        counts = {"added": 0, "updated": 0, "unchanged": 0, "rejected": 0}
        for item in values:
            item_key = str(getattr(item, key, ""))
            if not item_key:
                counts["rejected"] += 1
                continue
            old = existing.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old else "added"] += 1
                existing[item_key] = item
        self._write(name, existing.values(), key)
        return counts

    def upsert_tenures(self, values): return self._upsert("tenures", values, "tenure_id", ManagementTenure)
    def upsert_financial_facts(self, values): return self._upsert("financial_facts", values, "fact_id", FinancialFact)
    def upsert_operating_outcomes(self, values): return self._upsert("operating_outcomes", values, "outcome_id", OperatingOutcome)
    def upsert_guidance_commitments(self, values): return self._upsert("guidance_commitments", values, "guidance_id", GuidanceCommitment)
    def upsert_guidance_outcomes(self, values): return self._upsert("guidance_outcomes", values, "guidance_outcome_id", GuidanceOutcome)
    def upsert_strategic_commitments(self, values): return self._upsert("strategic_commitments", values, "commitment_id", StrategicCommitment)
    def upsert_strategic_outcomes(self, values): return self._upsert("strategic_outcomes", values, "outcome_id", StrategicOutcome)
    def upsert_capital_events(self, values): return self._upsert("capital_allocation_events", values, "event_id", CapitalAllocationEvent)
    def upsert_snapshots(self, values): return self._upsert("track_record_snapshots", values, "snapshot_id", PersonTrackRecordSnapshot)
    def record_run(self, value: TrackRecordRun): return self._upsert("track_record_runs", [value], "run_id", TrackRecordRun)
    def upsert_guidance_diagnostics(self, values): return self._upsert("guidance_coverage_diagnostics", values, "ticker", GuidanceCoverageDiagnostic)

    def list_tenures(self, ticker: str | None = None): return self._filter(self._read("tenures", ManagementTenure), ticker)
    def list_financial_facts(self, ticker: str | None = None): return self._filter(self._read("financial_facts", FinancialFact), ticker)
    def list_operating_outcomes(self, ticker: str | None = None): return self._filter(self._read("operating_outcomes", OperatingOutcome), ticker)
    def list_guidance_commitments(self, ticker: str | None = None): return self._filter(self._read("guidance_commitments", GuidanceCommitment), ticker)
    def list_guidance_outcomes(self, ticker: str | None = None):
        return self._filter(self._read("guidance_outcomes", GuidanceOutcome), ticker, attr="ticker")
    def list_strategic_commitments(self, ticker: str | None = None): return self._filter(self._read("strategic_commitments", StrategicCommitment), ticker)
    def list_strategic_outcomes(self, ticker: str | None = None): return self._read("strategic_outcomes", StrategicOutcome)
    def list_capital_events(self, ticker: str | None = None): return self._filter(self._read("capital_allocation_events", CapitalAllocationEvent), ticker)
    def list_snapshots(self, ticker: str | None = None): return self._filter(self._read("track_record_snapshots", PersonTrackRecordSnapshot), ticker)
    def list_runs(self): return self._read("track_record_runs", TrackRecordRun)
    def list_guidance_diagnostics(self, ticker: str | None = None): return self._filter(self._read("guidance_coverage_diagnostics", GuidanceCoverageDiagnostic), ticker)

    @staticmethod
    def _filter(values, ticker: str | None, attr: str = "ticker"):
        if ticker is None:
            return values
        return [value for value in values if getattr(value, attr, None) and str(getattr(value, attr)).upper() == ticker.upper()]


__all__ = ["TrackRecordStore"]
