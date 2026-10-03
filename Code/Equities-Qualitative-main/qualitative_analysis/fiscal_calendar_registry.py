"""Data-driven issuer fiscal-calendar metadata.

The parser remains generic; issuer facts live in a small replaceable registry
so SEC companion filings that omit an explicit fiscal label can still inherit
the issuer's documented calendar.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict[str, Any]]:
    path = Path(__file__).resolve().parent.parent / "config" / "issuer_fiscal_calendars.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return {str(key).upper(): dict(item) for key, item in value.items() if isinstance(item, Mapping)}


def lookup_fiscal_calendar(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    for key in ("ticker", "issuer_ticker", "issuer_cik"):
        value = str(metadata.get(key) or "").strip().upper()
        if not value:
            continue
        match = _registry().get(value)
        if match:
            return dict(match)
    return {}


__all__ = ["lookup_fiscal_calendar"]
