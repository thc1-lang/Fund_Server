"""Issuer-owned source helpers.

Company leadership pages are supplemental evidence.  They never overwrite an
SEC filing assertion and are optional when an issuer's site is unavailable.
"""

from __future__ import annotations

from .models import OfficialSource


def leadership_source(ticker: str, url: str, *, local_path: str | None = None, title: str | None = None) -> OfficialSource:
    return OfficialSource(
        source_id=f"issuer:{ticker.upper()}:leadership:{url}",
        source_authority="issuer_official",
        source_type="leadership_page",
        url=url,
        local_path=local_path,
        title=title,
    )


__all__ = ["leadership_source"]
