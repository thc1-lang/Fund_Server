"""Board-history extraction facade for Component 6A."""

from __future__ import annotations

from insider_intelligence.models import SECFiling

from .career_history import parse_proxy_people
from .models import BoardMembership


def extract_board_history(html: str | bytes, filing: SECFiling, company_name: str) -> list[BoardMembership]:
    return parse_proxy_people(html, filing, company_name).boards


__all__ = ["extract_board_history"]
