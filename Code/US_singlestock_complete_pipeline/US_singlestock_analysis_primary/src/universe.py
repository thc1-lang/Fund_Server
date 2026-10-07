from __future__ import annotations
import pandas as pd


def eligible_universe(df: pd.DataFrame, allowed: tuple[str, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    types = df["COM/ADR/Canadian"].fillna("").astype(str).str.strip()
    normalized_types = types.str.upper()
    normalized_allowed = {value.strip().upper() for value in allowed}
    ticker = df["Ticker"].fillna("").astype(str).str.strip()
    sector = df["Sector"].fillna("").astype(str).str.strip()
    industry = df["Industry"].fillna("").astype(str).str.strip()
    reasons = pd.Series("", index=df.index, dtype="object")
    reasons.loc[ticker.eq("")] = "blank ticker"
    reasons.loc[reasons.eq("") & sector.eq("")] = "blank sector"
    reasons.loc[reasons.eq("") & industry.eq("")] = "blank industry"
    excluded_type = ~normalized_types.isin(normalized_allowed)
    reasons.loc[reasons.eq("") & excluded_type] = "security type excluded: " + types
    eligible = df.loc[reasons.eq("")].copy()
    normalized_tickers = eligible["Ticker"].astype(str).str.strip().str.casefold()
    dupes = normalized_tickers.duplicated(keep=False)
    if dupes.any():
        values = sorted(eligible.loc[dupes, "Ticker"].unique())
        raise ValueError(f"Duplicate eligible tickers: {values}")
    excluded = df.loc[~reasons.eq("")].copy()
    excluded["Exclusion Reason"] = reasons.loc[excluded.index]
    return eligible, excluded
