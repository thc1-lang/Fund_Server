"""Deterministic registry for all official source records used by 6A."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .models import OfficialSource


class ManagementSourceRegistry:
    def __init__(self, root: str | Path = "artifacts/management_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "sources.jsonl"

    def _read(self) -> dict[str, OfficialSource]:
        result: dict[str, OfficialSource] = {}
        if not self.path.exists():
            return result
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                source = OfficialSource.from_dict(json.loads(line))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            result[source.source_id] = source
        return result

    def list_all(self) -> list[OfficialSource]:
        return sorted(self._read().values(), key=lambda item: (item.source_authority, item.source_type, item.source_id))

    def get(self, source_id: str) -> OfficialSource | None:
        return self._read().get(source_id)

    def upsert_many(self, sources: Iterable[OfficialSource]) -> dict[str, int]:
        values = self._read()
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for source in sources:
            previous = values.get(source.source_id)
            if previous is not None and previous.to_dict() == source.to_dict():
                counts["unchanged"] += 1
            else:
                counts["updated" if previous else "added"] += 1
                values[source.source_id] = source
                changed = True
        if changed or not self.path.exists():
            self._write(values.values())
        return counts

    def _write(self, sources: Iterable[OfficialSource]) -> None:
        fd, temporary = tempfile.mkstemp(prefix="management-sources-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(temporary)
        try:
            path.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in sorted(sources, key=lambda item: item.source_id)), encoding="utf-8")
            os.replace(path, self.path)
        finally:
            if path.exists():
                path.unlink()


__all__ = ["ManagementSourceRegistry"]
