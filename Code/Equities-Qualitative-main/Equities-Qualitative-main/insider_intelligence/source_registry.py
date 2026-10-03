"""Accession-level registry for official SEC filings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .models import SECFiling


class SourceRegistry:
    def __init__(self, root: str | Path = "artifacts/insider_intelligence"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "filings.jsonl"

    def _read(self) -> dict[str, SECFiling]:
        values: dict[str, SECFiling] = {}
        if not self.path.exists():
            return values
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                filing = SECFiling.from_dict(json.loads(line))
            except (ValueError, TypeError, KeyError):
                continue
            values[filing.accession_number] = filing
        return values

    def list_all(self) -> list[SECFiling]:
        return sorted(self._read().values(), key=lambda item: (item.ticker, item.filing_date or "", item.accession_number))

    def get(self, accession_number: str) -> SECFiling | None:
        return self._read().get(accession_number)

    def upsert(self, filing: SECFiling) -> str:
        values = self._read()
        previous = values.get(filing.accession_number)
        if previous is not None and previous.to_dict() == filing.to_dict():
            return "unchanged"
        values[filing.accession_number] = filing
        self._write(values.values())
        return "updated" if previous is not None else "added"

    def upsert_many(self, filings: Iterable[SECFiling]) -> dict[str, int]:
        counts = {"added": 0, "updated": 0, "unchanged": 0}
        values = self._read()
        changed = False
        for filing in filings:
            previous = values.get(filing.accession_number)
            if previous is not None and previous.to_dict() == filing.to_dict():
                counts["unchanged"] += 1
                continue
            counts["updated" if previous is not None else "added"] += 1
            values[filing.accession_number] = filing
            changed = True
        if changed or not self.path.exists():
            self._write(values.values())
        return counts

    def _write(self, filings: Iterable[SECFiling]) -> None:
        fd, temporary = tempfile.mkstemp(prefix="sec-filings-", suffix=".jsonl", dir=self.root)
        os.close(fd)
        path = Path(temporary)
        try:
            ordered = sorted(filings, key=lambda item: item.accession_number)
            path.write_text("".join(json.dumps(item.to_dict(), sort_keys=True) + "\n" for item in ordered), encoding="utf-8")
            os.replace(path, self.path)
        finally:
            if path.exists():
                path.unlink()


__all__ = ["SourceRegistry"]
