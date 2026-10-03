"""Command-line entry point for the official SEC insider-intelligence component."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
from collections import Counter
from pathlib import Path

from .alignment import build_alignment_snapshot
from .models import SECIngestionResult
from .ownership import reconcile_transaction
from .sec_ingestion import DEFAULT_FORMS, SECSourceProvider, SEC_USER_AGENT
from .store import InsiderStore
from .transactions import transaction_counts


DEFAULT_TICKERS = ("ZM", "PLTR", "EXEL")
DEFAULT_LOOKBACK_DAYS = 365


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acquire factual insider intelligence from official SEC EDGAR filings.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--ticker")
    scope.add_argument("--all", action="store_true", dest="all_companies")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true", help="emit safe SEC request diagnostics")
    parser.add_argument("--full-history", action="store_true", help="include archived SEC submissions; use deliberately for backfills")
    parser.add_argument("--forms", nargs="+", default=list(DEFAULT_FORMS))
    parser.add_argument("--cache-root", type=Path, default=Path("artifacts/insider_intelligence/sec"))
    parser.add_argument("--store-root", type=Path, default=Path("artifacts/insider_intelligence"))
    parser.add_argument("--user-agent", default=SEC_USER_AGENT)
    return parser


def resolve_date_window(args: argparse.Namespace, *, today: date | None = None) -> tuple[str | None, str | None, bool, bool]:
    """Resolve a bounded routine window without silently enabling history."""
    today = today or date.today()
    full_history = bool(getattr(args, "full_history", False))
    explicit_window = args.from_date is not None or args.to_date is not None
    if full_history and not explicit_window:
        return None, None, False, True
    to_date = args.to_date or today.isoformat()
    from_date = args.from_date or (date.fromisoformat(to_date) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()
    return from_date, to_date, not explicit_window, full_history


def _print_result(
    result: SECIngestionResult,
    store: InsiderStore,
    *,
    as_of_date: str,
    from_date: str | None,
    to_date: str | None,
    default_lookback_applied: bool,
) -> None:
    ticker = result.ticker.upper()
    filing_ids = {item.accession_number for item in result.filings}
    transactions = [item for item in store.get_transactions(ticker) if item.filing_id in filing_ids]
    people_ids = {item.person_id for item in transactions}
    positions = store.get_current_holdings(ticker, filing_ids=filing_ids)
    people = [item for item in store.list_people() if item.ticker.upper() == ticker and (item.person_id in people_ids or filing_ids.intersection(item.source_filing_ids))]
    counts = transaction_counts(transactions)
    snapshot, clusters = build_alignment_snapshot(ticker, as_of_date, transactions, people, positions)
    print(f"Ticker: {ticker}")
    print(f"Date window: {from_date or 'all available'} -> {to_date or 'all available'}")
    if default_lookback_applied:
        print(f"Default lookback applied: {DEFAULT_LOOKBACK_DAYS} days")
    print(f"Insiders identified: {len(people)}")
    print(f"Current ownership positions: {len(positions)}")
    print(f"Filings discovered: {result.filings_discovered}")
    print(f"Filings processed: {result.filings_processed}")
    print(f"Filings reused: {result.filings_reused}")
    print(f"People added/updated: {result.people_added}/{result.people_updated}")
    print(f"Transactions added/updated/unchanged: {result.transactions_added}/{result.transactions_updated}/{result.transactions_unchanged}")
    print(f"Ownership positions added/updated/unchanged: {result.ownership_positions_added}/{result.ownership_positions_updated}/{result.ownership_positions_unchanged}")
    forms = Counter(item.form_type.upper().split("/", 1)[0] for item in result.filings)
    print(f"Form 3 count: {forms.get('3', 0)}")
    print(f"Form 4 count: {forms.get('4', 0)}")
    print(f"Form 5 count: {forms.get('5', 0)}")
    print("Transactions:")
    print(f"Open-market purchases: {counts['open_market_purchases']}")
    print(f"Open-market sales: {counts['open_market_sales']}")
    print(f"Automatic sales: {counts['automatic_sales']}")
    print(f"Planned sales: {counts['planned_sales']}")
    print(f"Option exercises: {counts['option_exercises']}")
    print(f"Awards/vesting: {counts['awards_vesting']}")
    print(f"Tax withholding: {counts['tax_withholding']}")
    print(f"Gifts/transfers: {counts['gifts_transfers']}")
    print(f"Conversions: {counts['conversions']}")
    print(f"Other: {counts['other']}")
    print(f"Known insider ownership: {snapshot.known_insider_shares}")
    print(f"Known insider percentage: {snapshot.known_insider_percent}")
    print(f"30-day activity: purchases={snapshot.open_market_purchases_30d}, sales={snapshot.open_market_sales_30d}")
    print(f"90-day activity: purchases={snapshot.open_market_purchases_90d}, sales={snapshot.open_market_sales_90d}")
    print(f"365-day activity: purchases={snapshot.open_market_purchases_365d}, sales={snapshot.open_market_sales_365d}")
    print(f"365-day values: purchase={snapshot.purchase_value_365d}, sale={snapshot.sale_value_365d}, net={snapshot.net_open_market_value_365d}")
    print(f"Automatic/planned sale value: automatic={snapshot.automatic_sale_value_365d}, planned={snapshot.planned_sale_value_365d}")
    print(f"Clusters: {len(clusters)}")
    print(f"Classification failures: {len(result.classification_failures)}")
    print(f"Provenance failures: {len(result.provenance_failures)}")
    if transactions:
        print("Sample transactions:")
        by_person = {person.person_id: person for person in people}
        for item in transactions[:5]:
            person = by_person.get(item.person_id)
            print(f"- Person: {person.full_name if person else item.person_id}")
            print(f"  Role: {person.primary_role if person else None}")
            print(f"  Date: {item.transaction_date}")
            print(f"  Type: {item.transaction_type}")
            print(f"  Shares: {item.shares}")
            print(f"  Price: {item.price}")
            print(f"  Value: {item.transaction_value}")
            print(f"  Post-transaction holdings: {item.shares_owned_after}")
            print(f"  Percent of holdings transacted: {item.percent_of_post_transaction_holdings}")
            print(f"  10b5-1: {item.is_10b5_1}")
            print(f"  Source: {item.source_url}")


def run(args: argparse.Namespace) -> int:
    tickers = [args.ticker.upper()] if args.ticker else list(DEFAULT_TICKERS)
    provider = SECSourceProvider(cache_root=args.cache_root, user_agent=args.user_agent, debug=args.debug)
    store = InsiderStore(args.store_root)
    today = date.today()
    from_date, to_date, default_lookback_applied, include_historical = resolve_date_window(args, today=today)
    for ticker in tickers:
        result = provider.acquire(ticker, forms=tuple(args.forms), from_date=from_date, to_date=to_date, force=args.force, dry_run=args.dry_run, include_historical=include_historical)
        if not args.dry_run:
            for parsed in result.parsed_filings:
                parsed.transactions = [reconcile_transaction(item) for item in parsed.transactions]
                counters = store.upsert_parsed(parsed)
                result.people_added += counters["people"]["added"]
                result.people_updated += counters["people"]["updated"]
                result.transactions_added += counters["transactions"]["added"]
                result.transactions_updated += counters["transactions"]["updated"]
                result.transactions_unchanged += counters["transactions"]["unchanged"]
                result.ownership_positions_added += counters["ownership"]["added"]
                result.ownership_positions_updated += counters["ownership"]["updated"]
                result.ownership_positions_unchanged += counters["ownership"]["unchanged"]
            as_of = to_date or today.isoformat()
            filing_ids = {item.accession_number for item in result.filings}
            scoped_transactions = [item for item in store.get_transactions(ticker) if item.filing_id in filing_ids]
            scoped_people = [item for item in store.list_people() if item.ticker.upper() == ticker and filing_ids.intersection(item.source_filing_ids)]
            scoped_positions = store.get_current_holdings(ticker, filing_ids=filing_ids)
            snapshot, clusters = build_alignment_snapshot(ticker, as_of, scoped_transactions, scoped_people, scoped_positions)
            store.upsert_alignment_snapshots([snapshot])
            store.upsert_clusters(clusters)
        _print_result(result, store, as_of_date=to_date or today.isoformat(), from_date=from_date, to_date=to_date, default_lookback_applied=default_lookback_applied)
        if args.debug and result.warnings:
            print("Warnings:")
            for warning in result.warnings[:20]:
                print(f"- {warning}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
