"""Holdings reconciliation and factual transaction significance metrics."""

from __future__ import annotations

from dataclasses import replace

from .models import InsiderOwnershipPosition, InsiderTransaction


def derive_pre_transaction_shares(transaction: InsiderTransaction) -> tuple[float | None, str]:
    """Derive pre-trade holdings only when post holdings and direction are safe."""
    if transaction.shares_owned_after is None or transaction.shares is None:
        return None, "unknown"
    if transaction.acquired_or_disposed == "A":
        value = transaction.shares_owned_after - transaction.shares
        return (value, "derived") if value >= 0 else (None, "unknown")
    if transaction.acquired_or_disposed == "D":
        value = transaction.shares_owned_after + transaction.shares
        return (value, "derived") if value >= 0 else (None, "unknown")
    return None, "unknown"


def reconcile_transaction(transaction: InsiderTransaction) -> InsiderTransaction:
    """Prefer reported post holdings and fill only safely derivable pre holdings."""
    if transaction.shares_owned_after is None:
        return transaction
    # The parser deliberately marks same-day sequences as unknown when the
    # reported rows cannot be reconciled in XML order. Do not recreate a
    # pre-holding value here just because an individual row has a post total.
    if transaction.pre_holdings_method == "unknown" and transaction.pre_transaction_shares is None:
        return replace(transaction, percent_of_pre_transaction_holdings=None)
    pre, method = derive_pre_transaction_shares(transaction)
    percent_pre = transaction.percent_of_pre_transaction_holdings
    percent_post = transaction.percent_of_post_transaction_holdings
    if pre and transaction.shares is not None:
        percent_pre = transaction.shares / pre * 100
    if transaction.shares_owned_after and transaction.shares is not None:
        percent_post = transaction.shares / transaction.shares_owned_after * 100
    return replace(
        transaction,
        pre_transaction_shares=pre,
        pre_holdings_method=method,
        percent_of_pre_transaction_holdings=percent_pre,
        percent_of_post_transaction_holdings=percent_post,
    )


def ownership_total(position: InsiderOwnershipPosition) -> float | None:
    values = [position.shares_owned_direct, position.shares_owned_indirect]
    known = [value for value in values if value is not None]
    if known:
        return sum(known)
    return position.shares_beneficially_owned


def avoid_double_counting_positions(positions: list[InsiderOwnershipPosition]) -> tuple[list[InsiderOwnershipPosition], list[str]]:
    """Keep positions separate when ownership may overlap through a trust or spouse."""
    retained: list[InsiderOwnershipPosition] = []
    warnings: list[str] = []
    seen_beneficial: dict[tuple[str, str | None, float | None], InsiderOwnershipPosition] = {}
    for position in sorted(positions, key=lambda item: (item.person_id, item.security_title or "", item.as_of_date or "", item.ownership_id)):
        nature = (position.nature_of_indirect_ownership or "").lower()
        key = (position.person_id, position.security_title, position.shares_beneficially_owned)
        relationship_indirect = position.direct_indirect == "I" and (
            "trust" in nature or "spouse" in nature or "joint" in nature
        )
        ambiguous_indirect = position.direct_indirect == "I" and (
            "see footnote" in nature
            or "disclaim" in nature
        )
        if relationship_indirect or ambiguous_indirect:
            duplicate = seen_beneficial.get(key)
            if duplicate is not None:
                warnings.append(f"possible overlapping indirect ownership: {position.ownership_id} and {duplicate.ownership_id}")
                continue
            if ambiguous_indirect:
                warnings.append(f"ambiguous indirect ownership: {position.ownership_id}")
        retained.append(position)
        if position.shares_beneficially_owned is not None:
            seen_beneficial[key] = position
    return retained, warnings


__all__ = ["avoid_double_counting_positions", "derive_pre_transaction_shares", "ownership_total", "reconcile_transaction"]
