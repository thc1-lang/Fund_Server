"""Command-line entry point for Component 5B proxy baselines."""

from __future__ import annotations

import argparse
import json

from .proxy_baseline import ProxyBaselineProvider
from .sec_ingestion import SECSourceProvider
from .store import InsiderStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acquire official SEC proxy ownership baselines")
    parser.add_argument("--ticker", action="append", required=True, help="Ticker to process; repeat for multiple issuers")
    parser.add_argument("--cache-root", default="artifacts/insider_intelligence/sec")
    parser.add_argument("--store-root", default="artifacts/insider_intelligence")
    parser.add_argument("--force", action="store_true", help="Refresh the SEC cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = ProxyBaselineProvider(SECSourceProvider(cache_root=args.cache_root), InsiderStore(args.store_root))
    results = provider.acquire_many(args.ticker, force=args.force)
    print(json.dumps({ticker: {"as_of_date": result.as_of_date, "baselines": len(result.baselines), "warnings": result.warnings} for ticker, result in results.items()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
