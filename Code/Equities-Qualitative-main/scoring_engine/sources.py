from __future__ import annotations

from pathlib import Path
from typing import Any

from .data import records, read_jsonl


class EvidenceSources:
    """Read-only adapter over the frozen Component 4–6C materialized views."""

    def __init__(self, artifacts_root: str | Path = "artifacts"):
        root = Path(artifacts_root)
        self.qualitative_root = root / "qualitative_analysis"
        self.management_root = root / "management_intelligence"
        self.insider_root = root / "insider_intelligence"

    def states(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.qualitative_root, "qualitative_states", ticker)

    def guidance_outcomes(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.management_root, "guidance_outcomes", ticker)

    def guidance_commitments(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.management_root, "guidance_commitments", ticker)

    def strategic_commitments(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.management_root, "strategic_commitments", ticker)

    def strategic_outcomes(self, ticker: str) -> list[dict[str, Any]]:
        outcomes = read_jsonl(self.management_root / "strategic_outcomes.jsonl")
        commitments = {str(row.get("commitment_id")): row for row in self.strategic_commitments(ticker)}
        return [row for row in outcomes if str(row.get("commitment_id")) in commitments]

    def operating_outcomes(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.management_root, "operating_outcomes", ticker)

    def capital_events(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.management_root, "capital_allocation_events", ticker)

    def alignment(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.insider_root, "alignment_snapshots", ticker)

    def transactions(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.insider_root, "transactions", ticker)

    def clusters(self, ticker: str) -> list[dict[str, Any]]:
        return records(self.insider_root, "clusters", ticker)

    def governance(self, name: str, ticker: str) -> list[dict[str, Any]]:
        return records(self.management_root, name, ticker)

    def governance_snapshot(self, ticker: str, as_of: str | None = None) -> dict[str, Any] | None:
        rows = self.governance("governance_snapshots", ticker)
        if as_of:
            rows = [row for row in rows if not (row.get("as_of_date") or row.get("created_at")) or str(row.get("as_of_date") or row.get("created_at"))[:10] <= as_of]
        return sorted(rows, key=lambda row: (str(row.get("as_of_date", "")), str(row.get("created_at", ""))))[-1] if rows else None
