from __future__ import annotations

from .market_data import MarketDataProvider
from .models import ForwardOutcome, HistoricalObservation


def _ret(rows):
    if len(rows) < 2 or rows[0].adjusted_close == 0: return None
    return rows[-1].adjusted_close / rows[0].adjusted_close - 1.0


def forward_outcome(observation: HistoricalObservation, provider: MarketDataProvider, benchmark: str, horizon_days: int, execution_lag: str = "next_trading_day_open") -> ForwardOutcome:
    # The provider owns the trading calendar. Empty results are an explicit
    # unavailable outcome, never a fabricated zero return.
    sessions = provider.trading_days(observation.information_cutoff, "2999-12-31")
    after = [d for d in sessions if d > observation.information_cutoff]
    if not after:
        return ForwardOutcome(observation.observation_id, observation.ticker, horizon_days, execution_lag, None, None, None, None, None, status="UNAVAILABLE", exclusion_reason="NO_MARKET_OUTCOME")
    start = after[0]
    end_index = min(horizon_days - 1, len(after) - 1)
    end = after[end_index]
    prices = provider.prices(observation.ticker, start, end)
    benchmark_prices = provider.benchmark_prices(benchmark, start, end)
    raw = _ret(prices); bench = _ret(benchmark_prices)
    excess = raw - bench if raw is not None and bench is not None else None
    status = "AVAILABLE" if raw is not None else "UNAVAILABLE"
    return ForwardOutcome(observation.observation_id, observation.ticker, horizon_days, execution_lag, start, end, raw, bench, excess, status=status, exclusion_reason=None if raw is not None else "NO_MARKET_OUTCOME")


def mark_overlapping_windows(outcomes: list[ForwardOutcome]) -> list[ForwardOutcome]:
    rows=sorted([x for x in outcomes if x.execution_date and x.end_date],key=lambda x:(x.execution_date,x.end_date))
    for i,row in enumerate(rows):
        row.overlap_flag=any(other is not row and other.execution_date < row.end_date and row.execution_date < other.end_date for other in rows)
    return outcomes
