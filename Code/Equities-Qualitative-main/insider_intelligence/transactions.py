"""Convenience transaction queries and factual aggregate helpers."""

from __future__ import annotations

from .models import InsiderTransaction


def purchases(transactions: list[InsiderTransaction]) -> list[InsiderTransaction]:
    return [item for item in transactions if item.transaction_type == "open_market_purchase"]


def sales(transactions: list[InsiderTransaction]) -> list[InsiderTransaction]:
    return [item for item in transactions if item.transaction_type in {"open_market_sale", "automatic_sale", "planned_sale"}]


def transaction_counts(transactions: list[InsiderTransaction]) -> dict[str, int]:
    result = {
        "open_market_purchases": 0, "open_market_sales": 0, "automatic_sales": 0,
        "planned_sales": 0, "option_exercises": 0,
        "awards_vesting": 0, "tax_withholding": 0, "gifts_transfers": 0,
        "conversions": 0, "other": 0,
    }
    for item in transactions:
        if item.transaction_type == "open_market_purchase":
            result["open_market_purchases"] += 1
        elif item.transaction_type == "open_market_sale":
            result["open_market_sales"] += 1
        elif item.transaction_type == "automatic_sale":
            result["automatic_sales"] += 1
        elif item.transaction_type == "planned_sale":
            result["planned_sales"] += 1
        elif item.transaction_type in {"option_exercise", "option_exercise_and_sale"}:
            result["option_exercises"] += 1
        elif item.transaction_type in {"award_grant", "restricted_stock_vesting", "rsu_vesting"}:
            result["awards_vesting"] += 1
        elif item.transaction_type == "tax_withholding":
            result["tax_withholding"] += 1
        elif item.transaction_type in {"gift", "transfer"}:
            result["gifts_transfers"] += 1
        elif item.transaction_type == "conversion":
            result["conversions"] += 1
        else:
            result["other"] += 1
    return result


__all__ = ["purchases", "sales", "transaction_counts"]
