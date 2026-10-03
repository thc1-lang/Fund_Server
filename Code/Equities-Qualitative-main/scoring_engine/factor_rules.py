from __future__ import annotations

from typing import Any


def map_ordinal(value: Any, mapping: dict[str, Any]) -> float | None:
    if value is None:
        return None
    mapped = mapping.get(str(value).strip().lower())
    return float(mapped) if isinstance(mapped, (int, float)) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def normalize_label(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")
