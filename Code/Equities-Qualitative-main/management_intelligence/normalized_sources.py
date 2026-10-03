"""Read-only handoff from the normalized SEC document store.

This adapter is the single bridge used by management and governance.  It
reuses the authoritative normalized document and its original SEC HTML when
available; it does not create a second downloader or duplicate analytical
parsers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from insider_intelligence.models import SECFiling


_FORMS = {"DEF 14A", "DEFA14A", "10-K", "10-Q", "8-K", "8-K/A"}


def _accession(value: str | None, url: str | None) -> str | None:
    if value:
        text = str(value)
        if re.fullmatch(r"\d{10}-\d{2}-\d{6}", text):
            return text
        compact = re.sub(r"[^0-9]", "", text)
        if len(compact) == 18:
            return f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    match = re.search(r"/(\d{18})/", str(url or ""))
    if match:
        compact = match.group(1)
        return f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    return None


def _cik(value: str | None, url: str | None) -> str:
    text = str(value or "")
    if text.isdigit():
        return text.zfill(10)
    match = re.search(r"/data/(\d+)/", str(url or ""), re.I)
    return match.group(1).zfill(10) if match else ""


def _form(metadata: dict, title: str, source_url: str) -> str | None:
    explicit = metadata.get("sec_form") or metadata.get("form_type") or metadata.get("form")
    if explicit and str(explicit).upper() in _FORMS:
        return str(explicit).upper()
    combined = " ".join((title, source_url)).upper()
    for form in ("DEF 14A", "DEFA14A", "10-K", "10-Q", "8-K/A", "8-K"):
        if form in combined or form.replace("-", "") in combined:
            return form
    return None


@dataclass(frozen=True)
class NormalizedSECSource:
    ticker: str
    company_name: str
    issuer_cik: str
    form_type: str
    accession_number: str
    filing_date: str | None
    report_date: str | None
    available_date: str | None
    source_url: str
    local_source_path: str | None
    normalized_document_id: str
    content_hash: str
    title: str
    text: str
    html: str

    @property
    def is_proxy(self) -> bool:
        return self.form_type in {"DEF 14A", "DEFA14A"}

    def filing(self) -> SECFiling:
        primary = Path(urlparse(self.source_url).path).name or "filing.htm"
        return SECFiling(
            ticker=self.ticker,
            issuer_cik=self.issuer_cik,
            form_type=self.form_type,
            accession_number=self.accession_number,
            filing_date=self.filing_date,
            report_date=self.report_date,
            primary_document=primary,
            source_url=self.source_url,
            local_source_path=self.local_source_path,
            downloaded=True,
            cache_hit=True,
            source_authority="sec_official_filing",
        )

    def body(self) -> bytes:
        return (self.html or self.text).encode("utf-8", errors="replace")


class NormalizedSECSourceCatalog:
    """Ticker-scoped, point-in-time view of SEC documents in normalization."""

    def __init__(self, root: str | Path = "artifacts/qualitative_analysis"):
        root = Path(root)
        self.path = root if root.name == "documents.jsonl" else root / "documents.jsonl"

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    @staticmethod
    def _allowed(value: str | None, as_of_date: str | None) -> bool:
        # Unknown filing dates cannot safely enter a historical observation.
        return bool(value) and (not as_of_date or value[:10] <= as_of_date)

    def sources(self, ticker: str, *, forms: tuple[str, ...] | None = None, as_of_date: str | None = None) -> list[NormalizedSECSource]:
        wanted = {item.upper() for item in forms} if forms else _FORMS
        result: dict[tuple[str, str], NormalizedSECSource] = {}
        for row in self._rows():
            if str(row.get("ticker") or "").upper() != ticker.upper():
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            source_url = str(row.get("source_url") or metadata.get("source_url") or "")
            if "sec.gov" not in source_url.lower() or str(metadata.get("source_family") or "SEC_EDGAR").upper() not in {"SEC_EDGAR", "SEC_OFFICIAL", ""}:
                continue
            form = _form(metadata, str(row.get("title") or ""), source_url)
            if not form or form not in wanted:
                continue
            accession = _accession(metadata.get("accession_number"), source_url)
            if not accession:
                continue
            # Historical SEC source admission is keyed to filing date.  Do not
            # silently substitute a publication or download date when the
            # filing date is absent; unknown filing dates remain unavailable
            # for a point-in-time observation.
            filing_date = str(row.get("filing_date") or metadata.get("filing_date") or "")[:10] or None
            if filing_date is None:
                provenance = metadata.get("provenance")
                if isinstance(provenance, list):
                    filing_date = next((str(item.get("filing_date"))[:10] for item in provenance if isinstance(item, dict) and item.get("filing_date")), None)
            if not self._allowed(filing_date, as_of_date):
                continue
            available = str(row.get("available_date") or metadata.get("available_date") or filing_date or "")[:10] or None
            local = row.get("local_path") or metadata.get("local_path") or metadata.get("local_filename")
            if local and not Path(str(local)).is_absolute() and metadata.get("artifact_root"):
                local = str(Path(str(metadata["artifact_root"])) / str(local))
            source = NormalizedSECSource(
                ticker=str(row.get("ticker") or ticker).upper(),
                company_name=str(row.get("company_name") or ticker),
                issuer_cik=_cik(metadata.get("issuer_cik") or row.get("issuer_cik"), source_url),
                form_type=form,
                accession_number=accession,
                filing_date=filing_date,
                report_date=str(row.get("period_end_date") or metadata.get("report_date") or "")[:10] or None,
                available_date=available,
                source_url=source_url,
                local_source_path=str(local) if local else None,
                normalized_document_id=str(row.get("document_id") or ""),
                content_hash=str(row.get("content_hash") or metadata.get("sha256") or ""),
                title=str(row.get("title") or ""),
                text=str(row.get("text") or ""),
                html=str(metadata.get("content_html") or ""),
            )
            result[(accession, source.normalized_document_id)] = source
        return sorted(result.values(), key=lambda item: (item.filing_date or "", item.accession_number, item.normalized_document_id))

    def latest_proxy(self, ticker: str, *, as_of_date: str | None = None) -> NormalizedSECSource | None:
        values = self.sources(ticker, forms=("DEF 14A", "DEFA14A"), as_of_date=as_of_date)
        definitive = [item for item in values if item.form_type == "DEF 14A"]
        return max(definitive or values, key=lambda item: (item.filing_date or "", item.accession_number), default=None)


__all__ = ["NormalizedSECSource", "NormalizedSECSourceCatalog"]
