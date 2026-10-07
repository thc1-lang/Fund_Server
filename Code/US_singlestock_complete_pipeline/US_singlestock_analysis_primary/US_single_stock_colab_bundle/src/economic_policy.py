"""Conservative scoring policy; source values remain available for audit.

No timestamps/period tags or vendor definitions are supplied for these ratios.
They remain calculated diagnostics, but cannot add alpha evidence by default.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


DIAGNOSTIC_ONLY_SIGNALS = {
    # The Primary model is a single-date fundamental snapshot.  Keep vendor
    # history and market-path fields in the audit, never in a score.
    "PEG Ratio": "Vendor growth denominator may use a historical EPS comparison",
    "This Yr`s Est.d Growth (F(1)/F(0))": "Compares a forecast with a prior fiscal year",
    "Sales Growth F(0)/F(-1)": "Compares two fiscal years",
    "Last EPS Surprise (%)": "Past earnings event, not current financial condition",
    "Previous EPS Surprise (%)": "Past earnings event",
    "Avg EPS Surprise (Last 4 Qtrs)": "Four-quarter historical aggregate",
    "% Change F1 Est. (4 weeks)": "Four-week estimate revision",
    "% Change F2 Est. (4 weeks)": "Four-week estimate revision",
    "% Change Q0 Est. (4 weeks)": "Four-week estimate revision",
    "% Change Q1 Est. (4 weeks)": "Four-week estimate revision",
    "% Change Q2 Est. (4 weeks)": "Four-week estimate revision",
    "% Change LT Growth Est. (4 weeks)": "Four-week estimate revision",
    "% Price Change (1 Week)": "Historical price return",
    "% Price Change (4 Weeks)": "Historical price return",
    "% Price Change (12 Weeks)": "Historical price return",
    "% Price Change (YTD)": "Historical price return",
    "Relative Price Change (YTD)": "Historical relative price return",
    "distance_52w_high": "Historical price-range position",
    "momentum_acceleration": "Historical price momentum",
    "range_width_52w": "Historical price range",
    "upgrade_downgrade_count_balance": "Historical broker-rating changes",
    "gross_margin": "Annual sales and COGS periods are not verified",
    "ebitda_margin": "Annual sales and EBITDA periods are not verified",
    "ebit_margin": "EBIT is not vendor operating income; periods are not verified",
    "pretax_margin": "Annual sales and pretax-income periods are not verified",
    "net_income_margin": "Use vendor net margin once; derived periods are not verified",
    "cash_flow_margin": "Generic cash flow is not defined operating or free cash flow",
    "forward_sales_growth": "Forward fiscal sales and historical sales periods are not aligned",
    "growth_adjusted_pe_f1": "Vendor F1/F0 growth base semantics are not verified",
    "eps_growth_acceleration": "Vendor F1/F0 growth and positive-base F2/F1 are not comparable",
}


BUSINESS_TYPES = {"STANDARD_OPERATING_COMPANY", "SPECIALIST_FINANCIAL", "MORTGAGE_FINANCE", "EQUITY_REIT", "REAL_ESTATE"}


def classify_business(df: pd.DataFrame, overrides: dict[str, str] | None = None, *,
                      financial_sectors: tuple[str, ...] = ("Finance", "Financials", "Financial Services")) -> pd.DataFrame:
    """Use specific industries before broad vendor sectors; never invent a type."""
    industry = df.get("Industry", pd.Series("", index=df.index)).astype(str).str.casefold()
    sector = df.get("Sector", pd.Series("", index=df.index)).astype(str).str.casefold()
    mortgage = industry.str.contains("mortgage", regex=False)
    ambiguous_reit = industry.eq("reit and equity trust")
    reit = industry.str.contains("reit", regex=False) & ~mortgage & ~ambiguous_reit
    real_estate = industry.str.contains("real estate", regex=False) & ~mortgage & ~reit
    specialist = industry.str.contains(
        r"\bbanks?\b|savings and loan|insurance|securities|investment bank|investment management|"
        r"consumer loans|sbic|investment funds", regex=True
    ) & ~mortgage & ~reit & ~real_estate
    ambiguous = sector.isin({str(s).strip().casefold() for s in financial_sectors}) & ~(
        mortgage | reit | real_estate | specialist | ambiguous_reit
    )
    kind = pd.Series(np.select(
        [mortgage, reit, real_estate, specialist, ambiguous_reit, ambiguous],
        ["MORTGAGE_FINANCE", "EQUITY_REIT", "REAL_ESTATE", "SPECIALIST_FINANCIAL", "UNCLASSIFIED_REIT", "UNCLASSIFIED_FINANCIAL"],
        default="STANDARD_OPERATING_COMPANY"), index=df.index)
    overrides = {str(k).strip().upper(): str(v).strip().upper() for k, v in (overrides or {}).items()}
    if any(v not in BUSINESS_TYPES for v in overrides.values()):
        raise ValueError("Invalid business-type override")
    tickers = df.get("Ticker", pd.Series("", index=df.index)).astype(str).str.strip().str.upper()
    manual = tickers.map(overrides)
    kind = manual.combine_first(kind)
    verified = ~kind.str.startswith("UNCLASSIFIED")
    return pd.DataFrame({
        "economic_business_type": kind,
        "economic_classification_verified": verified,
        "economic_classification_note": np.select([manual.notna(), ~verified],
            ["CONTROL_PANEL_OVERRIDE", "Broad industry/sector label requires review before selection"], default="INDUSTRY_RULE"),
        "ordinary_liquidity_applicable": kind.eq("STANDARD_OPERATING_COMPANY"),
        "ordinary_leverage_applicable": kind.isin(["STANDARD_OPERATING_COMPANY", "EQUITY_REIT", "REAL_ESTATE"]),
    }, index=df.index)


def signal_applicability(df: pd.DataFrame, factor: str, family: str) -> pd.Series:
    """Structural exclusions, independent of whether a row happens to have data."""
    applicable = pd.Series(True, index=df.index)
    if family == "LIQUIDITY_AND_EFFICIENCY":
        applicable &= df.get("ordinary_liquidity_applicable", ~df.get("Financial Company Liquidity Exclusion", pd.Series(False, index=df.index)).fillna(False)).fillna(False)
    if family == "BALANCE_SHEET_AND_LEVERAGE" and "ordinary_leverage_applicable" in df:
        applicable &= df["ordinary_leverage_applicable"].fillna(False)
    if factor == "payout_sustainability" and "economic_business_type" in df:
        # EPS payout is inappropriate for REITs without FFO/AFFO.
        applicable &= ~df["economic_business_type"].isin(["EQUITY_REIT", "MORTGAGE_FINANCE"])
    return applicable
