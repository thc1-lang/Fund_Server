from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import EvidenceRef


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def record_id(row: dict[str, Any], fallback: str) -> str:
    for key in ("state_id", "change_id", "claim_id", "document_id", "guidance_id", "guidance_outcome_id",
                "commitment_id", "event_id", "outcome_id", "snapshot_id", "ownership_id", "transaction_id",
                "filing_id", "person_id", "assertion_id", "coverage_id", "rights_id", "voting_control_id",
                "committee_id", "expertise_id", "record_id", "factor_score_id", "pillar_score_id"):
        if row.get(key):
            return _s(row[key])
    return fallback


@dataclass
class SourceCatalog:
    records: dict[str, EvidenceRef]
    _keys: dict[str, str]
    _aliases: dict[str, str]

    @classmethod
    def create(cls) -> "SourceCatalog":
        return cls({}, {}, {})

    def add(self, row: dict[str, Any], rec_id: str, rec_type: str, snippet: str = "") -> EvidenceRef:
        urls = row.get("source_urls") or row.get("source_url") or row.get("url") or []
        if isinstance(urls, str): urls = [urls]
        paths = row.get("source_local_paths") or row.get("local_source_path") or row.get("local_path") or []
        if isinstance(paths, str): paths = [paths]
        urls = [u for u in urls if u]
        paths = [p for p in paths if p]
        title = _s(row.get("title") or row.get("document_title") or row.get("form_type") or row.get("source_form") or rec_type)
        date = _s(row.get("publication_date") or row.get("event_date") or row.get("filing_date") or row.get("source_date") or row.get("as_of_date") or row.get("issued_date"))
        key = (urls[0] if urls else (paths[0] if paths else f"record:{rec_id}"))
        existing_id = self._keys.get(key)
        if existing_id and existing_id in self.records:
            old = self.records[existing_id]
            if not old.snippet and snippet: old.snippet = snippet
            self._aliases[rec_id] = existing_id
            return old
        ref = EvidenceRef(None, rec_id, rec_type, title, date, urls[0] if urls else "", paths[0] if paths else "", snippet[:500])
        self.records[rec_id] = ref
        self._keys[key] = rec_id
        self._aliases[rec_id] = rec_id
        return ref

    def finalize(self) -> list[EvidenceRef]:
        refs = sorted(self.records.values(), key=lambda x: (x.date or "", x.title.lower(), x.url or x.local_path, x.record_id))
        for i, ref in enumerate(refs, 1): ref.number = i
        return refs

    def number_for(self, rec_id: str) -> int | None:
        canonical = self._aliases.get(rec_id, rec_id)
        ref = self.records.get(canonical)
        return ref.number if ref else None


class ArtifactReader:
    """Read-only JSONL reader. It never writes to an upstream artifact root."""
    def __init__(self, artifacts_root: str | Path = "artifacts"):
        self.root = Path(artifacts_root)
        self._cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def rows(self, subdir: str, filename: str) -> list[dict[str, Any]]:
        key = (subdir, filename)
        if key in self._cache: return self._cache[key]
        candidates = [self.root / subdir / filename]
        if subdir == "management_intelligence":
            candidates += [self.root / "management_intelligence_final8" / filename]
        for path in candidates:
            if path.exists():
                out=[]
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try: out.append(json.loads(line))
                        except json.JSONDecodeError: continue
                self._cache[key] = out; return out
        self._cache[key] = []; return []

    def all_rows(self, subdir: str) -> dict[str, list[dict[str, Any]]]:
        out={}
        for p in (self.root/subdir).glob("*.jsonl") if (self.root/subdir).exists() else []:
            out[p.name] = self.rows(subdir,p.name)
        return out
