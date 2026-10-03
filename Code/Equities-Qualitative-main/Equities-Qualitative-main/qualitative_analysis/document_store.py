"""Transparent JSONL document store with a small derived index."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .models import NormalizedDocument
from .normalization import canonical_url


class DocumentStore:
    def __init__(self, root: str | Path = "artifacts/qualitative_analysis"):
        self.root = Path(root)
        if self.root.suffix.lower() == ".jsonl":
            self.documents_path = self.root
            self.root = self.root.parent
        else:
            self.documents_path = self.root / "documents.jsonl"
        self.index_path = self.root / "document_index.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, NormalizedDocument]:
        result: dict[str, NormalizedDocument] = {}
        if not self.documents_path.exists():
            return result
        with self.documents_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    document = NormalizedDocument.from_dict(item)
                    result[document.document_id] = document
                except (ValueError, TypeError, KeyError):
                    continue
        return result

    def list_all(self) -> list[NormalizedDocument]:
        return sorted(self._read().values(), key=lambda d: (d.ticker, d.publication_date or d.event_date or "", d.document_id))

    def get_by_id(self, document_id: str) -> NormalizedDocument | None:
        return self._read().get(document_id)

    def get_by_ticker(self, ticker: str) -> list[NormalizedDocument]:
        return [d for d in self.list_all() if d.ticker.upper() == ticker.upper()]

    def get_by_document_type(self, document_type: str) -> list[NormalizedDocument]:
        return [d for d in self.list_all() if d.document_type == document_type]

    # Friendly alias matching the public requirement wording.
    get_by_type = get_by_document_type

    def get_by_fiscal_period(self, fiscal_year: int | None, fiscal_quarter: int | None = None) -> list[NormalizedDocument]:
        return [d for d in self.list_all() if d.fiscal_year == fiscal_year and (fiscal_quarter is None or d.fiscal_quarter == fiscal_quarter)]

    def _write(self, values: Iterable[NormalizedDocument]) -> None:
        ordered = sorted(values, key=lambda d: d.document_id)
        fd, temp_name = tempfile.mkstemp(prefix="documents-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for document in ordered:
                    handle.write(json.dumps(document.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(temp_path, self.documents_path)
            self._write_index(ordered)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _write_index(self, values: list[NormalizedDocument]) -> None:
        index = {
            "version": 1,
            "documents": len(values),
            "by_id": {d.document_id: i for i, d in enumerate(values)},
            "by_ticker": self._group(values, lambda d: d.ticker),
            "by_type": self._group(values, lambda d: d.document_type),
            "by_period": self._group(values, lambda d: d.period_label or "unknown"),
        }
        fd, temp_name = tempfile.mkstemp(prefix="index-", suffix=".json", dir=self.root)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temp_path, self.index_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _group(values: list[NormalizedDocument], key) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for document in values:
            grouped.setdefault(str(key(document)), []).append(document.document_id)
        return grouped

    def upsert(self, document: NormalizedDocument) -> str:
        current = self._read()
        duplicate_ids = self._duplicate_identity_ids(current, document)
        for duplicate_id in duplicate_ids:
            current.pop(duplicate_id, None)
        previous = current.get(document.document_id)
        if previous is None:
            action = "updated" if duplicate_ids else "added"
        elif self._equivalent(previous, document):
            return "unchanged"
        else:
            action = "updated"
        current[document.document_id] = document
        self._write(current.values())
        return action

    def upsert_many(self, documents: Iterable[NormalizedDocument]) -> dict[str, int]:
        current = self._read()
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        changed = False
        for document in documents:
            duplicate_ids = self._duplicate_identity_ids(current, document)
            for duplicate_id in duplicate_ids:
                current.pop(duplicate_id, None)
            if duplicate_ids:
                changed = True
            previous = current.get(document.document_id)
            if previous is None:
                counts["updated" if duplicate_ids else "added"] += 1
                changed = True
            elif self._equivalent(previous, document):
                counts["unchanged"] += 1
                continue
            else:
                counts["updated"] += 1
                changed = True
            current[document.document_id] = document
        if changed or not self.documents_path.exists():
            self._write(current.values())
        return counts

    @staticmethod
    def _identity_key(document: NormalizedDocument) -> tuple[str, ...]:
        source = canonical_url(document.source_url)
        if source:
            return (document.ticker.upper(), source, document.document_type, document.content_hash)
        return (document.ticker.upper(), document.local_path or "", document.document_type, document.content_hash)

    @classmethod
    def _duplicate_identity_ids(cls, current: dict[str, NormalizedDocument], document: NormalizedDocument) -> list[str]:
        key = cls._identity_key(document)
        return [document_id for document_id, existing in current.items() if document_id != document.document_id and cls._identity_key(existing) == key]

    @staticmethod
    def _equivalent(left: NormalizedDocument, right: NormalizedDocument) -> bool:
        """Compare source identity and metadata while ignoring ingestion time."""
        a, b = left.to_dict(), right.to_dict()
        a.pop("created_at", None)
        b.pop("created_at", None)
        return a == b

    def rebuild(self) -> None:
        for path in (self.documents_path, self.index_path):
            if path.exists():
                path.unlink()

    def replace_ticker(self, ticker: str, documents: Iterable[NormalizedDocument]) -> dict[str, int]:
        """Replace one issuer's normalized view while preserving other issuers."""
        target = ticker.strip().upper()
        current = {key: value for key, value in self._read().items() if value.ticker.upper() != target}
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        for document in documents:
            if document.ticker.upper() != target:
                raise ValueError(f"replace_ticker expected {target}, got {document.ticker}")
            duplicate_ids = self._duplicate_identity_ids(current, document)
            for duplicate_id in duplicate_ids:
                current.pop(duplicate_id, None)
            current[document.document_id] = document
            counts["added"] += 1
        self._write(current.values())
        return counts
