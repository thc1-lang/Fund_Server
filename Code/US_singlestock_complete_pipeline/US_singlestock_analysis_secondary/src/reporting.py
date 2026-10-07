from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

import config


def build_model_registry(controls: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for category, cfg in config.CATEGORIES.items():
        slug = cfg["slug"]
        for metric, (family, direction, description) in config.FEATURE_SPECS.items():
            metric_weight = float(controls[f"METRIC_WEIGHT_{slug}_{metric.upper()}"])
            family_weight = float(controls[f"FAMILY_WEIGHT_{slug}_{family}"])
            rows.append({
                "Category": category, "Metric": metric, "Family": family, "Direction": direction,
                "Description / Lineage": description, "Metric Weight": metric_weight,
                "Family Weight": family_weight, "Active": metric_weight > 0 and family_weight > 0,
            })
    from src.shorts import SHORT_METRICS, SHORT_WEIGHTS
    for family, metrics in SHORT_METRICS.items():
        family_weight = float(controls[SHORT_WEIGHTS[family]])
        for metric, direction in metrics.items():
            metric_weight = float(controls[f"SHORT_METRIC_WEIGHT_{metric.upper()}"])
            description = (
                "Current primary Short score, four-/twelve-week share-price changes or four-week estimate revisions."
                if family == "point_in_time" else
                "Four-year or latest fiscal snapshot; adverse direction explicitly inverted for short ranking."
            )
            rows.append({"Category": "Short", "Metric": metric, "Family": family.upper(),
                         "Direction": f"{direction} supports Short",
                         "Description / Lineage": description,
                         "Metric Weight": metric_weight, "Family Weight": family_weight,
                         "Active": metric_weight > 0 and family_weight > 0})
    return pd.DataFrame(rows)


def build_run_audit(
    datasets: dict[str, pd.DataFrame],
    analyses: dict[str, pd.DataFrame],
    summaries: dict[str, pd.DataFrame],
    controls: dict[str, Any],
    reconciliation: dict[str, dict[str, float]],
    short_data: pd.DataFrame,
    short_analysis: pd.DataFrame,
    short_summary: pd.DataFrame,
) -> pd.DataFrame:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = []
    for category, table in analyses.items():
        raw = datasets[category]
        rows.append({
            "Generated At": generated,
            "Model Version": controls["MODEL_VERSION"],
            "Category": category,
            "Analysis As Of": table["analysis_as_of"].iloc[0],
            "Financial Age Pass": int(table["financial_age_gate_pass"].sum()),
            "Absolute Trend Pass": int(table["absolute_trend_gate_pass"].sum()),
            "Recent Direction Pass": int(table["recent_direction_gate_pass"].sum()),
            "Recent Coverage Pass": int(table["recent_coverage_gate_pass"].sum()),
            "Source Rows": len(raw),
            "Unique Tickers": table["Ticker"].nunique(),
            "Four-Year Histories": int(table["history_years"].eq(4).sum()),
            "Incomplete Histories": int(table["history_years"].lt(4).sum()),
            "Duplicate Fiscal Rows Dropped": int(pd.to_numeric(table["duplicate_fiscal_rows_dropped"], errors="coerce").fillna(0).sum()),
            "Eligible": int(table["eligible"].sum()),
            "Source Integrity Pass": int(table["source_integrity_gate_pass"].sum()),
            "Source Integrity Fail": int((~table["source_integrity_gate_pass"]).sum()),
            "Latest Essential Fields Pass": int(table["source_essential_gate_pass"].sum()),
            "Latest Essential Fields Fail": int((~table["source_essential_gate_pass"]).sum()),
            "Selected": len(summaries[category]),
            "Quality Score Pass": int(table["quality_score_gate_pass"].sum()),
            "Selection Coverage Pass": int(table["selection_coverage_gate_pass"].sum()),
            "Trend Reliability Pass": int(table["trend_reliability_gate_pass"].sum()),
            "Family Breadth Pass": int(table["family_breadth_gate_pass"].sum()),
            "Evidence Domain Breadth Pass": int(table["evidence_domain_breadth_gate_pass"].sum()),
            "Leave-One-Family-Out Pass": int(table["robustness_gate_pass"].sum()),
            "Median Effective Sector Peer Weight": pd.to_numeric(table["effective_sector_peer_weight"], errors="coerce").median(),
            "Median Weight Coverage": pd.to_numeric(table["overall_weight_coverage"], errors="coerce").median(),
            "Median Trend Metric Coverage": pd.to_numeric(table["TREND__metric_coverage"], errors="coerce").median(),
            "Median Effective Trend Reliability": pd.to_numeric(table["effective_trend_reliability"], errors="coerce").median(),
            "Median Reliability Penalty": pd.to_numeric(table["trend_data_penalty_points"], errors="coerce").median(),
            "Max Score Reconciliation Error": reconciliation[category]["max_score_reconciliation_error"],
            "Max Trend Reconciliation Error": reconciliation[category]["max_trend_reconciliation_error"],
        })
    def short_column(name: str) -> pd.Series:
        return short_analysis[name] if name in short_analysis else pd.Series(dtype=float)

    def short_passes(name: str) -> int:
        return int(short_column(name).fillna(False).astype(bool).sum())

    def short_median(name: str) -> float:
        return pd.to_numeric(short_column(name), errors="coerce").median()

    raw_score = pd.to_numeric(short_column("short_raw_score"), errors="coerce")
    reliability = pd.to_numeric(short_column("short_trend_reliability"), errors="coerce")
    score = pd.to_numeric(short_column("short_score"), errors="coerce")
    score_error = (score - (50 + (raw_score - 50) * reliability)).abs()
    rows.append({
        "Generated At": generated,
        "Model Version": controls["MODEL_VERSION"],
        "Category": "Short",
        "Analysis As Of": short_column("analysis_as_of").iloc[0] if not short_column("analysis_as_of").empty else "",
        "Financial Age Pass": short_passes("short_freshness_pass"),
        "Absolute Trend Pass": short_passes("short_absolute_trend_pass"),
        "Recent Direction Pass": short_passes("short_recent_deterioration_pass"),
        "Source Rows": len(short_data),
        "Unique Tickers": short_column("Ticker").nunique(),
        "Four-Year Histories": int(pd.to_numeric(short_column("history_years"), errors="coerce").eq(4).sum()),
        "Incomplete Histories": int(pd.to_numeric(short_column("history_years"), errors="coerce").lt(4).sum()),
        "Duplicate Fiscal Rows Dropped": int(pd.to_numeric(short_column("duplicate_fiscal_rows_dropped"), errors="coerce").fillna(0).sum()),
        "Eligible": short_passes("short_eligible"),
        "Source Integrity Pass": short_passes("short_source_integrity_pass"),
        "Source Integrity Fail": int(len(short_analysis) - short_passes("short_source_integrity_pass")),
        "Latest Essential Fields Pass": short_passes("short_source_essential_pass"),
        "Latest Essential Fields Fail": int(len(short_analysis) - short_passes("short_source_essential_pass")),
        "Selected": len(short_summary),
        "Quality Score Pass": short_passes("short_score_pass"),
        "Selection Coverage Pass": short_passes("short_coverage_pass"),
        "Trend Reliability Pass": short_passes("short_reliability_pass"),
        "Median Weight Coverage": short_median("short_weight_coverage"),
        "Median Trend Metric Coverage": short_median("SHORT_TREND_coverage"),
        "Median Effective Trend Reliability": short_median("short_trend_reliability"),
        "Max Score Reconciliation Error": float(score_error.dropna().max()) if score_error.notna().any() else 0.0,
        "Short Point-in-Time Coverage Pass": short_passes("short_point_in_time_coverage_pass"),
        "Short History Pass": short_passes("short_history_pass"),
        "Short Freshness Pass": short_passes("short_freshness_pass"),
    })
    return pd.DataFrame(rows)


def validate_short_results(analysis: pd.DataFrame, summary: pd.DataFrame, controls: dict[str, Any], primary_tickers: set[str]) -> None:
    from src.shorts import ranked_short_candidates
    if analysis["Ticker"].duplicated().any() or summary["Ticker"].duplicated().any():
        raise ValueError("Short: duplicate tickers in primary candidate or selected output")
    if not set(analysis["Ticker"]).issubset(primary_tickers):
        raise ValueError("Short: candidate lies outside the live primary Short tab")
    if set(analysis["Ticker"]) != primary_tickers:
        raise ValueError("Short: candidate audit does not account for every primary Short ticker")
    expected = ranked_short_candidates(analysis)["Ticker"].head(int(controls["SHORT_TOP_N"])).tolist()
    if summary.get("Ticker", pd.Series(dtype=object)).tolist() != expected:
        raise ValueError("Short: sheet output does not match deterministic scored ranking")
