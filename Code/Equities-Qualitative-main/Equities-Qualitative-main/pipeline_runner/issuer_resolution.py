"""Resolve an explicitly requested issuer without making the worksheet a gate.

The runner needs a small amount of identity metadata before it can invoke the
downloader in manual-ticker mode.  Resolution is deliberately kept separate
from the analytical components: it only identifies the issuer and preserves
the source used for that identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IssuerResolutionError(LookupError):
    """Raised when an explicit ticker cannot be mapped to an issuer."""


@dataclass(frozen=True)
class IssuerMetadata:
    ticker: str
    company_name: str
    cik: str | None = None
    source: str = "unknown"
    source_url: str | None = None
    worksheet: str | None = None
    worksheet_row: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "cik": self.cik,
            "source": self.source,
            "source_url": self.source_url,
            "worksheet": self.worksheet,
            "worksheet_row": self.worksheet_row,
        }


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _candidate_from_manifest(path: Path, ticker: str) -> IssuerMetadata | None:
    data = _json_object(path)
    if not data or str(data.get("ticker") or "").upper() != ticker:
        return None
    company_name = str(data.get("company_name") or "").strip()
    if not company_name:
        return None
    cik = data.get("issuer_cik") or data.get("cik")
    return IssuerMetadata(
        ticker=ticker,
        company_name=company_name,
        cik=str(cik) if cik else None,
        source="cached_acquisition",
        source_url=str(data.get("source_url") or data.get("investor_relations_url") or "") or None,
        worksheet=str(data.get("worksheet") or "") or None,
        worksheet_row=int(data["spreadsheet_row"]) if str(data.get("spreadsheet_row") or "").isdigit() else None,
    )


def _cached_identity(ticker: str, roots: list[Path]) -> IssuerMetadata | None:
    """Recover identity from completed acquisition manifests, newest first."""
    candidates: list[tuple[float, Path]] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            paths = root.rglob("manifest.json")
        except OSError:
            continue
        for path in paths:
            try:
                resolved = path.resolve(strict=False)
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for _, path in sorted(candidates, reverse=True):
        candidate = _candidate_from_manifest(path, ticker)
        if candidate:
            return candidate
    return None


def _worksheet_cache(ticker: str, roots: list[Path]) -> IssuerMetadata | None:
    """Read an already cached worksheet row without contacting Google Sheets."""
    for root in roots:
        data = _json_object(root / "stock_metadata.json")
        if not data:
            continue
        stocks = data.get("stocks")
        if not isinstance(stocks, dict):
            continue
        for key, value in stocks.items():
            if str(key).upper() != ticker or not isinstance(value, dict):
                continue
            company_name = str(value.get("company_name") or "").strip()
            if not company_name:
                continue
            row = value.get("spreadsheet_row")
            return IssuerMetadata(
                ticker=ticker,
                company_name=company_name,
                source="worksheet_cache",
                worksheet=str(value.get("worksheet") or "") or None,
                worksheet_row=int(row) if str(row or "").isdigit() else None,
            )
    return None


def resolve_issuer_metadata(
    ticker: str,
    *,
    download_root: str | Path,
    data_root: str | Path,
    sec_provider: Any | None = None,
) -> IssuerMetadata:
    """Resolve a manual ticker using authoritative, non-worksheet sources.

    Existing local acquisition identity is preferred for repeatability.  SEC
    ticker metadata is the normal authoritative fallback for US issuers.  A
    cached worksheet row is used only if those sources are unavailable; no
    Google Sheets network read is performed here, so a missing row cannot gate
    an explicit ticker run.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise IssuerResolutionError("ISSUER_RESOLUTION_FAILED: ticker is empty")
    cache_root = Path(download_root)
    production_root = Path(data_root) / symbol / "01_Acquisition"
    cached = _cached_identity(symbol, [cache_root, production_root])
    worksheet = _worksheet_cache(symbol, [cache_root, Path(data_root)])
    if cached and cached.cik:
        return cached

    try:
        if sec_provider is None:
            from insider_intelligence.sec_ingestion import SECSourceProvider

            sec_provider = SECSourceProvider(cache_root=cache_root / ".issuer_resolution" / "sec")
        identity = sec_provider.resolve_issuer(symbol)
        return IssuerMetadata(
            ticker=symbol,
            company_name=str(identity.company_name).strip() or symbol,
            cik=str(identity.issuer_cik),
            source="sec_edgar",
            source_url=str(identity.source_url),
            worksheet=(cached.worksheet if cached else worksheet.worksheet if worksheet else None),
            worksheet_row=(cached.worksheet_row if cached else worksheet.worksheet_row if worksheet else None),
        )
    except Exception as sec_error:
        if cached:
            return cached
        if worksheet:
            return worksheet
        raise IssuerResolutionError(f"ISSUER_RESOLUTION_FAILED: {symbol} ({sec_error})") from sec_error
