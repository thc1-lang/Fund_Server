"""Observed schema contract and central source-header mappings."""
from __future__ import annotations
from collections import Counter

COLUMN_MAP = {
    "company": "Company Name", "ticker": "Ticker", "sector": "Sector",
    "industry": "Industry", "exchange": "Exchange", "security_type": "COM/ADR/Canadian",
    "market_cap_mil": "Market Cap (mil)", "share_price": "Last Close",
    "book_value_per_share": "Book Value", "price_to_book": "Price/Book",
    "debt_equity_pct": "Debt/Equity Ratio",
}

REQUIRED_HEADERS = set(COLUMN_MAP.values()) | {
    "P/E (F1)", "P/E (F2)", "PEG Ratio", "F1 Consensus Est.", "F2 Consensus Est.",
    "This Yr`s Est.d Growth (F(1)/F(0))", "Common Equity ($mil)", "Annual Sales ($mil)",
    "Shares Outstanding (mil)", "Avg Volume", "52 Week High", "52 Week Low",
}

IDENTITY_FIELDS = {
    "Company Name", "Ticker", "Sector", "Industry", "Exchange", "COM/ADR/Canadian",
    "Optionable", "S&P 500 - ETF",
}
DATE_FIELDS = {
    "Last Reported Qtr (yyyymm)": "yyyymm", "Last Reported Fiscal Yr  (yyyymm)": "yyyymm",
    "Last EPS Report Date (yyyymmdd)": "yyyymmdd", "Next EPS Report Date  (yyyymmdd)": "yyyymmdd",
}

UNAVAILABLE_CALCULATIONS = {
    "enterprise_value": "No complete enterprise-value or cash balance field.",
    "ev_ebit": "Enterprise value unavailable.", "ev_ebitda": "Enterprise value unavailable.",
    "net_debt": "No cash-and-equivalents balance.",
    "free_cash_flow": "No capital-expenditure field; Cash Flow definition is ambiguous.",
    "interest_coverage": "No interest-expense field.",
    "roic_nopat": "Insufficient tax and invested-capital detail.",
    "altman_z": "Missing total assets, retained earnings, and other components.",
    "piotroski_f": "Insufficient multi-period balance-sheet and cash-flow inputs.",
    "dupont": "No total-assets field and insufficient component detail.",
    "accruals": "No defined operating cash flow and average total assets.",
    "time_series_indicators": "No price history; volatility, Sharpe, RSI and moving averages unavailable.",
}


def validate_headers(headers: list[str]) -> None:
    missing = sorted(REQUIRED_HEADERS - set(headers))
    if missing:
        raise ValueError(f"Incompatible source schema; missing required headers: {missing}")
    dupes = sorted(header for header, count in Counter(headers).items() if count > 1)
    if dupes:
        raise ValueError(f"Incompatible source schema; duplicate headers: {dupes}")
