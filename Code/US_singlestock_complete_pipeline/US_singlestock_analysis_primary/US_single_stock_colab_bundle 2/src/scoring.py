from __future__ import annotations
from collections import defaultdict
import numpy as np
import pandas as pd
from .transformations import DERIVED_FEATURES

RAW_SIGNALS = {
    "PEG Ratio": ("VALUATION", "lower_better", "growth_adjusted_value"),
    "This Yr`s Est.d Growth (F(1)/F(0))": ("GROWTH", "higher_better", "eps_growth"),
    "eg2_growth_pct": ("GROWTH", "higher_better", "eps_growth"),
    "eps_growth_acceleration": ("GROWTH", "higher_better", "growth_acceleration"),
    "Long-Term Growth Consensus Est.": ("GROWTH", "higher_better", "eps_growth"),
    "Sales Growth F(0)/F(-1)": ("GROWTH", "higher_better", "sales_growth"),
    "Current ROI (TTM)": ("PROFITABILITY_AND_RETURNS", "higher_better", "returns"),
    "Current ROA (TTM)": ("PROFITABILITY_AND_RETURNS", "higher_better", "returns"),
    "Net Margin %": ("PROFITABILITY_AND_RETURNS", "higher_better", "operating_margins"),
    "Current Ratio": ("LIQUIDITY_AND_EFFICIENCY", "target_range", "current_ratio"),
    "Quick Ratio": ("LIQUIDITY_AND_EFFICIENCY", "target_range", "quick_ratio"),
    "Cash Ratio": ("LIQUIDITY_AND_EFFICIENCY", "target_range", "cash_ratio"),
    "Turnover": ("LIQUIDITY_AND_EFFICIENCY", "higher_better", "asset_efficiency"),
    "Inventory Turnover": ("LIQUIDITY_AND_EFFICIENCY", "higher_better", "inventory_efficiency"),
    "Asset Utilization": ("LIQUIDITY_AND_EFFICIENCY", "higher_better", "asset_efficiency"),
    "Last EPS Surprise (%)": ("ESTIMATES_AND_REVISIONS", "higher_better", "eps_surprise"),
    "Previous EPS Surprise (%)": ("ESTIMATES_AND_REVISIONS", "higher_better", "eps_surprise"),
    "Avg EPS Surprise (Last 4 Qtrs)": ("ESTIMATES_AND_REVISIONS", "higher_better", "eps_surprise"),
    "% Change F1 Est. (4 weeks)": ("ESTIMATES_AND_REVISIONS", "higher_better", "annual_estimate_revision"),
    "% Change F2 Est. (4 weeks)": ("ESTIMATES_AND_REVISIONS", "higher_better", "annual_estimate_revision"),
    "% Change Q0 Est. (4 weeks)": ("ESTIMATES_AND_REVISIONS", "higher_better", "quarterly_estimate_revision"),
    "% Change Q1 Est. (4 weeks)": ("ESTIMATES_AND_REVISIONS", "higher_better", "quarterly_estimate_revision"),
    "% Change Q2 Est. (4 weeks)": ("ESTIMATES_AND_REVISIONS", "higher_better", "quarterly_estimate_revision"),
    "% Change LT Growth Est. (4 weeks)": ("ESTIMATES_AND_REVISIONS", "higher_better", "long_term_estimate_revision"),
    "Div. Yield %": ("SHAREHOLDER_AND_YIELD", "higher_better", "dividend_yield"),
    "% Price Change (1 Week)": ("MARKET_BEHAVIOUR", "higher_better", "return_short"),
    "% Price Change (4 Weeks)": ("MARKET_BEHAVIOUR", "higher_better", "return_short"),
    "% Price Change (12 Weeks)": ("MARKET_BEHAVIOUR", "higher_better", "return_medium"),
    "% Price Change (YTD)": ("MARKET_BEHAVIOUR", "higher_better", "return_medium"),
    "Relative Price Change (YTD)": ("MARKET_BEHAVIOUR", "higher_better", "return_medium"),
    "Current Avg Broker Rec": ("BROKER_AND_TARGET", "lower_better", "broker_rating"),
    "Market Cap (mil)": ("TRADABILITY", "higher_better", "liquidity_scale"),
}


def active_signal_registry(df: pd.DataFrame) -> dict[str, tuple[str, str, str]]:
    reg = {k: v for k, v in RAW_SIGNALS.items() if k in df and v[1] in {"higher_better", "lower_better"}}
    for name, spec in DERIVED_FEATURES.items():
        if name in df and spec.family and spec.direction in {"higher_better", "lower_better"}:
            reg[name] = (spec.family, spec.direction, spec.duplicate_group)
    for signal, family in (
        ("liquidity_current_ratio", "LIQUIDITY_AND_EFFICIENCY"),
        ("liquidity_quick_ratio", "LIQUIDITY_AND_EFFICIENCY"),
        ("liquidity_cash_ratio", "LIQUIDITY_AND_EFFICIENCY"),
        ("working_capital_efficiency", "LIQUIDITY_AND_EFFICIENCY"),
        ("payout_sustainability", "SHAREHOLDER_AND_YIELD"),
    ):
        if f"{signal}__directional_score" in df:
            reg[signal] = (family, "higher_better", signal)
    return reg


def score_expanded(
    df: pd.DataFrame,
    family_weights: dict[str, float],
    min_factor_coverage: float = .70,
    min_family_coverage: float = .50,
    confidence_factor_weight: float = .60,
    confidence_family_weight: float = .40,
    signal_weights: dict[str, float] | None = None,
    duplicate_group_weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Build the shared quant composite from independent economic groups.

    Correlated signals first collapse into duplicate-exposure groups, groups then
    collapse into families, and families into the final score.  Coverage and
    confidence are measured on independent groups rather than raw column count.
    """
    reg = active_signal_registry(df)
    signal_weights = signal_weights or {}
    duplicate_group_weights = duplicate_group_weights or {}
    financial_liquidity_exclusion = df.get(
        "Financial Company Liquidity Exclusion", pd.Series(False, index=df.index)
    ).fillna(False).astype(bool)
    families: dict[str, dict[str, list[tuple[str, pd.Series, pd.Series, float]]]] = defaultdict(lambda: defaultdict(list))
    out: dict[str, pd.Series | float] = {}
    raw_factor_available = []
    raw_factor_applicable = []
    for factor, (family, direction, duplicate) in reg.items():
        score_col = f"{factor}__directional_score" if f"{factor}__directional_score" in df else f"{factor}__peer_score"
        if score_col not in df:
            continue
        p = pd.to_numeric(df[score_col], errors="coerce")
        long = p if direction == "higher_better" else 100 - p
        short = 100 - p if direction == "higher_better" else p
        structurally_applicable = (
            ~financial_liquidity_exclusion
            if family == "LIQUIDITY_AND_EFFICIENCY" else pd.Series(True, index=df.index)
        )
        long = long.where(structurally_applicable)
        short = short.where(structurally_applicable)
        signal_weight = float(signal_weights.get(factor, 1.0))
        families[family][duplicate].append((factor, long, short, signal_weight))
        out[f"{factor}__economic_long_score"] = long
        out[f"{factor}__economic_short_score"] = short
        out[f"{factor}__configured_signal_weight"] = signal_weight
        raw_factor_available.append(long.notna() & (signal_weight > 0))
        raw_factor_applicable.append(structurally_applicable & (signal_weight > 0))
    family_long, family_short = {}, {}
    independent_group_available: list[pd.Series] = []
    independent_group_applicable: list[pd.Series] = []
    independent_group_weights: list[float] = []
    for fam, groups in families.items():
        glong, gshort, group_names = [], [], []
        for group_name, pairs in groups.items():
            weights = pd.Series([p[3] for p in pairs], dtype=float)
            lraw = pd.concat([p[1] for p in pairs], axis=1)
            sraw = pd.concat([p[2] for p in pairs], axis=1)
            valid_weight = lraw.notna().mul(weights.values, axis=1).sum(axis=1).replace(0, np.nan)
            group_long = lraw.mul(weights.values, axis=1).sum(axis=1, min_count=1) / valid_weight
            group_short = sraw.mul(weights.values, axis=1).sum(axis=1, min_count=1) / valid_weight
            configured_signal_weight = float(weights.clip(lower=0).sum())
            group_cov = valid_weight.fillna(0) / configured_signal_weight if configured_signal_weight > 0 else 0.0
            glong.append(group_long); gshort.append(group_short)
            group_names.append(group_name)
            group_weight = float(duplicate_group_weights.get(group_name, 1.0))
            out[f"{fam} | {group_name} Group Long Score"] = group_long
            out[f"{fam} | {group_name} Group Short Score"] = group_short
            out[f"{fam} | {group_name} Group Signal Coverage"] = group_cov
            out[f"{fam} | {group_name} Group Weight"] = group_weight
            independent_group_available.append(group_long.notna() & (group_weight > 0))
            independent_group_applicable.append(
                (~financial_liquidity_exclusion if fam == "LIQUIDITY_AND_EFFICIENCY"
                 else pd.Series(True, index=df.index)) & (group_weight > 0)
            )
            independent_group_weights.append(max(group_weight, 0.0))
        ldf, sdf = pd.concat(glong, axis=1), pd.concat(gshort, axis=1)
        ldf.columns = sdf.columns = group_names
        group_weights = pd.Series({g: float(duplicate_group_weights.get(g, 1.0)) for g in group_names})
        available_weight = ldf.notna().mul(group_weights, axis=1).sum(axis=1).replace(0, np.nan)
        total_group_weight = float(group_weights.clip(lower=0).sum())
        cov = ldf.notna().mul(group_weights, axis=1).sum(axis=1) / total_group_weight if total_group_weight > 0 else 0.0
        lscore = (ldf.mul(group_weights, axis=1).sum(axis=1, min_count=1) / available_weight).where(cov >= min_family_coverage)
        sscore = (sdf.mul(group_weights, axis=1).sum(axis=1, min_count=1) / available_weight).where(cov >= min_family_coverage)
        family_long[fam], family_short[fam] = lscore, sscore
        out[f"{fam} Long Sub-score"] = lscore
        out[f"{fam} Short Sub-score"] = sscore
        out[f"{fam} Coverage"] = cov
    weighted_l, weighted_s, applied_family_weights = [], [], []
    for fam, lscore in family_long.items():
        w = float(family_weights.get(fam, 1.0))
        weighted_l.append(lscore * w); weighted_s.append(family_short[fam] * w)
        applied = lscore.notna().astype(float) * w
        applied_family_weights.append(applied)
        out[f"Quant Composite {fam} Weight"] = w
        out[f"Quant Composite {fam} Applied Weight"] = applied
        out[f"Quant Composite {fam} Long Weighted Numerator"] = lscore * applied
        out[f"Quant Composite {fam} Short Weighted Numerator"] = family_short[fam] * applied
    denom = pd.concat(applied_family_weights, axis=1).sum(axis=1)
    long = pd.concat(weighted_l, axis=1).sum(axis=1, min_count=1) / denom.replace(0, np.nan)
    short = pd.concat(weighted_s, axis=1).sum(axis=1, min_count=1) / denom.replace(0, np.nan)
    factor_score_frame = pd.concat([out[f"{factor}__economic_long_score"] for factor in reg], axis=1)
    raw_available_frame = pd.concat(raw_factor_available, axis=1)
    raw_applicable_frame = pd.concat(raw_factor_applicable, axis=1)
    raw_factor_cov = raw_available_frame.sum(axis=1) / raw_applicable_frame.sum(axis=1).replace(0, np.nan)
    group_availability_frame = pd.concat(independent_group_available, axis=1)
    group_applicability_frame = pd.concat(independent_group_applicable, axis=1)
    group_weight_series = pd.Series(independent_group_weights, dtype=float)
    group_weight_total = group_applicability_frame.mul(group_weight_series.values, axis=1).sum(axis=1)
    independent_group_cov = group_availability_frame.mul(group_weight_series.values, axis=1).sum(axis=1) / group_weight_total.replace(0, np.nan)
    out["Positive Peer Evidence Count"] = factor_score_frame.gt(50).sum(axis=1)
    out["Negative Peer Evidence Count"] = factor_score_frame.lt(50).sum(axis=1)
    out["Neutral Peer Evidence Count"] = factor_score_frame.eq(50).sum(axis=1)
    family_availability = pd.concat([v.notna() for v in family_long.values()], axis=1)
    family_availability.columns = list(family_long)
    family_applicability = pd.DataFrame(True, index=df.index, columns=list(family_long))
    if "LIQUIDITY_AND_EFFICIENCY" in family_applicability:
        family_applicability.loc[financial_liquidity_exclusion, "LIQUIDITY_AND_EFFICIENCY"] = False
    family_weight_vector = pd.Series([max(float(family_weights.get(fam, 1.0)), 0.0) for fam in family_long])
    family_weight_total = family_applicability.mul(family_weight_vector.values, axis=1).sum(axis=1)
    family_coverage = family_availability.mul(family_weight_vector.values, axis=1).sum(axis=1) / family_weight_total.replace(0, np.nan)
    out["Raw Signal Coverage"] = raw_factor_cov
    out["Independent Group Coverage"] = independent_group_cov
    out["Expanded Family Coverage"] = family_coverage
    confidence_total = confidence_factor_weight + confidence_family_weight
    out["Score Confidence"] = (
        100 * (confidence_factor_weight * independent_group_cov + confidence_family_weight * family_coverage)
        / confidence_total
    ).clip(0, 100)
    data_pass = independent_group_cov.ge(min_factor_coverage) & denom.gt(0)
    raw_long = long.clip(0, 100).where(data_pass)
    raw_short = short.clip(0, 100).where(data_pass)
    reliability = out["Score Confidence"] / 100.0
    adjusted_long = (50.0 + reliability * (raw_long - 50.0)).clip(0, 100)
    adjusted_short = (50.0 + reliability * (raw_short - 50.0)).clip(0, 100)
    structurally_applicable_family_weight = family_weight_total
    out["Quant Composite Total Configured Weight"] = structurally_applicable_family_weight
    out["Quant Composite Applicable Weight"] = denom
    out["Quant Composite Weight Coverage"] = denom / structurally_applicable_family_weight.replace(0, np.nan)
    out["Quant Composite Components Available"] = family_availability.sum(axis=1)
    out["Quant Composite Structural Components Excluded"] = len(family_long) - family_applicability.sum(axis=1)
    out["Quant Composite Components Missing"] = family_applicability.sum(axis=1) - out["Quant Composite Components Available"]
    out["Quant Composite Minimum Data Pass"] = data_pass
    out["Expanded Raw Long Score"] = raw_long
    out["Expanded Raw Short Score"] = raw_short
    out["Score Reliability Adjustment"] = adjusted_long - raw_long
    for fam in family_long:
        out[f"Quant Composite {fam} Long Contribution Points"] = out[f"Quant Composite {fam} Long Weighted Numerator"] / denom.replace(0, np.nan)
        out[f"Quant Composite {fam} Short Contribution Points"] = out[f"Quant Composite {fam} Short Weighted Numerator"] / denom.replace(0, np.nan)
    out["Expanded Long Score"] = adjusted_long
    out["Expanded Short Score"] = adjusted_short
    out["Expanded Net Quant Score"] = adjusted_long - adjusted_short
    out["Data Status"] = np.where(data_pass, "SUFFICIENT", "INSUFFICIENT DATA")
    return pd.DataFrame(out, index=df.index)
