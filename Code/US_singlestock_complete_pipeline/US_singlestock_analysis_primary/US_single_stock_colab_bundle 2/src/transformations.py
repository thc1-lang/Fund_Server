"""Auditable, sign-safe feature construction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import numpy as np
import pandas as pd
from .normalization import apply_unit_normalization


def number(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.replace("", np.nan), errors="coerce").replace([np.inf, -np.inf], np.nan)


def safe_div(num: pd.Series, den: pd.Series, *, positive_den: bool = False) -> pd.Series:
    n, d = number(num), number(den)
    valid = d.notna() & n.notna() & (d != 0)
    if positive_den:
        valid &= d > 0
    return (n / d).where(valid)


def percent_to_decimal(s: pd.Series) -> pd.Series:
    return number(s) / 100.0


def natural_log(s: pd.Series) -> pd.Series:
    x = number(s)
    out = pd.Series(np.nan, index=x.index, dtype=float)
    valid = x > 0
    out.loc[valid] = np.log(x.loc[valid])
    return out


def signed_log(s: pd.Series) -> pd.Series:
    x = number(s)
    return np.sign(x) * np.log1p(np.abs(x))


def inverse_positive(s: pd.Series) -> pd.Series:
    x = number(s)
    return (1.0 / x).where(x > 0)


def derive_eg2(f1: pd.Series, f2: pd.Series, require_positive: bool = True) -> tuple[pd.Series, pd.Series]:
    a, b = number(f1), number(f2)
    valid = a.notna() & b.notna() & (a != 0)
    reason = pd.Series("", index=a.index, dtype="object")
    reason.loc[a.isna() | b.isna()] = "NO DATA: missing F1 or F2 consensus"
    reason.loc[a.eq(0)] = "NOT MEANINGFUL: zero F1 denominator"
    if require_positive:
        bad_sign = valid & ((a <= 0) | (b <= 0))
        reason.loc[bad_sign] = "NOT MEANINGFUL: nonpositive forward EPS"
        valid &= (a > 0) & (b > 0)
    return ((b / a - 1) * 100).where(valid), reason.where(~valid, "")


@dataclass(frozen=True)
class FeatureSpec:
    inputs: tuple[str, ...]
    formula: str
    units: str
    valid_when: str
    family: str | None
    direction: str
    duplicate_group: str
    fn: Callable[[pd.DataFrame], pd.Series]


def _ratio(n: str, d: str, positive_den: bool = True, scale: float = 1.0):
    return lambda df: safe_div(df[n], df[d], positive_den=positive_den) * scale


DERIVED_FEATURES: dict[str, FeatureSpec] = {
    "debt_equity_ratio": FeatureSpec(("Debt/Equity Ratio",), "configured ratio; identity transformation", "ratio", "finite and non-negative", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "capital_leverage", lambda d: number(d["debt_equity_ratio__normalized"]).where(number(d["debt_equity_ratio__normalized"]) >= 0)),
    "debt_total_capital_ratio": FeatureSpec(("Debt/Total Capital",), "configured percent / 100", "ratio", "finite and non-negative", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "capital_leverage", lambda d: number(d["debt_total_capital_ratio__normalized"]).where(number(d["debt_total_capital_ratio__normalized"]) >= 0)),
    "roe_valid": FeatureSpec(("Current ROE (TTM)", "Common Equity ($mil)"), "reported_roe when common_equity > 0", "percentage points", "common equity > 0", "PROFITABILITY_AND_RETURNS", "higher_better", "returns", lambda d: number(d["Current ROE (TTM)"]).where(number(d["Common Equity ($mil)"]) > 0)),
    # Three independent earnings-yield/profitability signals. The legacy horizon
    # reciprocals remain auditable but are not scored separately.
    "earnings_yield_f1": FeatureSpec(("P/E (F1)",), "1 / pe_f1", "decimal", "pe_f1 > 0", None, "confidence_only", "legacy_pe_yield", lambda d: inverse_positive(d["P/E (F1)"])),
    "earnings_yield_f2": FeatureSpec(("P/E (F2)",), "1 / pe_f2", "decimal", "pe_f2 > 0", None, "confidence_only", "legacy_pe_yield", lambda d: inverse_positive(d["P/E (F2)"])),
    "earnings_yield_trailing": FeatureSpec(("P/E (Trailing 12 Months)",), "1 / trailing_pe", "decimal", "trailing_pe > 0", None, "confidence_only", "legacy_pe_yield", lambda d: inverse_positive(d["P/E (Trailing 12 Months)"])),
    "earnings_yield_pe_valid": FeatureSpec(("P/E (F1)", "P/E (F2)", "P/E (Trailing 12 Months)"), "mean(1/valid positive P/E horizons)", "decimal", "at least one P/E > 0", "VALUATION", "higher_better", "earnings_value", lambda d: pd.concat([inverse_positive(d[c]) for c in ("P/E (F1)", "P/E (F2)", "P/E (Trailing 12 Months)")], axis=1).mean(axis=1)),
    "signed_trailing_eps_yield": FeatureSpec(("12 Mo Trailing EPS", "Last Close"), "trailing_eps / price", "decimal", "price > 0; EPS may be signed", "VALUATION", "higher_better", "earnings_value", lambda d: safe_div(d["12 Mo Trailing EPS"], d["Last Close"], positive_den=True)),
    "forward_eps_profitability": FeatureSpec(("F1 Consensus Est.", "F2 Consensus Est.", "Last Close"), "mean(F1 EPS,F2 EPS) / price", "decimal", "price > 0; forward EPS may be signed", "VALUATION", "higher_better", "earnings_value", lambda d: safe_div(pd.concat([number(d["F1 Consensus Est."]), number(d["F2 Consensus Est."])], axis=1).mean(axis=1), d["Last Close"], positive_den=True)),
    "book_yield": FeatureSpec(("Price/Book",), "1 / price_book", "decimal", "price_book > 0", "VALUATION", "higher_better", "price_book_value", lambda d: inverse_positive(d["Price/Book"])),
    "sales_yield": FeatureSpec(("Price/Sales",), "1 / price_sales", "decimal", "price_sales > 0", "VALUATION", "higher_better", "price_sales_value", lambda d: inverse_positive(d["Price/Sales"])),
    "cash_flow_yield": FeatureSpec(("Price/Cash Flow",), "1 / price_cash_flow", "decimal", "multiple > 0", "VALUATION", "higher_better", "price_cash_flow_value", lambda d: inverse_positive(d["Price/Cash Flow"])),
    "growth_adjusted_pe_f1": FeatureSpec(("P/E (F1)", "This Yr`s Est.d Growth (F(1)/F(0))"), "pe_f1 / eg1_pp", "multiple", "both > 0", "VALUATION", "lower_better", "growth_adjusted_value", lambda d: safe_div(d["P/E (F1)"], d["This Yr`s Est.d Growth (F(1)/F(0))"], positive_den=True).where(number(d["P/E (F1)"]) > 0)),
    "forward_sales_growth": FeatureSpec(("F(1) Consensus Sales Est. ($mil)", "Annual Sales ($mil)"), "forward_sales_estimate / annual_sales - 1", "decimal", "both > 0", "GROWTH", "higher_better", "sales_growth", lambda d: (safe_div(d["F(1) Consensus Sales Est. ($mil)"], d["Annual Sales ($mil)"], positive_den=True) - 1).where(number(d["F(1) Consensus Sales Est. ($mil)"]) > 0)),
    "forward_pe_change": FeatureSpec(("P/E (F1)", "P/E (F2)"), "pe_f2 / pe_f1 - 1", "decimal", "both > 0", None, "confidence_only", "pe_curve", lambda d: (safe_div(d["P/E (F2)"], d["P/E (F1)"], positive_den=True) - 1).where(number(d["P/E (F2)"]) > 0)),
    "forward_pe_spread": FeatureSpec(("P/E (F1)", "P/E (F2)"), "pe_f2 - pe_f1", "multiple", "finite", None, "confidence_only", "pe_curve", lambda d: number(d["P/E (F2)"]) - number(d["P/E (F1)"])),
    "gross_profit_mil": FeatureSpec(("Annual Sales ($mil)", "Cost of Goods Sold ($mil)"), "sales - cogs", "$mil", "finite", None, "confidence_only", "gross_profit", lambda d: number(d["Annual Sales ($mil)"]) - number(d["Cost of Goods Sold ($mil)"])),
    "gross_margin": FeatureSpec(("Annual Sales ($mil)", "Cost of Goods Sold ($mil)"), "(sales-cogs)/sales", "decimal", "sales > 0", "PROFITABILITY_AND_RETURNS", "higher_better", "operating_margins", lambda d: (number(d["Annual Sales ($mil)"]) - number(d["Cost of Goods Sold ($mil)"])) / number(d["Annual Sales ($mil)"]).where(number(d["Annual Sales ($mil)"]) > 0)),
    "ebitda_margin": FeatureSpec(("EBITDA ($mil)", "Annual Sales ($mil)"), "ebitda/sales", "decimal", "sales > 0", "PROFITABILITY_AND_RETURNS", "higher_better", "operating_margins", _ratio("EBITDA ($mil)", "Annual Sales ($mil)")),
    "ebit_margin": FeatureSpec(("EBIT ($mil)", "Annual Sales ($mil)"), "ebit/sales", "decimal", "sales > 0", "PROFITABILITY_AND_RETURNS", "higher_better", "operating_margins", _ratio("EBIT ($mil)", "Annual Sales ($mil)")),
    "pretax_margin": FeatureSpec(("Pretax Income ($mil)", "Annual Sales ($mil)"), "pretax/sales", "decimal", "sales > 0", "PROFITABILITY_AND_RETURNS", "higher_better", "operating_margins", _ratio("Pretax Income ($mil)", "Annual Sales ($mil)")),
    "net_income_margin": FeatureSpec(("Net Income  ($mil)", "Annual Sales ($mil)"), "net_income/sales", "decimal", "sales > 0", "PROFITABILITY_AND_RETURNS", "higher_better", "operating_margins", _ratio("Net Income  ($mil)", "Annual Sales ($mil)")),
    "cash_flow_margin": FeatureSpec(("Cash Flow ($mil)", "Annual Sales ($mil)"), "generic_cash_flow/sales", "decimal", "sales > 0", "EARNINGS_QUALITY", "higher_better", "cash_flow_margin", _ratio("Cash Flow ($mil)", "Annual Sales ($mil)")),
    "cash_conversion": FeatureSpec(("Cash Flow ($mil)", "Net Income  ($mil)"), "generic_cash_flow/net_income", "ratio", "net_income > 0", "EARNINGS_QUALITY", "target_range", "cash_conversion", _ratio("Cash Flow ($mil)", "Net Income  ($mil)")),
    "long_term_debt_equity": FeatureSpec(("Long Term Debt ($mil)", "Common Equity ($mil)"), "long_term_debt/common_equity", "ratio", "equity > 0", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "capital_leverage", _ratio("Long Term Debt ($mil)", "Common Equity ($mil)")),
    "long_term_debt_ebitda": FeatureSpec(("Long Term Debt ($mil)", "EBITDA ($mil)"), "long_term_debt/ebitda", "ratio", "ebitda > 0", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "debt_coverage_proxy", _ratio("Long Term Debt ($mil)", "EBITDA ($mil)")),
    "long_term_debt_ebit": FeatureSpec(("Long Term Debt ($mil)", "EBIT ($mil)"), "long_term_debt/ebit", "ratio", "ebit > 0", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "debt_coverage_proxy", _ratio("Long Term Debt ($mil)", "EBIT ($mil)")),
    "preferred_common_equity": FeatureSpec(("Preferred Equity ($mil)", "Common Equity ($mil)"), "preferred/common_equity", "ratio", "equity > 0", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "preferred_equity", _ratio("Preferred Equity ($mil)", "Common Equity ($mil)")),
    "working_capital_mil": FeatureSpec(("Current Assets  ($mil)", "Current Liabilities ($mil)"), "current_assets-current_liabilities", "$mil", "finite", None, "confidence_only", "working_capital", lambda d: number(d["Current Assets  ($mil)"]) - number(d["Current Liabilities ($mil)"])),
    "working_capital_to_sales": FeatureSpec(("Current Assets  ($mil)", "Current Liabilities ($mil)", "Annual Sales ($mil)"), "(current_assets-current_liabilities)/sales", "ratio", "sales > 0", "LIQUIDITY_AND_EFFICIENCY", "target_range", "working_capital", lambda d: (number(d["Current Assets  ($mil)"]) - number(d["Current Liabilities ($mil)"])) / number(d["Annual Sales ($mil)"]).where(number(d["Annual Sales ($mil)"]) > 0)),
    "current_liabilities_assets": FeatureSpec(("Current Liabilities ($mil)", "Current Assets  ($mil)"), "current_liabilities/current_assets", "ratio", "assets > 0", None, "confidence_only", "current_balance", _ratio("Current Liabilities ($mil)", "Current Assets  ($mil)")),
    "receivables_days": FeatureSpec(("Receivables ($mil)", "Annual Sales ($mil)"), "receivables/sales*365", "days", "sales > 0", "LIQUIDITY_AND_EFFICIENCY", "lower_better", "asset_efficiency", _ratio("Receivables ($mil)", "Annual Sales ($mil)", scale=365)),
    "inventory_days": FeatureSpec(("Inventory ($mil)", "Cost of Goods Sold ($mil)"), "inventory/cogs*365", "days", "cogs > 0", "LIQUIDITY_AND_EFFICIENCY", "lower_better", "inventory_efficiency", _ratio("Inventory ($mil)", "Cost of Goods Sold ($mil)", scale=365)),
    "intangibles_equity": FeatureSpec(("Intangibles ($mil)", "Common Equity ($mil)"), "intangibles/common_equity", "ratio", "equity > 0", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "intangibles", _ratio("Intangibles ($mil)", "Common Equity ($mil)")),
    "intangibles_market_cap": FeatureSpec(("Intangibles ($mil)", "Market Cap (mil)"), "intangibles/market_cap", "ratio", "market_cap > 0", "BALANCE_SHEET_AND_LEVERAGE", "lower_better", "intangibles", _ratio("Intangibles ($mil)", "Market Cap (mil)")),
    "average_daily_dollar_volume": FeatureSpec(("Avg Volume", "Last Close"), "avg_volume*last_close", "$/day", "both > 0", "TRADABILITY", "higher_better", "liquidity_scale", lambda d: (number(d["Avg Volume"]) * number(d["Last Close"])).where((number(d["Avg Volume"]) > 0) & (number(d["Last Close"]) > 0))),
    "sales_per_share": FeatureSpec(("Annual Sales ($mil)", "Shares Outstanding (mil)"), "sales/shares", "$/share", "shares > 0", None, "confidence_only", "sales_per_share", _ratio("Annual Sales ($mil)", "Shares Outstanding (mil)")),
    "ebitda_per_share": FeatureSpec(("EBITDA ($mil)", "Shares Outstanding (mil)"), "ebitda/shares", "$/share", "shares > 0", None, "confidence_only", "ebitda_per_share", _ratio("EBITDA ($mil)", "Shares Outstanding (mil)")),
    "dividend_payout_ratio": FeatureSpec(("Dividend ", "12 Mo Trailing EPS"), "dividend/trailing_eps", "ratio", "eps > 0", "SHAREHOLDER_AND_YIELD", "target_range", "payout", _ratio("Dividend ", "12 Mo Trailing EPS")),
    "dividend_yield_reconstructed": FeatureSpec(("Dividend ", "Last Close"), "dividend/last_close", "decimal", "price > 0", None, "confidence_only", "dividend_yield", _ratio("Dividend ", "Last Close")),
    "distance_52w_high": FeatureSpec(("Last Close", "52 Week High"), "last_close/high-1", "decimal", "high > 0", "MARKET_BEHAVIOUR", "higher_better", "range_position", lambda d: safe_div(d["Last Close"], d["52 Week High"], positive_den=True) - 1),
    "distance_52w_low": FeatureSpec(("Last Close", "52 Week Low"), "last_close/low-1", "decimal", "low > 0", None, "confidence_only", "range_position", lambda d: safe_div(d["Last Close"], d["52 Week Low"], positive_den=True) - 1),
    "range_position_reconstructed": FeatureSpec(("Last Close", "52 Week Low", "52 Week High"), "(close-low)/(high-low)", "ratio", "high > low", None, "confidence_only", "range_position", lambda d: safe_div(number(d["Last Close"]) - number(d["52 Week Low"]), number(d["52 Week High"]) - number(d["52 Week Low"]), positive_den=True)),
    "momentum_acceleration": FeatureSpec(("% Price Change (4 Weeks)", "% Price Change (12 Weeks)"), "ret_4w-ret_12w/3", "percentage points", "finite; overlapping-horizon approximation", "MARKET_BEHAVIOUR", "higher_better", "momentum_acceleration", lambda d: number(d["% Price Change (4 Weeks)"]) - number(d["% Price Change (12 Weeks)"]) / 3),
    "beta_distance_one": FeatureSpec(("Beta",), "abs(beta-1)", "absolute beta", "finite", "MARKET_BEHAVIOUR", "two_sided_risk", "beta_risk", lambda d: abs(number(d["Beta"]) - 1)),
    "absolute_beta": FeatureSpec(("Beta",), "abs(beta)", "absolute beta", "finite", "RISK_AND_STABILITY", "lower_better", "market_risk", lambda d: abs(number(d["Beta"]))),
    "range_width_52w": FeatureSpec(("52 Week High", "52 Week Low", "Last Close"), "(high-low)/last_close", "ratio", "high >= low and close > 0", "RISK_AND_STABILITY", "lower_better", "market_risk", lambda d: safe_div(number(d["52 Week High"]) - number(d["52 Week Low"]), d["Last Close"], positive_den=True).where(number(d["52 Week High"]) >= number(d["52 Week Low"]))),
    "target_price_upside": FeatureSpec(("Average Target Price", "Last Close"), "target/close-1", "decimal", "both > 0", "BROKER_AND_TARGET", "higher_better", "target_upside", lambda d: (safe_div(d["Average Target Price"], d["Last Close"], positive_den=True) - 1).where(number(d["Average Target Price"]) > 0)),
    "net_rating_balance": FeatureSpec(("% Rating Strong Buy or Buy", "% Rating Strong Sell or Sell"), "bullish_pct/100-bearish_pct/100", "decimal", "finite", "BROKER_AND_TARGET", "higher_better", "broker_rating", lambda d: percent_to_decimal(d["% Rating Strong Buy or Buy"]) - percent_to_decimal(d["% Rating Strong Sell or Sell"])),
    "upgrade_downgrade_count_balance": FeatureSpec(("# Rating Upgrades", "# Rating Downgrades ", "# of Brokers in Rating"), "(upgrades-downgrades)/max(brokers,1)", "ratio", "finite", "BROKER_AND_TARGET", "higher_better", "rating_revision", lambda d: (number(d["# Rating Upgrades"]) - number(d["# Rating Downgrades "])) / number(d["# of Brokers in Rating"]).clip(lower=1)),
}


def build_features(df: pd.DataFrame, require_positive_eg2: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = df.copy()
    out = pd.concat([out, apply_unit_normalization(out)], axis=1)
    issues: list[dict] = []
    # The shared quant factor only accepts economically interpretable conventional
    # growth. Strategy-specific negative-EPS permissions are audited separately.
    eg2, eg2_reason = derive_eg2(out["F1 Consensus Est."], out["F2 Consensus Est."], True)
    out["eg2_growth_pct"] = eg2
    out["eg2_growth_pct__reason"] = eg2_reason
    out["eps_growth_acceleration"] = eg2 - number(out["This Yr`s Est.d Growth (F(1)/F(0))"])
    out["book_price_divergence"] = (abs(number(out["Last Close"]) - number(out["Book Value"])) / abs(number(out["Last Close"]))).where((number(out["Last Close"]) > 0) & (number(out["Book Value"]) > 0))
    out["price_book_reconstructed"] = safe_div(out["Last Close"], out["Book Value"], positive_den=True)
    out["market_cap_per_share"] = safe_div(out["Market Cap (mil)"], out["Shares Outstanding (mil)"], positive_den=True)
    if "Optionable" in out:
        option_text = out["Optionable"].astype(str).str.strip().str.casefold()
        known = ~option_text.isin({"", "nan", "none", "no data"})
        out["Optionability Implementation Flag"] = option_text.isin({"y", "yes", "true", "1", "optionable"}).where(known)
        out["Implementation Feasibility Note"] = np.select(
            [out["Optionability Implementation Flag"].eq(True), out["Optionability Implementation Flag"].eq(False)],
            ["OPTIONS AVAILABLE", "NO LISTED OPTIONS / CASH EQUITY ONLY"],
            default="OPTIONABILITY UNKNOWN",
        )
    else:
        out["Optionability Implementation Flag"] = pd.Series(np.nan, index=out.index)
        out["Implementation Feasibility Note"] = "OPTIONABILITY UNKNOWN"
    derived: dict[str, pd.Series] = {}
    for name, spec in DERIVED_FEATURES.items():
        try:
            derived[name] = spec.fn(out).replace([np.inf, -np.inf], np.nan)
        except KeyError as exc:
            derived[name] = pd.Series(np.nan, index=out.index)
            issues.append({"feature": name, "reason": f"NO DATA: missing input {exc}"})
    out = pd.concat([out, pd.DataFrame(derived, index=out.index)], axis=1)
    reasons = {}
    for name, spec in DERIVED_FEATURES.items():
        reasons[f"{name}__reason"] = np.where(out[name].isna(), f"NO DATA/NOT MEANINGFUL: {spec.valid_when}", "")
    out = pd.concat([out, pd.DataFrame(reasons, index=out.index)], axis=1)
    return out, pd.DataFrame(issues)


def derived_registry_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"feature": name, "inputs": " | ".join(s.inputs), "formula": s.formula, "units": s.units,
         "valid_when": s.valid_when, "expanded_family": s.family, "direction": s.direction,
         "duplicate_group": s.duplicate_group}
        for name, s in DERIVED_FEATURES.items()
    ])
