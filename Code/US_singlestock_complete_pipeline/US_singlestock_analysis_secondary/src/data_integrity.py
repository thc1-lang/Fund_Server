"""Conservative, row-level checks on the current annual source snapshot.

Flags identify records needing source review. They cannot reconstruct the
original filing vintage or turn a fiscal-period date into an availability date.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


AUDIT_COLUMNS = [
    "Category", "Ticker", "Fiscal Year", "Fiscal Period End", "Market Cap",
    "Shares Outstanding", "Year End Price", "Price Currency", "Revenue Filed",
    "Integrity Flags", "Integrity Pass", "Missing Essential Fields",
    "Provenance Missing Fields", "Provenance Complete", "Research Traceable",
]


def _number(value: Any) -> float:
    try:
        number = float(str(value).replace(",", ""))
        return number if np.isfinite(number) else np.nan
    except (TypeError, ValueError):
        return np.nan


def _text(value: Any) -> str:
    return str(value).strip() if pd.notna(value) else ""


def audit_source_rows(category: str, source: pd.DataFrame, primary: pd.DataFrame | None = None,
                      as_of: str | None = None) -> pd.DataFrame:
    """Audit every supplied annual row, retaining its original order and flags."""
    if source.empty:
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    frame = source.reset_index(drop=True).copy()
    frame["Ticker"] = frame["Ticker"].fillna("").astype(str).str.strip().str.upper()
    primary_caps = {}
    primary_types = {}
    if primary is not None and {"Ticker", "Market Cap (mil)"} <= set(primary):
        primary_caps = {str(r["Ticker"]).upper(): _number(r["Market Cap (mil)"]) * 1_000_000
                        for _, r in primary.iterrows()}
    if primary is not None and {"Ticker", "COM/ADR/Canadian"} <= set(primary):
        primary_types = {str(r["Ticker"]).upper(): str(r["COM/ADR/Canadian"]).upper()
                         for _, r in primary.iterrows()}
    dates = pd.to_datetime(frame.get("Fiscal Period End"), errors="coerce", utc=True)
    years = pd.to_numeric(frame.get("Fiscal Year"), errors="coerce")
    caps = pd.to_numeric(frame.get("Market Cap"), errors="coerce")
    shares = pd.to_numeric(frame.get("Shares Outstanding", pd.Series(np.nan, index=frame.index)), errors="coerce")
    prices = pd.to_numeric(frame.get("Year End Price", pd.Series(np.nan, index=frame.index)), errors="coerce")
    filed = pd.to_datetime(frame.get("Revenue Filed", pd.Series(index=frame.index, dtype=object)), errors="coerce", utc=True)
    cutoff = pd.Timestamp(as_of, tz="UTC") if as_of else None
    flags: list[list[str]] = [[] for _ in range(len(frame))]
    for ticker, indices in frame.groupby("Ticker", sort=False).groups.items():
        ordered = sorted(indices, key=lambda i: (years.iloc[i] if pd.notna(years.iloc[i]) else -1, i))
        seen_periods: dict[str, int] = {}
        for i in ordered:
            if pd.notna(dates.iloc[i]):
                period = dates.iloc[i].date().isoformat()
                if period in seen_periods and years.iloc[seen_periods[period]] != years.iloc[i]:
                    flags[i].append("DUPLICATE_PERIOD_END_ACROSS_FISCAL_YEARS")
                    flags[seen_periods[period]].append("DUPLICATE_PERIOD_END_ACROSS_FISCAL_YEARS")
                seen_periods[period] = i
            if pd.notna(shares.iloc[i]) and pd.notna(prices.iloc[i]) and pd.notna(caps.iloc[i]) and shares.iloc[i] > 0 and prices.iloc[i] > 0:
                implied = shares.iloc[i] * prices.iloc[i]
                if abs(caps.iloc[i] / implied - 1) > 0.05:
                    flags[i].append("MARKET_CAP_PRICE_SHARES_MISMATCH")
            if pd.notna(filed.iloc[i]) and pd.notna(dates.iloc[i]) and filed.iloc[i] < dates.iloc[i]:
                flags[i].append("FILING_BEFORE_PERIOD_END")
            if cutoff is not None and pd.notna(filed.iloc[i]) and filed.iloc[i] > cutoff:
                flags[i].append("FILING_AFTER_ANALYSIS_DATE")
            price_currency = _text(frame.iloc[i].get("Price Currency", "")).upper()
            cap_currency = _text(frame.iloc[i].get("Market Cap Currency", "")).upper()
            statement_currency = _text(frame.iloc[i].get("Statement Currency", "")).upper()
            if price_currency and cap_currency and price_currency != cap_currency:
                flags[i].append("PRICE_MARKET_CAP_CURRENCY_MISMATCH")
            if price_currency and statement_currency and price_currency != statement_currency:
                flags[i].append("STATEMENT_PRICE_CURRENCY_REVIEW")
            if "ADR" in primary_types.get(ticker, "") and not _text(frame.iloc[i].get("ADR Ratio", "")):
                flags[i].append("ADR_RATIO_MISSING")
        unique = []
        for i in ordered:
            if unique and years.iloc[i] == years.iloc[unique[-1]]:
                continue
            unique.append(i)
        for before, after in zip(unique, unique[1:]):
            for series, label in ((shares, "SHARE_COUNT_JUMP_20X"), (caps, "MARKET_CAP_JUMP_20X")):
                a, b = series.iloc[before], series.iloc[after]
                if pd.notna(a) and pd.notna(b) and min(a, b) > 0 and max(a, b) / min(a, b) >= 20:
                    flags[before].append(label)
                    flags[after].append(label)
        if ticker in primary_caps and pd.notna(primary_caps[ticker]) and primary_caps[ticker] > 0:
            latest = max(ordered, key=lambda i: (years.iloc[i] if pd.notna(years.iloc[i]) else -1, i))
            cap = caps.iloc[latest]
            if pd.notna(cap) and cap > 0 and max(cap, primary_caps[ticker]) / min(cap, primary_caps[ticker]) >= 20:
                flags[latest].append("LATEST_HISTORICAL_PRIMARY_CAP_20X_REVIEW")
    result = pd.DataFrame({column: frame[column] if column in frame else pd.Series("", index=frame.index)
                           for column in AUDIT_COLUMNS[1:9]})
    result.insert(0, "Category", category)
    result["Integrity Flags"] = ["; ".join(dict.fromkeys(items)) for items in flags]
    result["Integrity Pass"] = result["Integrity Flags"].eq("")
    essential = ("Ticker", "Fiscal Year", "Fiscal Period End", "Revenue", "Net Income",
                 "Free Cash Flow", "Market Cap", "Price Date")
    result["Missing Essential Fields"] = [
        "; ".join(field for field in essential if field not in frame or
                  pd.isna(frame.iloc[i][field]) or str(frame.iloc[i][field]).strip() == "")
        for i in range(len(frame))
    ]
    # This source does not yet persist these required fields for each selected
    # fact. Keep the handoff closed until the complete lineage exists.
    required = ("CIK", "Accession", "Form", "Accepted At", "Statement Currency", "ADR Ratio")
    complete = pd.Series(True, index=frame.index)
    missing_provenance = [[] for _ in range(len(frame))]
    for field in required:
        present = (frame[field].notna() & frame[field].astype(str).str.strip().ne("")) if field in frame else pd.Series(False, index=frame.index)
        complete &= present
        for i in frame.index[~present]:
            missing_provenance[i].append(field)
    result["Provenance Missing Fields"] = ["; ".join(fields) for fields in missing_provenance]
    result["Provenance Complete"] = complete
    # SEC CompanyFacts supplies filing dates, accessions, concepts and units,
    # but cannot verify the Primary vendor vintage or a current TTM snapshot.
    research_fields = ("CIK", "Accession", "Form", "Revenue Filed", "Statement Currency", "Fact Provenance JSON")
    research = pd.Series(True, index=frame.index)
    for field in research_fields:
        present = (frame[field].notna() & frame[field].astype(str).str.strip().ne("")) if field in frame else pd.Series(False, index=frame.index)
        research &= present
    result["Research Traceable"] = research
    return result[AUDIT_COLUMNS]


def apply_integrity_gate(analysis: pd.DataFrame, audit: pd.DataFrame, *, short: bool = False) -> pd.DataFrame:
    """Reject anomalous or incomplete latest source rows without changing scores."""
    result = analysis.copy()
    gate = "short_source_integrity_pass" if short else "source_integrity_gate_pass"
    essential_gate = "short_source_essential_pass" if short else "source_essential_gate_pass"
    eligible = "short_eligible" if short else "eligible"
    reasons = "short_fail_reasons" if short else "gate_fail_reasons"
    clean_by_ticker = audit.groupby("Ticker")["Integrity Pass"].all() if not audit.empty else pd.Series(dtype=bool)
    if not audit.empty and {"Fiscal Period End", "Missing Essential Fields"} <= set(audit):
        dated = audit.copy()
        dated["_period"] = pd.to_datetime(dated["Fiscal Period End"], errors="coerce")
        latest = dated.sort_values("_period", na_position="first").groupby("Ticker", sort=False).tail(1)
        essential_by_ticker = latest.set_index("Ticker")["Missing Essential Fields"].fillna("").astype(str).str.strip().eq("") & latest.set_index("Ticker")["_period"].notna()
    else:
        essential_by_ticker = pd.Series(dtype=bool)
    result[gate] = result["Ticker"].map(clean_by_ticker).fillna(False).astype(bool)
    result[essential_gate] = result["Ticker"].map(essential_by_ticker).fillna(False).astype(bool)
    result[eligible] = result[eligible].fillna(False).astype(bool) & result[gate] & result[essential_gate]
    old = result[reasons].fillna("").astype(str)
    result[reasons] = old.where(result[gate], old.str.rstrip("; ") + "; source_integrity")
    old = result[reasons]
    result[reasons] = old.where(result[essential_gate], old.str.rstrip("; ") + "; source_essential")
    result[reasons] = result[reasons].str.lstrip("; ")
    return result
