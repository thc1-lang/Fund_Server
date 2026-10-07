from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import config
from src.model import _cross_section_score, _finite

SHORT_METRICS = {
    "operating": {
        "latest_roa": "lower", "latest_roe": "lower", "latest_net_margin": "lower",
        "latest_gross_margin": "lower", "latest_pre_tax_margin": "lower",
        "revenue_cagr": "lower", "latest_revenue_growth": "lower",
    },
    "cash_flow": {
        "latest_fcf_margin": "lower", "latest_fcf_yield": "lower", "positive_fcf_share": "lower",
    },
    "balance_sheet": {
        "latest_current_ratio": "lower", "latest_quick_ratio": "lower",
        "latest_working_capital_to_assets": "lower", "latest_debt_to_equity": "higher",
        "latest_interest_coverage": "lower", "latest_net_debt_to_ebitda": "higher",
    },
    "trend": {
        "revenue_four_year_trend": "lower", "net_margin_four_year_trend": "lower",
        "fcf_margin_four_year_trend": "lower", "roa_four_year_trend": "lower",
        "gross_margin_four_year_trend": "lower", "debt_to_equity_four_year_trend": "higher",
        "positive_revenue_growth_share": "lower",
    },
    "inflection": {
        "revenue_growth_acceleration": "lower", "net_margin_change": "lower",
        "fcf_margin_change": "lower", "roa_change": "lower", "ebit_margin_change": "lower",
        "debt_to_equity_change": "higher",
    },
    "point_in_time": {
        "primary_short_score": "higher", "primary_price_change_4w": "lower",
        "primary_price_change_12w": "lower", "primary_f1_estimate_change_4w": "lower",
        "primary_f2_estimate_change_4w": "lower",
    },
}
SHORT_WEIGHTS = {
    "operating": "SHORT_WEIGHT_OPERATING", "cash_flow": "SHORT_WEIGHT_CASH_FLOW",
    "balance_sheet": "SHORT_WEIGHT_BALANCE_SHEET", "trend": "SHORT_WEIGHT_TREND",
    "inflection": "SHORT_WEIGHT_INFLECTION", "point_in_time": "SHORT_WEIGHT_POINT_IN_TIME",
}


def score_shorts(features: pd.DataFrame, metadata: pd.DataFrame, controls: dict[str, Any], as_of: str) -> pd.DataFrame:
    if features.empty:
        raise ValueError("Short: no primary-shortlist history available")
    table = features.copy()
    if not metadata.empty:
        table = table.merge(metadata, on="Ticker", how="left", validate="one_to_one")
    weights = {family: float(controls[key]) for family, key in SHORT_WEIGHTS.items()}
    scoring_controls = dict(controls)
    scoring_controls["MIN_CROSS_SECTION_OBSERVATIONS"] = int(controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"])
    family_score: dict[str, pd.Series] = {}
    family_coverage: dict[str, pd.Series] = {}
    metric_score: dict[str, pd.Series] = {}
    for family, metrics in SHORT_METRICS.items():
        metric_weights = {name: float(controls[f"SHORT_METRIC_WEIGHT_{name.upper()}"]) for name in metrics}
        active = {name: direction for name, direction in metrics.items() if weights[family] > 0 and metric_weights[name] > 0}
        scores, available, total = {}, 0.0, sum(metric_weights[name] for name in active)
        for name, direction in active.items():
            result = _cross_section_score(table[name], direction, scoring_controls, table.get("Sector"))
            scores[name] = result["score"]
            metric_score[name] = result["score"]
            available += result["score"].notna().astype(float) * metric_weights[name]
        den = total if total else 1.0
        coverage = available / den if isinstance(available, pd.Series) else pd.Series(float(available) / den, index=table.index)
        family_coverage[family] = coverage
        if scores:
            values = pd.DataFrame(scores, index=table.index)
            point_values = sum(values[name].fillna(0) * metric_weights[name] for name in active)
            valid_weights = sum(values[name].notna().astype(float) * metric_weights[name] for name in active)
            family_score[family] = (point_values / valid_weights.replace(0, np.nan)).where(coverage.ge(float(controls["MIN_FAMILY_METRIC_COVERAGE"])))
        else:
            family_score[family] = pd.Series(np.nan, index=table.index)

    for family, score in family_score.items():
        table[f"SHORT_{family.upper()}_score"] = score
        table[f"SHORT_{family.upper()}_coverage"] = family_coverage[family]
    for metric, score in metric_score.items():
        table[f"SHORT_{metric}_score"] = score

    enabled = [f for f, w in weights.items() if w > 0]
    all_weight = sum(weights[f] for f in enabled)
    applicable = sum(family_score[f].notna().astype(float) * weights[f] for f in enabled)
    numerator = sum(family_score[f].fillna(0) * weights[f] for f in enabled)
    table["short_weight_coverage"] = applicable / all_weight if all_weight else 0.0
    table["short_raw_score"] = numerator / applicable.replace(0, np.nan)
    trend_metrics = [m for m in SHORT_METRICS["trend"] if float(controls[f"SHORT_METRIC_WEIGHT_{m.upper()}"]) > 0]
    trend_metric_weights = {m: float(controls[f"SHORT_METRIC_WEIGHT_{m.upper()}"]) for m in trend_metrics}
    slope_metrics = [m for m in trend_metrics if m.endswith("_four_year_trend")]
    trend_total_weight = sum(trend_metric_weights[m] for m in slope_metrics)
    trend_reliability_parts = {}
    for metric in slope_metrics:
        obs = table[f"{metric}_observations"].fillna(0).clip(upper=4) / 4
        fit = 0.5 + 0.5 * table[f"{metric}_r2"].fillna(0).clip(0, 1)
        trend_reliability_parts[metric] = obs * fit * trend_metric_weights[metric]
    table["short_trend_reliability"] = sum(trend_reliability_parts.values()) / trend_total_weight if trend_total_weight else 0.0
    table["short_score"] = 50 + (table["short_raw_score"] - 50) * table["short_trend_reliability"]

    trend_directions = {m: d for m, d in SHORT_METRICS["trend"].items() if trend_metric_weights.get(m, 0) > 0}
    adverse = pd.DataFrame({
        metric: (
            table[metric].lt(float(controls["SHORT_MAX_POSITIVE_REVENUE_GROWTH_SHARE"]))
            if metric == "positive_revenue_growth_share"
            else (table[metric].lt(0) if direction == "lower" else table[metric].gt(0))
        ).astype(float).where(table[metric].notna())
        for metric, direction in trend_directions.items()
    })
    recent_inputs = {
        "revenue": ("latest_revenue_growth", "lower"),
        "net_margin": ("net_margin_change", "lower"), "fcf_margin": ("fcf_margin_change", "lower"),
        "roa": ("roa_change", "lower"), "ebit_margin": ("ebit_margin_change", "lower"),
        "debt": ("debt_to_equity_change", "higher"),
    }
    trend_raw = pd.DataFrame({m: table[m] for m in SHORT_METRICS["trend"]})
    table["short_adverse_trend_observations"] = adverse.notna().sum(axis=1)
    table["short_adverse_trend_breadth"] = adverse.mean(axis=1, skipna=True)
    recent = pd.DataFrame({name: ((table[column] < 0) if direction == "lower" else (table[column] > 0)).astype(float).where(table[column].notna())
                           for name, (column, direction) in recent_inputs.items()})
    table["short_adverse_recent_observations"] = recent.notna().sum(axis=1)
    table["short_adverse_recent_breadth"] = recent.mean(axis=1, skipna=True)

    # Independent absolute weakness checks stop peer-relative underperformance
    # alone from creating a short thesis.
    weak_domains = pd.DataFrame({
        "OPERATING": table["latest_net_income"].lt(0) | table["latest_net_margin"].lt(0)
                     | table["latest_ebit"].le(0) | table["latest_ebitda"].le(0),
        "CASH_FLOW": table["latest_free_cash_flow"].lt(0),
        "BALANCE_SHEET": table["latest_current_ratio"].lt(1) | table["latest_interest_coverage_raw"].lt(1)
                         | table["latest_shareholders_equity"].le(0) | table["latest_net_debt_to_ebitda"].gt(3),
    }).astype(float).where(pd.DataFrame({
        "OPERATING": table["latest_net_income"].notna() | table["latest_net_margin"].notna()
                     | table["latest_ebit"].notna() | table["latest_ebitda"].notna(),
        "CASH_FLOW": table["latest_free_cash_flow"].notna(),
        "BALANCE_SHEET": table["latest_current_ratio"].notna() | table["latest_interest_coverage_raw"].notna()
                         | table["latest_shareholders_equity"].notna() | table["latest_net_debt_to_ebitda"].notna(),
    }))
    table["short_nonpositive_ebit"] = table["latest_ebit"].le(0).where(table["latest_ebit"].notna())
    table["short_nonpositive_ebitda"] = table["latest_ebitda"].le(0).where(table["latest_ebitda"].notna())
    table["short_nonpositive_equity"] = table["latest_shareholders_equity"].le(0).where(table["latest_shareholders_equity"].notna())
    table["short_nonpositive_interest_coverage"] = table["latest_interest_coverage_raw"].le(0).where(table["latest_interest_coverage_raw"].notna())
    table["short_weak_fundamental_domains"] = weak_domains.sum(axis=1, min_count=1)
    table["short_trend_coverage_pass"] = family_coverage["trend"].ge(float(controls["SHORT_MIN_TREND_COVERAGE"]))
    table["short_point_in_time_coverage_pass"] = family_coverage["point_in_time"].ge(float(controls["SHORT_MIN_POINT_IN_TIME_COVERAGE"]))
    table["short_history_pass"] = table["history_years"].ge(int(controls["SHORT_MIN_HISTORY_YEARS"])) & table["trend_observations"].ge(int(controls["SHORT_MIN_TREND_OBSERVATIONS"])) & table["max_fiscal_year_gap"].le(int(controls["MAX_FISCAL_YEAR_GAP"]))
    table["short_reliability_pass"] = table["short_trend_reliability"].ge(float(controls["SHORT_MIN_TREND_RELIABILITY"]))
    table["short_trend_score_pass"] = table["SHORT_TREND_score"].ge(float(controls["SHORT_MIN_TREND_SCORE"]))
    table["short_absolute_trend_pass"] = table["short_adverse_trend_observations"].ge(int(controls["SHORT_MIN_DIRECTION_OBSERVATIONS"])) & table["short_adverse_trend_breadth"].ge(float(controls["SHORT_MIN_ADVERSE_TREND_BREADTH"]))
    table["short_recent_deterioration_pass"] = table["short_adverse_recent_observations"].ge(int(controls["SHORT_MIN_DIRECTION_OBSERVATIONS"])) & table["short_adverse_recent_breadth"].ge(float(controls["SHORT_MIN_ADVERSE_RECENT_BREADTH"]))
    table["short_fundamental_weakness_pass"] = table["short_weak_fundamental_domains"].ge(int(controls["SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS"]))
    table["short_coverage_pass"] = table["short_weight_coverage"].ge(float(controls["SHORT_MIN_WEIGHT_COVERAGE"]))
    table["short_score_pass"] = table["short_score"].ge(float(controls["SHORT_MIN_SCORE"]))
    period_end = pd.to_datetime(table["latest_fiscal_period_end"], errors="coerce", utc=True).dt.tz_localize(None)
    evaluation_date = pd.Timestamp(as_of).normalize()
    table["financial_age_days"] = (evaluation_date - period_end).dt.days
    table["short_freshness_pass"] = table["financial_age_days"].between(0, int(controls["MAX_FINANCIAL_AGE_DAYS"]))
    table["short_error_pass"] = table["source_error_count"].fillna(1).eq(0) if controls["REQUIRE_BLANK_ERROR_FIELD"] else True
    gate_cols = [c for c in table if c.startswith("short_") and c.endswith("_pass")]
    table["short_eligible"] = table[gate_cols].all(axis=1)
    table["short_fail_reasons"] = table.apply(lambda r: "; ".join(c.removeprefix("short_").removesuffix("_pass") for c in gate_cols if not bool(r[c])), axis=1)
    table["short_rank"] = np.nan
    table = table.sort_values(["short_score", "short_trend_reliability", "Ticker"], ascending=[False, False, True], kind="stable").reset_index(drop=True)
    displayable = ranked_short_candidates(table)
    table.loc[displayable.index, "short_rank"] = range(1, len(displayable) + 1)
    return table


def ranked_short_candidates(table: pd.DataFrame) -> pd.DataFrame:
    """Rank finite Short scores with sufficient source history for comparison."""
    if "short_score" not in table:
        return table.iloc[0:0].copy()
    score = pd.to_numeric(table["short_score"], errors="coerce")
    valid = score.notna() & np.isfinite(score)
    history = pd.to_numeric(table.get("history_years", pd.Series(np.nan, index=table.index)), errors="coerce")
    observations = pd.to_numeric(table.get("trend_observations", pd.Series(np.nan, index=table.index)), errors="coerce")
    gaps = pd.to_numeric(table.get("max_fiscal_year_gap", pd.Series(np.nan, index=table.index)), errors="coerce")
    source_errors = pd.to_numeric(table.get("source_error_count", pd.Series(np.nan, index=table.index)), errors="coerce")
    valid &= (history.ge(4) & observations.ge(4)
              & gaps.le(int(config.GENERAL_DEFAULTS["MAX_FISCAL_YEAR_GAP"]))
              & source_errors.fillna(1).eq(0))
    return table.loc[valid].sort_values(
        ["short_score", "short_trend_reliability", "Ticker"],
        ascending=[False, False, True], kind="stable",
    )


def _short_reason(row: pd.Series, controls: dict[str, Any], rank: int) -> str:
    def number(column: str, label: str, style: str = ".1f") -> str | None:
        value = _finite(row.get(column))
        return f"{label} {format(value, style)}" if np.isfinite(value) else None

    drivers = []
    available = [family for family in SHORT_WEIGHTS if np.isfinite(_finite(row.get(f"SHORT_{family.upper()}_score"))) and float(controls[SHORT_WEIGHTS[family]]) > 0]
    weight_total = sum(float(controls[SHORT_WEIGHTS[family]]) for family in available)
    reliability = _finite(row.get("short_trend_reliability"))
    for family in available:
        score = _finite(row.get(f"SHORT_{family.upper()}_score"))
        points = (score - 50) * float(controls[SHORT_WEIGHTS[family]]) / weight_total * reliability
        if np.isfinite(points):
            drivers.append((points, family.replace("_", " "), score))
    drivers.sort(reverse=True)
    driver_text = ", ".join(f"{name} {score:.1f}/100 ({points:+.1f} pts)" for points, name, score in drivers[:3])
    trend = _finite(row.get("short_adverse_trend_breadth"))
    trend_n = _finite(row.get("short_adverse_trend_observations"))
    recent = _finite(row.get("short_adverse_recent_breadth"))
    recent_n = _finite(row.get("short_adverse_recent_observations"))
    breadth = []
    if np.isfinite(trend) and np.isfinite(trend_n):
        breadth.append(f"{trend:.0%} adverse four-year directions across {int(trend_n)} observed metrics")
    if np.isfinite(recent) and np.isfinite(recent_n):
        breadth.append(f"{recent:.0%} adverse recent directions across {int(recent_n)} observed metrics")
    weak = _finite(row.get("short_weak_fundamental_domains"))
    if np.isfinite(weak):
        breadth.append(f"{int(weak)} weak fundamental domains")
    fundamentals = [item for item in (
        number("revenue_cagr", "revenue CAGR", ".1%"),
        number("latest_revenue_growth", "latest revenue growth", ".1%"),
        number("latest_net_margin", "net margin", ".1%"),
        number("latest_free_cash_flow", "free cash flow", ",.0f"),
        number("latest_debt_to_equity", "debt/equity", ".2f"),
    ) if item]
    primary = [item for item in (
        number("Primary Short Rank", "primary Short rank", ".0f"),
        number("Primary Short Score", "primary Short score"),
        number("Primary 4-Week Price Change (%)", "four-week price change (%)", "+.1f"),
        number("Primary 12-Week Price Change (%)", "12-week price change (%)", "+.1f"),
        number("Primary F1 Estimate Change 4-Week (%)", "F1 estimate change (%)", "+.1f"),
    ) if item]
    coverage = _finite(row.get("short_weight_coverage"))
    history = _finite(row.get("history_years"))
    year = _finite(row.get("latest_fiscal_year"))
    quality = []
    if np.isfinite(history):
        quality.append(f"{int(history)} fiscal years" + (f" through FY{int(year)}" if np.isfinite(year) else ""))
    if np.isfinite(reliability):
        quality.append(f"{reliability:.0%} trend reliability")
    if np.isfinite(coverage):
        quality.append(f"{coverage:.0%} scored family weight coverage")
    parts = [f"Ranked #{rank} among current primary Short candidates with a {row['short_score']:.1f} secondary score."]
    if driver_text:
        parts.append(f"Largest family contributions above or below the neutral 50-point base: {driver_text}.")
    if breadth:
        parts.append("Deterioration evidence: " + "; ".join(breadth) + ".")
    if fundamentals:
        parts.append("Fiscal fundamentals: " + "; ".join(fundamentals) + ".")
    if primary:
        parts.append("Primary signals: " + "; ".join(primary) + ".")
    if quality:
        parts.append("Evidence quality: " + "; ".join(quality) + ".")
    return " ".join(parts)


def build_short_summary(table: pd.DataFrame, controls: dict[str, Any]) -> pd.DataFrame:
    selected = ranked_short_candidates(table).head(int(controls["SHORT_TOP_N"])).reset_index(drop=True)
    if selected.empty:
        return pd.DataFrame(columns=[
            "Rank", "Ticker", "Company Name", "Quantitative Reason", "Sector", "Industry", "Short Score",
            "Primary Short Rank", "Primary Short Score", "Primary 4-Week Price Change (%)",
            "Primary 12-Week Price Change (%)", "Primary F1 Estimate Change 4-Week (%)",
            "Primary F2 Estimate Change 4-Week (%)", "Primary Short Signal Source",
            "Primary Dataset As Of Note", "Trend Score", "Four Year Adverse Trend Breadth",
            "Recent Deterioration Breadth", "Weak Fundamental Domains", "Trend Reliability",
            "Financial Age Days", "Revenue CAGR", "Recent Revenue Growth", "Latest Net Income",
            "Latest Net Margin", "Latest FCF", "Latest Debt / Equity", "Short Thesis Evidence", "Research Checks",
        ])
    return pd.DataFrame({
        "Rank": range(1, len(selected) + 1), "Ticker": selected["Ticker"],
        "Company Name": selected.get("Company Name", selected.get("Primary Company Name", pd.Series("", index=selected.index))),
        "Quantitative Reason": selected.apply(lambda r: _short_reason(r, controls, int(r.name) + 1), axis=1),
        "Sector": selected.get("Sector", pd.Series("", index=selected.index)),
        "Industry": selected.get("Industry", selected.get("Primary Industry", pd.Series("", index=selected.index))),
        "Short Score": selected["short_score"],
        "Primary Short Rank": selected["Primary Short Rank"],
        "Primary Short Score": selected["Primary Short Score"],
        "Primary 4-Week Price Change (%)": selected["Primary 4-Week Price Change (%)"],
        "Primary 12-Week Price Change (%)": selected["Primary 12-Week Price Change (%)"],
        "Primary F1 Estimate Change 4-Week (%)": selected["Primary F1 Estimate Change 4-Week (%)"],
        "Primary F2 Estimate Change 4-Week (%)": selected["Primary F2 Estimate Change 4-Week (%)"],
        "Primary Short Signal Source": selected.get("Primary Short Signal Source", pd.Series("", index=selected.index)),
        "Primary Dataset As Of Note": selected.get("Primary Dataset As Of Note", pd.Series("", index=selected.index)),
        "Trend Score": selected["SHORT_TREND_score"],
        "Four Year Adverse Trend Breadth": selected["short_adverse_trend_breadth"],
        "Recent Deterioration Breadth": selected["short_adverse_recent_breadth"],
        "Weak Fundamental Domains": selected["short_weak_fundamental_domains"],
        "Trend Reliability": selected["short_trend_reliability"],
        "Financial Age Days": selected["financial_age_days"],
        "Revenue CAGR": selected["revenue_cagr"], "Recent Revenue Growth": selected["latest_revenue_growth"],
        "Latest Net Income": selected["latest_net_income"],
        "Latest Net Margin": selected["latest_net_margin"], "Latest FCF": selected["latest_free_cash_flow"],
        "Latest Debt / Equity": selected["latest_debt_to_equity"],
        "Short Thesis Evidence": selected.apply(lambda r: f"{r['short_adverse_trend_breadth']:.0%} adverse four year breadth; {r['short_adverse_recent_breadth']:.0%} adverse recent breadth; {int(r['short_weak_fundamental_domains'])} weak fundamental domains; {r['short_trend_reliability']:.0%} trend reliability", axis=1),
        "Research Checks": pd.Series(["Borrow availability, borrow fee, liquidity, catalyst and event timing are not in source data; verify before any trade."] * len(selected), index=selected.index),
    }).reset_index(drop=True)
