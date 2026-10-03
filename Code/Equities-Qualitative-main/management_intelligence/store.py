"""JSONL persistence for factual management intelligence records."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, TypeVar

from .models import BoardMembership, CareerEntry, EducationEntry, InsiderIdentityLink, ManagementPerson, ManagementRun, PersonCompanyRelationship, RoleAssertion, RoleChangeEvent
from .source_registry import ManagementSourceRegistry


T = TypeVar("T")


class ManagementStore:
    def __init__(self, root: str | Path = "artifacts/management_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources = ManagementSourceRegistry(self.root)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def _read(self, name: str, cls: type[T]) -> list[T]:
        path = self._path(name)
        if not path.exists():
            return []
        result: list[T] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                result.append(cls.from_dict(json.loads(line)))  # type: ignore[attr-defined]
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return result

    def _upsert_typed(self, name: str, values: Iterable[Any], key: str, cls: type[Any]) -> dict[str, int]:
        items = list(values)
        previous = self._read(name, cls)
        existing = {str(getattr(item, key)): item for item in previous}
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        for item in items:
            item_key = str(getattr(item, key))
            old = existing.get(item_key)
            if old is not None and old.to_dict() == item.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if old else "added"] += 1
                existing[item_key] = item
        self._write_all(name, existing.values(), key)
        return counts

    def _write_all(self, name: str, values: Iterable[Any], key: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f"management-{name}-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(temporary)
        try:
            ordered = sorted(values, key=lambda item: str(getattr(item, key)))
            path.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in ordered), encoding="utf-8")
            os.replace(path, self._path(name))
        finally:
            if path.exists():
                path.unlink()

    def upsert_people(self, values: Iterable[ManagementPerson]) -> dict[str, int]:
        return self._upsert_typed("people", values, "person_id", ManagementPerson)

    def upsert_roles(self, values: Iterable[RoleAssertion]) -> dict[str, int]:
        return self._upsert_typed("roles", values, "assertion_id", RoleAssertion)

    def upsert_career(self, values: Iterable[CareerEntry]) -> dict[str, int]:
        return self._upsert_typed("career_history", values, "career_id", CareerEntry)

    def upsert_boards(self, values: Iterable[BoardMembership]) -> dict[str, int]:
        return self._upsert_typed("board_history", values, "board_id", BoardMembership)

    def upsert_relationships(self, values: Iterable[PersonCompanyRelationship]) -> dict[str, int]:
        return self._upsert_typed("relationships", values, "person_company_id", PersonCompanyRelationship)

    def upsert_education(self, values: Iterable[EducationEntry]) -> dict[str, int]:
        return self._upsert_typed("education", values, "education_id", EducationEntry)

    def upsert_insider_links(self, values: Iterable[InsiderIdentityLink]) -> dict[str, int]:
        return self._upsert_typed("insider_links", values, "link_id", InsiderIdentityLink)

    def upsert_role_changes(self, values: Iterable[RoleChangeEvent]) -> dict[str, int]:
        return self._upsert_typed("role_changes", values, "event_id", RoleChangeEvent)

    def record_run(self, run: ManagementRun) -> None:
        self._upsert_typed("runs", [run], "run_id", ManagementRun)

    def list_people(self, ticker: str | None = None) -> list[ManagementPerson]:
        values = self._read("people", ManagementPerson)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_roles(self, ticker: str | None = None) -> list[RoleAssertion]:
        values = self._read("roles", RoleAssertion)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_career(self, ticker: str | None = None) -> list[CareerEntry]:
        values = self._read("career_history", CareerEntry)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_boards(self, ticker: str | None = None) -> list[BoardMembership]:
        values = self._read("board_history", BoardMembership)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_relationships(self, ticker: str | None = None) -> list[PersonCompanyRelationship]:
        values = self._read("relationships", PersonCompanyRelationship)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_education(self, ticker: str | None = None) -> list[EducationEntry]:
        values = self._read("education", EducationEntry)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_insider_links(self, ticker: str | None = None) -> list[InsiderIdentityLink]:
        values = self._read("insider_links", InsiderIdentityLink)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_role_changes(self, ticker: str | None = None) -> list[RoleChangeEvent]:
        values = self._read("role_changes", RoleChangeEvent)
        return [item for item in values if ticker is None or item.ticker.upper() == ticker.upper()]

    def list_runs(self) -> list[ManagementRun]:
        return self._read("runs", ManagementRun)


__all__ = ["ManagementStore"]
