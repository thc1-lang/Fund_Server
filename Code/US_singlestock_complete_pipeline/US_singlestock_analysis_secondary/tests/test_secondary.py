from __future__ import annotations

import pandas as pd
import pytest
import config
from src.data_integrity import apply_integrity_gate, audit_source_rows
from src.handoff import build_handoff, build_research_candidates

from src.io import default_controls, read_controls
from src.model import _trend_stats, append_missing_primary, build_features, build_summary, score_category


def test_long_sheet_names_follow_the_same_category_pattern():
    for category, sheets in config.CATEGORIES.items():
        assert sheets["primary_summary_sheet"] == f"{category} Summary"
        assert sheets["data_sheet"] == f"{category} Data"
        assert sheets["analysis_sheet"] == f"{category} Analysis"
        assert sheets["secondary_summary_sheet"] == f"{category} Secondary Summary"


def sample_data() -> pd.DataFrame:
    rows = []
    for ticker, scale in (("AAA", 1.0), ("BBB", 0.7), ("CCC", 0.4)):
        for year, growth in ((2022, 1.0), (2023, 1.1), (2024, 1.25), (2025, 1.5)):
            revenue = 100 * scale * growth
            rows.append({
                "Ticker": ticker, "Fiscal Year": year, "Fiscal Period End": f"{year}-12-31",
                "Revenue": revenue, "Net Income": revenue * (0.12 * scale), "EBIT": revenue * .15,
                "EBITDA": revenue * .18, "Interest Expense": 2, "Interest Coverage Ratio": 8 * scale,
                "Current Assets": 80, "Current Liabilities": 40, "Total Assets": 200,
                "Shareholders Equity": 100, "Operating Cash Flow": revenue * .15,
                "Free Cash Flow": revenue * .12, "Year End Price": 20, "Market Cap": 1000,
                "Cash": 50, "Total Debt": 20, "Enterprise Value": 970,
                "Free Cash Flow Yield": .05 * scale, "Current Ratio": 2 * scale,
                "Quick Ratio": 1.5 * scale, "Return on Assets Ratio": .10 * scale,
                "Return on Equity Ratio": .18 * scale, "EV / EBIT": 12 / scale,
                "EV / EBITDA": 10 / scale, "Gross Margin": .4 * scale, "Net Margin": .12 * scale,
                "Pre-Tax Profit Ratio": .14 * scale, "Sales to Assets Ratio": .5 * scale,
                "Working Capital to Total Assets Ratio": .2 * scale,
                "Debt to Equity Ratio": .2 / scale, "Price / Free Cash Flow Per Share": 15 / scale,
                "Error": "", "Missing Data Notes": "", "Data Source": "test", "Audit Notes": "",
                "Price Date": f"{year}-12-31",
            })
    return pd.DataFrame(rows)


def test_features_and_scores_are_auditable():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    features = build_features(sample_data())
    assert features.loc[features.Ticker.eq("AAA"), "history_years"].iloc[0] == 4
    assert features["revenue_cagr"].notna().all()
    assert features["revenue_four_year_trend"].notna().all()
    assert features["four_year_revenue_history"].str.count(r"\|").eq(3).all()
    analysis = score_category(features, pd.DataFrame({"Ticker": ["AAA", "BBB", "CCC"], "Company Name": ["A", "B", "C"]}), "Safe", controls)
    assert {"latest_roa__score", "latest_roa__weight", "latest_roa__weighted_points", "PROFITABILITY__score", "TREND__score", "TREND__contribution_points", "final_score", "eligible"}.issubset(analysis.columns)
    assert analysis.iloc[0]["final_score"] >= analysis.iloc[-1]["final_score"]
    contribution_cols = [column for column in analysis if column.endswith("__contribution_points") and column.split("__", 1)[0].isupper()]
    assert abs(analysis.iloc[0][contribution_cols].sum() - analysis.iloc[0]["final_score"]) < 1e-9
    trend_metric_points = [column for column in analysis if column.endswith("__final_contribution_points") and "four_year_trend" in column]
    trend_metric_points.append("positive_revenue_growth_share__final_contribution_points")
    trend_metric_points.append("trend_direction_breadth__final_contribution_points")
    assert abs(analysis.iloc[0][trend_metric_points].sum() - analysis.iloc[0]["TREND__contribution_points"]) < 1e-9
    assert "pts" in build_summary(analysis, "Safe", controls).iloc[0]["Quantitative Reason"]


def test_long_reasons_follow_the_selected_candidate_in_column_d():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    analysis = score_category(build_features(sample_data()), pd.DataFrame(), "Safe", controls)
    summary = build_summary(analysis, "Safe", controls)
    assert summary.columns[3] == "Quantitative Reason"
    assert summary.columns[4] == "Final Score"
    for _, candidate in summary.iterrows():
        reason = candidate["Quantitative Reason"]
        assert f"Ranked #{candidate['Rank']}" in reason
        assert f"{candidate['Final Score']:.1f} secondary score" in reason
        assert "Trajectory:" in reason and "Latest fundamentals" in reason
        assert "Evidence quality:" in reason
    assert summary["Quantitative Reason"].nunique() == len(summary)
    replacement = next(ticker for ticker in analysis["Ticker"] if ticker != summary.iloc[0]["Ticker"])
    changed = analysis.copy()
    changed.loc[changed["Ticker"].eq(replacement), "final_score"] = 200.0
    changed.loc[changed["Ticker"].eq(replacement), "eligible"] = True
    refreshed = build_summary(changed, "Safe", controls).iloc[0]
    assert refreshed["Ticker"] == replacement
    assert "Ranked #1" in refreshed["Quantitative Reason"]
    assert "200.0 secondary score" in refreshed["Quantitative Reason"]


def test_short_reasons_follow_the_selected_candidate_in_column_d():
    from src.shorts import build_short_summary, score_shorts

    controls = default_controls()
    controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    metadata = pd.DataFrame({
        "Ticker": ["AAA", "BBB", "CCC"], "Primary Short Rank": [1, 2, 3],
        "Primary Short Score": [80, 70, 60],
        "Primary 4-Week Price Change (%)": [-10, -5, 2],
        "Primary 12-Week Price Change (%)": [-20, -8, 5],
        "Primary F1 Estimate Change 4-Week (%)": [-8, -3, 1],
        "Primary F2 Estimate Change 4-Week (%)": [-5, -2, 2],
        "primary_short_score": [80, 70, 60],
        "primary_price_change_4w": [-10, -5, 2],
        "primary_price_change_12w": [-20, -8, 5],
        "primary_f1_estimate_change_4w": [-8, -3, 1],
        "primary_f2_estimate_change_4w": [-5, -2, 2],
    })
    scored = score_shorts(build_features(sample_data()), metadata, controls, "2026-09-26")
    summary = build_short_summary(scored, controls)
    assert summary.columns[3] == "Quantitative Reason"
    assert summary.columns[4] == "Sector"
    for _, candidate in summary.iterrows():
        reason = candidate["Quantitative Reason"]
        assert f"Ranked #{candidate['Rank']}" in reason
        assert f"{candidate['Short Score']:.1f} secondary score" in reason
        assert "Deterioration evidence:" in reason and "Fiscal fundamentals:" in reason
        assert "Evidence quality:" in reason
    assert summary["Quantitative Reason"].nunique() == len(summary)
    replacement = next(ticker for ticker in scored["Ticker"] if ticker != summary.iloc[0]["Ticker"])
    changed = scored.copy()
    changed.loc[changed["Ticker"].eq(replacement), "short_score"] = 200.0
    refreshed = build_short_summary(changed, controls).iloc[0]
    assert refreshed["Ticker"] == replacement
    assert "Ranked #1" in refreshed["Quantitative Reason"]
    assert "200.0 secondary score" in refreshed["Quantitative Reason"]


def test_top_n_control_and_control_parser():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["TOP_N_SAFE"] = 2
    controls["MIN_SELECTION_SCORE_SAFE"] = 0
    controls["MIN_SELECTION_WEIGHT_COVERAGE_SAFE"] = 0
    controls["MIN_EFFECTIVE_TREND_RELIABILITY_SAFE"] = 0
    controls["MIN_STRONG_FAMILIES_SAFE"] = 0
    controls["MIN_LEAVE_ONE_FAMILY_OUT_SCORE"] = 0
    controls["SAFE_MIN_POSITIVE_HISTORY_SHARE"] = 0
    features = build_features(sample_data())
    analysis = score_category(features, pd.DataFrame(), "Safe", controls)
    analysis["eligible"] = True
    summary = build_summary(analysis, "Safe", controls)
    assert len(summary) == 2
    parsed = read_controls([["Selection cap", "TOP_N_SAFE", "7", ""]])
    assert parsed["TOP_N_SAFE"] == 7


def test_long_summary_uses_score_rank_even_when_quality_gates_fail():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["TOP_N_SAFE"] = 2
    analysis = score_category(build_features(sample_data()), pd.DataFrame(), "Safe", controls)
    analysis["eligible"] = False
    analysis["gate_fail_reasons"] = "quality_score_gate"
    analysis.loc[analysis["Ticker"].eq("CCC"), "final_score"] = 200.0

    summary = build_summary(analysis, "Safe", controls)

    assert summary["Ticker"].tolist() == ["CCC", "AAA"]
    assert "Qualified" not in summary
    assert "Failed Gates" not in summary


def test_minimum_trend_observations_is_effective():
    features = build_features(sample_data(), min_trend_observations=5)
    assert features["trend_observations"].eq(4).all()
    assert features["revenue_four_year_trend"].isna().all()

    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    analysis = score_category(features, pd.DataFrame(), "Safe", controls)
    expected_penalty = (
        (1 - analysis["effective_trend_reliability"]) * controls["MISSING_TREND_PENALTY_POINTS"]
        + analysis["effective_trend_reliability"].eq(0) * controls["NO_USABLE_TREND_EXTRA_PENALTY_POINTS"]
    )
    assert (analysis["trend_data_penalty_points"] - expected_penalty).abs().max() < 1e-9
    family_points = [column for column in analysis if column.endswith("__contribution_points") and column.split("__", 1)[0].isupper()]
    assert (analysis[family_points].sum(axis=1) - analysis["final_score"]).abs().max() < 1e-9


def test_duplicate_fiscal_years_are_deduplicated_deterministically():
    data = sample_data()
    duplicate = data.loc[(data["Ticker"] == "AAA") & (data["Fiscal Year"] == 2025)].copy()
    duplicate["Revenue"] = 999
    features = build_features(pd.concat([data, duplicate], ignore_index=True))
    aaa = features.loc[features["Ticker"].eq("AAA")].iloc[0]
    assert aaa["history_years"] == 4
    assert aaa["duplicate_fiscal_rows_dropped"] == 1
    assert aaa["latest_revenue"] == 999
    assert "Dropped 1 duplicate" in aaa["warnings"]


def test_source_integrity_flags_duplicate_period_and_implausible_share_jump():
    source = pd.DataFrame({
        "Ticker": ["ASTS", "ASTS", "PDD", "PDD"],
        "Fiscal Year": [2022, 2023, 2023, 2024],
        "Fiscal Period End": ["2022-12-31", "2022-12-31", "2023-12-31", "2024-12-31"],
        "Market Cap": [100., 120., 792_000., 537.],
        "Shares Outstanding": [10., 12., 5_400., 5.5],
        "Year End Price": [10., 10., 146.6666667, 97.6363636],
        "Price Currency": ["USD"] * 4,
    })
    audit = audit_source_rows("Short", source, as_of="2026-09-26")
    assert audit.loc[audit.Ticker.eq("ASTS"), "Integrity Flags"].str.contains("DUPLICATE_PERIOD_END").all()
    assert audit.loc[audit.Ticker.eq("PDD"), "Integrity Flags"].str.contains("SHARE_COUNT_JUMP_20X").all()
    assert audit.loc[audit.Ticker.eq("PDD"), "Integrity Flags"].str.contains("MARKET_CAP_JUMP_20X").all()
    assert not audit["Provenance Complete"].any()


def test_source_integrity_gate_rejects_anomaly_without_changing_score():
    analysis = pd.DataFrame({"Ticker": ["BAD", "OK"], "eligible": [True, True],
                             "gate_fail_reasons": ["", ""], "final_score": [75., 75.]})
    audit = pd.DataFrame({"Ticker": ["BAD", "OK"], "Integrity Pass": [False, True],
                          "Fiscal Period End": ["2025-12-31"] * 2,
                          "Missing Essential Fields": ["", ""]})
    gated = apply_integrity_gate(analysis, audit)
    assert gated["eligible"].tolist() == [False, True]
    assert gated["final_score"].tolist() == [75., 75.]
    assert gated.loc[0, "gate_fail_reasons"] == "source_integrity"


def test_latest_essential_gap_blocks_model_eligibility_without_changing_score():
    analysis = pd.DataFrame({"Ticker": ["GAP", "OK"], "eligible": [True, True],
                             "gate_fail_reasons": ["", ""], "final_score": [70., 65.]})
    audit = pd.DataFrame({"Ticker": ["GAP", "GAP", "OK"], "Integrity Pass": [True] * 3,
                          "Fiscal Period End": ["2024-12-31", "2025-12-31", "2025-12-31"],
                          "Missing Essential Fields": ["", "Market Cap", ""]})
    gated = apply_integrity_gate(analysis, audit).set_index("Ticker")
    assert not gated.loc["GAP", "eligible"]
    assert gated.loc["GAP", "gate_fail_reasons"] == "source_essential"
    assert gated.loc["GAP", "final_score"] == 70
    assert gated.loc["OK", "eligible"]


def test_handoff_keeps_score_ranked_candidates_in_data_review_until_source_verified():
    controls = default_controls()
    candidate = pd.DataFrame({
        "Ticker": ["A", "B"], "Company Name": ["A Co", "B Co"],
        "Primary Universe Rank": [1, 5], "Primary Final Strategy Score": [70., 68.],
        "Primary PIT Verification Status": ["UNVERIFIED_VENDOR_SNAPSHOT"] * 2,
        "history_years": [4, 4], "final_score": [80., 75.], "rank": [1., 5.],
    })
    primary = {category: (candidate[["Ticker", "Company Name", "Primary Universe Rank",
                                    "Primary Final Strategy Score", "Primary PIT Verification Status"]]
                          if category == "Safe" else pd.DataFrame(columns=["Ticker"]))
               for category in config.CATEGORIES}
    audit = pd.DataFrame({"Category": ["Safe", "Safe"], "Ticker": ["A", "B"],
                          "Integrity Flags": ["", ""], "Provenance Complete": [False, False]})
    ready, watch = build_handoff({"Safe": candidate}, primary,
                                  pd.DataFrame(columns=["Ticker"]), pd.DataFrame(columns=["Ticker"]),
                                  audit, controls)
    assert ready.empty
    assert set(watch.Ticker) == {"A", "B"}
    assert watch.loc[watch.Ticker.eq("B"), "Disposition"].iloc[0] == "DATA REVIEW"
    assert "PRIMARY_POINT_IN_TIME_UNVERIFIED" in watch.loc[watch.Ticker.eq("B"), "Data Review Items"].iloc[0]


def test_research_candidates_separate_traceable_score_leaders_from_data_review():
    watch = pd.DataFrame({"Category": ["Safe", "Safe", "Safe"], "Ticker": ["A", "B", "C"],
                          "Score Rank": [1., 2., 4.], "Disposition": ["DATA REVIEW"] * 3,
                          "Secondary Score": [70., 65., 80.]})
    audit = pd.DataFrame({"Category": ["Safe", "Safe"], "Ticker": ["A", "B"],
                          "Integrity Pass": [True, True], "Research Traceable": [True, True],
                          "Fiscal Period End": ["2025-12-31"] * 2,
                          "Missing Essential Fields": ["", "Market Cap"]})
    research = build_research_candidates(watch, audit, default_controls()).set_index("Ticker")
    assert set(research.index) == {"A", "B"}
    assert research.loc["A", "Research Status"] == "CURRENT-DATA RESEARCH"
    assert research.loc["B", "Research Status"] == "SOURCE REVIEW"
    assert "Market Cap" in research.loc["B", "Research Blockers"]


def test_trend_reliability_rewards_complete_clean_history():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    features = build_features(sample_data())
    analysis = score_category(features, pd.DataFrame(), "Safe", controls)
    assert analysis["trend_history_reliability"].eq(1).all()
    assert analysis["effective_trend_reliability"].between(0, 1).all()
    assert analysis["trend_data_penalty_points"].ge(0).all()


def test_model_upgrade_preserves_existing_user_weights():
    import config
    parsed = read_controls([
        ["Model", "MODEL_VERSION", "secondary_trend_point_impact_v3", ""],
        ["Safe family weight", "FAMILY_WEIGHT_SAFE_TREND", "47", ""],
        ["Selection cap", "TOP_N_SAFE", "13", ""],
    ])
    assert parsed["MODEL_VERSION"] == config.GENERAL_DEFAULTS["MODEL_VERSION"]
    assert parsed["FAMILY_WEIGHT_SAFE_TREND"] == 47
    assert parsed["TOP_N_SAFE"] == 13


def test_v14_default_display_caps_migrate_to_three():
    parsed = read_controls([
        ["Model", "MODEL_VERSION", "secondary_trend_first_v14"],
        ["Selection cap", "TOP_N_SAFE", "20"],
        ["Selection cap", "TOP_N_HIGH_GROWTH_POTENTIAL", "20"],
        ["Selection cap", "TOP_N_TURNAROUND_STORY", "20"],
        ["Short strategy", "SHORT_TOP_N", "3"],
    ])
    assert all(parsed[f"TOP_N_{cfg['slug']}"] == 3 for cfg in config.CATEGORIES.values())
    assert parsed["SHORT_TOP_N"] == 3


def test_v11_defaults_migrate_to_four_year_long_and_stricter_short_gates():
    import config

    rows = [
        ["Model", "MODEL_VERSION", "secondary_trend_first_v11"],
        ["", "MIN_HISTORY_YEARS_SAFE", "2"],
        ["", "MIN_HISTORY_YEARS_HIGH_GROWTH_POTENTIAL", "2"],
        ["", "MIN_HISTORY_YEARS_TURNAROUND_STORY", "2"],
        ["", "MIN_TREND_OBSERVATIONS", "3"],
        ["", "SHORT_MIN_CROSS_SECTION_OBSERVATIONS", "5"],
        ["", "SHORT_MIN_POINT_IN_TIME_COVERAGE", "0.6"],
        ["", "HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR", "FALSE"],
    ]
    upgraded = read_controls(rows)
    assert upgraded["MODEL_VERSION"] == config.GENERAL_DEFAULTS["MODEL_VERSION"]
    assert [upgraded[f"MIN_HISTORY_YEARS_{slug}"] for slug in ("SAFE", "HIGH_GROWTH_POTENTIAL", "TURNAROUND_STORY")] == [4, 4, 4]
    assert upgraded["MIN_TREND_OBSERVATIONS"] == 4
    assert upgraded["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] == 8
    assert upgraded["SHORT_MIN_POINT_IN_TIME_COVERAGE"] == 0.8
    assert upgraded["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"] is True

    customised = read_controls([
        *rows,
        ["", "MIN_HISTORY_YEARS_SAFE", "5"],
        ["", "MIN_TREND_OBSERVATIONS", "5"],
        ["", "SHORT_MIN_CROSS_SECTION_OBSERVATIONS", "9"],
        ["", "SHORT_MIN_POINT_IN_TIME_COVERAGE", "0.9"],
    ])
    assert customised["MIN_HISTORY_YEARS_SAFE"] == 5
    assert customised["MIN_TREND_OBSERVATIONS"] == 5
    assert customised["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] == 9
    assert customised["SHORT_MIN_POINT_IN_TIME_COVERAGE"] == 0.9


def test_control_panel_emits_each_short_metric_weight_once():
    from src.shorts import SHORT_METRICS
    from src.writer import control_rows

    rows = control_rows(default_controls())
    short_weight_names = [
        row[1] for row in rows if len(row) > 1 and str(row[1]).startswith("SHORT_METRIC_WEIGHT_")
    ]
    expected = {
        f"SHORT_METRIC_WEIGHT_{metric.upper()}"
        for family in SHORT_METRICS.values()
        for metric in family
    }
    assert len(short_weight_names) == len(set(short_weight_names))
    assert set(short_weight_names) == expected
    by_key = {row[1]: row for row in rows if len(row) > 1}
    assert "four unique fiscal-year observations" in by_key["MIN_HISTORY_YEARS_SAFE"][3]
    assert "zero disables this family" in by_key["SHORT_WEIGHT_TREND"][3]


def test_four_fiscal_year_long_gates_cannot_be_lowered():
    import pytest
    from src.io import validate_controls

    for key in ("MIN_HISTORY_YEARS_SAFE", "MIN_TREND_OBSERVATIONS"):
        controls = default_controls()
        controls[key] = 3
        with pytest.raises(ValueError, match="four"):
            validate_controls(controls)


def test_high_growth_revenue_floor_is_inclusive_and_positive_switch_is_effective():
    raw = sample_data()
    raw.loc[raw.Ticker.eq("AAA"), "Revenue"] = [110, 108, 104, 100]
    features = build_features(raw)
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["HIGH_GROWTH_MIN_REVENUE_CAGR"] = float(
        features.loc[features.Ticker.eq("AAA"), "revenue_cagr"].iloc[0]
    )
    controls["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"] = False
    without_positive_gate = score_category(features, pd.DataFrame(), "High Growth Potential", controls)
    assert without_positive_gate.set_index("Ticker").loc["AAA", "strategy_specific_gate_pass"]

    controls["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"] = True
    with_positive_gate = score_category(features, pd.DataFrame(), "High Growth Potential", controls)
    assert not with_positive_gate.set_index("Ticker").loc["AAA", "strategy_specific_gate_pass"]


def test_latest_value_is_not_silently_backfilled_from_an_older_year():
    data = sample_data()
    data.loc[(data["Ticker"] == "AAA") & (data["Fiscal Year"] == 2025), "Free Cash Flow"] = None
    features = build_features(data)
    aaa = features.loc[features["Ticker"].eq("AAA")].iloc[0]
    assert pd.isna(aaa["latest_free_cash_flow"])


def test_missing_period_date_does_not_override_a_newer_fiscal_year():
    data = sample_data()
    older = data.loc[(data["Ticker"] == "AAA") & (data["Fiscal Year"] == 2022)].copy()
    older["Fiscal Period End"] = None
    older["Revenue"] = 1
    features = build_features(pd.concat([data, older], ignore_index=True))
    aaa = features.loc[features["Ticker"].eq("AAA")].iloc[0]
    assert aaa["latest_fiscal_year"] == 2025
    assert aaa["latest_revenue"] > 1


def test_sparse_cross_section_scores_are_shrunk_toward_neutral():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["CROSS_SECTION_SHRINKAGE_STRENGTH"] = 10
    features = build_features(sample_data())
    analysis = score_category(features, pd.DataFrame(), "Safe", controls)
    scores = analysis["latest_roa__score"].dropna()
    assert scores.max() < 100
    assert scores.min() > 0


def test_top_ranked_display_keeps_score_leaders_when_quality_gates_fail():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["TOP_N_SAFE"] = 3
    controls["MIN_SELECTION_SCORE_SAFE"] = 99
    analysis = score_category(build_features(sample_data()), pd.DataFrame(), "Safe", controls)
    summary = build_summary(analysis, "Safe", controls)
    assert len(summary) == 3
    assert "Qualified" not in summary
    assert analysis["gate_fail_reasons"].str.contains("quality_score_gate").any()


def test_theil_sen_trend_resists_one_extreme_year():
    slope, _, observations = _trend_stats(
        pd.Series([1.0, 2.0, 100.0, 4.0]), pd.Series([2022, 2023, 2024, 2025]), minimum=3,
    )
    assert observations == 4
    assert abs(slope - 1.0) < 1e-12


def test_sector_scores_are_used_only_with_enough_peers():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["MIN_SECTOR_PEER_OBSERVATIONS"] = 2
    metadata = pd.DataFrame({
        "Ticker": ["AAA", "BBB", "CCC"],
        "Sector": ["Technology", "Technology", "Finance"],
    })
    analysis = score_category(build_features(sample_data()), metadata, "Safe", controls)
    tech = analysis[analysis["Ticker"].isin(["AAA", "BBB"])]
    finance = analysis[analysis["Ticker"].eq("CCC")]
    assert tech["latest_roa__sector_score"].notna().all()
    assert tech["latest_roa__sector_blend_weight"].gt(0).all()
    assert finance["latest_roa__sector_score"].isna().all()
    assert finance["latest_roa__sector_blend_weight"].eq(0).all()


def test_v5_default_duplicate_weights_are_migrated_but_custom_weights_survive():
    parsed = read_controls([
        ["Model", "MODEL_VERSION", "secondary_quality_first_v5", ""],
        ["Safe metric weight", "METRIC_WEIGHT_SAFE_LATEST_PRICE_FCF", "0.33", ""],
        ["High Growth metric weight", "METRIC_WEIGHT_HIGH_GROWTH_POTENTIAL_REVENUE_FOUR_YEAR_TREND", "3", ""],
        ["Turnaround metric weight", "METRIC_WEIGHT_TURNAROUND_STORY_REVENUE_FOUR_YEAR_TREND", "9", ""],
    ])
    assert parsed["METRIC_WEIGHT_SAFE_LATEST_PRICE_FCF"] == 0
    assert parsed["METRIC_WEIGHT_HIGH_GROWTH_POTENTIAL_REVENUE_FOUR_YEAR_TREND"] == 1.5
    assert parsed["METRIC_WEIGHT_TURNAROUND_STORY_REVENUE_FOUR_YEAR_TREND"] == 9


def test_v7_migrates_legacy_profiles_and_activates_recent_improvement():
    import config

    rows = [["Model", "MODEL_VERSION", "secondary_sector_robust_v6"]]
    for category, weights in config.LEGACY_FAMILY_WEIGHTS.items():
        slug = config.CATEGORIES[category]["slug"]
        rows.extend(["", f"FAMILY_WEIGHT_{slug}_{family}", str(weight)] for family, weight in weights.items())
        if weights["INFLECTION"] == 0:
            rows.extend(["", f"METRIC_WEIGHT_{slug}_{metric.upper()}", "0"]
                        for metric, (family, _, _) in config.FEATURE_SPECS.items() if family == "INFLECTION")
    parsed = read_controls(rows)
    for category, cfg in config.CATEGORIES.items():
        slug = cfg["slug"]
        assert sum(parsed[f"FAMILY_WEIGHT_{slug}_{f}"] for f in config.FAMILY_WEIGHTS[category]) == 100
        assert sum(parsed[f"FAMILY_WEIGHT_{slug}_{f}"] for f in ("TREND", "GROWTH", "INFLECTION")) == 80
        assert parsed[f"METRIC_WEIGHT_{slug}_NET_MARGIN_CHANGE"] > 0
    # A published v7 panel must be stable on every subsequent run.
    assert read_controls([["", k, str(v)] for k, v in parsed.items()]) == parsed
    # Preserve a deliberately customised profile as a whole.
    custom = [row[:] for row in rows]
    next(row for row in custom if row[1] == "FAMILY_WEIGHT_SAFE_TREND")[2] = "47"
    assert read_controls(custom)["FAMILY_WEIGHT_SAFE_PROFITABILITY"] == 15
    assert read_controls(custom)["FAMILY_WEIGHT_SAFE_TREND"] == 47


def test_same_latest_snapshot_rewards_improving_history_in_every_strategy():
    # Identical latest financials, different paths: ranking must use history.
    import config

    raw = sample_data().loc[lambda d: d.Ticker.eq("AAA")].copy()
    improving = raw.copy()
    declining = raw.copy()
    improving["Ticker"] = "UP"
    declining["Ticker"] = "DOWN"
    for field in ("Net Margin", "Gross Margin", "Return on Assets Ratio"):
        latest = float(raw.iloc[-1][field])
        improving[field] = [latest * x for x in (.4, .55, .75, 1)]
        declining[field] = [latest * x for x in (1.6, 1.45, 1.25, 1)]
    features = build_features(pd.concat([improving, declining], ignore_index=True))
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    for category in config.CATEGORIES:
        table = score_category(features, pd.DataFrame(), category, controls).set_index("Ticker")
        assert table.loc["UP", "TREND__score"] > table.loc["DOWN", "TREND__score"]
        assert table.loc["UP", "INFLECTION__contribution_points"] > table.loc["DOWN", "INFLECTION__contribution_points"]
        assert table.loc["UP", "final_score"] > table.loc["DOWN", "final_score"]


def test_recent_changes_never_bridge_missing_values_or_years():
    from src.model import _change, _growth_series, _cagr
    years = pd.Series([2022, 2023, 2024, 2025])
    assert pd.isna(_change(pd.Series([1, 2, 3, None]), years))
    assert pd.isna(_change(pd.Series([1, 2, None, 4]), years))
    assert pd.isna(_change(pd.Series([1, 2]), pd.Series([2022, 2024])))
    assert pd.isna(_growth_series(pd.Series([100, 150]), pd.Series([2022, 2024])).iloc[-1])
    assert pd.isna(_cagr(pd.Series([100, 120, 130, None]), years))
    assert pd.isna(_cagr(pd.Series([None, 120, 130, 150]), years))


def test_tied_cross_sections_are_neutral_and_directions_symmetric():
    import numpy as np
    from src.model import _base_cross_section_score
    def score(values, direction="higher"):
        return _base_cross_section_score(pd.Series(values), direction, 2, 10, 2.5, .7)
    assert score([1, 1, 1]).eq(50).all()
    assert (score([1, 2, 3]) + score([1, 2, 3], "lower")).eq(100).all()
    assert pd.isna(score([1, 2, np.inf]).iloc[-1])


def test_primary_universe_excludes_stale_names_before_peer_scoring():
    from src.io import restrict_to_primary
    import pytest
    raw, primary = restrict_to_primary(sample_data(), pd.DataFrame({"Ticker": [" aaa ", "BBB"]}), "Safe")
    assert set(raw.Ticker) == {"AAA", "BBB"}
    assert len(primary) == 2
    missing, retained = restrict_to_primary(sample_data(), pd.DataFrame({"Ticker": ["MISSING"]}), "Safe")
    assert missing.empty
    assert retained.Ticker.tolist() == ["MISSING"]
    with pytest.raises(ValueError, match="shortlist is empty"):
        restrict_to_primary(sample_data(), pd.DataFrame({"Ticker": [""]}), "Safe")


def test_latest_missing_trend_is_not_backfilled_and_counts_are_per_metric():
    raw = sample_data()
    raw.loc[(raw.Ticker == "AAA") & (raw["Fiscal Year"] == 2025), "Net Margin"] = None
    raw.loc[(raw.Ticker == "BBB") & (raw["Fiscal Year"] == 2022), "Net Margin"] = None
    features = build_features(raw).set_index("Ticker")
    assert pd.isna(features.loc["AAA", "net_margin_four_year_trend"])
    assert pd.isna(features.loc["AAA", "net_margin_change"])
    assert features.loc["BBB", "net_margin_four_year_trend_observations"] == 3
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    scored = score_category(features.reset_index(), pd.DataFrame(), "Safe", controls, as_of="2026-09-16").set_index("Ticker")
    assert scored.loc["BBB", "trend_history_reliability"] < scored.loc["CCC", "trend_history_reliability"]


def test_declining_peer_universe_cannot_pass_absolute_direction_gates():
    raw = sample_data()
    for _, indices in raw.groupby("Ticker").groups.items():
        for field in ("Net Margin", "Gross Margin", "Return on Assets Ratio"):
            raw.loc[indices, field] = [.3, .25, .2, .1]
        raw.loc[indices, "Revenue"] = [150, 140, 120, 100]
        raw.loc[indices, "Free Cash Flow"] = [45, 35, 24, 10]
        raw.loc[indices, "Debt to Equity Ratio"] = [.1, .2, .3, .4]
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    table = score_category(build_features(raw), pd.DataFrame(), "Safe", controls, as_of="2026-09-16")
    assert not table["absolute_trend_gate_pass"].any()
    assert not table["recent_direction_gate_pass"].any()
    assert not table.eligible.any()


def test_stale_and_future_financials_fail_even_when_whole_cohort_matches():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    features = build_features(sample_data())
    stale = score_category(features, pd.DataFrame(), "Safe", controls, as_of="2029-01-01")
    future = score_category(features, pd.DataFrame(), "Safe", controls, as_of="2025-01-01")
    assert stale["freshness_gate_pass"].all()  # Old relative-only check missed this.
    assert not stale["financial_age_gate_pass"].any()
    assert not future["financial_age_gate_pass"].any()
    assert not stale.eligible.any()


def test_missing_recent_momentum_cannot_be_renormalised_away():
    raw = sample_data()
    mask = raw["Fiscal Year"].eq(2024)
    raw.loc[mask, ["Revenue", "Net Margin", "Return on Assets Ratio"]] = None
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    table = score_category(build_features(raw), pd.DataFrame(), "Safe", controls, as_of="2026-09-16")
    assert not table["recent_coverage_gate_pass"].any()
    assert not table.eligible.any()


def test_control_parser_rejects_nonfinite_and_fractional_integer_settings():
    import pytest
    for key, value in [("TOP_N_SAFE", "2.5"), ("FAMILY_WEIGHT_SAFE_TREND", "nan"), ("FAMILY_WEIGHT_SAFE_TREND", "inf")]:
        with pytest.raises(ValueError):
            read_controls([["", key, value]])


def test_tied_ranking_is_stable_under_input_reordering():
    raw = sample_data().loc[lambda x: x.Ticker.eq("AAA")]
    identical = pd.concat([raw.assign(Ticker=t) for t in ("ZZZ", "AAA", "MMM")], ignore_index=True)
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["MIN_SELECTION_SCORE_SAFE"] = 0
    features = build_features(identical)
    a = score_category(features, pd.DataFrame(), "Safe", controls, as_of="2026-09-16")
    b = score_category(features.iloc[::-1], pd.DataFrame(), "Safe", controls, as_of="2026-09-16")
    assert a.Ticker.tolist() == b.Ticker.tolist() == ["AAA", "MMM", "ZZZ"]
    assert a["rank"].tolist() == b["rank"].tolist()


def test_complete_reporting_pipeline_reconciles_all_categories():
    import config
    from src.reporting import build_run_audit, build_model_registry
    from src.validation import validate_results
    from src.writer import control_rows
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    data = {c: sample_data() for c in config.CATEGORIES}
    analyses = {c: score_category(build_features(d), pd.DataFrame(), c, controls, as_of="2026-09-16")
                .assign(source_integrity_gate_pass=True, source_essential_gate_pass=True) for c, d in data.items()}
    summaries = {c: build_summary(a, c, controls) for c, a in analyses.items()}
    evidence = validate_results(analyses, summaries, controls)
    from src.shorts import SHORT_METRICS
    assert len(build_model_registry(controls)) == len(config.FEATURE_SPECS) * 3 + sum(len(metrics) for metrics in SHORT_METRICS.values())
    short_analysis = pd.DataFrame({
        "Ticker": ["AAA"], "history_years": [4], "short_eligible": [False],
        "short_history_pass": [True], "short_point_in_time_coverage_pass": [False],
        "short_score_pass": [False], "short_raw_score": [60.0],
        "short_trend_reliability": [0.5], "short_score": [55.0],
        "short_source_integrity_pass": [True],
    })
    audit = build_run_audit(data, analyses, summaries, controls, evidence, sample_data(), short_analysis, pd.DataFrame({"Ticker": ["AAA"]}))
    assert audit["Category"].tolist() == [*config.CATEGORIES, "Short"]
    assert audit.loc[audit["Category"].eq("Short"), "Selected"].iloc[0] == 1
    assert audit.loc[audit["Category"].eq("Short"), "Short Point-in-Time Coverage Pass"].iloc[0] == 0
    assert not any(row[1] == "MIN_RECENT_METRIC_COVERAGE" for row in control_rows(controls) if len(row) > 1)
    for result in evidence.values():
        assert result["max_score_reconciliation_error"] < 1e-8
        assert result["max_trend_reconciliation_error"] < 1e-8


def test_suppressed_family_has_no_metric_point_contributions():
    controls = default_controls()
    controls["MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["MIN_FAMILY_METRIC_COVERAGE"] = 1.0
    raw = sample_data()
    raw.loc[raw.Ticker.eq("AAA"), "Gross Margin"] = None
    table = score_category(build_features(raw), pd.DataFrame(), "Safe", controls, as_of="2026-09-16").set_index("Ticker")
    assert pd.isna(table.loc["AAA", "TREND__contribution_points"])
    assert pd.isna(table.loc["AAA", "net_margin_four_year_trend__final_contribution_points"])


def test_missing_history_does_not_inflate_confirmed_positive_share():
    raw = sample_data()
    raw.loc[(raw.Ticker == "AAA") & (raw["Fiscal Year"] < 2025), "Free Cash Flow"] = None
    features = build_features(raw).set_index("Ticker")
    assert features.loc["AAA", "positive_fcf_share"] == .25


def test_revenue_growth_persistence_uses_three_possible_changes_in_four_years():
    raw = sample_data()
    raw.loc[raw.Ticker.eq("AAA"), "Revenue"] = [100, 110, 121, 133.1]
    features = build_features(raw).set_index("Ticker")
    assert features.loc["AAA", "positive_revenue_growth_share"] == 1.0
    raw.loc[raw.Ticker.eq("AAA") & raw["Fiscal Year"].eq(2024), "Revenue"] = None
    features = build_features(raw).set_index("Ticker")
    assert features.loc["AAA", "positive_revenue_growth_share"] == pytest.approx(1 / 3)


def test_v12_custom_growth_gate_survives_model_version_update():
    controls = read_controls([
        ["Model", "MODEL_VERSION", "secondary_trend_first_v12"],
        ["Growth", "HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR", "FALSE"],
    ])
    assert controls["MODEL_VERSION"] == config.GENERAL_DEFAULTS["MODEL_VERSION"]
    assert controls["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"] is False


def test_missing_primary_candidate_is_explicitly_ineligible():
    controls = default_controls()
    primary = pd.DataFrame({
        "Ticker": ["AAA", "MISSING"], "Universe Rank": [1, 2],
        "Final Strategy Score": [80, 75],
        "PIT Verification Status": ["UNVERIFIED_VENDOR_SNAPSHOT"] * 2,
    })
    scored = score_category(build_features(sample_data().loc[lambda d: d.Ticker.eq("AAA")]), primary, "Safe", controls, as_of="2026-09-26")
    audited = append_missing_primary(scored, primary, "2026-09-26").set_index("Ticker")
    assert len(audited) == 2
    assert audited.loc["MISSING", "eligible"] == False
    assert audited.loc["MISSING", "gate_fail_reasons"] == "missing_source_history"
    assert audited.loc["MISSING", "history_years"] == 0
    assert audited.loc["MISSING", "Primary Final Strategy Score"] == 75
    assert "MISSING" not in build_summary(audited.reset_index(), "Safe", controls)["Ticker"].tolist()


def test_source_dates_normalise_sheets_serials_without_rounding_metrics():
    from src.io import _normalise_source_dates
    frame = pd.DataFrame({
        "Fiscal Period End": [44926, "2025-12-31"],
        "Price Date": ["44926.", "2025-12-30"],
        "Revenue": [0.123456789, 0.987654321],
    })
    result = _normalise_source_dates(frame)
    assert result["Fiscal Period End"].tolist() == ["2022-12-31", "2025-12-31"]
    assert result["Price Date"].tolist() == ["2022-12-31", "2025-12-30"]
    assert result["Revenue"].tolist() == frame["Revenue"].tolist()


def test_short_ranking_uses_finite_scores_with_valid_source_history():
    from src.shorts import ranked_short_candidates
    table = pd.DataFrame({
        "Ticker": ["AAA", "BBB", "CCC", "DDD"],
        "short_score": [60.0, 80.0, 90.0, float("nan")],
        "short_trend_reliability": [0.8, 0.7, 0.9, 0.9],
        "history_years": [4, 4, 3, 4],
        "trend_observations": [4, 4, 4, 4],
        "max_fiscal_year_gap": [1, 1, 1, 1],
        "source_error_count": [0, 0, 0, 0],
    })
    assert ranked_short_candidates(table)["Ticker"].tolist() == ["BBB", "AAA"]


def test_primary_mirror_must_match_authoritative_summary():
    from src.io import validate_primary_mirror
    source = [["Title"], ["Note"], ["Rank", "Ticker", "Score"], ["1", "AAA", "70"]]
    validate_primary_mirror(source, source, "Safe Summary")
    stale = [["Mirror"], ["Note"], ["Rank", "Ticker", "Score"], ["1", "AAA", "69"]]
    with pytest.raises(ValueError, match="Primary/mirror differs at row 4"):
        validate_primary_mirror(source, stale, "Safe Summary")


def test_primary_short_parser_keeps_only_ranked_rows_and_point_in_time_inputs():
    from src.io import parse_primary_short_candidates
    header = [
        "Short Rank", "Ticker", "Company Name", "Industry", "Short Score",
        "% Change F1 Est. (4 weeks)", "% Change F2 Est. (4 weeks)",
        "% Price Change (4 Weeks)", "% Price Change (12 Weeks)",
    ]
    rows = [
        ["Primary Short", "", "", "", "", "", "", "", ""],
        ["As of today", "", "", "", "", "", "", "", ""],
        header,
        ["1", " aaa ", "Acme", "Software", "78.5", "-4.2", "-2.1", "-8", "-14"],
        ["Selection rules", "", "", "", "", "", "", "", ""],
    ]
    candidates = parse_primary_short_candidates(rows)
    assert candidates.Ticker.tolist() == ["AAA"]
    assert candidates["Primary Short Rank"].tolist() == [1]
    assert candidates.loc[0, "primary_short_score"] == 78.5
    assert candidates.loc[0, "primary_price_change_12w"] == -14


def test_primary_short_parser_preserves_missing_optional_signals():
    from src.io import parse_primary_short_candidates
    rows = [
        ["Short candidates"], ["Dataset as-of: NOT PROVIDED"],
        ["Short Rank", "Ticker", "Company Name", "Industry", "Short Score"],
        ["1", "AAA", "Acme", "Software", "78.5"],
    ]
    candidates = parse_primary_short_candidates(rows)
    assert candidates.Ticker.tolist() == ["AAA"]
    assert candidates.loc[0, "primary_short_score"] == 78.5
    assert pd.isna(candidates.loc[0, "primary_price_change_4w"])
    assert pd.isna(candidates.loc[0, "primary_f1_estimate_change_4w"])
    assert candidates.loc[0, "Primary Dataset As Of Note"] == "Dataset as-of: NOT PROVIDED"
    assert candidates.loc[0, "Primary PIT Verification Status"] == "UNAVAILABLE"


def test_primary_short_signals_join_reconciled_dataset_and_reject_stale_short_tab():
    from src.io import merge_primary_short_dataset_signals, parse_primary_short_candidates
    short = [
        ["Short candidates"], ["Dataset as-of: NOT PROVIDED"],
        ["Short Rank", "Ticker", "Company Name", "Industry", "Short Score", "Market Cap (mil)", "Last Close"],
        ["1", "AAA", "Acme", "Software", "78.5", "1000", "20"],
    ]
    dataset = [
        ["Ticker", "Market Cap (mil)", "Last Close", "% Price Change (4 Weeks)",
         "% Price Change (12 Weeks)", "% Change F1 Est. (4 weeks)", "% Change F2 Est. (4 weeks)"],
        ["AAA", "1000", "20", "-8", "-14", "-4.2", "-2.1"],
    ]
    candidates = merge_primary_short_dataset_signals(parse_primary_short_candidates(short), short, dataset)
    assert candidates.loc[0, "primary_price_change_4w"] == -8
    assert candidates.loc[0, "primary_price_change_12w"] == -14
    assert candidates.loc[0, "primary_f1_estimate_change_4w"] == -4.2
    assert candidates.loc[0, "primary_f2_estimate_change_4w"] == -2.1
    stale = [dataset[0], ["AAA", "1000", "21", "-8", "-14", "-4.2", "-2.1"]]
    with pytest.raises(ValueError, match="differs from published Short"):
        merge_primary_short_dataset_signals(parse_primary_short_candidates(short), short, stale)


def test_short_history_pool_is_limited_to_primary_short_tickers():
    import config
    from src.io import build_short_universe
    annual = sample_data()
    sources = {category: annual for category in config.CATEGORIES}
    candidates = pd.DataFrame({"Ticker": ["AAA", "SHORT"], "Primary Short Rank": [1, 2]})
    pooled, metadata = build_short_universe(sources, candidates)
    assert set(pooled.Ticker) == {"AAA"}
    assert metadata.Ticker.tolist() == ["AAA", "SHORT"]
    assert metadata.loc[metadata.Ticker.eq("SHORT"), "History Source Tabs"].iloc[0] == ""


def test_short_history_pool_uses_imported_short_data_when_long_tabs_lack_the_ticker():
    import config
    from src.io import build_short_universe

    sources = {category: sample_data() for category in config.CATEGORIES}
    imported = sample_data().loc[lambda frame: frame.Ticker.eq("AAA")].copy()
    imported["Ticker"] = "SHORT"
    pooled, metadata = build_short_universe(
        sources,
        pd.DataFrame({"Ticker": ["SHORT"], "Primary Short Rank": [1]}),
        imported,
    )
    assert pooled.Ticker.unique().tolist() == ["SHORT"]
    assert pooled["Fiscal Year"].tolist() == [2022, 2023, 2024, 2025]
    assert metadata.loc[0, "History Source Tabs"] == "Short Data"


def test_sec_importer_selects_annual_priority_and_emits_screener_schema(monkeypatch, tmp_path):
    import requests
    from src import sec_importer
    from src.model import REQUIRED_SOURCE_COLUMNS

    annual = lambda value, end, filed, concept="RevenueFromContractWithCustomerExcludingAssessedTax": {
        "fy": int(end[:4]), "val": value, "form": "10-K", "fp": "FY",
        "start": f"{int(end[:4])}-01-01", "end": end, "filed": filed,
    }
    facts = {"us-gaap": {}}
    for concept, values in {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [annual(100, "2022-12-31", "2023-02-01"), annual(120, "2023-12-31", "2024-02-01"), annual(150, "2024-12-31", "2025-02-01"), annual(180, "2025-12-31", "2026-02-01")],
        "Revenues": [annual(999, "2025-12-31", "2026-03-01")],
        "NetIncomeLoss": [annual(10, "2022-12-31", "2023-02-01"), annual(12, "2023-12-31", "2024-02-01"), annual(15, "2024-12-31", "2025-02-01"), annual(18, "2025-12-31", "2026-02-01")],
        "OperatingIncomeLoss": [annual(15, "2022-12-31", "2023-02-01"), annual(18, "2023-12-31", "2024-02-01"), annual(22, "2024-12-31", "2025-02-01"), annual(26, "2025-12-31", "2026-02-01")],
        "Assets": [annual(200, "2022-12-31", "2023-02-01"), annual(220, "2023-12-31", "2024-02-01"), annual(250, "2024-12-31", "2025-02-01"), annual(280, "2025-12-31", "2026-02-01")],
    }.items():
        facts["us-gaap"][concept] = {"units": {"USD": values}}
    payload = {"facts": facts}
    selected = sec_importer.annual_facts(payload, "revenue")
    assert selected.loc[selected.fy.eq(2025), "value"].iloc[0] == 180

    monkeypatch.setattr(sec_importer, "fiscal_end_price", lambda *args, **kwargs: {"price": 10.0, "date": "2025-12-31", "currency": "USD", "source": "test", "note": ""})
    settings = sec_importer.ImportSettings("Test Research test@example.test", tmp_path)
    rows = sec_importer.company_rows("TEST", payload, requests.Session(), settings)
    assert len(rows) == 4
    assert rows[-1]["Revenue"] == 180
    assert rows[-1]["Revenue Source Concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert set(REQUIRED_SOURCE_COLUMNS) <= set(rows[-1])


def test_sec_importer_uses_theo_contact_by_default_and_allows_overrides(monkeypatch, tmp_path):
    from src.sec_importer import default_settings

    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert default_settings(cache_dir=tmp_path).user_agent == "Theo Cooper t.h.c.1@icloud.com"
    monkeypatch.setenv("SEC_USER_AGENT", "Research Team team@company.test")
    assert default_settings(cache_dir=tmp_path).user_agent == "Research Team team@company.test"
    assert default_settings("Other Research other@company.test", tmp_path).user_agent == "Other Research other@company.test"


def test_sec_importer_uses_primary_summary_survivors_and_short_tickers(monkeypatch, tmp_path):
    from src import sec_importer

    class Sheet:
        def __init__(self, title, values=None):
            self.title = title
            self.values = values

        def get_all_values(self, **kwargs):
            if self.values is None:
                raise AssertionError(f"Unexpected read of {self.title}")
            return self.values

    class Book:
        def __init__(self, title, sheets):
            self.title = title
            self.sheets = sheets

        def worksheet(self, title):
            return self.sheets[title]

        def worksheets(self):
            return list(self.sheets.values())

    primary_sheets = {}
    selections = {
        "Safe": ["AAA", "OVR"],
        "High Growth Potential": ["BBB"],
        "Turnaround Story": ["CCC"],
    }
    secondary_sheets = {"Short Data": Sheet("Short Data", [["Ticker"], ["OLD"]])}
    for category, cfg in config.CATEGORIES.items():
        name = cfg["primary_summary_sheet"]
        primary_sheets[name] = Sheet(name, [["Title"], ["Note"], ["Rank", "Ticker"]]
                                     + [[str(rank), ticker] for rank, ticker in enumerate(selections[category], 1)])
        secondary_sheets[name] = Sheet(name)  # The imported mirror may be stale.
        secondary_sheets[cfg["data_sheet"]] = Sheet(cfg["data_sheet"], [["Ticker"], ["OLD"]])
    primary_sheets[config.PRIMARY_SHORT_TAB] = Sheet(config.PRIMARY_SHORT_TAB, [
        ["Short title"], ["As of note"],
        ["Short Rank", "Ticker", "Company Name", "Industry", "Short Score"],
        ["1", "SHRT", "Short Co", "Industry", "80"],
        ["2", "OVR", "Overlap Co", "Industry", "75"],
    ])
    books = {
        config.SPREADSHEET_ID: Book(config.EXPECTED_WORKBOOK_TITLE, secondary_sheets),
        config.PRIMARY_SHORT_SPREADSHEET_ID: Book(config.PRIMARY_SHORT_WORKBOOK_TITLE, primary_sheets),
    }
    monkeypatch.setattr(sec_importer, "client", lambda _: type("Client", (), {"open_by_key": lambda self, key: books[key]})())
    imported = []
    monkeypatch.setattr(sec_importer, "import_tickers", lambda tickers, _: imported.extend(tickers) or pd.DataFrame([
        {"Ticker": ticker, "Fiscal Year": year, "Fiscal Period End": f"{year}-12-31"}
        for ticker in tickers for year in range(2022, 2026)
    ]))
    written = {}
    monkeypatch.setattr(sec_importer, "_write_frame", lambda sheet, frame: written.update({sheet.title: frame["Ticker"].tolist()}))

    result = sec_importer.import_to_workbook(None, sec_importer.ImportSettings("Test Research test@example.test", tmp_path))

    assert imported == ["AAA", "OVR", "BBB", "CCC", "SHRT"]
    assert written == {
        "Safe Data": ["AAA"] * 4 + ["OVR"] * 4,
        "High Growth Potential Data": ["BBB"] * 4,
        "Turnaround Story Data": ["CCC"] * 4,
        "Short Data": ["OVR"] * 4 + ["SHRT"] * 4,
    }
    assert result["imported_tickers"] == 5


def test_sec_importer_excludes_quarterly_fact_inside_annual_filing():
    from src.sec_importer import annual_facts
    payload = {"facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        {"fy": 2025, "val": 30, "form": "10-K", "fp": "FY", "start": "2025-10-01", "end": "2025-12-31", "filed": "2026-02-01"},
    ]}}}}}
    assert annual_facts(payload, "revenue").empty


def test_sec_importer_keeps_annual_facts_on_their_actual_periods(monkeypatch, tmp_path):
    import requests
    from src import sec_importer
    fact = lambda fy, end, value, accn: {
        "fy": fy, "fp": "FY", "start": f"{end[:4]}-01-01", "end": end,
        "filed": "2024-02-01", "form": "10-K", "accn": accn, "val": value,
    }
    payload = {"cik": 1234, "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            fact(2022, "2022-12-31", 100, "a"), fact(2023, "2022-12-31", 110, "b")]}},
        "NetIncomeLoss": {"units": {"USD": [fact(2023, "2023-12-31", 15, "c")]}},
    }}}
    monkeypatch.setattr(sec_importer, "fiscal_end_price", lambda *args, **kwargs: {
        "price": 10., "date": "2022-12-30", "currency": "USD", "source": "test", "note": ""})
    rows = sec_importer.company_rows("TEST", payload, requests.Session(),
                                     sec_importer.ImportSettings("Test Research test@example.test", tmp_path),
                                     {"a": "2024-02-01T20:00:00+00:00"})
    assert len(rows) == 2
    assert [row["Fiscal Period End"] for row in rows] == ["2022-12-31", "2023-12-31"]
    assert [row["Fiscal Year"] for row in rows] == [2022, 2023]
    assert rows[0]["Revenue"] == 100
    assert rows[0]["Net Income"] is None
    assert rows[1]["Revenue"] is None
    assert rows[1]["Net Income"] == 15
    assert rows[1]["Error"] == ""
    assert rows[1]["CIK"] == "0000001234"
    assert '"accn": "a"' in rows[0]["Fact Provenance JSON"]
    assert rows[0]["Accepted At"] == "2024-02-01T20:00:00+00:00"
    assert '"accepted_at_utc": "2024-02-01T20:00:00+00:00"' in rows[0]["Fact Provenance JSON"]


def test_sec_importer_fetches_fiscal_prices_once_per_ticker(monkeypatch, tmp_path):
    import requests
    from datetime import datetime, timezone
    from src import sec_importer
    called = []
    def chart(*args, **kwargs):
        called.append(args[1])
        dates = [datetime(2024, 12, 30, 14, tzinfo=timezone.utc),
                 datetime(2025, 12, 30, 14, tzinfo=timezone.utc)]
        return {"chart": {"result": [{"timestamp": [int(d.timestamp()) for d in dates],
                                      "indicators": {"quote": [{"close": [10, 20]}]},
                                      "meta": {"gmtoffset": -5 * 3600, "currency": "USD"}}]}}
    monkeypatch.setattr(sec_importer, "_request_json", chart)
    prices = sec_importer.fiscal_end_prices(
        "TEST", ["2024-12-31", "2025-12-31"], requests.Session(),
        sec_importer.ImportSettings("Test Research test@example.test", tmp_path))
    assert len(called) == 1
    assert prices["2024-12-31"]["price"] == 10
    assert prices["2025-12-31"]["price"] == 20
    assert prices["2024-12-31"]["date"] == "2024-12-30"


def test_sec_importer_rejects_narrow_revenue_tag_when_same_filing_has_consolidated_total():
    from src import sec_importer
    def fact(end, value, accession):
        return {"fy": int(end[:4]), "fp": "FY", "start": f"{end[:4]}-01-01",
                "end": end, "filed": "2026-02-01", "form": "10-K", "accn": accession, "val": value}
    payload = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [fact("2025-12-31", 12, "one")]}},
        "Revenues": {"units": {"USD": [fact("2025-12-31", 240, "one")]}}}}}
    concepts = sec_importer.revenue_concepts_for_issuer(payload, "USD")
    chosen = sec_importer.annual_facts(payload, "revenue", "USD", concepts_override=concepts)
    assert concepts[0] == ("us-gaap", "Revenues")
    assert chosen.iloc[-1]["value"] == 240


def test_sec_importer_uses_fiscal_end_shares_not_filing_date_or_weighted_average(monkeypatch, tmp_path):
    import requests
    from src import sec_importer
    fact = lambda end, value: {"fy": 2025, "fp": "FY", "start": "2025-01-01",
                               "end": end, "filed": "2026-02-01", "form": "10-K", "val": value}
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [fact("2025-12-31", 100)]}},
        "CommonStockSharesOutstanding": {"units": {"shares": [fact("2025-12-31", 10)]}},
        "WeightedAverageNumberOfSharesOutstandingBasic": {"units": {"shares": [fact("2025-12-31", 999)]}},
    }, "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [fact("2026-02-01", 20)]}}}}}
    monkeypatch.setattr(sec_importer, "fiscal_end_price", lambda *args, **kwargs: {
        "price": 5., "date": "2025-12-31", "currency": "USD", "source": "test", "note": ""})
    row = sec_importer.company_rows("TEST", payload, requests.Session(),
                                    sec_importer.ImportSettings("Test Research test@example.test", tmp_path))[0]
    assert row["Shares Outstanding"] == 10
    assert row["Market Cap"] == 50
    assert row["Error"] == ""


def test_sec_importer_does_not_apply_ordinary_share_count_to_adr_price(monkeypatch, tmp_path):
    import requests
    from src import sec_importer
    fact = {"fy": 2025, "fp": "FY", "start": "2025-01-01", "end": "2025-12-31",
            "filed": "2026-03-01", "form": "20-F", "val": 100}
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [fact]}},
        "CommonStockSharesOutstanding": {"units": {"shares": [{**fact, "val": 1_000_000}]}},
    }}}
    monkeypatch.setattr(sec_importer, "fiscal_end_price", lambda *args, **kwargs: {
        "price": 10., "date": "2025-12-31", "currency": "USD", "source": "test", "note": ""})
    row = sec_importer.company_rows("ADR", payload, requests.Session(),
                                    sec_importer.ImportSettings("Test Research test@example.test", tmp_path))[0]
    assert row["Market Cap"] is None
    assert "ADR/share conversion unverified" in row["Audit Notes"]


def test_sec_importer_uses_one_statement_currency(monkeypatch, tmp_path):
    import requests
    from src import sec_importer
    fact = {"fy": 2025, "fp": "FY", "start": "2025-01-01", "end": "2025-12-31",
            "filed": "2026-03-01", "form": "20-F", "val": 100}
    payload = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerIncludingAssessedTax": {"units": {"CNY": [fact], "USD": [{**fact, "val": 15}]}},
        "NetIncomeLoss": {"units": {"CNY": [{**fact, "val": 20}]}},
    }}}
    monkeypatch.setattr(sec_importer, "fiscal_end_price", lambda *args, **kwargs: {
        "price": 10., "date": "2025-12-31", "currency": "USD", "source": "test", "note": ""})
    row = sec_importer.company_rows("FX", payload, requests.Session(),
                                    sec_importer.ImportSettings("Test Research test@example.test", tmp_path))[0]
    assert row["Statement Currency"] == "CNY"
    assert row["Revenue"] == 100
    assert row["Net Income"] == 20
    assert row["Market Cap"] is None


def test_short_score_uses_primary_point_in_time_signals():
    from src.shorts import score_shorts
    controls = default_controls()
    controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    features = build_features(sample_data())
    metadata = pd.DataFrame({
        "Ticker": ["AAA", "BBB", "CCC"],
        "Primary Short Rank": [1, 2, 3],
        "Primary Short Score": [80, 70, 60],
        "Primary 4-Week Price Change (%)": [-10, -5, 2],
        "Primary 12-Week Price Change (%)": [-20, -8, 5],
        "Primary F1 Estimate Change 4-Week (%)": [-8, -3, 1],
        "Primary F2 Estimate Change 4-Week (%)": [-5, -2, 2],
        "primary_short_score": [80, 70, 60],
        "primary_price_change_4w": [-10, -5, 2],
        "primary_price_change_12w": [-20, -8, 5],
        "primary_f1_estimate_change_4w": [-8, -3, 1],
        "primary_f2_estimate_change_4w": [-5, -2, 2],
    })
    scored = score_shorts(features, metadata, controls, "2026-09-23")
    assert scored["SHORT_POINT_IN_TIME_score"].notna().all()
    assert scored.set_index("Ticker").loc["AAA", "SHORT_primary_short_score_score"] > scored.set_index("Ticker").loc["CCC", "SHORT_primary_short_score_score"]


def test_short_adverse_breadth_counts_low_positive_revenue_growth_share():
    from src.shorts import score_shorts

    raw = sample_data()
    raw.loc[raw.Ticker.eq("AAA"), "Revenue"] = 100
    features = build_features(raw)
    assert features.set_index("Ticker").loc["AAA", "positive_revenue_growth_share"] == 0

    controls = default_controls()
    controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["SHORT_WEIGHT_POINT_IN_TIME"] = 0
    included = score_shorts(features, pd.DataFrame(), controls, "2026-09-24").set_index("Ticker")

    controls["SHORT_METRIC_WEIGHT_POSITIVE_REVENUE_GROWTH_SHARE"] = 0
    excluded = score_shorts(features, pd.DataFrame(), controls, "2026-09-24").set_index("Ticker")
    included_observations = included.loc["AAA", "short_adverse_trend_observations"]
    excluded_observations = excluded.loc["AAA", "short_adverse_trend_observations"]
    included_adverse_count = included.loc["AAA", "short_adverse_trend_breadth"] * included_observations
    excluded_adverse_count = excluded.loc["AAA", "short_adverse_trend_breadth"] * excluded_observations
    assert included_observations == excluded_observations + 1
    assert abs(included_adverse_count - excluded_adverse_count - 1) < 1e-9


def test_short_adverse_raw_states_do_not_disappear_as_missing_ratios():
    from src.shorts import score_shorts

    raw = sample_data()
    latest = raw.Ticker.eq("AAA") & raw["Fiscal Year"].eq(2025)
    raw.loc[latest, ["EBIT", "EBITDA", "Shareholders Equity", "Interest Coverage Ratio"]] = [-10, -5, -20, -1]
    controls = default_controls()
    controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] = 2
    controls["SHORT_WEIGHT_POINT_IN_TIME"] = 0
    row = score_shorts(build_features(raw), pd.DataFrame(), controls, "2026-09-26").set_index("Ticker").loc["AAA"]
    assert pd.isna(row["latest_interest_coverage"])
    assert pd.isna(row["latest_net_debt_to_ebitda"])
    assert row["short_nonpositive_ebit"]
    assert row["short_nonpositive_ebitda"]
    assert row["short_nonpositive_equity"]
    assert row["short_nonpositive_interest_coverage"]
    assert row["short_weak_fundamental_domains"] >= 2


def test_short_direction_observation_control_matches_six_recent_measures():
    import pytest
    from src.io import validate_controls

    controls = default_controls()
    controls["SHORT_MIN_DIRECTION_OBSERVATIONS"] = 7
    with pytest.raises(ValueError, match="between 1 and 6"):
        validate_controls(controls)


def test_short_process_audit_keeps_primary_order_and_reports_data_status():
    from src.writer import short_process_audit_values
    controls = default_controls()
    primary = pd.DataFrame({
        "Ticker": ["AAA", "BBB"], "Primary Short Rank": [2, 1],
    })
    analysis = pd.DataFrame({
        "Ticker": ["AAA", "BBB"], "History Source Tabs": ["", ""],
        "history_years": [0, 0], "trend_observations": [0, 0],
        "short_eligible": [False, False], "short_history_pass": [False, False],
        "short_fail_reasons": ["missing_source_history", "missing_source_history"],
    })
    summary = pd.DataFrame(columns=["Ticker"])

    values = short_process_audit_values(primary, analysis, summary, controls)
    headers, rows = values[0], values[1:]
    assert [row[0] for row in rows] == ["BBB", "AAA"]
    assert all(len(row) == len(headers) for row in rows)
    audit = dict(zip(headers, rows[0]))
    assert audit["latest_revenue_growth Input"] == "NO DATA"
    assert audit["Top Ranked Display"] == "NO"
    assert audit["Data Status"] == "Insufficient annual history"


def test_short_data_export_keeps_only_primary_candidates_and_carries_lineage():
    from src.io import build_short_data_export

    history = pd.DataFrame({
        "Ticker": ["AAA", "AAA", "BBB"],
        "Fiscal Year": [2023, 2024, 2024],
        "Revenue": [100, 120, 999],
    })
    candidates = pd.DataFrame({
        "Ticker": ["AAA"], "Primary Short Rank": [2],
        "Primary Company Name": ["Alpha"], "Primary Industry": ["Software"],
        "History Source Tabs": ["Safe Data"],
    })

    exported = build_short_data_export(history, candidates)
    assert exported["Ticker"].tolist() == ["AAA", "AAA"]
    assert exported["Primary Short Rank"].tolist() == [2, 2]
    assert exported["Primary Company Name"].tolist() == ["Alpha", "Alpha"]
    assert exported["History Source Tabs"].tolist() == ["Safe Data", "Safe Data"]
    assert exported["Revenue"].tolist() == [100, 120]
    pd.testing.assert_frame_equal(build_short_data_export(exported, candidates), exported)
    stale = exported.assign(**{"Primary Short Rank_x": [99, 99], "Primary Short Rank_y": [98, 98]})
    pd.testing.assert_frame_equal(build_short_data_export(stale, candidates), exported)

    empty = build_short_data_export(history.iloc[0:0], candidates)
    assert empty.empty
    assert {"Ticker", "Primary Short Rank", "Fiscal Year", "Revenue"} <= set(empty.columns)


def test_short_primary_summary_imports_the_full_primary_short_tab():
    import config
    from src.writer import short_primary_summary_formula

    formula = short_primary_summary_formula()
    assert formula == f'=IMPORTRANGE("{config.PRIMARY_SHORT_SPREADSHEET_ID}","{config.PRIMARY_SHORT_TAB}!A:AX")'


def test_v10_default_short_weights_migrate_without_overwriting_custom_weights():
    old_defaults = [
        ["Short", "MODEL_VERSION", "secondary_trend_first_v10"],
        ["Short", "SHORT_WEIGHT_TREND", "50"],
        ["Short", "SHORT_WEIGHT_INFLECTION", "15"],
        ["Short", "SHORT_WEIGHT_OPERATING", "20"],
        ["Short", "SHORT_WEIGHT_CASH_FLOW", "10"],
        ["Short", "SHORT_WEIGHT_BALANCE_SHEET", "5"],
    ]
    migrated = read_controls(old_defaults)
    assert [migrated[f"SHORT_WEIGHT_{name}"] for name in ("TREND", "INFLECTION", "OPERATING", "CASH_FLOW", "BALANCE_SHEET", "POINT_IN_TIME")] == [45, 15, 15, 5, 5, 15]
    customised = read_controls([*old_defaults, ["Short", "SHORT_WEIGHT_TREND", "52"]])
    assert customised["SHORT_WEIGHT_TREND"] == 52
