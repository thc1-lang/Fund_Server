from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Protocol


@dataclass(frozen=True)
class PriceBar:
    ticker: str
    session_date: str
    adjusted_close: float
    adjusted_open: float | None = None


class MarketDataProvider(Protocol):
    name: str
    status: str

    def prices(self, ticker: str, start: str, end: str) -> list[PriceBar]: ...
    def benchmark_prices(self, benchmark: str, start: str, end: str) -> list[PriceBar]: ...
    def trading_days(self, start: str, end: str) -> list[str]: ...
    def corporate_actions(self, ticker: str) -> list[dict]: ...


class NullMarketDataProvider:
    name = "none"
    status = "MARKET_DATA_PROVIDER_NOT_CONFIGURED"

    def prices(self, ticker: str, start: str, end: str) -> list[PriceBar]: return []
    def benchmark_prices(self, benchmark: str, start: str, end: str) -> list[PriceBar]: return []
    def trading_days(self, start: str, end: str) -> list[str]: return []
    def corporate_actions(self, ticker: str) -> list[dict]: return []


class FixtureMarketDataProvider:
    """Offline provider used by tests; no network access is performed."""
    name = "fixture"
    status = "CONFIGURED_OFFLINE_FIXTURE"

    def __init__(self, prices: Iterable[PriceBar], benchmark_prices: Iterable[PriceBar] = (), actions: dict[str, list[dict]] | None = None):
        self._prices = list(prices)
        self._benchmark = list(benchmark_prices)
        self._actions = actions or {}

    @staticmethod
    def _between(rows: list[PriceBar], ticker: str, start: str, end: str) -> list[PriceBar]:
        return sorted([r for r in rows if r.ticker == ticker and start <= r.session_date <= end], key=lambda r: r.session_date)

    def prices(self, ticker: str, start: str, end: str) -> list[PriceBar]: return self._between(self._prices, ticker, start, end)
    def benchmark_prices(self, benchmark: str, start: str, end: str) -> list[PriceBar]: return self._between(self._benchmark, benchmark, start, end)
    def trading_days(self, start: str, end: str) -> list[str]:
        return sorted({r.session_date for r in self._prices + self._benchmark if start <= r.session_date <= end})
    def corporate_actions(self, ticker: str) -> list[dict]: return list(self._actions.get(ticker, []))
