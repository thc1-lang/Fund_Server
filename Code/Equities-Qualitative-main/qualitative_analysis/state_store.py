"""JSONL persistence and query indexes for synthesized qualitative states."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .state_models import ALL_STATE_VALUES, RULES_VERSION, STATE_VERSION, TREND_VALUES, QualitativeState


class StateStore:
    def __init__(self, root: str | Path = "artifacts/qualitative_analysis"):
        root_path = Path(root)
        self.root = root_path.parent if root_path.suffix.lower() == ".jsonl" else root_path
        self.states_path = root_path if root_path.suffix.lower() == ".jsonl" else self.root / "qualitative_states.jsonl"
        self.index_path = self.root / "state_index.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, QualitativeState]:
        values: dict[str, QualitativeState] = {}
        if not self.states_path.exists():
            return values
        with self.states_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    state = QualitativeState.from_dict(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    continue
                if state.state_id:
                    values[state.state_id] = state
        return values

    def list_all(self) -> list[QualitativeState]:
        return sorted(self._read().values(), key=lambda item: (item.ticker, item.period_label or "", item.dimension, item.topic or "", item.state_id))

    def get_by_id(self, state_id: str) -> QualitativeState | None:
        return self._read().get(state_id)

    def get_by_ticker(self, ticker: str) -> list[QualitativeState]:
        return [state for state in self.list_all() if state.ticker.upper() == ticker.upper()]

    def get_by_dimension(self, dimension: str) -> list[QualitativeState]:
        return [state for state in self.list_all() if state.dimension == dimension]

    def get_by_topic(self, topic: str) -> list[QualitativeState]:
        return [state for state in self.list_all() if state.topic == topic]

    def get_by_period(self, period_label: str) -> list[QualitativeState]:
        return [state for state in self.list_all() if state.period_label == period_label]

    def get_by_current_state(self, current_state: str) -> list[QualitativeState]:
        return [state for state in self.list_all() if state.current_state == current_state]

    def get_by_trend(self, trend: str) -> list[QualitativeState]:
        return [state for state in self.list_all() if state.trend == trend]

    def query(
        self,
        *,
        ticker: str | None = None,
        dimension: str | None = None,
        topic: str | None = None,
        period_label: str | None = None,
        current_state: str | None = None,
        trend: str | None = None,
    ) -> list[QualitativeState]:
        values = self.list_all()
        return [
            state for state in values
            if (ticker is None or state.ticker.upper() == ticker.upper())
            and (dimension is None or state.dimension == dimension)
            and (topic is None or state.topic == topic)
            and (period_label is None or state.period_label == period_label)
            and (current_state is None or state.current_state == current_state)
            and (trend is None or state.trend == trend)
        ]

    @staticmethod
    def _shape_errors(state: QualitativeState) -> list[str]:
        errors: list[str] = []
        if not state.state_id:
            errors.append("state_id is required")
        if not state.ticker or not state.dimension:
            errors.append("ticker and dimension are required")
        if state.current_state not in ALL_STATE_VALUES:
            errors.append(f"unsupported current_state: {state.current_state}")
        if state.trend not in TREND_VALUES:
            errors.append(f"unsupported trend: {state.trend}")
        if not (0.0 <= float(state.confidence) <= 1.0):
            errors.append("confidence must be between 0 and 1")
        if not state.supporting_claim_ids and state.current_state != "unknown":
            errors.append("non-unknown state requires claim provenance")
        if state.state_version != STATE_VERSION:
            errors.append(f"unsupported state_version: {state.state_version}")
        if state.rules_version != RULES_VERSION:
            errors.append(f"unsupported rules_version: {state.rules_version}")
        return errors

    @staticmethod
    def _equivalent(left: QualitativeState, right: QualitativeState) -> bool:
        a, b = left.to_dict(), right.to_dict()
        a.pop("created_at", None)
        b.pop("created_at", None)
        return a == b

    def _write(self, values: Iterable[QualitativeState]) -> None:
        ordered = sorted(values, key=lambda item: item.state_id)
        fd, temp_name = tempfile.mkstemp(prefix="qualitative-states-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for state in ordered:
                    handle.write(json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(temp_path, self.states_path)
            self._write_index(ordered)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _group(values: list[QualitativeState], key) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for value in values:
            grouped.setdefault(str(key(value)), []).append(value.state_id)
        return grouped

    def _write_index(self, values: list[QualitativeState]) -> None:
        index = {
            "version": 1,
            "state_version": STATE_VERSION,
            "states": len(values),
            "by_id": {state.state_id: position for position, state in enumerate(values)},
            "by_ticker": self._group(values, lambda state: state.ticker.upper()),
            "by_dimension": self._group(values, lambda state: state.dimension),
            "by_topic": self._group(values, lambda state: state.topic or "dimension"),
            "by_period": self._group(values, lambda state: state.period_label or "unknown"),
            "by_current_state": self._group(values, lambda state: state.current_state),
            "by_trend": self._group(values, lambda state: state.trend),
        }
        fd, temp_name = tempfile.mkstemp(prefix="state-index-", suffix=".json", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temp_path, self.index_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def upsert_many(self, states: Iterable[QualitativeState]) -> dict[str, int]:
        current = self._read()
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for state in states:
            errors = self._shape_errors(state)
            if errors:
                raise ValueError("; ".join(errors))
            previous = current.get(state.state_id)
            if previous is None:
                counts["added"] += 1
                changed = True
            elif self._equivalent(previous, state):
                counts["unchanged"] += 1
                continue
            else:
                counts["updated"] += 1
                changed = True
            current[state.state_id] = state
        if changed or not self.states_path.exists():
            self._write(current.values())
        return counts

    def remove_scope(self, ticker: str, period_label: str, dimension: str | None = None) -> int:
        current = self._read()
        remove = [
            state_id for state_id, state in current.items()
            if state.ticker.upper() == ticker.upper()
            and state.period_label == period_label
            and (dimension is None or state.dimension == dimension)
        ]
        for state_id in remove:
            current.pop(state_id, None)
        if remove:
            self._write(current.values())
        return len(remove)

    def invalidate_scope(self, ticker: str, period_label: str, state_version: str, dimension: str | None = None) -> int:
        current = self._read()
        remove = [
            state_id for state_id, state in current.items()
            if state.ticker.upper() == ticker.upper()
            and state.period_label == period_label
            and state.state_version != state_version
            and (dimension is None or state.dimension == dimension)
        ]
        for state_id in remove:
            current.pop(state_id, None)
        if remove:
            self._write(current.values())
        return len(remove)

    def remove_invalid_periods(self, ticker: str, valid_periods: set[str]) -> int:
        """Remove states whose period disappeared after document repair."""
        current = self._read()
        remove = [
            state_id for state_id, state in current.items()
            if state.ticker.upper() == ticker.upper() and (state.period_label or "UNKNOWN") not in valid_periods
        ]
        for state_id in remove:
            current.pop(state_id, None)
        if remove:
            self._write(current.values())
        return len(remove)


__all__ = ["StateStore"]
