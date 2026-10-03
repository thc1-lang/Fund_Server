"""Conservative organization mapping for prior employers and boards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class OrganizationReference:
    name: str
    classification: str
    ticker: str | None = None
    cik: str | None = None
    evidence: str = ""


def _normalize(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def resolve_organization(name: str, *, ticker_cache: str | Path = "artifacts/insider_intelligence/sec/company_tickers.json") -> OrganizationReference:
    """Map only exact/strong SEC ticker-title matches; never guess a ticker."""
    clean = " ".join(str(name or "").replace("\xa0", " ").split()).strip(" ,.;")
    if not clean:
        return OrganizationReference(clean, "UNRESOLVED")
    normalized = _normalize(clean)
    aliases = {
        "servicenow": "servicenow inc",
        "gilead sciences": "gilead sciences inc",
        "microsoft corporation": "microsoft corp",
        "cisco systems": "cisco systems inc",
        "palantir": "palantir technologies inc",
        "zoom": "zoom communications inc",
    }
    target = aliases.get(normalized, normalized)
    path = Path(ticker_cache)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        matches = []
        for value in (payload.values() if isinstance(payload, dict) else []):
            title = _normalize(str(value.get("title", "")))
            if title == target or title.startswith(target + " ") or target.startswith(title + " "):
                matches.append(value)
        if len(matches) == 1:
            value = matches[0]
            return OrganizationReference(
                name=str(value.get("title") or clean),
                classification="PUBLIC_COMPANY_CONFIRMED",
                ticker=str(value.get("ticker") or "") or None,
                cik=str(value.get("cik_str") or "") or None,
                evidence="Exact official SEC company-tickers title match",
            )
    lower = normalized
    if any(token in lower for token in ("university", "college", "institute", "hospital", "foundation", "department", "school of")):
        return OrganizationReference(clean, "NON_COMPANY_ORGANIZATION")
    if any(token in lower for token in ("venture", "capital", "fund", "private", "family office")):
        return OrganizationReference(clean, "PRIVATE_COMPANY")
    return OrganizationReference(clean, "UNRESOLVED")


__all__ = ["OrganizationReference", "resolve_organization"]
