"""Transparent strategy overlays built from the existing family sub-scores."""
from __future__ import annotations

import numpy as np
import pandas as pd


STRATEGIES = ("Safe", "High Growth Potential", "Turnaround Story")
INFLECTION_FAMILIES = ("GROWTH", "ESTIMATES_AND_REVISIONS", "MARKET_BEHAVIOUR")


def _deterministic_rank(df: pd.DataFrame, score: pd.Series, group: pd.Series | None = None) -> pd.Series:
    valid = score.notna()
    work = pd.DataFrame({"score": score.loc[valid]}, index=df.index[valid])
    work["ticker"] = df.loc[valid, "Ticker"].fillna("").astype(str).str.casefold()
    work["source"] = pd.to_numeric(df.loc[valid].get("Source Row"), errors="coerce")
    if group is not None:
        work["group"] = group.loc[valid].fillna("").astype(str)
        work = work.sort_values(["group", "score", "ticker", "source"], ascending=[True, False, True, True], kind="stable")
        ranked = work.groupby("group", sort=False).cumcount().add(1)
    else:
        work = work.sort_values(["score", "ticker", "source"], ascending=[False, True, True], kind="stable")
        ranked = pd.Series(np.arange(1, len(work) + 1), index=work.index)
    out = pd.Series(np.nan, index=df.index, dtype=float)
    out.loc[ranked.index] = ranked.astype(float)
    return out


def score_strategies(
    df: pd.DataFrame,
    strategy_weights: dict[str, dict[str, float]],
    min_weight_coverage: float,
    min_components: int,
    turnaround_min_inflection_score: float,
    turnaround_min_inflection_components: int,
    require_positive_eps_safe: bool = True,
    require_positive_eps_high_growth: bool = False,
    require_positive_eps_turnaround: bool = False,
) -> pd.DataFrame:
    """Score each strategy with stock-specific missing-data weight normalisation.

    For each available family, numerator = family score * configured weight.
    Final score = sum(numerators) / sum(applicable weights).  Missing families do
    not become zero; eligibility requires both weight coverage and component count.
    """
    columns: dict[str, object] = {}
    base_data = df["Data Status"].eq("SUFFICIENT")
    financial = df.get("financial_sector_leverage_exemption", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    for strategy, weights in strategy_weights.items():
        total_weight = float(sum(float(w) for w in weights.values() if float(w) > 0))
        structural_total_weight = pd.Series(total_weight, index=df.index, dtype=float)
        liquidity_weight = max(float(weights.get("LIQUIDITY_AND_EFFICIENCY", 0.0)), 0.0)
        structural_total_weight.loc[financial] -= liquidity_weight
        numerators, applied_weights = [], []
        for family, raw_weight in weights.items():
            weight = float(raw_weight)
            score = pd.to_numeric(df.get(f"{family} Long Sub-score"), errors="coerce")
            applicable = score.notna() & (weight > 0)
            applied = applicable.astype(float) * weight
            numerator = score * applied
            columns[f"{strategy} {family} Score"] = score
            columns[f"{strategy} {family} Weight"] = pd.Series(weight, index=df.index)
            columns[f"{strategy} {family} Applied Weight"] = applied
            columns[f"{strategy} {family} Weighted Numerator"] = numerator
            numerators.append(numerator)
            applied_weights.append(applied)
        denominator = pd.concat(applied_weights, axis=1).sum(axis=1)
        numerator_total = pd.concat(numerators, axis=1).sum(axis=1, min_count=1)
        raw_score = numerator_total / denominator.replace(0, np.nan)
        available_count = pd.concat([w.gt(0) for w in applied_weights], axis=1).sum(axis=1)
        positive_count = pd.Series(sum(float(w) > 0 for w in weights.values()), index=df.index)
        if liquidity_weight > 0:
            positive_count.loc[financial] -= 1
        weight_coverage = denominator / structural_total_weight.replace(0, np.nan)
        data_pass = weight_coverage.ge(min_weight_coverage) & available_count.ge(min_components)
        columns[f"{strategy} Total Configured Weight"] = structural_total_weight
        columns[f"{strategy} Applicable Weight"] = denominator
        columns[f"{strategy} Weight Coverage"] = weight_coverage
        columns[f"{strategy} Components Available"] = available_count
        columns[f"{strategy} Structural Components Excluded"] = sum(float(w) > 0 for w in weights.values()) - positive_count
        columns[f"{strategy} Components Missing"] = positive_count - available_count
        columns[f"{strategy} Component Availability"] = available_count / positive_count.replace(0, np.nan)
        columns[f"{strategy} Minimum Data Pass"] = data_pass
        columns[f"{strategy} Score Before Gates"] = raw_score.clip(0, 100)
        for family in weights:
            columns[f"{strategy} {family} Contribution Points"] = (
                columns[f"{strategy} {family} Weighted Numerator"] / denominator.replace(0, np.nan)
            )

    base_balance_pass = df.get("balance_sheet_gate_pass", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    high_growth_balance_pass = base_balance_pass | df.get("high_growth_balance_sheet_exemption", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    turnaround_balance_pass = base_balance_pass | df.get("turnaround_balance_sheet_exemption", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    safe_eps = df.get("eps_growth_valid_for_safe", pd.Series(True, index=df.index)).fillna(False).astype(bool) if require_positive_eps_safe else pd.Series(True, index=df.index)
    high_growth_eps = df.get("eps_growth_valid_for_high_growth", pd.Series(True, index=df.index)).fillna(False).astype(bool) if require_positive_eps_high_growth else pd.Series(True, index=df.index)
    turnaround_eps = df.get("eps_growth_valid_for_turnaround", pd.Series(True, index=df.index)).fillna(False).astype(bool) if require_positive_eps_turnaround else pd.Series(True, index=df.index)
    safe_risk_pass = base_balance_pass & safe_eps
    columns["Safe Balance-Sheet Gate Pass"] = safe_risk_pass
    columns["Safe Eligible"] = base_data & columns["Safe Minimum Data Pass"] & safe_risk_pass
    columns["Safe Score"] = columns["Safe Score Before Gates"].where(columns["Safe Eligible"])

    columns["High Growth Potential Balance-Sheet Gate Pass"] = high_growth_balance_pass
    columns["High Growth Potential Eligible"] = base_data & columns["High Growth Potential Minimum Data Pass"] & high_growth_balance_pass & high_growth_eps
    columns["High Growth Potential Score"] = columns["High Growth Potential Score Before Gates"].where(
        columns["High Growth Potential Eligible"]
    )

    inflection_weights = strategy_weights["Turnaround Story"]
    inflection_scores, inflection_applied = [], []
    for family in INFLECTION_FAMILIES:
        score = pd.to_numeric(df.get(f"{family} Long Sub-score"), errors="coerce")
        weight = float(inflection_weights.get(family, 0.0))
        applied = score.notna().astype(float) * weight
        inflection_scores.append(score * applied)
        inflection_applied.append(applied)
    inflection_denominator = pd.concat(inflection_applied, axis=1).sum(axis=1)
    inflection_score = pd.concat(inflection_scores, axis=1).sum(axis=1, min_count=1) / inflection_denominator.replace(0, np.nan)
    inflection_count = pd.concat([x.gt(0) for x in inflection_applied], axis=1).sum(axis=1)
    growth = pd.to_numeric(df.get("GROWTH Long Sub-score"), errors="coerce")
    estimates = pd.to_numeric(df.get("ESTIMATES_AND_REVISIONS Long Sub-score"), errors="coerce")
    core_inflection = growth.ge(turnaround_min_inflection_score) | estimates.ge(turnaround_min_inflection_score)
    inflection_pass = (
        inflection_score.ge(turnaround_min_inflection_score)
        & inflection_count.ge(turnaround_min_inflection_components)
        & core_inflection
    )
    columns["Turnaround Inflection Score"] = inflection_score
    columns["Turnaround Inflection Components Available"] = inflection_count
    columns["Turnaround Core Growth/Revisions Pass"] = core_inflection
    columns["Turnaround Inflection Gate Pass"] = inflection_pass
    columns["Turnaround Story Balance-Sheet Gate Pass"] = turnaround_balance_pass
    columns["Turnaround Story Eligible"] = base_data & columns["Turnaround Story Minimum Data Pass"] & inflection_pass & turnaround_balance_pass & turnaround_eps
    columns["Turnaround Story Score"] = columns["Turnaround Story Score Before Gates"].where(
        columns["Turnaround Story Eligible"]
    )

    for strategy in STRATEGIES:
        score = columns[f"{strategy} Score"]
        columns[f"{strategy} Within-Industry Rank"] = _deterministic_rank(df, score, df["Industry"])
        columns[f"{strategy} Universe Rank"] = _deterministic_rank(df, score)
        columns[f"{strategy} Implementation Eligible"] = columns[f"{strategy} Eligible"] & df.get("tradability_gate_pass", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    return pd.DataFrame(columns, index=df.index)


def reconstruct_strategy_score(row: pd.Series, strategy: str, families: list[str]) -> float | None:
    """Independent numerator/denominator reconciliation used by tests and live validation."""
    numerator = sum(
        float(row[f"{strategy} {family} Weighted Numerator"])
        for family in families
        if pd.notna(row[f"{strategy} {family} Weighted Numerator"])
    )
    denominator = float(row[f"{strategy} Applicable Weight"])
    return numerator / denominator if denominator > 0 else None
