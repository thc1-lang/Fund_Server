from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def dedupe_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate materialized inputs by their stable record identifier."""
    preferred = (
        "factor_score_id", "pillar_score_id", "snapshot_id", "state_id", "change_id", "claim_id", "document_id",
        "guidance_outcome_id", "guidance_id", "commitment_id", "outcome_id", "event_id", "transaction_id",
        "cluster_id", "rights_id", "coverage_id", "committee_id", "expertise_id", "tenure_id", "board_membership_id",
        "overboarding_id", "relationship_id", "voting_control_id", "voting_class_id", "filing_id",
    )
    result: dict[str, dict[str, Any]] = {}
    without_id: list[dict[str, Any]] = []
    for row in rows:
        key = next((str(row[name]) for name in preferred if row.get(name)), None)
        if key is None:
            key = next((str(row[name]) for name in row if name.endswith("_id") and row.get(name) and name not in {"person_id", "issuer_id", "source_id"}), None)
        if not key:
            without_id.append(row)
            continue
        result[key] = row
    return list(result.values()) + without_id


def records(root: str | Path, name: str, ticker: str | None = None) -> list[dict[str, Any]]:
    values = dedupe_records(read_jsonl(Path(root) / f"{name}.jsonl"))
    if ticker is None:
        return values
    upper = ticker.upper()
    return [row for row in values if str(row.get("ticker", row.get("issuer_ticker", ""))).upper() == upper]


def first_present(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return default
