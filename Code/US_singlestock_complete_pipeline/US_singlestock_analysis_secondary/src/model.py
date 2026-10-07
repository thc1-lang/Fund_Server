from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import config


NUMERIC_SOURCE_COLUMNS = [
    "Fiscal Year", "Revenue", "Net Income", "EBIT", "EBITDA", "Interest Expense",
    "Interest Coverage Ratio", "Current Assets", "Current Liabilities", "Total Assets",
    "Shareholders Equity", "Operating Cash Flow", "Free Cash Flow", "Year End Price",
    "Market Cap", "Cash", "Total Debt", "Enterprise Value", "Free Cash Flow Yield",
    "Current Ratio", "Quick Ratio", "Return on Assets Ratio", "Return on Equity Ratio",
    "EV / EBIT", "EV / EBITDA", "Gross Margin", "Net Margin", "Debt to Equity Ratio",
    "Price / Free Cash Flow Per Share", "Working Capital to Total Assets Ratio",
    "Sales to Assets Ratio", "Pre-Tax Profit Ratio",
]

REQUIRED_SOURCE_COLUMNS = {
    "Ticker", "Fiscal Year", "Fiscal Period End", "Revenue", "Net Income", "EBIT", "EBITDA",
    "Interest Coverage Ratio", "Operating Cash Flow", "Free Cash Flow", "Market Cap", "Cash",
    "Total Debt", "Free Cash Flow Yield", "Current Ratio", "Quick Ratio",
    "Return on Assets Ratio", "Return on Equity Ratio", "EV / EBIT", "EV / EBITDA",
    "Gross Margin", "Net Margin", "Debt to Equity Ratio", "Price / Free Cash Flow Per Share",
    "Working Capital to Total Assets Ratio", "Sales to Assets Ratio", "Pre-Tax Profit Ratio",
}


def _finite(value: Any) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else np.nan
    except (TypeError, ValueError):
        return np.nan


def _latest(series: pd.Series) -> float:
    """Return the value on the latest fiscal row; never silently backfill."""
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return float(numeric.iloc[-1]) if len(numeric) and np.isfinite(numeric.iloc[-1]) else np.nan


def _valid_positive(value: float) -> float:
    return value if np.isfinite(value) and value > 0 else np.nan


def _valid_nonnegative(value: float) -> float:
    return value if np.isfinite(value) and value >= 0 else np.nan


def _change(series: pd.Series, years: pd.Series | None = None) -> float:
    # Use the actual final two rows, never the last two non-missing values.
    clean = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if len(clean) < 2 or clean.iloc[-2:].isna().any():
        return np.nan
    if years is not None and float(years.iloc[-1] - years.iloc[-2]) != 1:
        return np.nan
    return float(clean.iloc[-1] - clean.iloc[-2])


def _growth_series(series: pd.Series, years: pd.Series | None = None) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    prior = values.shift(1)
    valid = (values > 0) & (prior > 0)
    if years is not None:
        valid &= years.diff().eq(1)
    return (values / prior - 1).where(valid)


def _cagr(values: pd.Series, years: pd.Series) -> float:
    # Missing/invalid endpoints cannot silently shorten the stated history.
    if len(values) < 2:
        return np.nan
    first, last = _finite(values.iloc[0]), _finite(values.iloc[-1])
    elapsed = _finite(years.iloc[-1] - years.iloc[0])
    if not all(np.isfinite(x) for x in (first, last, elapsed)) or min(first, last, elapsed) <= 0:
        return np.nan
    return float((last / first) ** (1 / elapsed) - 1)


def _trend_stats(values: pd.Series, years: pd.Series, minimum: int = 3, log_values: bool = False) -> tuple[float, float, int]:
    valid = pd.DataFrame({"v": pd.to_numeric(values, errors="coerce"), "y": pd.to_numeric(years, errors="coerce")}).dropna()
    if log_values:
        valid = valid.loc[valid["v"].gt(0)].copy()
        valid["v"] = np.log(valid["v"])
    if len(valid) < minimum or valid["y"].nunique() < 2 or not np.isfinite(_latest(values)) or (log_values and _latest(values) <= 0):
        return np.nan, np.nan, int(len(valid))
    x = valid["y"].to_numpy(dtype=float)
    y = valid["v"].to_numpy(dtype=float)
    # Theil-Sen median slope is materially less sensitive than OLS to one
    # unusual fiscal year, which matters when only three or four points exist.
    slopes = [(y[j] - y[i]) / (x[j] - x[i]) for i in range(len(x)) for j in range(i + 1, len(x)) if x[j] != x[i]]
    if not slopes:
        return np.nan, np.nan, int(len(valid))
    slope = float(np.median(slopes))
    intercept = float(np.median(y - slope * x))
    fitted = intercept + slope * x
    total = float(np.sum((y - y.mean()) ** 2))
    residual = float(np.sum((y - fitted) ** 2))
    r_squared = 1.0 if total == 0 and residual == 0 else (1 - residual / total if total > 0 else np.nan)
    return (float(np.expm1(slope)) if log_values else slope), float(np.clip(r_squared, 0, 1)), int(len(valid))


def _linear_trend(values: pd.Series, years: pd.Series, minimum: int = 3, log_values: bool = False) -> float:
    return _trend_stats(values, years, minimum, log_values)[0]


def _direction_breadth(trends: list[tuple[float, str]]) -> float:
    observations = []
    for value, direction in trends:
        if not np.isfinite(value):
            continue
        favourable = value if direction == "higher" else -value
        observations.append(1.0 if favourable > 0 else (0.5 if favourable == 0 else 0.0))
    return float(np.mean(observations)) if observations else np.nan


def _history_trace(years: pd.Series, values: pd.Series, percent: bool = False) -> str:
    pairs = []
    for year, value in zip(pd.to_numeric(years, errors="coerce"), pd.to_numeric(values, errors="coerce")):
        if not np.isfinite(year):
            continue
        if not np.isfinite(value):
            rendered = "NO DATA"
        elif percent:
            rendered = f"{value:.1%}"
        else:
            rendered = f"{value:.6g}"
        pairs.append(f"{int(year)}: {rendered}")
    return " | ".join(pairs)


def build_features(raw: pd.DataFrame, min_trend_observations: int = 4) -> pd.DataFrame:
    missing = sorted(REQUIRED_SOURCE_COLUMNS - set(raw.columns))
    if missing:
        raise ValueError(f"Source data is missing required columns: {missing}")
    frame = raw.copy()
    frame["Ticker"] = frame["Ticker"].fillna("").astype(str).str.strip().str.upper()
    frame = frame.loc[frame["Ticker"].ne("")].copy()
    for column in NUMERIC_SOURCE_COLUMNS:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
    invalid_year = frame["Fiscal Year"].notna() & frame["Fiscal Year"].mod(1).ne(0)
    if invalid_year.any():
        raise ValueError("Fiscal Year must contain integer annual periods")
    if frame.empty or not frame["Fiscal Year"].notna().any():
        raise ValueError("No usable fiscal-year history in source data")
    frame["_period"] = pd.to_datetime(frame.get("Fiscal Period End"), errors="coerce")
    # Fiscal year is authoritative. Missing period dates must not sort after a
    # newer fiscal year and accidentally become the "latest" observation.
    frame = frame.sort_values(["Ticker", "Fiscal Year", "_period"], kind="stable", na_position="first")
    rows: list[dict[str, Any]] = []
    for ticker, group in frame.groupby("Ticker", sort=True):
        usable = group.loc[group["Fiscal Year"].notna()].copy()
        if usable.empty:
            latest = group.iloc[-1]
            rows.append({"Ticker": ticker, "history_years": 0, "latest_error": str(latest.get("Error", "")).strip(),
                         "warnings": "No usable fiscal-year history"})
            continue
        available_history_years = int(usable["Fiscal Year"].nunique())
        duplicate_fiscal_rows_dropped = int(len(usable) - available_history_years)
        usable = usable.drop_duplicates("Fiscal Year", keep="last").tail(4).copy()
        latest = usable.iloc[-1]
        revenue = usable["Revenue"]
        revenue_growth = _growth_series(revenue, usable["Fiscal Year"])
        net_margin = usable["Net Margin"]
        fcf_margin = (usable["Free Cash Flow"] / revenue).where(revenue.gt(0))
        ebit_margin = (usable["EBIT"] / revenue).where(revenue.gt(0))
        trend_inputs = {
            "revenue": _trend_stats(revenue, usable["Fiscal Year"], min_trend_observations, log_values=True),
            "net_margin": _trend_stats(net_margin, usable["Fiscal Year"], min_trend_observations),
            "fcf_margin": _trend_stats(fcf_margin, usable["Fiscal Year"], min_trend_observations),
            "roa": _trend_stats(usable["Return on Assets Ratio"], usable["Fiscal Year"], min_trend_observations),
            "gross_margin": _trend_stats(usable["Gross Margin"], usable["Fiscal Year"], min_trend_observations),
            "debt_to_equity": _trend_stats(
                usable["Debt to Equity Ratio"].where(usable["Debt to Equity Ratio"].ge(0)),
                usable["Fiscal Year"], min_trend_observations,
            ),
        }
        recent_directions = [
            (_latest(revenue_growth), "higher"),
            (_change(net_margin, usable["Fiscal Year"]), "higher"),
            (_change(fcf_margin, usable["Fiscal Year"]), "higher"),
            (_change(usable["Return on Assets Ratio"], usable["Fiscal Year"]), "higher"),
            (_change(usable["Gross Margin"], usable["Fiscal Year"]), "higher"),
            (_change(usable["Debt to Equity Ratio"].where(usable["Debt to Equity Ratio"].ge(0)), usable["Fiscal Year"]), "lower"),
        ]
        fit_values = [stats[1] for stats in trend_inputs.values() if np.isfinite(stats[1])]
        latest_ni = _latest(usable["Net Income"])
        latest_fcf = _latest(usable["Free Cash Flow"])
        latest_ocf = _latest(usable["Operating Cash Flow"])
        latest_ebitda = _latest(usable["EBITDA"])
        latest_debt = _latest(usable["Total Debt"])
        latest_cash = _latest(usable["Cash"])
        warnings = []
        if duplicate_fiscal_rows_dropped:
            warnings.append(f"Dropped {duplicate_fiscal_rows_dropped} duplicate fiscal-year row(s); kept latest period")
        source_errors = usable.get("Error", pd.Series(index=usable.index, dtype=object)).fillna("").astype(str).str.strip()
        if source_errors.ne("").any():
            warnings.append("Source Error field populated")
        missing_notes = str(latest.get("Missing Data Notes", "")).strip()
        if missing_notes:
            warnings.append(missing_notes)
        row = {
            "Ticker": ticker,
            "history_years": int(len(usable)),
            "available_history_years": available_history_years,
            "duplicate_fiscal_rows_dropped": duplicate_fiscal_rows_dropped,
            "source_error_count": int(source_errors.ne("").sum()),
            "trend_observations": int(len(usable)),
            "trend_year_span": int(usable["Fiscal Year"].max() - usable["Fiscal Year"].min()) if len(usable) > 1 else 0,
            "max_fiscal_year_gap": int(pd.to_numeric(usable["Fiscal Year"], errors="coerce").sort_values().diff().max()) if len(usable) > 1 else 0,
            "four_year_fiscal_years": " | ".join(str(int(y)) for y in usable["Fiscal Year"] if np.isfinite(y)),
            "four_year_revenue_history": _history_trace(usable["Fiscal Year"], revenue),
            "four_year_net_income_history": _history_trace(usable["Fiscal Year"], usable["Net Income"]),
            "four_year_fcf_history": _history_trace(usable["Fiscal Year"], usable["Free Cash Flow"]),
            "four_year_net_margin_history": _history_trace(usable["Fiscal Year"], net_margin, percent=True),
            "four_year_roa_history": _history_trace(usable["Fiscal Year"], usable["Return on Assets Ratio"], percent=True),
            "four_year_debt_to_equity_history": _history_trace(usable["Fiscal Year"], usable["Debt to Equity Ratio"]),
            "latest_fiscal_year": int(latest["Fiscal Year"]) if np.isfinite(latest["Fiscal Year"]) else np.nan,
            "latest_fiscal_period_end": latest.get("Fiscal Period End", ""),
            "latest_price_date": latest.get("Price Date", ""),
            "latest_revenue": _latest(revenue),
            "latest_net_income": latest_ni,
            "latest_ebit": _latest(usable["EBIT"]),
            "latest_ebitda": latest_ebitda,
            "latest_shareholders_equity": _latest(usable["Shareholders Equity"]),
            "latest_interest_coverage_raw": _latest(usable["Interest Coverage Ratio"]),
            "latest_free_cash_flow": latest_fcf,
            "latest_market_cap": _latest(usable["Market Cap"]),
            "latest_roa": _latest(usable["Return on Assets Ratio"]),
            "latest_roe": _latest(usable["Return on Equity Ratio"]),
            "latest_net_margin": _latest(net_margin),
            "latest_gross_margin": _latest(usable["Gross Margin"]),
            "latest_pre_tax_margin": _latest(usable["Pre-Tax Profit Ratio"]),
            "latest_sales_to_assets": _latest(usable["Sales to Assets Ratio"]),
            "latest_fcf_margin": _latest(fcf_margin),
            "latest_fcf_yield": _latest(usable["Free Cash Flow Yield"]),
            "positive_fcf_share": float(pd.to_numeric(usable["Free Cash Flow"], errors="coerce").gt(0).mean()) if usable["Free Cash Flow"].notna().any() else np.nan,
            "cash_conversion": latest_ocf / latest_ni if np.isfinite(latest_ocf) and np.isfinite(latest_ni) and latest_ni > 0 else np.nan,
            "latest_current_ratio": _valid_positive(_latest(usable["Current Ratio"])),
            "latest_quick_ratio": _valid_positive(_latest(usable["Quick Ratio"])),
            "latest_working_capital_to_assets": _latest(usable["Working Capital to Total Assets Ratio"]),
            "latest_debt_to_equity": _valid_nonnegative(_latest(usable["Debt to Equity Ratio"])),
            "latest_interest_coverage": _valid_positive(_latest(usable["Interest Coverage Ratio"])),
            "latest_net_debt_to_ebitda": (latest_debt - latest_cash) / latest_ebitda if all(np.isfinite(v) for v in (latest_debt, latest_cash, latest_ebitda)) and latest_ebitda > 0 else np.nan,
            "positive_net_income_share": float(pd.to_numeric(usable["Net Income"], errors="coerce").gt(0).mean()) if usable["Net Income"].notna().any() else np.nan,
            "revenue_growth_stability": float(revenue_growth.dropna().std(ddof=0)) if revenue_growth.notna().sum() >= 2 else np.nan,
            "latest_ev_ebitda": _valid_positive(_latest(usable["EV / EBITDA"])),
            "latest_ev_ebit": _valid_positive(_latest(usable["EV / EBIT"])),
            "latest_price_fcf": _valid_positive(_latest(usable["Price / Free Cash Flow Per Share"])),
            "revenue_cagr": _cagr(revenue, usable["Fiscal Year"]),
            "latest_revenue_growth": _latest(revenue_growth),
            "revenue_growth_acceleration": _change(revenue_growth, usable["Fiscal Year"]),
            "net_margin_change": _change(net_margin, usable["Fiscal Year"]),
            "fcf_margin_change": _change(fcf_margin, usable["Fiscal Year"]),
            "roa_change": _change(usable["Return on Assets Ratio"], usable["Fiscal Year"]),
            "ebit_margin_change": _change(ebit_margin, usable["Fiscal Year"]),
            "debt_to_equity_change": _change(usable["Debt to Equity Ratio"].where(usable["Debt to Equity Ratio"].ge(0)), usable["Fiscal Year"]),
            "revenue_four_year_trend": trend_inputs["revenue"][0],
            "net_margin_four_year_trend": trend_inputs["net_margin"][0],
            "fcf_margin_four_year_trend": trend_inputs["fcf_margin"][0],
            "roa_four_year_trend": trend_inputs["roa"][0],
            "gross_margin_four_year_trend": trend_inputs["gross_margin"][0],
            "debt_to_equity_four_year_trend": trend_inputs["debt_to_equity"][0],
            "revenue_four_year_trend_r2": trend_inputs["revenue"][1],
            "net_margin_four_year_trend_r2": trend_inputs["net_margin"][1],
            "fcf_margin_four_year_trend_r2": trend_inputs["fcf_margin"][1],
            "roa_four_year_trend_r2": trend_inputs["roa"][1],
            "gross_margin_four_year_trend_r2": trend_inputs["gross_margin"][1],
            "debt_to_equity_four_year_trend_r2": trend_inputs["debt_to_equity"][1],
            "recent_direction_observations": sum(np.isfinite(v) for v, _ in recent_directions),
            "recent_favourable_breadth": _direction_breadth(recent_directions),
            "trend_fit_r2_mean": float(np.mean(fit_values)) if fit_values else np.nan,
            # Four annual observations contain only three possible year-on-year
            # changes. Missing changes count as non-confirming evidence, but
            # the first observation is never a possible change.
            "positive_revenue_growth_share": (
                float(revenue_growth.iloc[1:].gt(0).mean())
                if len(revenue_growth) > 1 and revenue_growth.notna().any() else np.nan
            ),
            "trend_direction_breadth": _direction_breadth([
                (trend_inputs["revenue"][0], "higher"), (trend_inputs["net_margin"][0], "higher"),
                (trend_inputs["fcf_margin"][0], "higher"), (trend_inputs["roa"][0], "higher"),
                (trend_inputs["gross_margin"][0], "higher"), (trend_inputs["debt_to_equity"][0], "lower"),
            ]),
            "latest_error": str(latest.get("Error", "")).strip(),
            "warnings": "; ".join(dict.fromkeys(warnings)),
            "data_source": latest.get("Data Source", ""),
            "audit_notes": latest.get("Audit Notes", ""),
        }
        for name, stats in trend_inputs.items():
            row[f"{name}_four_year_trend_observations"] = stats[2]
        rows.append(row)
    return pd.DataFrame(rows)


def _robust_score(series: pd.Series, direction: str, cap: float) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    median = numeric.median()
    mad = (numeric - median).abs().median()
    scale = 1.4826 * mad
    if not np.isfinite(scale) or scale == 0:
        scale = numeric.std(ddof=0)
    if not np.isfinite(scale) or scale == 0:
        out = pd.Series(50.0, index=numeric.index).where(numeric.notna())
    else:
        z = ((numeric - median) / scale).clip(-cap, cap)
        if direction == "lower":
            z = -z
        out = 50 + 50 * z / cap
    return out.clip(0, 100)


def _base_cross_section_score(
    series: pd.Series, direction: str, minimum_observations: int, shrinkage_strength: float,
    robust_z_cap: float, percentile_weight: float,
) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    observations = int(numeric.notna().sum())
    if observations < minimum_observations:
        return pd.Series(np.nan, index=series.index)
    # Pandas assigns larger ranks to later values in the requested order:
    # high values should score highly for "higher", while low values should
    # score highly for "lower".
    ascending = direction != "lower"
    percentile = (numeric.rank(method="average", ascending=ascending) - 0.5) / observations * 100
    robust = _robust_score(numeric, direction, robust_z_cap)
    blend = percentile_weight
    raw_score = blend * percentile + (1 - blend) * robust
    # Sparse cross-sections otherwise create false 0/100 precision. Shrink
    # toward neutral in proportion to the number of valid peer observations.
    reliability = observations / (observations + shrinkage_strength) if shrinkage_strength > 0 else 1.0
    return 50 + (raw_score - 50) * reliability


def _cross_section_score(
    series: pd.Series, direction: str, controls: dict[str, Any], peer_groups: pd.Series | None = None,
) -> dict[str, pd.Series]:
    robust_z_cap = float(controls["ROBUST_Z_CAP"])
    percentile_weight = float(controls["PERCENTILE_WEIGHT"])
    category_score = _base_cross_section_score(
        series, direction, int(controls["MIN_CROSS_SECTION_OBSERVATIONS"]),
        float(controls["CROSS_SECTION_SHRINKAGE_STRENGTH"]), robust_z_cap, percentile_weight,
    )
    sector_score = pd.Series(np.nan, index=series.index, dtype=float)
    sector_observations = pd.Series(0, index=series.index, dtype=int)
    sector_blend_weight = pd.Series(0.0, index=series.index, dtype=float)
    if peer_groups is not None:
        groups = peer_groups.fillna("").astype(str).str.strip()
        numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
        for label in groups.loc[groups.ne("")].unique():
            index = groups.index[groups.eq(label)]
            count = int(numeric.loc[index].notna().sum())
            sector_observations.loc[index] = count
            scored = _base_cross_section_score(
                numeric.loc[index], direction, int(controls["MIN_SECTOR_PEER_OBSERVATIONS"]),
                float(controls["SECTOR_PEER_SHRINKAGE_STRENGTH"]), robust_z_cap, percentile_weight,
            )
            sector_score.loc[index] = scored
            if count >= int(controls["MIN_SECTOR_PEER_OBSERVATIONS"]):
                reliability = count / (count + float(controls["SECTOR_PEER_SHRINKAGE_STRENGTH"]))
                sector_blend_weight.loc[index] = float(controls["SECTOR_PEER_WEIGHT"]) * reliability
    combined = category_score.copy()
    usable = category_score.notna() & sector_score.notna()
    combined.loc[usable] = (
        category_score.loc[usable] * (1 - sector_blend_weight.loc[usable])
        + sector_score.loc[usable] * sector_blend_weight.loc[usable]
    )
    return {
        "score": combined,
        "category_score": category_score,
        "sector_score": sector_score,
        "sector_peer_observations": sector_observations,
        "sector_blend_weight": sector_blend_weight,
    }


def _control_weights(category: str, controls: dict[str, Any]):
    slug = config.CATEGORIES[category]["slug"]
    family = {name: float(controls[f"FAMILY_WEIGHT_{slug}_{name}"]) for name in config.FAMILY_WEIGHTS[category]}
    metric = {name: float(controls[f"METRIC_WEIGHT_{slug}_{name.upper()}"]) for name in config.FEATURE_SPECS}
    return family, metric


def _primary_metadata(metadata: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in (
        "Ticker", "Company Name", "Market Cap (mil)", "Industry", "Sector", "Exchange", "COM/ADR/Canadian",
        "Universe Rank", "Final Strategy Score", "PIT Verification Status",
        "Latest Verified Information Availability Date",
    ) if c in metadata]
    meta = metadata[keep].copy()
    meta["Ticker"] = meta["Ticker"].astype(str).str.strip().str.upper()
    return meta.drop_duplicates("Ticker").rename(columns={
        "Universe Rank": "Primary Universe Rank",
        "Final Strategy Score": "Primary Final Strategy Score",
        "PIT Verification Status": "Primary PIT Verification Status",
        "Latest Verified Information Availability Date": "Primary Latest Verified Availability Date",
    })


def append_missing_primary(table: pd.DataFrame, metadata: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Keep missing Primary names visible as explicit non-eligible audit rows."""
    meta = _primary_metadata(metadata)
    missing = meta.loc[~meta["Ticker"].isin(table["Ticker"])].copy()
    if missing.empty:
        return table
    rows = pd.DataFrame(index=missing.index, columns=table.columns)
    for column in missing:
        if column in rows:
            rows[column] = missing[column]
    rows["history_years"] = 0
    rows["available_history_years"] = 0
    rows["trend_observations"] = 0
    rows["analysis_as_of"] = as_of
    rows["eligible"] = False
    rows["gate_fail_reasons"] = "missing_source_history"
    for column in table.columns:
        if column.endswith("_gate_pass"):
            rows[column] = False
    return pd.concat([table, rows], ignore_index=True)


def score_category(features: pd.DataFrame, metadata: pd.DataFrame, category: str, controls: dict[str, Any], as_of: str | None = None) -> pd.DataFrame:
    table = features.copy()
    if not metadata.empty and "Ticker" in metadata:
        table = table.merge(_primary_metadata(metadata), on="Ticker", how="inner", validate="one_to_one")
    if table.empty:
        raise ValueError(f"{category}: no source stocks match the primary shortlist")
    family_weights, metric_weights = _control_weights(category, controls)
    metric_audit: dict[str, Any] = {}
    for metric, (family, direction, _) in config.FEATURE_SPECS.items():
        components = _cross_section_score(table[metric], direction, controls, table.get("Sector"))
        for name, values in components.items():
            metric_audit[f"{metric}__{name}"] = values
        metric_audit[f"{metric}__weight"] = metric_weights[metric]
        metric_audit[f"{metric}__weighted_points"] = components["score"] * metric_weights[metric]
    table = pd.concat([table, pd.DataFrame(metric_audit, index=table.index)], axis=1)

    active_metrics = [metric for metric, weight in metric_weights.items() if weight > 0 and family_weights[config.FEATURE_SPECS[metric][0]] > 0]
    valid_metric_weight = sum(table[f"{metric}__score"].notna().astype(float) * metric_weights[metric] for metric in active_metrics)
    sector_weighted = sum(
        table[f"{metric}__sector_blend_weight"] * table[f"{metric}__score"].notna().astype(float) * metric_weights[metric]
        for metric in active_metrics
    )
    table["effective_sector_peer_weight"] = sector_weighted / valid_metric_weight.replace(0, np.nan)

    # Defragment after constructing the wide metric audit block; the live tables are
    # intentionally wide because every input, score, weight and contribution is visible.
    table = table.copy()
    for family in config.FAMILY_WEIGHTS[category]:
        metrics = [m for m, (f, _, _) in config.FEATURE_SPECS.items() if f == family and metric_weights[m] > 0]
        configured = sum(metric_weights[m] for m in metrics)
        valid_weight = sum(table[f"{m}__score"].notna().astype(float) * metric_weights[m] for m in metrics) if metrics else pd.Series(0.0, index=table.index)
        numerator = sum(table[f"{m}__score"].fillna(0) * metric_weights[m] for m in metrics) if metrics else pd.Series(0.0, index=table.index)
        coverage = valid_weight / configured if configured > 0 else pd.Series(np.nan, index=table.index)
        score = (numerator / valid_weight.replace(0, np.nan)).where(coverage.ge(float(controls["MIN_FAMILY_METRIC_COVERAGE"])))
        table[f"{family}__metric_coverage"] = coverage
        table[f"{family}__score"] = score
        table[f"{family}__weight"] = family_weights[family]
        table[f"{family}__weighted_numerator"] = score * family_weights[family]
        for metric in metrics:
            table[f"{metric}__family_contribution_points"] = (
                (table[f"{metric}__score"] * metric_weights[metric] / valid_weight.replace(0, np.nan)).where(score.notna())
            )

    table = table.copy()
    positive_family_total = sum(weight for weight in family_weights.values() if weight > 0)
    valid_family_weight = sum(table[f"{family}__score"].notna().astype(float) * weight for family, weight in family_weights.items() if weight > 0)
    final_numerator = sum(table[f"{family}__score"].fillna(0) * weight for family, weight in family_weights.items() if weight > 0)
    table["applicable_family_weight"] = valid_family_weight
    table["final_score_numerator"] = final_numerator
    table["overall_weight_coverage"] = valid_family_weight / positive_family_total
    table["raw_final_score"] = final_numerator / valid_family_weight.replace(0, np.nan)
    for family, weight in family_weights.items():
        table[f"{family}__contribution_points"] = (
            table[f"{family}__score"] * weight / valid_family_weight.replace(0, np.nan)
            if weight > 0 else np.nan
        )
        metrics = [m for m, (f, _, _) in config.FEATURE_SPECS.items() if f == family and metric_weights[m] > 0]
        for metric in metrics:
            table[f"{metric}__final_contribution_points"] = (
                table[f"{metric}__family_contribution_points"] * weight / valid_family_weight.replace(0, np.nan)
                if weight > 0 else np.nan
            )
    trend_coverage = pd.to_numeric(table.get("TREND__metric_coverage"), errors="coerce").fillna(0).clip(0, 1)
    slope_metrics = [m for m in config.FEATURE_SPECS if m.endswith("_four_year_trend") and metric_weights[m] > 0]
    slope_total = sum(metric_weights[m] for m in slope_metrics)
    history_parts, fit_parts = [], []
    floor = float(controls["TREND_FIT_RELIABILITY_FLOOR"])
    for metric in slope_metrics:
        counts = table[f"{metric}_observations"].fillna(0)
        history = pd.Series(np.where(counts.ge(4), 1.0, np.where(counts.eq(3), float(controls["THREE_YEAR_TREND_RELIABILITY"]), 0.0)), index=table.index)
        history = history.where(table[metric].notna(), 0.0)
        fit = table[f"{metric}_r2"].fillna(0).clip(0, 1)
        history_parts.append(history * metric_weights[metric])
        fit_parts.append((floor + (1 - floor) * fit) * metric_weights[metric])
    table["trend_history_reliability"] = sum(history_parts) / slope_total if slope_total else 0.0
    table["trend_fit_reliability"] = sum(fit_parts) / slope_total if slope_total else 0.0
    table["effective_trend_reliability"] = (
        trend_coverage * table["trend_history_reliability"] * table["trend_fit_reliability"]
    ).clip(0, 1)
    table["no_usable_trend_extra_penalty_points"] = table["effective_trend_reliability"].eq(0).astype(float) * float(
        controls["NO_USABLE_TREND_EXTRA_PENALTY_POINTS"]
    )
    table["trend_data_penalty_points"] = (
        (1 - table["effective_trend_reliability"]) * float(controls["MISSING_TREND_PENALTY_POINTS"])
        + table["no_usable_trend_extra_penalty_points"]
    )
    table["TREND_DATA_PENALTY__contribution_points"] = -table["trend_data_penalty_points"]
    table["final_score"] = table["raw_final_score"] + table["TREND_DATA_PENALTY__contribution_points"]

    active_families = [family for family, weight in family_weights.items() if weight > 0]
    table["strong_family_count"] = sum(
        table[f"{family}__score"].ge(float(controls["STRONG_FAMILY_SCORE"])).astype(int)
        for family in active_families
    )
    for domain, families in config.EVIDENCE_DOMAINS.items():
        active = [family for family in families if family_weights.get(family, 0) > 0]
        domain_weight = sum(table[f"{family}__score"].notna().astype(float) * family_weights[family] for family in active)
        domain_points = sum(table[f"{family}__score"].fillna(0) * family_weights[family] for family in active)
        table[f"{domain}__evidence_score"] = domain_points / domain_weight.replace(0, np.nan) if active else np.nan
    table["strong_evidence_domain_count"] = sum(
        table[f"{domain}__evidence_score"].ge(float(controls["STRONG_FAMILY_SCORE"])).astype(int)
        for domain in config.EVIDENCE_DOMAINS
    )
    leave_one_out = []
    for family in active_families:
        valid = table[f"{family}__score"].notna().astype(float)
        remaining_weight = valid_family_weight - valid * family_weights[family]
        remaining_numerator = final_numerator - table[f"{family}__score"].fillna(0) * family_weights[family]
        leave_one_out.append(
            remaining_numerator / remaining_weight.replace(0, np.nan)
            + table["TREND_DATA_PENALTY__contribution_points"]
        )
    table["leave_one_family_out_min_score"] = pd.concat(leave_one_out, axis=1).min(axis=1, skipna=True)

    slug = config.CATEGORIES[category]["slug"]
    latest_benchmark = pd.to_numeric(table["latest_fiscal_year"], errors="coerce").max()
    evaluation_date = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now(tz="UTC")
    evaluation_date = evaluation_date.tz_localize(None).normalize()
    period_end = pd.to_datetime(table["latest_fiscal_period_end"], errors="coerce", utc=True).dt.tz_localize(None)
    table["analysis_as_of"] = evaluation_date.strftime("%Y-%m-%d")
    table["financial_age_days"] = (evaluation_date - period_end).dt.days
    table["financial_age_gate_pass"] = table["financial_age_days"].between(0, int(controls["MAX_FINANCIAL_AGE_DAYS"]))
    table["absolute_trend_gate_pass"] = table["trend_direction_breadth"].ge(float(controls["MIN_FAVOURABLE_TREND_BREADTH"]))
    table["recent_direction_gate_pass"] = (
        table["recent_direction_observations"].ge(int(controls["MIN_RECENT_DIRECTION_OBSERVATIONS"]))
        & table["recent_favourable_breadth"].ge(float(controls["MIN_RECENT_FAVOURABLE_BREADTH"]))
    )
    table["recent_coverage_gate_pass"] = (table["INFLECTION__metric_coverage"].ge(float(controls["MIN_RECENT_METRIC_COVERAGE"])) if family_weights["INFLECTION"] > 0 else True)
    table["history_gate_pass"] = table["history_years"].ge(int(controls[f"MIN_HISTORY_YEARS_{slug}"]))
    table["history_continuity_gate_pass"] = table["max_fiscal_year_gap"].le(int(controls["MAX_FISCAL_YEAR_GAP"]))
    table["freshness_gate_pass"] = table["latest_fiscal_year"].ge(latest_benchmark - int(controls["MAX_FISCAL_YEAR_LAG"]))
    table["coverage_gate_pass"] = table["overall_weight_coverage"].ge(float(controls["MIN_OVERALL_WEIGHT_COVERAGE"]))
    table["selection_coverage_gate_pass"] = table["overall_weight_coverage"].ge(float(controls[f"MIN_SELECTION_WEIGHT_COVERAGE_{slug}"]))
    table["trend_reliability_gate_pass"] = table["effective_trend_reliability"].ge(float(controls[f"MIN_EFFECTIVE_TREND_RELIABILITY_{slug}"]))
    table["family_breadth_gate_pass"] = table["strong_family_count"].ge(int(controls[f"MIN_STRONG_FAMILIES_{slug}"]))
    table["evidence_domain_breadth_gate_pass"] = table["strong_evidence_domain_count"].ge(int(controls[f"MIN_STRONG_EVIDENCE_DOMAINS_{slug}"]))
    table["robustness_gate_pass"] = table["leave_one_family_out_min_score"].ge(float(controls["MIN_LEAVE_ONE_FAMILY_OUT_SCORE"]))
    table["source_error_gate_pass"] = table["source_error_count"].fillna(1).eq(0) if controls["REQUIRE_BLANK_ERROR_FIELD"] else True
    table["strategy_specific_gate_pass"] = True
    if category == "Safe":
        if controls["SAFE_REQUIRE_POSITIVE_NET_INCOME"]:
            table["strategy_specific_gate_pass"] &= table["latest_net_income"].gt(0)
        if controls["SAFE_REQUIRE_POSITIVE_FREE_CASH_FLOW"]:
            table["strategy_specific_gate_pass"] &= table["latest_free_cash_flow"].gt(0)
        history_floor = float(controls["SAFE_MIN_POSITIVE_HISTORY_SHARE"])
        table["strategy_specific_gate_pass"] &= table["positive_net_income_share"].ge(history_floor)
        table["strategy_specific_gate_pass"] &= table["positive_fcf_share"].ge(history_floor)
    elif category == "High Growth Potential":
        if controls["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"]:
            table["strategy_specific_gate_pass"] &= table["revenue_cagr"].gt(0)
        table["strategy_specific_gate_pass"] &= table["revenue_cagr"].ge(float(controls["HIGH_GROWTH_MIN_REVENUE_CAGR"]))
        if controls["HIGH_GROWTH_REQUIRE_POSITIVE_FREE_CASH_FLOW"]:
            table["strategy_specific_gate_pass"] &= table["latest_free_cash_flow"].gt(0)
    elif category == "Turnaround Story":
        if controls["TURNAROUND_REQUIRE_INFLECTION_GATE"]:
            table["strategy_specific_gate_pass"] &= table["INFLECTION__score"].ge(float(controls["TURNAROUND_MIN_INFLECTION_SCORE"]))
        table["strategy_specific_gate_pass"] &= table["trend_direction_breadth"].ge(float(controls["TURNAROUND_MIN_FAVOURABLE_TREND_BREADTH"]))
    table["score_gate_pass"] = table["final_score"].ge(float(controls[f"MIN_SCORE_{slug}"]))
    table["quality_score_gate_pass"] = table["final_score"].ge(float(controls[f"MIN_SELECTION_SCORE_{slug}"]))
    gate_cols = [
        "history_gate_pass", "history_continuity_gate_pass", "freshness_gate_pass",
        "financial_age_gate_pass", "absolute_trend_gate_pass", "recent_direction_gate_pass", "recent_coverage_gate_pass",
        "coverage_gate_pass", "selection_coverage_gate_pass", "trend_reliability_gate_pass",
        "family_breadth_gate_pass", "evidence_domain_breadth_gate_pass", "robustness_gate_pass", "source_error_gate_pass",
        "strategy_specific_gate_pass", "score_gate_pass", "quality_score_gate_pass",
    ]
    table["eligible"] = table[gate_cols].all(axis=1)
    table["gate_fail_reasons"] = table.apply(lambda r: "; ".join(c.removesuffix("_pass") for c in gate_cols if not bool(r[c])), axis=1)
    table = table.sort_values(["final_score", "effective_trend_reliability", "Ticker"], ascending=[False, False, True], kind="stable").reset_index(drop=True)
    table["rank"] = np.nan
    displayable = ranked_long_candidates(table)
    table.loc[displayable.index, "rank"] = range(1, len(displayable) + 1)
    return table


def ranked_long_candidates(table: pd.DataFrame) -> pd.DataFrame:
    """Rank finite scores with sufficient source history for comparison."""
    score = pd.to_numeric(table["final_score"], errors="coerce")
    valid = score.notna() & np.isfinite(score)
    history = pd.to_numeric(table.get("history_years", pd.Series(np.nan, index=table.index)), errors="coerce")
    gaps = pd.to_numeric(table.get("max_fiscal_year_gap", pd.Series(np.nan, index=table.index)), errors="coerce")
    source_errors = pd.to_numeric(table.get("source_error_count", pd.Series(np.nan, index=table.index)), errors="coerce")
    valid &= history.ge(4) & gaps.le(int(config.GENERAL_DEFAULTS["MAX_FISCAL_YEAR_GAP"])) & source_errors.fillna(1).eq(0)
    return table.loc[valid].sort_values(
        ["final_score", "effective_trend_reliability", "Ticker"],
        ascending=[False, False, True], kind="stable",
    )


def build_summary(table: pd.DataFrame, category: str, controls: dict[str, Any]) -> pd.DataFrame:
    slug = config.CATEGORIES[category]["slug"]
    ranked = ranked_long_candidates(table)
    # Summary pages are score-ranked research lists.  Quality gates remain
    # visible for every displayed name, but do not suppress a valid, scored
    # four-year history from the configured Top N display.
    selected = ranked.head(int(controls[f"TOP_N_{slug}"])).reset_index(drop=True)
    out = pd.DataFrame()
    out["Rank"] = range(1, len(selected) + 1)
    for column in ("Ticker", "Company Name", "Market Cap (mil)", "Industry", "Sector", "Exchange", "COM/ADR/Canadian",
                   "Primary Universe Rank", "Primary Final Strategy Score", "Primary PIT Verification Status"):
        if column in selected:
            out[column] = selected[column]
        elif column == "Company Name":
            out[column] = ""
    out.insert(3, "Quantitative Reason", (
        selected.apply(lambda r: _reason(r, category, controls, int(r.name) + 1), axis=1)
        if not selected.empty else pd.Series(index=out.index, dtype=object)
    ))
    out["Final Score"] = selected["final_score"]
    out["Leave-One-Family-Out Min Score"] = selected["leave_one_family_out_min_score"]
    out["Strong Family Count"] = selected["strong_family_count"]
    out["Strong Evidence Domain Count"] = selected["strong_evidence_domain_count"]
    out["Effective Sector Peer Weight"] = selected["effective_sector_peer_weight"]
    for family, weight in _control_weights(category, controls)[0].items():
        if weight > 0:
            out[f"{family.replace('_', ' ').title()} Score"] = selected[f"{family}__score"]
            out[f"{family.replace('_', ' ').title()} Contribution"] = selected[f"{family}__contribution_points"]
    out["Weight Coverage"] = selected["overall_weight_coverage"]
    out["Trend Metric Coverage"] = selected["TREND__metric_coverage"]
    out["Trend Fit R²"] = selected["trend_fit_r2_mean"]
    out["Effective Trend Reliability"] = selected["effective_trend_reliability"]
    out["Recent Favourable Breadth"] = selected["recent_favourable_breadth"]
    out["Recent Direction Observations"] = selected["recent_direction_observations"]
    out["Recent Metric Coverage"] = selected["INFLECTION__metric_coverage"]
    out["Financial Age Days"] = selected["financial_age_days"]
    out["Analysis As Of"] = selected["analysis_as_of"]
    out["Favourable Trend Breadth"] = selected["trend_direction_breadth"]
    out["Trend Data Penalty"] = selected["TREND_DATA_PENALTY__contribution_points"]
    out["No-Usable-Trend Extra Penalty"] = -selected["no_usable_trend_extra_penalty_points"]
    out["History Years"] = selected["history_years"]
    out["Trend Observations"] = selected["trend_observations"]
    out["Latest Fiscal Year"] = selected["latest_fiscal_year"]
    out["Max Fiscal Year Gap"] = selected["max_fiscal_year_gap"]
    out["Revenue CAGR"] = selected["revenue_cagr"]
    out["Latest Revenue Growth"] = selected["latest_revenue_growth"]
    out["Latest ROA"] = selected["latest_roa"]
    out["Latest Net Margin"] = selected["latest_net_margin"]
    out["Latest Pre-Tax Margin"] = selected["latest_pre_tax_margin"]
    out["Latest Sales / Assets"] = selected["latest_sales_to_assets"]
    out["Latest Working Capital / Assets"] = selected["latest_working_capital_to_assets"]
    out["Latest FCF Yield"] = selected["latest_fcf_yield"]
    out["Latest Debt / Equity"] = selected["latest_debt_to_equity"]
    out["Revenue 4Y Trend / Yr"] = selected["revenue_four_year_trend"]
    out["Net Margin 4Y Trend / Yr"] = selected["net_margin_four_year_trend"]
    out["FCF Margin 4Y Trend / Yr"] = selected["fcf_margin_four_year_trend"]
    out["ROA 4Y Trend / Yr"] = selected["roa_four_year_trend"]
    out["Debt/Equity 4Y Trend / Yr"] = selected["debt_to_equity_four_year_trend"]
    for metric in (
        "revenue_four_year_trend", "net_margin_four_year_trend", "fcf_margin_four_year_trend",
        "roa_four_year_trend", "gross_margin_four_year_trend", "debt_to_equity_four_year_trend",
        "positive_revenue_growth_share",
        "trend_direction_breadth",
    ):
        label = metric.replace("_", " ").title().replace("Four Year", "4Y")
        out[f"{label} Point Contribution"] = selected[f"{metric}__final_contribution_points"]
    out["Four-Year Revenue History"] = selected["four_year_revenue_history"]
    out["Warnings"] = selected["warnings"]
    return out


def _reason(row: pd.Series, category: str, controls: dict[str, Any], rank: int) -> str:
    family_weights = _control_weights(category, controls)[0]
    leaders = []
    for family, weight in family_weights.items():
        score = _finite(row.get(f"{family}__score"))
        points = _finite(row.get(f"{family}__contribution_points"))
        if weight > 0 and np.isfinite(score) and np.isfinite(points):
            leaders.append((points, family.replace("_", " ").title(), score))
    leaders.sort(reverse=True)
    lead = ", ".join(f"{name} {score:.1f}/100 ({points:.1f} pts)" for points, name, score in leaders[:3])
    def percent(column: str, label: str) -> str | None:
        value = _finite(row.get(column))
        return f"{label} {value:.1%}" if np.isfinite(value) else None

    financials = [item for item in (
        percent("revenue_cagr", "revenue CAGR"),
        percent("latest_revenue_growth", "latest revenue growth"),
        percent("latest_roa", "ROA"),
        percent("latest_net_margin", "net margin"),
        percent("latest_fcf_yield", "FCF yield"),
    ) if item]
    debt = _finite(row.get("latest_debt_to_equity"))
    if np.isfinite(debt):
        financials.append(f"debt/equity {debt:.2f}")
    breadth = [item for item in (
        percent("trend_direction_breadth", "favourable four-year trend breadth"),
        percent("recent_favourable_breadth", "recent favourable breadth"),
    ) if item]
    penalty = _finite(row.get("trend_data_penalty_points"))
    reliability = _finite(row.get("effective_trend_reliability"))
    coverage = _finite(row.get("overall_weight_coverage"))
    history = _finite(row.get("history_years"))
    year = _finite(row.get("latest_fiscal_year"))
    evidence = []
    if np.isfinite(history):
        evidence.append(f"{int(history)} unique fiscal years" + (f" through FY{int(year)}" if np.isfinite(year) else ""))
    if np.isfinite(coverage):
        evidence.append(f"{coverage:.0%} scored weight coverage")
    if np.isfinite(reliability):
        evidence.append(f"{reliability:.0%} effective trend reliability")
    if np.isfinite(penalty) and penalty > 0:
        evidence.append(f"-{penalty:.1f} pts reliability penalty")
    robust = _finite(row.get("leave_one_family_out_min_score"))
    if np.isfinite(robust):
        evidence.append(f"{robust:.1f} minimum score after removing one family")
    parts = [
        f"Ranked #{rank} in the current {category} primary cohort with a {row['final_score']:.1f} secondary score.",
        f"Largest score contributions: {lead}." if lead else "No scored family contributions available.",
    ]
    if breadth:
        parts.append("Trajectory: " + "; ".join(breadth) + ".")
    if financials:
        parts.append("Latest fundamentals and growth: " + "; ".join(financials) + ".")
    if evidence:
        parts.append("Evidence quality: " + "; ".join(evidence) + ".")
    return " ".join(parts)
