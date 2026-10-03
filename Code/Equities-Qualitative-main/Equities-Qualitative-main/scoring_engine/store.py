from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


class ScoreStore:
    """Idempotent JSONL materialized views for score audits."""
    def __init__(self, root: str | Path = "artifacts/scoring_engine"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.jsonl"

    def read(self, name: str) -> list[dict[str, Any]]:
        path = self._path(name)
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict): result.append(row)
            except json.JSONDecodeError:
                continue
        return result

    def upsert(self, name: str, values: Iterable[Any], key: str) -> dict[str, int]:
        existing = {str(row.get(key)): row for row in self.read(name)}
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        for value in values:
            row = value.to_dict() if hasattr(value, "to_dict") else value
            row_key = str(row.get(key, ""))
            if not row_key: continue
            comparable = dict(row); comparable.pop("created_at", None)
            old_comparable = dict(existing[row_key]) if row_key in existing else None
            if old_comparable is not None: old_comparable.pop("created_at", None)
            if row_key in existing and old_comparable == comparable: counts["unchanged"] += 1
            else:
                counts["updated" if row_key in existing else "added"] += 1
                existing[row_key] = row
        fd, tmp = tempfile.mkstemp(prefix="score-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(tmp)
        try:
            path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in sorted(existing.values(), key=lambda row: str(row.get(key, "")))), encoding="utf-8")
            os.replace(path, self._path(name))
        finally:
            if path.exists(): path.unlink()
        return counts

    def write_run(self, factors, pillars, snapshots, run):
        return {
            "factor_scores": self.upsert("factor_scores", factors, "factor_score_id"),
            "pillar_scores": self.upsert("pillar_scores", pillars, "pillar_score_id"),
            "company_scores": self.upsert("company_scores", snapshots, "snapshot_id"),
            "score_runs": self.upsert("score_runs", [run], "run_id"),
        }
