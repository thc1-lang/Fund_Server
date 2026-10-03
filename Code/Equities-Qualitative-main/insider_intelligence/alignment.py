"""Factual insider activity windows and discretionary transaction clusters."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from .models import ALIGNMENT_VERSION, InsiderAlignmentSnapshot, InsiderCluster, InsiderOwnershipPosition, InsiderPerson, InsiderTransaction, OwnershipBaseline, OwnershipReconciliation
from .ownership import avoid_double_counting_positions, ownership_total


def _parse(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def is_discretionary_open_market(transaction: InsiderTransaction, side: str | None = None) -> bool:
    if transaction.is_open_market is not True or transaction.is_automatic_sale is True:
        return False
    if side == "purchase":
        return transaction.transaction_type == "open_market_purchase"
    if side == "sale":
        return transaction.transaction_type == "open_market_sale"
    return transaction.transaction_type in {"open_market_purchase", "open_market_sale"}


def detect_clusters(transactions: list[InsiderTransaction], *, window_days: int = 30) -> list[InsiderCluster]:
    """Find groups of at least two distinct insiders in a rolling window."""
    result: list[InsiderCluster] = []
    for side, transaction_type in (("purchase", "purchase"), ("sale", "sale")):
        values = sorted([item for item in transactions if is_discretionary_open_market(item, side)], key=lambda item: _parse(item.transaction_date) or date.max)
        consumed: set[str] = set()
        for anchor in values:
            anchor_date = _parse(anchor.transaction_date)
            if anchor_date is None or anchor.transaction_id in consumed:
                continue
            window = [item for item in values if (item.transaction_id not in consumed and _parse(item.transaction_date) is not None and timedelta(0) <= (_parse(item.transaction_date) - anchor_date) <= timedelta(days=window_days))]
            people = sorted({item.person_id for item in window})
            if len(people) < 2:
                continue
            dates = [_parse(item.transaction_date) for item in window]
            start = min(item for item in dates if item is not None)
            end = max(item for item in dates if item is not None)
            tx_ids = sorted(item.transaction_id for item in window)
            identity = "|".join((anchor.ticker.upper(), transaction_type, start.isoformat(), end.isoformat(), *tx_ids))
            cluster_id = "cluster:" + hashlib.sha256(identity.encode()).hexdigest()[:28]
            result.append(InsiderCluster(
                cluster_id=cluster_id,
                ticker=anchor.ticker.upper(),
                transaction_type=transaction_type,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                people=people,
                transaction_ids=tx_ids,
                combined_value=sum(item.transaction_value or 0.0 for item in window),
                window_days=window_days,
                alignment_version=ALIGNMENT_VERSION,
            ))
            consumed.update(tx_ids)
    return result


def build_alignment_snapshot(
    ticker: str,
    as_of_date: str,
    transactions: list[InsiderTransaction],
    people: list[InsiderPerson],
    positions: list[InsiderOwnershipPosition],
    *,
    cluster_window_days: int = 30,
    baselines: list[OwnershipBaseline] | None = None,
    reconciliations: list[OwnershipReconciliation] | None = None,
) -> tuple[InsiderAlignmentSnapshot, list[InsiderCluster]]:
    as_of = _parse(as_of_date) or date.today()
    ticker = ticker.upper()
    relevant = [item for item in transactions if item.ticker.upper() == ticker and _parse(item.transaction_date) and _parse(item.transaction_date) <= as_of]
    clusters = detect_clusters(relevant, window_days=cluster_window_days)

    def within(days: int, side: str) -> list[InsiderTransaction]:
        start = as_of - timedelta(days=days)
        return [item for item in relevant if is_discretionary_open_market(item, side) and start <= (_parse(item.transaction_date) or date.min) <= as_of]

    purchases = {days: within(days, "purchase") for days in (30, 90, 365)}
    sales = {days: within(days, "sale") for days in (30, 90, 365)}
    current_positions, _ = avoid_double_counting_positions([
        item for item in positions if item.ticker.upper() == ticker and item.security_type != "derivative"
    ])
    by_person = {person.person_id: person for person in people if person.ticker.upper() == ticker}

    def shares_for(role: str) -> float | None:
        ids = [person_id for person_id, person in by_person.items() if (role == "director" and person.is_director) or (role == "ceo" and person.is_ceo) or (role == "cfo" and person.is_cfo) or (role == "founder" and person.is_founder)]
        values = [ownership_total(item) for item in current_positions if item.person_id in ids]
        known = [item for item in values if item is not None]
        return sum(known) if known else None

    known_values = [ownership_total(item) for item in current_positions]
    known = [item for item in known_values if item is not None]
    start_365 = as_of - timedelta(days=365)
    automatic_sales = [item for item in relevant if item.transaction_type == "automatic_sale" and start_365 <= (_parse(item.transaction_date) or date.min) <= as_of]
    planned_sales = [item for item in relevant if item.transaction_type == "planned_sale" and start_365 <= (_parse(item.transaction_date) or date.min) <= as_of]
    source_filings = sorted({item.filing_id for item in current_positions if item.filing_id}.union(item.filing_id for item in relevant if item.filing_id))
    proxy = [item for item in (baselines or []) if item.ticker.upper() == ticker and not item.is_group_record and not item.is_major_beneficial_owner]
    proxy_values = [item.beneficial_shares for item in proxy if item.beneficial_shares is not None]
    proxy_for = lambda label: sum(item.beneficial_shares or 0.0 for item in proxy if item.beneficial_shares is not None and label in (item.role or "").lower()) or None
    recs = [item for item in (reconciliations or []) if item.ticker.upper() == ticker]
    reconstructed_values = [item.reconstructed_current_shares for item in recs if item.current_ownership_method == "proxy_plus_section16_reconciled" and item.reconstructed_current_shares is not None]
    if reconstructed_values:
        ownership_method = "proxy_plus_section16_reconciled"
    elif proxy:
        ownership_method = "proxy_and_section16_unreconciled" if known else "proxy_only"
    else:
        ownership_method = "section16_only"
    snapshot = InsiderAlignmentSnapshot(
        ticker=ticker,
        as_of_date=as_of.isoformat(),
        ceo_shares=shares_for("ceo"),
        cfo_shares=shares_for("cfo"),
        founder_shares=shares_for("founder"),
        director_shares=shares_for("director"),
        known_insider_shares=sum(known) if known else None,
        known_insider_percent=None,
        open_market_purchases_30d=len(purchases[30]),
        open_market_purchases_90d=len(purchases[90]),
        open_market_purchases_365d=len(purchases[365]),
        open_market_sales_30d=len(sales[30]),
        open_market_sales_90d=len(sales[90]),
        open_market_sales_365d=len(sales[365]),
        purchase_value_365d=sum(item.transaction_value or 0.0 for item in purchases[365]),
        sale_value_365d=sum(item.transaction_value or 0.0 for item in sales[365]),
        buyers_365d=len({item.person_id for item in purchases[365]}),
        sellers_365d=len({item.person_id for item in sales[365]}),
        cluster_purchase_count=sum(item.transaction_type == "purchase" for item in clusters),
        cluster_sale_count=sum(item.transaction_type == "sale" for item in clusters),
        source_filing_ids=source_filings,
        net_open_market_value_365d=sum(item.transaction_value or 0.0 for item in purchases[365]) - sum(item.transaction_value or 0.0 for item in sales[365]),
        automatic_sale_value_365d=sum(item.transaction_value or 0.0 for item in automatic_sales),
        planned_sale_value_365d=sum(item.transaction_value or 0.0 for item in planned_sales),
        proxy_ceo_shares=proxy_for("ceo"),
        proxy_cfo_shares=proxy_for("cfo"),
        proxy_founder_shares=proxy_for("founder"),
        proxy_director_shares=proxy_for("director"),
        proxy_group_shares=sum(item.beneficial_shares or 0.0 for item in (baselines or []) if item.ticker.upper() == ticker and item.is_group_record) or None,
        proxy_group_percent=next((item.voting_power_percent for item in (baselines or []) if item.ticker.upper() == ticker and item.is_group_record and item.voting_power_percent is not None), None),
        section16_reported_shares=sum(known) if known else None,
        reconstructed_current_shares=sum(reconstructed_values) if reconstructed_values else None,
        current_ownership_method=ownership_method,
    )
    return snapshot, clusters


__all__ = ["build_alignment_snapshot", "detect_clusters", "is_discretionary_open_market"]
