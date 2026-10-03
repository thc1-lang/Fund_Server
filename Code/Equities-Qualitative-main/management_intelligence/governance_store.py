"""Deterministic JSONL store for Board & Governance Intelligence (6C)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, TypeVar

from .board_governance_models import (
    BoardCommittee, BoardExpertise, BoardRefreshment, BoardTenure,
    GovernanceBoardMembership, GovernanceCoverage, GovernanceEvent,
    GovernanceRun, GovernanceSnapshot, OverboardingRecord,
    RelatedPartyRelationship, ShareholderRights, VotingClass,
    VotingControlSnapshot,
)


T = TypeVar("T")


class GovernanceStore:
    """Stable-key JSONL persistence with idempotent upserts."""

    SPECS: dict[str, tuple[str, type[Any]]] = {
        "governance_board_memberships": ("board_membership_id", GovernanceBoardMembership),
        "governance_committees": ("committee_id", BoardCommittee),
        "governance_tenure": ("tenure_id", BoardTenure),
        "governance_refreshment": ("refreshment_id", BoardRefreshment),
        "governance_expertise": ("expertise_id", BoardExpertise),
        "governance_overboarding": ("overboarding_id", OverboardingRecord),
        "governance_related_parties": ("relationship_id", RelatedPartyRelationship),
        "governance_voting_classes": ("voting_class_id", VotingClass),
        "governance_voting_control": ("voting_control_id", VotingControlSnapshot),
        "governance_shareholder_rights": ("rights_id", ShareholderRights),
        "governance_events": ("event_id", GovernanceEvent),
        "governance_coverage": ("coverage_id", GovernanceCoverage),
        "governance_snapshots": ("snapshot_id", GovernanceSnapshot),
        "governance_runs": ("run_id", GovernanceRun),
    }

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

    def _write_all(self, name: str, values: Iterable[Any], key: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f"governance-{name}-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(temporary)
        try:
            ordered = sorted(values, key=lambda item: str(getattr(item, key)))
            path.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in ordered), encoding="utf-8")
            os.replace(path, self._path(name))
        finally:
            if path.exists():
                path.unlink()

    def upsert(self, name: str, values: Iterable[Any]) -> dict[str, int]:
        if name not in self.SPECS:
            raise KeyError(name)
        key, cls = self.SPECS[name]
        incoming = list(values)
        existing = {str(getattr(item, key)): item for item in self._read(name, cls)}
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        for item in incoming:
            item_key = str(getattr(item, key))
            old = existing.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old is not None else "added"] += 1
                existing[item_key] = item
        self._write_all(name, existing.values(), key)
        return counts

    def replace_ticker(self, name: str, ticker: str, values: Iterable[Any]) -> dict[str, int]:
        """Replace one issuer's materialized view while retaining other issuers."""
        if name not in self.SPECS:
            raise KeyError(name)
        key, cls = self.SPECS[name]
        incoming = list(values)
        previous = self._read(name, cls)
        existing = {str(getattr(item, key)): item for item in previous if getattr(item, "ticker", "").upper() != ticker.upper()}
        old_for_ticker = {str(getattr(item, key)): item for item in previous if getattr(item, "ticker", "").upper() == ticker.upper()}
        incoming_keys = {str(getattr(item, key)) for item in incoming}
        counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": sum(1 for old_key in old_for_ticker if old_key not in incoming_keys)}
        for item in incoming:
            item_key = str(getattr(item, key))
            old = old_for_ticker.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old is not None else "added"] += 1
            existing[item_key] = item
        self._write_all(name, existing.values(), key)
        return counts

    def list(self, name: str, ticker: str | None = None) -> list[Any]:
        if name not in self.SPECS:
            raise KeyError(name)
        _, cls = self.SPECS[name]
        values = self._read(name, cls)
        return [item for item in values if ticker is None or getattr(item, "ticker", "").upper() == ticker.upper()]

    def counts(self, ticker: str | None = None) -> dict[str, int]:
        return {name: len(self.list(name, ticker)) for name in self.SPECS}

    def upsert_all(self, records: dict[str, Iterable[Any]]) -> dict[str, dict[str, int]]:
        return {name: self.upsert(name, values) for name, values in records.items() if name in self.SPECS}

    def record_run(self, run: GovernanceRun) -> dict[str, int]:
        return self.upsert("governance_runs", [run])


__all__ = ["GovernanceStore"]
