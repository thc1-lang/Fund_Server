"""JSONL persistence for temporal changes and query indexes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .temporal_models import CHANGE_TYPES, DIRECTION_VALUES, TemporalChange


class TemporalStore:
    def __init__(self, root: str | Path = "artifacts/qualitative_analysis"):
        root_path = Path(root)
        self.root = root_path.parent if root_path.suffix.lower() == ".jsonl" else root_path
        self.changes_path = root_path if root_path.suffix.lower() == ".jsonl" else self.root / "temporal_changes.jsonl"
        self.index_path = self.root / "temporal_index.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, TemporalChange]:
        result: dict[str, TemporalChange] = {}
        if not self.changes_path.exists():
            return result
        with self.changes_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    change = TemporalChange.from_dict(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    continue
                if change.change_id:
                    result[change.change_id] = change
        return result

    def list_all(self) -> list[TemporalChange]:
        return sorted(self._read().values(), key=lambda item: (item.ticker, item.from_period, item.to_period, item.change_id))

    def get_by_id(self, change_id: str) -> TemporalChange | None:
        return self._read().get(change_id)

    def get_by_ticker(self, ticker: str) -> list[TemporalChange]:
        return [item for item in self.list_all() if item.ticker.upper() == ticker.upper()]

    def get_by_dimension(self, dimension: str) -> list[TemporalChange]:
        return [item for item in self.list_all() if item.dimension == dimension]

    def get_by_topic(self, topic: str) -> list[TemporalChange]:
        return [item for item in self.list_all() if item.topic == topic or item.normalized_subject == topic]

    def get_by_periods(self, from_period: str, to_period: str) -> list[TemporalChange]:
        return [item for item in self.list_all() if item.from_period == from_period and item.to_period == to_period]

    @staticmethod
    def _equivalent(left: TemporalChange, right: TemporalChange) -> bool:
        a, b = left.to_dict(), right.to_dict()
        a.pop("created_at", None)
        b.pop("created_at", None)
        return a == b

    @staticmethod
    def _shape_errors(change: TemporalChange) -> list[str]:
        errors: list[str] = []
        if not change.change_id:
            errors.append("change_id is required")
        if not change.ticker or not change.from_period or not change.to_period:
            errors.append("ticker and compared periods are required")
        if change.change_type not in CHANGE_TYPES:
            errors.append(f"unsupported change_type: {change.change_type}")
        if change.direction not in DIRECTION_VALUES:
            errors.append(f"unsupported direction: {change.direction}")
        if not change.from_claim_ids and not change.to_claim_ids:
            errors.append("claim provenance is required")
        if not (0.0 <= float(change.confidence) <= 1.0):
            errors.append("confidence must be between 0 and 1")
        return errors

    def _write(self, values: Iterable[TemporalChange]) -> None:
        ordered = sorted(values, key=lambda item: item.change_id)
        fd, temp_name = tempfile.mkstemp(prefix="temporal-changes-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for change in ordered:
                    handle.write(json.dumps(change.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(temp_path, self.changes_path)
            self._write_index(ordered)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _group(values: list[TemporalChange], key) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for value in values:
            grouped.setdefault(str(key(value)), []).append(value.change_id)
        return grouped

    def _write_index(self, values: list[TemporalChange]) -> None:
        index = {
            "version": 1,
            "changes": len(values),
            "by_id": {item.change_id: index for index, item in enumerate(values)},
            "by_ticker": self._group(values, lambda item: item.ticker.upper()),
            "by_dimension": self._group(values, lambda item: item.dimension),
            "by_topic": self._group(values, lambda item: item.topic or item.normalized_subject or "unknown"),
            "by_from_period": self._group(values, lambda item: item.from_period),
            "by_to_period": self._group(values, lambda item: item.to_period),
            "by_change_type": self._group(values, lambda item: item.change_type),
        }
        fd, temp_name = tempfile.mkstemp(prefix="temporal-index-", suffix=".json", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temp_path, self.index_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def upsert_many(self, changes: Iterable[TemporalChange]) -> dict[str, int]:
        current = self._read()
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for change in changes:
            errors = self._shape_errors(change)
            if errors:
                raise ValueError("; ".join(errors))
            previous = current.get(change.change_id)
            if previous is None:
                counts["added"] += 1
                changed = True
            elif self._equivalent(previous, change):
                counts["unchanged"] += 1
                continue
            else:
                counts["updated"] += 1
                changed = True
            current[change.change_id] = change
        if changed or not self.changes_path.exists():
            self._write(current.values())
        return counts

    def remove_scope(self, ticker: str, from_period: str, to_period: str, dimension: str | None = None) -> int:
        current = self._read()
        remove = [
            change_id for change_id, change in current.items()
            if change.ticker.upper() == ticker.upper() and change.from_period == from_period and change.to_period == to_period and (dimension is None or change.dimension == dimension)
        ]
        for change_id in remove:
            current.pop(change_id, None)
        if remove:
            self._write(current.values())
        return len(remove)

    def invalidate_scope(self, ticker: str, from_period: str, to_period: str, comparison_version: str, dimension: str | None = None) -> int:
        """Remove only stale comparison versions before recomputation."""
        current = self._read()
        remove = [
            change_id for change_id, change in current.items()
            if change.ticker.upper() == ticker.upper()
            and change.from_period == from_period
            and change.to_period == to_period
            and change.comparison_version != comparison_version
            and (dimension is None or change.dimension == dimension)
        ]
        for change_id in remove:
            current.pop(change_id, None)
        if remove:
            self._write(current.values())
        return len(remove)

    def remove_invalid_periods(self, ticker: str, valid_periods: set[str]) -> int:
        """Remove rows for periods no longer present after re-normalization."""
        current = self._read()
        remove = [
            change_id for change_id, change in current.items()
            if change.ticker.upper() == ticker.upper()
            and (change.from_period not in valid_periods or change.to_period not in valid_periods)
        ]
        for change_id in remove:
            current.pop(change_id, None)
        if remove:
            self._write(current.values())
        return len(remove)
