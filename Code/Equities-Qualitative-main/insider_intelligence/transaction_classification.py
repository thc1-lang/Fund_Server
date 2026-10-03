"""SEC transaction-code preservation and factual transaction classification."""

from __future__ import annotations

import re
from dataclasses import dataclass


TRANSACTION_TYPES = (
    "open_market_purchase", "open_market_sale", "option_exercise",
    "option_exercise_and_sale", "restricted_stock_vesting", "rsu_vesting",
    "tax_withholding", "award_grant", "gift", "transfer", "conversion",
    "derivative_transaction", "automatic_sale", "planned_sale", "other", "unknown",
)


_CODE_LABELS = {
    "P": "open_market_purchase",
    "S": "open_market_sale",
    "M": "option_exercise",
    "A": "award_grant",
    "F": "tax_withholding",
    "G": "gift",
    "D": "transfer",
    "C": "conversion",
    "E": "other",
    "H": "other",
    "I": "other",
    "J": "other",
    "K": "derivative_transaction",
    "L": "other",
    "W": "other",
    "V": "other",
}


@dataclass(frozen=True)
class ClassificationResult:
    transaction_type: str
    is_open_market: bool | None
    is_option_exercise: bool | None
    is_equity_award: bool | None
    is_tax_withholding: bool | None
    is_gift: bool | None
    is_automatic_sale: bool | None
    is_10b5_1: bool | None
    confidence: float


def detect_10b5_1(footnotes: list[str] | tuple[str, ...] | None, explicit: bool | None = None) -> bool | None:
    """Detect only explicit plan language; ambiguity remains ``None``."""
    if explicit is not None:
        return explicit
    text = " ".join(str(item) for item in (footnotes or [])).lower()
    if re.search(r"not\s+(?:pursuant to|under)\s+(?:a\s+)?rule\s*10b5\s*[- ]?1", text):
        return False
    if re.search(r"rule\s*10b5\s*[- ]?1|10b5\s*[- ]?1\s*(?:plan|trading)", text):
        return True
    return None


def classify_transaction(
    transaction_code: str | None,
    acquired_or_disposed: str | None,
    *,
    security_title: str | None = None,
    footnotes: list[str] | None = None,
    is_10b5_1: bool | None = None,
) -> ClassificationResult:
    """Map one SEC row to a controlled factual class without losing its code."""
    code = (transaction_code or "").strip().upper() or None
    acquired = (acquired_or_disposed or "").strip().upper() or None
    text = " ".join([security_title or "", *(footnotes or [])]).lower()
    plan = detect_10b5_1(footnotes, is_10b5_1)
    base = _CODE_LABELS.get(code or "", "unknown")
    confidence = 0.98 if code in _CODE_LABELS else 0.55

    if code == "S" and plan is True:
        transaction_type = "automatic_sale"
    elif code == "S" and plan is None and re.search(r"automatic|pre[- ]?arranged|planned sale|scheduled sale", text):
        transaction_type = "planned_sale"
        confidence = 0.86
    elif code == "M" and acquired == "D":
        transaction_type = "option_exercise_and_sale"
    elif code == "M" or re.search(r"option\s+(?:exercise|conversion)|exercise of option", text):
        transaction_type = "option_exercise"
    elif code == "A" and re.search(r"rsu|restricted stock|vesting|vested", text):
        transaction_type = "rsu_vesting" if "rsu" in text else "restricted_stock_vesting"
    elif code == "F" or re.search(r"tax(?:es| withholding)|withhold", text):
        transaction_type = "tax_withholding"
    elif code == "G":
        transaction_type = "gift"
    elif code == "C":
        transaction_type = "conversion"
    elif code == "D":
        transaction_type = "transfer"
    else:
        transaction_type = base

    purchase = transaction_type == "open_market_purchase"
    sale = transaction_type in {"open_market_sale", "automatic_sale", "planned_sale"}
    return ClassificationResult(
        transaction_type=transaction_type,
        is_open_market=True if purchase or sale else (False if transaction_type != "unknown" else None),
        is_option_exercise=transaction_type in {"option_exercise", "option_exercise_and_sale"},
        is_equity_award=transaction_type in {"award_grant", "restricted_stock_vesting", "rsu_vesting"},
        is_tax_withholding=transaction_type == "tax_withholding",
        is_gift=transaction_type == "gift",
        is_automatic_sale=transaction_type in {"automatic_sale", "planned_sale"},
        is_10b5_1=plan,
        confidence=confidence,
    )


__all__ = ["TRANSACTION_TYPES", "ClassificationResult", "classify_transaction", "detect_10b5_1"]
