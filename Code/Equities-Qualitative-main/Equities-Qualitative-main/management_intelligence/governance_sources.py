"""Read-only official source catalog for Component 6C.

The catalog intentionally reads the existing Component 6A and SEC caches.  It
does not perform web requests; a future refresh can explicitly add a network
provider without changing the governance parsers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from insider_intelligence.models import SECFiling

from .models import OfficialSource
from .normalized_sources import NormalizedSECSourceCatalog
from .store import ManagementStore


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).replace("\xa0", " ").split()).replace("�", " ")


def html_text(payload: str | bytes) -> str:
    parser = _TextParser()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    parser.feed(payload)
    return parser.text()


@dataclass(frozen=True)
class CachedGovernanceSource:
    ticker: str
    accession_number: str
    form_type: str
    filing_date: str | None
    report_date: str | None
    source_url: str
    local_path: str
    text: str
    normalized_document_id: str | None = None
    content_hash: str | None = None
    available_date: str | None = None

    @property
    def is_proxy(self) -> bool:
        return self.form_type.upper() == "DEF 14A"

    def to_source(self) -> OfficialSource:
        return OfficialSource(
            source_id=f"sec:{self.accession_number}",
            source_authority="sec_official_filing",
            source_type=self.form_type.upper(),
            url=self.source_url,
            local_path=self.local_path,
            accession_number=self.accession_number,
            filing_date=self.filing_date,
            report_date=self.report_date,
        )


class GovernanceSourceCatalog:
    """Catalog cached proxy, 8-K, 10-K and exhibit material for one ticker."""

    def __init__(self, *, store_root: str | Path = "artifacts/management_intelligence", sec_cache_root: str | Path = "artifacts/insider_intelligence/sec", normalized_root: str | Path | None = None, as_of_date: str | None = None):
        self.store = ManagementStore(store_root)
        self.sec_cache_root = Path(sec_cache_root)
        self.normalized = NormalizedSECSourceCatalog(normalized_root) if normalized_root else None
        self.as_of_date = as_of_date
        self.network_requests = 0

    @staticmethod
    def _path_from_value(value: str | None, workspace: Path) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if path.exists():
            return path
        candidate = workspace / path
        return candidate if candidate.exists() else None

    @staticmethod
    def _url_for(ticker: str, accession: str, filename: str, cik: str | None = None) -> str:
        digits = (cik or "").lstrip("0") or "0"
        return f"https://www.sec.gov/Archives/edgar/data/{digits}/{accession.replace('-', '')}/{filename}"

    def _metadata_sources(self, ticker: str) -> Iterable[CachedGovernanceSource]:
        ticker_dir = self.sec_cache_root / ticker.upper() / "filings"
        if not ticker_dir.exists():
            return []
        result: list[CachedGovernanceSource] = []
        for filing_dir in sorted(p for p in ticker_dir.iterdir() if p.is_dir()):
            metadata_path = filing_dir / "filing_metadata.json"
            metadata: dict = {}
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    metadata = {}
            form = str(metadata.get("form_type") or metadata.get("form") or "").upper()
            if form not in {"DEF 14A", "8-K", "10-K", "8-K/A"}:
                # A source registry entry may still identify a cached proxy.
                form = ""
            for path in sorted(filing_dir.iterdir()):
                if path.name == "filing_metadata.json" or path.suffix.lower() not in {".htm", ".html", ".txt", ".xml"}:
                    continue
                if form == "" and path.suffix.lower() not in {".htm", ".html"}:
                    continue
                try:
                    text = html_text(path.read_bytes()) if path.suffix.lower() in {".htm", ".html"} else path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                accession = filing_dir.name
                cik = str(metadata.get("issuer_cik") or metadata.get("cik") or "")
                url = str(metadata.get("source_url") or self._url_for(ticker, accession, path.name, cik))
                result.append(CachedGovernanceSource(ticker.upper(), accession, form or str(metadata.get("form_type") or "UNKNOWN"), metadata.get("filing_date"), metadata.get("report_date"), url, str(path), text))
        return result

    def _normalized_sources(self, ticker: str) -> list[CachedGovernanceSource]:
        if self.normalized is None:
            return []
        result: list[CachedGovernanceSource] = []
        for source in self.normalized.sources(ticker, forms=("DEF 14A", "DEFA14A", "10-K", "8-K", "8-K/A"), as_of_date=self.as_of_date):
            result.append(CachedGovernanceSource(
                ticker=source.ticker,
                accession_number=source.accession_number,
                form_type=source.form_type,
                filing_date=source.filing_date,
                report_date=source.report_date,
                source_url=source.source_url,
                local_path=source.local_source_path or "",
                text=html_text(source.html or source.text),
                normalized_document_id=source.normalized_document_id,
                content_hash=source.content_hash,
                available_date=source.available_date,
            ))
        return result

    def sources(self, ticker: str) -> list[CachedGovernanceSource]:
        ticker = ticker.strip().upper()
        values: dict[tuple[str, str], CachedGovernanceSource] = {}

        # First use the exact local paths recorded by 6A.  This preserves the
        # accession and source authority even when a cache has unusual names.
        source_records = self.store.sources.list_all()
        for record in source_records:
            path = self._path_from_value(record.local_path, Path.cwd())
            if path is None or ticker.upper() not in str(path).upper():
                continue
            try:
                text = html_text(path.read_bytes()) if path.suffix.lower() in {".htm", ".html"} else path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            values[(record.accession_number or record.source_id, str(path))] = CachedGovernanceSource(
                ticker, record.accession_number or record.source_id, record.source_type.upper(), record.filing_date,
                record.report_date, record.url, str(path), text,
            )

        for source in self._metadata_sources(ticker):
            values.setdefault((source.accession_number, source.local_path), source)
        for source in self._normalized_sources(ticker):
            # The normalized catalog is the authoritative handoff.  A source
            # registry entry may point at a cached PDF while normalization has
            # already materialized extracted text for that same accession.  Do
            # not let the binary/local-path variant shadow the usable
            # normalized representation; retain its provenance fields on the
            # normalized source itself.
            for key in [key for key in values if key[0] == source.accession_number]:
                values.pop(key, None)
            values[(source.accession_number, source.normalized_document_id or source.local_path)] = source
        if self.as_of_date:
            values = {
                key: source for key, source in values.items()
                if source.filing_date and source.filing_date[:10] <= self.as_of_date
            }
        return sorted(values.values(), key=lambda item: (item.filing_date or "", item.accession_number, item.local_path))

    def latest_proxy(self, ticker: str) -> CachedGovernanceSource | None:
        proxies = [source for source in self.sources(ticker) if source.is_proxy]
        return max(proxies, key=lambda source: (source.filing_date or "", source.accession_number), default=None)

    def subsequent_events(self, ticker: str, after: str | None = None) -> list[CachedGovernanceSource]:
        values = [source for source in self.sources(ticker) if source.form_type.upper() in {"8-K", "8-K/A"} and (not after or (source.filing_date or "") > after)]
        return sorted(values, key=lambda source: (source.filing_date or "", source.accession_number))


__all__ = ["CachedGovernanceSource", "GovernanceSourceCatalog", "html_text"]
