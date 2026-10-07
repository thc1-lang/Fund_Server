"""Robust industry-relative statistics with continuous sector shrinkage."""
from __future__ import annotations

import numpy as np
import pandas as pd


def percentile(s: pd.Series) -> pd.Series:
    """Tie-aware empirical percentile with endpoints 0/100 and midpoint 50."""
    x = pd.to_numeric(s, errors="coerce")
    n = x.notna().sum()
    if n == 0:
        return pd.Series(np.nan, index=s.index, dtype=float)
    if n == 1:
        return pd.Series(np.where(x.notna(), 50.0, np.nan), index=s.index, dtype=float)
    return (x.rank(method="average") - 1.0) / (n - 1.0) * 100.0


def zscore(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    sd = x.std(ddof=0)
    return (x - x.mean()) / sd if pd.notna(sd) and sd > 0 else pd.Series(np.nan, index=s.index)


def robust_zscore(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    med = x.median()
    mad = (x - med).abs().median()
    return .6745 * (x - med) / mad if pd.notna(mad) and mad > 0 else pd.Series(np.nan, index=s.index)


def _group_percentile(x: pd.Series, group: pd.Series) -> pd.Series:
    return x.groupby(group, group_keys=False).transform(percentile)


def peer_enrich(
    df: pd.DataFrame,
    factor: str,
    min_obs: int = 5,
    shrinkage_strength: float = 8.0,
    z_cap: float = 3.0,
    percentile_weight: float = 0.50,
) -> pd.DataFrame:
    """Blend factor-specific industry statistics toward sector peers.

    The industry weight is ``n_industry / (n_industry + shrinkage_strength)``.
    A factor is unavailable when its sector has fewer than ``min_obs`` valid values;
    the model never falls back to a whole-US comparison.
    """
    x = pd.to_numeric(df[factor], errors="coerce").replace([np.inf, -np.inf], np.nan)
    from .economic_policy import signal_applicability
    from .scoring import active_signal_registry
    registry = active_signal_registry(df)
    if factor in registry:
        x = x.where(signal_applicability(df, factor, registry[factor][0]))
    if factor == "PEG Ratio":
        x = x.where(x > 0)
    industry, sector = df["Industry"], df["Sector"]
    ind_n = x.notna().groupby(industry).transform("sum").astype(float)
    sec_n = x.notna().groupby(sector).transform("sum").astype(float)
    valid_peer_set = sec_n >= min_obs
    weight = (ind_n / (ind_n + max(float(shrinkage_strength), 0.0))).where(valid_peer_set)
    if shrinkage_strength <= 0:
        weight = pd.Series(np.where(ind_n > 0, 1.0, 0.0), index=df.index).where(valid_peer_set)

    ind_mean = x.groupby(industry).transform("mean")
    sec_mean = x.groupby(sector).transform("mean")
    mean = weight * ind_mean + (1.0 - weight) * sec_mean

    ind_var = x.groupby(industry).transform(lambda s: s.var(ddof=0))
    sec_var = x.groupby(sector).transform(lambda s: s.var(ddof=0))
    variance = (
        weight * (ind_var.fillna(0.0) + (ind_mean - mean).pow(2))
        + (1.0 - weight) * (sec_var.fillna(0.0) + (sec_mean - mean).pow(2))
    )
    std = np.sqrt(variance).where(valid_peer_set)

    ind_median = x.groupby(industry).transform("median")
    sec_median = x.groupby(sector).transform("median")
    median = weight * ind_median + (1.0 - weight) * sec_median
    ind_mad = (x - ind_median).abs().groupby(industry).transform("median")
    sec_mad = (x - sec_median).abs().groupby(sector).transform("median")
    mad = (weight * ind_mad.fillna(0.0) + (1.0 - weight) * sec_mad.fillna(0.0)).where(valid_peer_set)

    ind_pct = _group_percentile(x, industry)
    sec_pct = _group_percentile(x, sector)
    pct = (weight * ind_pct + (1.0 - weight) * sec_pct).where(valid_peer_set & x.notna())
    z = ((x - mean) / std).where(std > 0)
    classical_score = (50.0 + 50.0 * z.clip(-z_cap, z_cap) / z_cap).clip(0, 100) if z_cap > 0 else pd.Series(np.nan, index=df.index)
    robust_z = (.6745 * (x - median) / mad).where(mad > 0)
    robust_score = (50.0 + 50.0 * robust_z.clip(-z_cap, z_cap) / z_cap).clip(0, 100) if z_cap > 0 else pd.Series(np.nan, index=df.index)
    robust_or_classical = robust_score.combine_first(classical_score)
    rank_weight = min(max(float(percentile_weight), 0.0), 1.0)
    # A finite constant cross-section supplies neutral evidence, not missing data.
    constant = valid_peer_set & x.notna() & std.eq(0)
    robust_or_classical = robust_or_classical.mask(constant, 50.0)
    z = z.mask(constant, 0.0)
    if rank_weight == 1.0:
        peer_score = pct
    elif rank_weight == 0.0:
        peer_score = robust_or_classical
    else:
        peer_score = rank_weight * pct + (1.0 - rank_weight) * robust_or_classical

    result = pd.DataFrame(index=df.index)
    result[f"{factor}__comparison_group_type"] = np.where(valid_peer_set, "Industry/sector shrinkage", "INSUFFICIENT SECTOR PEERS")
    result[f"{factor}__industry_valid_count"] = ind_n
    result[f"{factor}__sector_valid_count"] = sec_n
    result[f"{factor}__industry_weight"] = weight
    result[f"{factor}__peer_mean"] = mean.where(valid_peer_set)
    result[f"{factor}__peer_median"] = median.where(valid_peer_set)
    result[f"{factor}__peer_std"] = std
    result[f"{factor}__peer_mad"] = mad
    result[f"{factor}__industry_percentile"] = ind_pct.where(x.notna())
    result[f"{factor}__sector_percentile"] = sec_pct.where(x.notna())
    result[f"{factor}__percentile"] = pct
    result[f"{factor}__zscore"] = z
    result[f"{factor}__classical_z_score"] = classical_score.where(valid_peer_set & x.notna())
    result[f"{factor}__robust_zscore"] = robust_z
    result[f"{factor}__robust_z_score"] = robust_or_classical.where(valid_peer_set & x.notna())
    result[f"{factor}__percentile_weight"] = rank_weight
    result[f"{factor}__peer_score"] = peer_score.where(valid_peer_set & x.notna())
    return result


def enrich_factors(
    df: pd.DataFrame,
    factors: list[str],
    min_obs: int = 5,
    shrinkage_strength: float = 8.0,
    z_cap: float = 3.0,
    percentile_weight: float = 0.50,
) -> pd.DataFrame:
    parts = [df]
    for factor in factors:
        if factor in df:
            parts.append(peer_enrich(df, factor, min_obs, shrinkage_strength, z_cap, percentile_weight))
    return pd.concat(parts, axis=1)


def apply_liquidity_policy(
    df: pd.DataFrame,
    financial_sectors: tuple[str, ...],
    distress_z_threshold: float,
    distress_floor_score: float,
    excessive_wc_z_threshold: float,
    z_cap: float,
    payout_warning_ratio: float = 0.80,
    payout_distress_ratio: float = 1.50,
) -> pd.DataFrame:
    """Create asymmetric liquidity, working-capital and payout policy evidence."""
    out = pd.DataFrame(index=df.index)
    financial = df["Sector"].astype(str).str.casefold().isin({s.casefold() for s in financial_sectors})
    if "ordinary_liquidity_applicable" in df:
        financial = ~df["ordinary_liquidity_applicable"].fillna(False)
    out["Financial Company Liquidity Exclusion"] = financial
    ratio_map = {
        "Current Ratio": "liquidity_current_ratio",
        "Quick Ratio": "liquidity_quick_ratio",
        "Cash Ratio": "liquidity_cash_ratio",
    }
    lower_bound = -abs(float(z_cap))
    span = max(distress_z_threshold - lower_bound, 1e-12)
    for source, signal in ratio_map.items():
        z = pd.to_numeric(df.get(f"{source}__zscore", pd.Series(np.nan, index=df.index)), errors="coerce")
        score = pd.Series(50.0, index=df.index)
        distressed = z < distress_z_threshold
        score.loc[distressed] = distress_floor_score + (50.0 - distress_floor_score) * (
            (z.loc[distressed].clip(lower_bound, distress_z_threshold) - lower_bound) / span
        )
        score = score.where(z.notna() & ~financial)
        out[f"{signal}__directional_score"] = score.clip(0, 50)
        out[f"{signal}__distress_flag"] = distressed.where(z.notna() & ~financial)

    wc_z = pd.to_numeric(df.get("working_capital_to_sales__zscore", pd.Series(np.nan, index=df.index)), errors="coerce")
    out["Excessive Working Capital Flag"] = (wc_z > excessive_wc_z_threshold).where(wc_z.notna() & ~financial)
    out["Excessive Working Capital Z"] = wc_z.where(~financial)
    wc_score = pd.Series(50.0, index=df.index)
    wc_excess = wc_z > excessive_wc_z_threshold
    wc_span = max(abs(float(z_cap)) - excessive_wc_z_threshold, 1e-12)
    wc_score.loc[wc_excess] = 50.0 * (
        1.0 - (wc_z.loc[wc_excess].clip(excessive_wc_z_threshold, abs(float(z_cap))) - excessive_wc_z_threshold) / wc_span
    )
    out["working_capital_efficiency__directional_score"] = wc_score.clip(0, 50).where(wc_z.notna() & ~financial)

    def numeric_column(name: str) -> pd.Series:
        return pd.to_numeric(df[name], errors="coerce") if name in df else pd.Series(np.nan, index=df.index)

    payout = numeric_column("dividend_payout_ratio")
    dividend = numeric_column("Dividend ")
    trailing_eps = numeric_column("12 Mo Trailing EPS")
    payout_score = pd.Series(np.nan, index=df.index, dtype=float)
    no_dividend = dividend.notna() & dividend.eq(0)
    supported = payout.notna() & payout.ge(0)
    payout_score.loc[no_dividend | (supported & payout.le(payout_warning_ratio))] = 50.0
    elevated = supported & payout.gt(payout_warning_ratio)
    payout_span = max(payout_distress_ratio - payout_warning_ratio, 1e-12)
    payout_score.loc[elevated] = 50.0 * (
        1.0 - (payout.loc[elevated].clip(payout_warning_ratio, payout_distress_ratio) - payout_warning_ratio) / payout_span
    )
    loss_funded = dividend.fillna(0).gt(0) & trailing_eps.le(0)
    payout_score.loc[loss_funded] = 0.0
    out["payout_sustainability__directional_score"] = payout_score.clip(0, 50)
    out["Unsustainable Payout Flag"] = (loss_funded | payout.gt(payout_warning_ratio)).where(
        dividend.notna() | payout.notna()
    )
    return out
