"""JSONL persistence for extracted claims and a derived query index."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable

from .extraction_models import QualitativeClaim


def _replace_with_retry(source: Path, destination: Path, *, attempts: int = 5) -> None:
    """Atomically replace a JSONL artifact, tolerating brief Windows sharing locks.

    Antivirus, indexers, and file previews can momentarily hold the existing
    artifact open.  The temporary file is already fully written, so a short
    bounded retry preserves atomicity without turning a transient lock into a
    partial extraction run.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (2 ** attempt))


class ExtractionStore:
    def __init__(self, root: str | Path = "artifacts/qualitative_analysis"):
        root_path = Path(root)
        if root_path.suffix.lower() == ".jsonl":
            self.claims_path = root_path
            self.root = root_path.parent
        else:
            self.root = root_path
            self.claims_path = self.root / "claims.jsonl"
        self.index_path = self.root / "claim_index.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, QualitativeClaim]:
        claims: dict[str, QualitativeClaim] = {}
        if not self.claims_path.exists():
            return claims
        with self.claims_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    claim = QualitativeClaim.from_dict(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    continue
                if claim.claim_id:
                    claims[claim.claim_id] = claim
        return claims

    def list_all(self) -> list[QualitativeClaim]:
        return sorted(self._read().values(), key=lambda c: (c.ticker, c.document_id, c.claim_id))

    def get_by_id(self, claim_id: str) -> QualitativeClaim | None:
        return self._read().get(claim_id)

    def get_by_ticker(self, ticker: str) -> list[QualitativeClaim]:
        return [c for c in self.list_all() if c.ticker.upper() == ticker.upper()]

    def get_by_document_id(self, document_id: str) -> list[QualitativeClaim]:
        return [c for c in self.list_all() if c.document_id == document_id]

    get_by_document = get_by_document_id

    def get_by_dimension(self, dimension: str) -> list[QualitativeClaim]:
        return [c for c in self.list_all() if c.dimension == dimension]

    def get_by_fiscal_period(self, fiscal_year: int | str | None, fiscal_quarter: int | None = None) -> list[QualitativeClaim]:
        if isinstance(fiscal_year, str) and fiscal_quarter is None:
            return [c for c in self.list_all() if c.period_label == fiscal_year]
        return [c for c in self.list_all() if c.fiscal_year == fiscal_year and (fiscal_quarter is None or c.fiscal_quarter == fiscal_quarter)]

    def get_document_state(self, document_id: str) -> dict[str, str] | None:
        claims = self.get_by_document_id(document_id)
        if not claims:
            return None
        first = claims[0]
        return {"source_content_hash": first.source_content_hash, "extraction_version": first.extraction_version, "provider_version": first.provider_version, "rules_version": first.rules_version}

    @staticmethod
    def _shape_errors(claim: QualitativeClaim) -> list[str]:
        errors: list[str] = []
        if not claim.claim_id:
            errors.append("claim_id is required")
        if not claim.document_id:
            errors.append("document_id is required")
        if not claim.evidence_text:
            errors.append("evidence_text is required")
        if not isinstance(claim.source_location, dict) or not claim.source_location:
            errors.append("source_location is required")
        return errors

    @staticmethod
    def _equivalent(left: QualitativeClaim, right: QualitativeClaim) -> bool:
        a, b = left.to_dict(), right.to_dict()
        a.pop("created_at", None)
        b.pop("created_at", None)
        return a == b

    def _write(self, values: Iterable[QualitativeClaim]) -> None:
        ordered = sorted(values, key=lambda c: c.claim_id)
        fd, temp_name = tempfile.mkstemp(prefix="claims-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for claim in ordered:
                    handle.write(json.dumps(claim.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            _replace_with_retry(temp_path, self.claims_path)
            self._write_index(ordered)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _group(values: list[QualitativeClaim], key) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for claim in values:
            grouped.setdefault(str(key(claim)), []).append(claim.claim_id)
        return grouped

    def _write_index(self, values: list[QualitativeClaim]) -> None:
        index = {
            "version": 1,
            "claims": len(values),
            "by_id": {c.claim_id: i for i, c in enumerate(values)},
            "by_ticker": self._group(values, lambda c: c.ticker.upper()),
            "by_document": self._group(values, lambda c: c.document_id),
            "by_dimension": self._group(values, lambda c: c.dimension),
            "by_period": self._group(values, lambda c: c.period_label or "unknown"),
        }
        fd, temp_name = tempfile.mkstemp(prefix="claim-index-", suffix=".json", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
            _replace_with_retry(temp_path, self.index_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def upsert(self, claim: QualitativeClaim) -> str:
        counts = self.upsert_many([claim])
        if counts["added"]:
            return "added"
        if counts["updated"]:
            return "updated"
        return "unchanged"

    def upsert_many(self, claims: Iterable[QualitativeClaim]) -> dict[str, int]:
        current = self._read()
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for claim in claims:
            errors = self._shape_errors(claim)
            if errors:
                raise ValueError("; ".join(errors))
            previous = current.get(claim.claim_id)
            if previous is None:
                counts["added"] += 1
                changed = True
            elif self._equivalent(previous, claim):
                counts["unchanged"] += 1
                continue
            else:
                counts["updated"] += 1
                changed = True
            current[claim.claim_id] = claim
        if changed or not self.claims_path.exists():
            self._write(current.values())
        return counts

    def remove_for_document(self, document_id: str) -> int:
        current = self._read()
        remove = [claim_id for claim_id, claim in current.items() if claim.document_id == document_id]
        if remove:
            for claim_id in remove:
                current.pop(claim_id, None)
            self._write(current.values())
        return len(remove)

    def remove_ticker_except_documents(self, ticker: str, document_ids: set[str]) -> int:
        """Drop claims for an issuer's documents absent from a fresh view."""
        current = self._read()
        target = ticker.strip().upper()
        remove = [
            claim_id for claim_id, claim in current.items()
            if claim.ticker.upper() == target and claim.document_id not in document_ids
        ]
        for claim_id in remove:
            current.pop(claim_id, None)
        if remove:
            self._write(current.values())
        return len(remove)
