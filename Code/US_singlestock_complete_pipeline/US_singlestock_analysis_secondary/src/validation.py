from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import config
from src.model import ranked_long_candidates


def validate_results(
    analyses: dict[str, pd.DataFrame],
    summaries: dict[str, pd.DataFrame],
    controls: dict[str, Any],
) -> dict[str, dict[str, float]]:
    """Fail closed before publishing and return reconciliation evidence."""
    evidence: dict[str, dict[str, float]] = {}
    for category, table in analyses.items():
        summary = summaries[category]
        slug = config.CATEGORIES[category]["slug"]
        if table["Ticker"].duplicated().any():
            raise ValueError(f"{category}: duplicate tickers in analysis")
        if "Ticker" in summary and summary["Ticker"].duplicated().any():
            raise ValueError(f"{category}: duplicate tickers in summary")
        ranked = ranked_long_candidates(table)
        expected = ranked["Ticker"].head(int(controls[f"TOP_N_{slug}"])).tolist()
        actual = summary.get("Ticker", pd.Series(dtype=object)).tolist()
        if actual != expected:
            raise ValueError(f"{category}: summary does not match deterministic score ranking")
        if summary.get("Rank", pd.Series(dtype=int)).tolist() != list(range(1, len(summary) + 1)):
            raise ValueError(f"{category}: summary ranks are not sequential")
        family_cols = [
            f"{family}__contribution_points" for family, weight in config.FAMILY_WEIGHTS[category].items()
            if float(controls[f"FAMILY_WEIGHT_{slug}_{family}"]) > 0
        ]
        reconciled = table[family_cols].sum(axis=1, min_count=1) + table["TREND_DATA_PENALTY__contribution_points"]
        score_error = (reconciled - table["final_score"]).abs()
        max_score_error = float(score_error.dropna().max()) if score_error.notna().any() else 0.0
        if max_score_error > 1e-8:
            raise ValueError(f"{category}: family contribution reconciliation error {max_score_error}")

        _, metric_weights = _weights(category, controls)
        trend_metrics = [
            metric for metric, (family, _, _) in config.FEATURE_SPECS.items()
            if family == "TREND" and metric_weights[metric] > 0
        ]
        trend_points = table[[f"{metric}__final_contribution_points" for metric in trend_metrics]].sum(axis=1, min_count=1)
        trend_error = (trend_points - table["TREND__contribution_points"]).abs()
        max_trend_error = float(trend_error.dropna().max()) if trend_error.notna().any() else 0.0
        if max_trend_error > 1e-8:
            raise ValueError(f"{category}: trend contribution reconciliation error {max_trend_error}")
        evidence[category] = {
            "max_score_reconciliation_error": max_score_error,
            "max_trend_reconciliation_error": max_trend_error,
        }
    return evidence


def _weights(category: str, controls: dict[str, Any]):
    slug = config.CATEGORIES[category]["slug"]
    family = {name: float(controls[f"FAMILY_WEIGHT_{slug}_{name}"]) for name in config.FAMILY_WEIGHTS[category]}
    metric = {name: float(controls[f"METRIC_WEIGHT_{slug}_{name.upper()}"]) for name in config.FEATURE_SPECS}
    return family, metric
