import json
from datetime import date

import numpy as np
import pandas as pd
import pytest
import config

from src.audit import _industry_preview_filename
from src.peer_statistics import apply_liquidity_policy, percentile, robust_zscore, zscore, peer_enrich
from src.ranking import rank_quant_candidates
from src.schema import REQUIRED_HEADERS, validate_headers
from src.scoring import RAW_SIGNALS, active_signal_registry, score_expanded
from src.sheets_writer import (_retry_after_seconds, _section_ranges, _sheet_values,
                               build_helper_table, build_industry_summary, build_metric_registry,
                               build_quant_summary, build_strategy_summary, helper_sheet_name,
                               reconcile_tabs, validate_destination, write_analysis_batch)
from src.strategies import reconstruct_strategy_score, score_strategies
from src.transformations import DERIVED_FEATURES, derive_eg2, inverse_positive, natural_log, percent_to_decimal, safe_div, signed_log
from src.normalization import normalize_series, apply_unit_normalization
from src.institutional import (METRIC_AVAILABILITY_REGISTRY, availability_registry_frame,
                               balance_sheet_gates, coverage_audit, eps_audit,
                               margin_reconciliation, tradability_gates)
from src.universe import eligible_universe


def test_schema_contract():
    validate_headers(list(REQUIRED_HEADERS))
    with pytest.raises(ValueError):
        validate_headers([h for h in REQUIRED_HEADERS if h != "Ticker"])


def test_configured_unit_normalization_cases():
    cases = [(0.03821, "ratio", 0.03821), (62.97, "percent", 0.6297),
             (0.6297, "ratio", 0.6297), (0.44, "percent", 0.0044),
             (3.44, "ratio", 3.44), (24.77, "multiple", 24.77)]
    for value, unit, expected in cases:
        normalized, _ = normalize_series(pd.Series([value]), unit)
        assert normalized.iloc[0] == pytest.approx(expected)


def test_logs():
    s = natural_log(pd.Series([-1, 0, 1, np.e]))
    assert s.isna().iloc[:2].all() and s.iloc[3] == pytest.approx(1)
    assert signed_log(pd.Series([-1, 0, 1])).tolist() == pytest.approx([-np.log(2), 0, np.log(2)])


def test_safe_ratios_and_inverse():
    x = safe_div(pd.Series([1, 1, np.nan]), pd.Series([0, -1, 2]), positive_den=True)
    assert x.isna().all()
    inv = inverse_positive(pd.Series([2, 0, -2, np.nan]))
    assert inv.iloc[0] == .5 and inv.iloc[1:].isna().all()


def test_eg2_sign_rules():
    value, reason = derive_eg2(pd.Series([2, 0, -2, 2]), pd.Series([3, 3, 3, np.nan]))
    assert value.iloc[0] == pytest.approx(50)
    assert value.iloc[1:].isna().all()
    assert "zero" in reason.iloc[1] and "nonpositive" in reason.iloc[2] and "missing" in reason.iloc[3]


def test_peer_statistics():
    s = pd.Series([1, 2, 3, 4, 100])
    assert percentile(s).tolist() == [0, 25, 50, 75, 100]
    assert percentile(s).iloc[-1] == 100
    assert zscore(pd.Series([1, 1, 1])).isna().all()
    assert robust_zscore(pd.Series([1, 2, 3])).iloc[1] == 0


def test_hierarchical_industry_sector_shrinkage_has_no_jump():
    df = pd.DataFrame({"Industry": ["A"] * 4 + ["B"] * 2, "Sector": ["S"] * 6, "x": range(6)})
    out = peer_enrich(df, "x", min_obs=5, shrinkage_strength=8)
    assert out["x__comparison_group_type"].eq("Industry/sector shrinkage").all()
    assert out["x__sector_valid_count"].eq(6).all()
    assert out.loc[0, "x__industry_weight"] == pytest.approx(4 / 12)
    assert out.loc[4, "x__industry_weight"] == pytest.approx(2 / 10)
    assert not any(c.endswith("__winsorised") for c in out.columns)


def test_no_whole_us_fallback():
    df = pd.DataFrame({"Industry": ["A"] * 3 + ["B"] * 3, "Sector": ["S1"] * 3 + ["S2"] * 3, "x": range(6)})
    out = peer_enrich(df, "x", min_obs=5)
    assert out["x__comparison_group_type"].eq("INSUFFICIENT SECTOR PEERS").all()
    assert out["x__peer_score"].isna().all()


def test_peer_score_is_auditable_robust_blend():
    df = pd.DataFrame({"Industry": ["A"] * 5, "Sector": ["S"] * 5, "x": [1, 2, 3, 4, 100]})
    out = peer_enrich(df, "x", min_obs=5, shrinkage_strength=0, z_cap=3, percentile_weight=.5)
    expected = .5 * out["x__percentile"] + .5 * out["x__robust_z_score"]
    assert np.allclose(out["x__peer_score"], expected)
    assert (out["x__peer_score"] - out["x__classical_z_score"]).abs().max() > 1


def test_duplicate_metrics_do_not_inflate_independent_coverage_or_signs():
    df = pd.DataFrame({
        "Ticker": ["A"], "Industry": ["I"],
        "roe_valid": [10.0], "Current ROI (TTM)": [np.nan], "Current ROA (TTM)": [np.nan],
        "roe_valid__peer_score": [80.0], "Current ROI (TTM)__peer_score": [np.nan],
        "Current ROA (TTM)__peer_score": [np.nan], "book_yield": [2.0], "book_yield__peer_score": [20.0],
    })
    out = score_expanded(df, {"PROFITABILITY_AND_RETURNS": 1, "VALUATION": 1}, 0, 0)
    assert out.loc[0, "Raw Signal Coverage"] == pytest.approx(.5)
    assert out.loc[0, "Independent Group Coverage"] == 1.0
    assert out.loc[0, "book_yield__economic_long_score"] == 20.0
    assert out.loc[0, "Positive Peer Evidence Count"] == 1
    assert abs(out.loc[0, "Expanded Long Score"] - 50) <= abs(out.loc[0, "Expanded Raw Long Score"] - 50)


def test_structurally_inapplicable_financial_liquidity_does_not_reduce_coverage():
    df = pd.DataFrame({
        "Ticker": ["BANK"], "Industry": ["Bank"], "book_yield": [1.0],
        "book_yield__peer_score": [25.0], "Financial Company Liquidity Exclusion": [True],
        "liquidity_current_ratio__directional_score": [np.nan],
    })
    out = score_expanded(df, {"VALUATION": 1, "LIQUIDITY_AND_EFFICIENCY": 1}, 0, 0)
    assert out.loc[0, "Independent Group Coverage"] == 1.0
    assert out.loc[0, "Quant Composite Total Configured Weight"] == 1.0
    assert out.loc[0, "Quant Composite Weight Coverage"] == 1.0


def test_registry_governance():
    for spec in DERIVED_FEATURES.values():
        assert spec.formula and spec.inputs and spec.units and spec.valid_when and spec.duplicate_group
        assert spec.direction in {"higher_better", "lower_better", "target_range", "two_sided_risk", "confidence_only"}
    assert DERIVED_FEATURES["forward_pe_change"].family is None
    assert DERIVED_FEATURES["forward_pe_change"].direction == "confidence_only"
    scored_earnings = {k for k, v in DERIVED_FEATURES.items() if v.duplicate_group == "earnings_value" and v.family is not None}
    assert scored_earnings == {"earnings_yield_pe_valid", "signed_trailing_eps_yield", "forward_eps_profitability"}


def test_no_fabricated_ev_features():
    assert not any(k in DERIVED_FEATURES for k in ["enterprise_value", "ev_ebit", "ev_ebitda", "net_debt"])


def test_etf_cef_exclusion_and_duplicates():
    df = pd.DataFrame({
        "Ticker": ["A", "E", "C"], "COM/ADR/Canadian": ["com", "ETF", "CEF"],
        "Sector": ["S", "S", "S"], "Industry": ["I", "I", "I"],
    })
    eligible, excluded = eligible_universe(df, ("COM",))
    assert eligible["Ticker"].tolist() == ["A"] and len(excluded) == 2
    dup = pd.DataFrame({
        "Ticker": ["A", "a"], "COM/ADR/Canadian": ["COM", "COM"],
        "Sector": ["S", "S"], "Industry": ["I", "I"],
    })
    with pytest.raises(ValueError): eligible_universe(dup, ("COM",))


def test_universe_excludes_blank_peer_keys():
    df = pd.DataFrame({
        "Ticker": ["A", "B"], "COM/ADR/Canadian": ["COM", "COM"],
        "Sector": ["", "S"], "Industry": ["I", ""],
    })
    eligible, excluded = eligible_universe(df, ("COM",))
    assert eligible.empty
    assert set(excluded["Exclusion Reason"]) == {"blank sector", "blank industry"}


def test_exact_tab_matching_and_destination_safety():
    m = reconcile_tabs(["Oil & Gas", "Banks"], ["Oil & Gas", "banks", "Other"])
    assert m.matched == ["Oil & Gas"] and "Banks" in m.missing and "banks" in m.unexpected
    with pytest.raises(ValueError): validate_destination("a", None, False)
    with pytest.raises(ValueError): validate_destination("a", "a", False)


def test_sheet_values_are_json_serializable():
    rows = [[np.int64(2), np.float64(3.5), np.bool_(True), np.nan, pd.NA, date(2026, 8, 22)]]
    values = _sheet_values(rows)
    assert values == [[2, 3.5, True, "NO DATA", "NO DATA", "2026-08-22"]]
    json.dumps({"values": values}, allow_nan=False)


def test_retry_after_parser_accepts_seconds_and_http_dates():
    assert _retry_after_seconds("12", 5) == 12
    assert _retry_after_seconds("not-a-date", 5) == 5


def test_industry_preview_filenames_are_nonempty_and_collision_safe():
    used = set()
    first = _industry_preview_filename("A/B", used)
    second = _industry_preview_filename("A:B", used)
    empty = _industry_preview_filename("///", used)
    assert first != second
    assert empty == "UNNAMED_INDUSTRY.csv"


def test_live_analysis_uses_batched_sheet_requests():
    class FakeBook:
        def __init__(self):
            self.clears = []
            self.updates = []

        def values_batch_clear(self, *, body):
            self.clears.append(body)

        def values_batch_update(self, *, body):
            self.updates.append(body)

        def batch_update(self, body):
            self.updates.append(body)

        def values_batch_get(self, ranges, params=None):
            written = {x["range"].split("!")[0]: x["values"] for call in self.updates for x in call.get("data", [])}
            return {"valueRanges": [{"values": written[r.split("!")[0]]} for r in ranges]}

    empty_results = pd.DataFrame(columns=[
        "Quantitative Candidate", "Expanded Long Rank", "Expanded Short Rank", "Industry",
        "Ticker", "Company Name", "Market Cap (mil)", "Sector", "Exchange", "COM/ADR/Canadian", "Data Status", "Pass Rate",
        "Safe Score", "Safe Universe Rank", "Safe Within-Industry Rank", "Safe Weight Coverage",
        "Safe Components Available", "Safe Components Missing", "High Growth Potential Score",
        "High Growth Potential Universe Rank", "High Growth Potential Within-Industry Rank",
        "High Growth Potential Weight Coverage", "High Growth Potential Components Available",
        "High Growth Potential Components Missing", "Turnaround Story Score",
        "Turnaround Story Universe Rank", "Turnaround Story Within-Industry Rank",
        "Turnaround Story Weight Coverage", "Turnaround Story Components Available",
        "Turnaround Story Components Missing", "Turnaround Value Score", "Turnaround Profitability Score", "Turnaround Setup Gate Pass",
    ])
    book = FakeBook()
    updated = write_analysis_batch(
        book=book, control_rows=[], strategy_rows=[],
        strategy_weights={"Safe": {}, "High Growth Potential": {}, "Turnaround Story": {}},
        quant_weights={},
        industry_tables=[], all_rows=empty_results, strategy_summary_top_n=50, min_strategy_score=55,
        sheet_metadata={"sheets": [
            {"properties": {"sheetId": 1, "title": "Control Panel", "gridProperties": {"rowCount": 1, "columnCount": 1}}},
            {"properties": {"sheetId": 6, "title": "Metric Registry", "gridProperties": {"rowCount": 1, "columnCount": 1}}},
            {"properties": {"sheetId": 3, "title": "Safe Summary", "gridProperties": {"rowCount": 1, "columnCount": 1}}},
            {"properties": {"sheetId": 4, "title": "High Growth Potential Summary", "gridProperties": {"rowCount": 1, "columnCount": 1}}},
            {"properties": {"sheetId": 5, "title": "Turnaround Story Summary", "gridProperties": {"rowCount": 1, "columnCount": 1}}},
        ]},
    )
    assert updated == 5
    assert len(book.clears) == 0
    value_updates = [update for update in book.updates if "data" in update]
    assert len(value_updates) == 1
    assert len(value_updates[0]["data"]) == 5
    format_requests = [request for update in book.updates for request in update.get("requests", [])]
    assert sum("autoResizeDimensions" in request for request in format_requests) == 5
    expansions = [request["updateSheetProperties"] for request in format_requests
                  if "updateSheetProperties" in request and "rowCount" in request["updateSheetProperties"]["fields"]]
    assert len(expansions) == 5


def test_helper_exposes_every_model_column():
    table = pd.DataFrame({
        "Ticker": ["A"], "Industry": ["I"], "Safe Universe Rank": [1],
        "High Growth Potential Universe Rank": [1], "Turnaround Story Universe Rank": [1],
        "custom_new_calculation": [42.0], "Data Status": ["SUFFICIENT"],
    })
    helper = build_helper_table(
        table, {"Safe": {}, "High Growth Potential": {}, "Turnaround Story": {}}
    )
    assert set(table.columns) <= set(helper.columns)
    assert helper.loc[0, "custom_new_calculation"] == 42.0


def test_strategy_summary_puts_dynamic_shortlist_reason_in_column_e():
    frame = pd.DataFrame({
        "Ticker": ["ABC"], "Company Name": ["Example"], "Market Cap (mil)": [12_345.0],
        "Industry": ["Software"], "Sector": ["Technology"], "Exchange": ["NYSE"],
        "COM/ADR/Canadian": ["COM"], "Data Status": ["SUFFICIENT"],
        "Safe Implementation Eligible": [True], "Safe Score": [70.0], "Safe Universe Rank": [1], "Safe Within-Industry Rank": [1],
        "Safe Weight Coverage": [1.0], "Safe Components Available": [5], "Safe Components Missing": [0],
        "BALANCE_SHEET_AND_LEVERAGE Long Sub-score": [82.0],
        "Safe BALANCE_SHEET_AND_LEVERAGE Contribution Points": [24.6],
    })
    weights = {"BALANCE_SHEET_AND_LEVERAGE": 30.0}
    summary = build_strategy_summary(frame, "Safe", weights, top_n=50, min_score=55)
    assert summary.columns[:7].tolist() == [
        "Universe Rank", "Within-Industry Rank", "Ticker", "Company Name",
        "Shortlist Reason", "Market Cap (mil)", "Industry",
    ]
    assert summary.loc[0, "Market Cap (mil)"] == 12_345.0
    assert "balance sheet and leverage 24.6 points" in summary.loc[0, "Shortlist Reason"]
    changed = frame.copy()
    changed.loc[0, "Safe BALANCE_SHEET_AND_LEVERAGE Contribution Points"] = 18.2
    assert "18.2 points" in build_strategy_summary(changed, "Safe", weights, 50, 55).loc[0, "Shortlist Reason"]


def test_metric_registry_is_unique_complete_and_uses_control_weights():
    registry = build_metric_registry(config.STRATEGY_WEIGHTS)
    expected = set(RAW_SIGNALS) | set(DERIVED_FEATURES)
    assert expected <= set(registry["Metric / Score"])
    assert not registry["Metric / Score"].duplicated().any()
    row = registry.loc[registry["Metric / Score"].eq("debt_equity_ratio")].iloc[0]
    assert row["Safe Weight"] == config.STRATEGY_WEIGHTS["Safe"]["BALANCE_SHEET_AND_LEVERAGE"]


def test_strategy_scores_renormalise_missing_and_reconcile():
    families = list(config.STRATEGY_WEIGHTS["Safe"])
    data = {"Ticker": ["A", "B"], "Industry": ["I", "I"], "Source Row": [2, 3],
            "Data Status": ["SUFFICIENT", "SUFFICIENT"], "debt_equity_ratio": [1.0, 1.0]}
    for family in families:
        data[f"{family} Long Sub-score"] = [80.0, 60.0]
    data["GROWTH Long Sub-score"][1] = np.nan
    out = score_strategies(pd.DataFrame(data), config.STRATEGY_WEIGHTS, .50, 3, 55, 2)
    joined = pd.concat([pd.DataFrame(data), out], axis=1)
    for idx in joined.index:
        rebuilt = reconstruct_strategy_score(joined.loc[idx], "Safe", families)
        assert rebuilt == pytest.approx(joined.loc[idx, "Safe Score"])
    assert joined.loc[1, "Safe Applicable Weight"] < joined.loc[0, "Safe Applicable Weight"]
    assert joined.loc[1, "Safe Score"] == pytest.approx(60.0)


def test_strategy_weight_change_is_isolated():
    families = list(config.STRATEGY_WEIGHTS["Safe"])
    data = {"Ticker": ["A", "B"], "Industry": ["I", "I"], "Source Row": [2, 3],
            "Data Status": ["SUFFICIENT", "SUFFICIENT"], "debt_equity_ratio": [1.0, 1.0]}
    for family in families:
        data[f"{family} Long Sub-score"] = [50.0, 50.0]
    data["GROWTH Long Sub-score"] = [95.0, 40.0]
    data["BALANCE_SHEET_AND_LEVERAGE Long Sub-score"] = [40.0, 95.0]
    frame = pd.DataFrame(data)
    base = score_strategies(frame, config.STRATEGY_WEIGHTS, .5, 3, 50, 2)
    changed = {k: dict(v) for k, v in config.STRATEGY_WEIGHTS.items()}
    changed["Safe"]["GROWTH"] = 1000.0
    revised = score_strategies(frame, changed, .5, 3, 50, 2)
    assert not base["Safe Score"].equals(revised["Safe Score"])
    assert base["High Growth Potential Score"].equals(revised["High Growth Potential Score"])
    assert base["Turnaround Story Score"].equals(revised["Turnaround Story Score"])


def test_helper_name_is_stable_and_within_google_limit():
    name = helper_sheet_name("X" * 100)
    assert len(name) <= 100 and name.endswith(" - Helper")


def test_industry_format_ranges_follow_variable_group_sizes():
    rows = [
        ["INDUSTRY DIAGNOSTIC SUMMARY"], ["Metric", "Value"], ["Industry", "I"], [""],
        ["QUANT LONG CANDIDATES"], ["Ticker"], ["L1"], ["L2"], [""],
        ["QUANT SHORT CANDIDATES"], ["Ticker"], ["S1"], [""],
        ["FULL INDUSTRY RANKING"], ["Ticker"], ["A"], ["B"], ["C"],
    ]
    ranges = _section_ranges(rows)
    assert ranges["QUANT LONG CANDIDATES"] == (4, 5, 8)
    assert ranges["QUANT SHORT CANDIDATES"] == (9, 10, 12)
    assert ranges["FULL INDUSTRY RANKING"] == (13, 14, 18)


def test_candidate_no_overlap_and_threshold():
    df = pd.DataFrame({
        "Ticker": ["A", "B"], "Industry": ["I", "I"], "Data Status": ["SUFFICIENT", "SUFFICIENT"],
        "Expanded Long Score": [90, 10], "Expanded Short Score": [80, 95], "Short Thesis Gate Pass": [True, True], "Short Business Type Gate Pass": [True, True],
    })
    out = rank_quant_candidates(df, 3, 3, 55, 55)
    assert out.loc[0, "Quantitative Candidate"] == "QUANT LONG"
    assert out.loc[1, "Quantitative Candidate"] == "QUANT SHORT"
    assert out.loc[0, "Candidate Conflict Resolved"]
    assert not out.loc[1, "Candidate Conflict Resolved"]


def test_ranking_ties_are_deterministic_and_bounded():
    df = pd.DataFrame({
        "Ticker": ["D", "B", "A", "C"], "Industry": ["I"] * 4,
        "Data Status": ["SUFFICIENT"] * 4,
        "Expanded Long Score": [60] * 4, "Expanded Short Score": [40] * 4,
    })
    out = rank_quant_candidates(df, 3, 3, 55, 55)
    assert set(out.loc[out["Quantitative Candidate"].eq("QUANT LONG"), "Ticker"]) == {"A", "B", "C"}
    assert out["Expanded Long Rank"].max() == 4


def test_quant_candidates_have_confidence_gate_and_global_caps():
    df = pd.DataFrame({
        "Ticker": ["A", "B", "C", "D", "E", "F"],
        "Industry": ["I1", "I2", "I3", "I4", "I5", "I6"],
        "Data Status": ["SUFFICIENT"] * 6,
        "Expanded Long Score": [70, 69, 68, 20, 19, 18],
        "Expanded Short Score": [30, 31, 32, 80, 81, 82],
        "Score Confidence": [95, 90, 80, 95, 90, 80], "Short Thesis Gate Pass": [True] * 6, "Short Business Type Gate Pass": [True] * 6,
    })
    out = rank_quant_candidates(df, 3, 3, 55, 55, 1, 1, 85)
    assert out.loc[out["Quantitative Candidate"].eq("QUANT LONG"), "Ticker"].tolist() == ["A"]
    assert out.loc[out["Quantitative Candidate"].eq("QUANT SHORT"), "Ticker"].tolist() == ["E"]


def test_balance_sheet_gate_filters_selection_but_preserves_research_rank():
    df = pd.DataFrame({
        "Ticker": ["PASS", "DEBT", "BOOK", "MISSING"],
        "Industry": ["I1", "I2", "I3", "I4"],
        "Data Status": ["SUFFICIENT"] * 4,
        "Expanded Long Score": [70, 80, 90, 95],
        "Expanded Short Score": [30, 20, 10, 5],
        "Score Confidence": [100] * 4,
        "debt_equity_ratio": [1.99, 2.0, 1.0, np.nan],
        "book_price_divergence": [0.0, 0.0, 0.01, 0.0],
    })
    df["tradability_gate_pass"] = True
    df["balance_sheet_gate_pass"] = [True, False, False, False]
    out = rank_quant_candidates(df, 3, 3, 55, 55, 10, 10, 85)
    assert set(out.loc[out["Quantitative Candidate"].eq("QUANT LONG"), "Ticker"]) == {"PASS"}
    assert out.loc[out["Implementation Candidate"].eq("IMPLEMENTABLE LONG"), "Ticker"].tolist() == ["PASS"]


def test_financial_leverage_exemption_is_explicit():
    df = pd.DataFrame({
        "Ticker": ["FIN", "RISKY"], "Industry": ["Bank", "Industrial"],
        "Sector": ["Financials", "Industrials"], "Data Status": ["SUFFICIENT"] * 2,
        "Expanded Long Score": [80, 20], "Expanded Short Score": [20, 80],
        "Score Confidence": [100, 100], "debt_equity_ratio": [8.0, 8.0],
    })
    gates = balance_sheet_gates(df.assign(debt_total_capital_ratio=np.nan, **{"Current Ratio": 1.0}),
        financial_sectors=("Financials",), max_de=2, max_debt_capital=.75, min_current=.75,
        max_reit_debt_ebitda=10, max_reit_debt_ebit=12)
    assert gates.loc[0, "financial_sector_leverage_exemption"]
    assert gates.loc[0, "balance_sheet_gate_pass"]


def test_quant_summary_contains_both_directions():
    df = pd.DataFrame({
        "Industry": ["I", "I"], "Quantitative Candidate": ["QUANT LONG", "QUANT SHORT"],
        "Expanded Long Rank": [1, 2], "Expanded Short Rank": [2, 1], "Ticker": ["L", "S"],
        "Company Name": ["Long", "Short"], "Expanded Long Score": [80, 20], "Expanded Short Score": [20, 80],
    })
    s = build_quant_summary(df)
    assert set(s["Quantitative Candidate"]) == {"QUANT LONG", "QUANT SHORT"}


def test_liquidity_policy_is_asymmetric_and_excludes_financials():
    df = pd.DataFrame({
        "Sector": ["Industrials", "Industrials", "Financials"],
        "Current Ratio__zscore": [-3, 1, -3],
        "Quick Ratio__zscore": [-2, 0, -2],
        "Cash Ratio__zscore": [-0.5, 3, -3],
        "working_capital_to_sales__zscore": [2.5, 0, 3],
    })
    out = apply_liquidity_policy(df, ("Financials",), -1, 0, 2, 3)
    assert out.loc[0, "liquidity_current_ratio__directional_score"] == 0
    assert out.loc[1, "liquidity_current_ratio__directional_score"] == 50
    assert pd.isna(out.loc[2, "liquidity_current_ratio__directional_score"])
    assert bool(out.loc[0, "Excessive Working Capital Flag"])
    assert pd.isna(out.loc[2, "Excessive Working Capital Flag"])


def test_payout_policy_penalises_only_economic_distress():
    df = pd.DataFrame({
        "Sector": ["Industrials"] * 4,
        "Dividend ": [0, 1, 1, 1], "12 Mo Trailing EPS": [1, 2, 1, -1],
        "dividend_payout_ratio": [0, .5, 1.0, np.nan],
    })
    for source in ("Current Ratio", "Quick Ratio", "Cash Ratio", "working_capital_to_sales"):
        df[f"{source}__zscore"] = 0.0
    out = apply_liquidity_policy(df, (), -1, 0, 2, 3, .8, 1.5)
    assert out["payout_sustainability__directional_score"].tolist() == pytest.approx([50, 50, 35.7142857, 0])
    assert out["Unsustainable Payout Flag"].tolist() == [False, False, True, True]


def test_industry_summary_has_diagnostics():
    df = pd.DataFrame({
        "Industry": ["I", "I"], "Sector": ["S", "S"], "Ticker": ["A", "B"],
        "Data Status": ["SUFFICIENT", "SUFFICIENT"], "Quantitative Candidate": ["QUANT LONG", "QUANT SHORT"],
        "Expanded Long Score": [80, 30], "Expanded Short Score": [20, 75],
        "Expanded Net Quant Score": [60, -45], "Score Confidence": [90, 80],
    })
    out = build_industry_summary(df)
    assert out.loc[0, "Top Long Ticker"] == "A"
    assert out.loc[0, "Top Short Ticker"] == "B"
    assert out.loc[0, "Eligible Stocks"] == 2


def test_google_control_panel_covers_and_applies_all_model_controls():
    parameters = {row[1] for row in config.control_panel_rows()}
    assert {"PEER_SHRINKAGE_STRENGTH", "LIQUIDITY_DISTRESS_Z_THRESHOLD", "MIN_STRATEGY_SCORE"} <= parameters
    assert {f"WEIGHT_{family}" for family in config.FAMILY_WEIGHTS} <= parameters
    old = config.PEER_SHRINKAGE_STRENGTH
    try:
        config.apply_control_panel([["Peer model", "PEER_SHRINKAGE_STRENGTH", 12, "test"]])
        assert config.PEER_SHRINKAGE_STRENGTH == 12
        assert config.MODEL_CONTROLS["PEER_SHRINKAGE_STRENGTH"] == 12
    finally:
        config.apply_control_panel([["Peer model", "PEER_SHRINKAGE_STRENGTH", old, "restore"]])


def test_control_panel_is_transactional_and_resets_missing_values():
    baseline = config.PEER_SHRINKAGE_STRENGTH
    config.apply_control_panel([["Peer model", "PEER_SHRINKAGE_STRENGTH", 12, "test"]])
    assert config.PEER_SHRINKAGE_STRENGTH == 12
    config.apply_control_panel([])
    assert config.PEER_SHRINKAGE_STRENGTH == baseline
    with pytest.raises(ValueError, match="MIN_FACTOR_COVERAGE"):
        config.apply_control_panel([["Coverage", "MIN_FACTOR_COVERAGE", 1.5, "invalid"]])
    assert config.MIN_FACTOR_COVERAGE <= 1
    with pytest.raises(ValueError, match="finite"):
        config.apply_control_panel([["Peer model", "PEER_Z_SCORE_CAP", "nan", "invalid"]])
    controls = config.apply_control_panel([["Risk gates", "MAX_BOOK_PRICE_DIVERGENCE", "none", "off"]])
    assert controls["MAX_BOOK_PRICE_DIVERGENCE"] is None


def test_control_panel_model_version_migrates_old_default_weights_once():
    config.apply_control_panel([["Family weight", "WEIGHT_VALUATION", 1.0, "legacy"]])
    assert config.FAMILY_WEIGHTS["VALUATION"] == 20.0
    config.apply_control_panel([
        ["System", "MODEL_VERSION", config.MODEL_VERSION, "current"],
        ["Family weight", "WEIGHT_VALUATION", 9.0, "custom"],
    ])
    assert config.FAMILY_WEIGHTS["VALUATION"] == 9.0
    config.apply_control_panel([])


def test_debt_equity_normalization_audit_proves_bug_fix():
    frame = pd.DataFrame({"Debt/Equity Ratio": [0.03821], "Debt/Total Capital": [20.0],
                          "Net Margin %": [62.97], "Div. Yield %": [0.44],
                          "Current Ratio": [3.44], "P/E (F1)": [24.77]})
    out = apply_unit_normalization(frame)
    assert out.loc[0, "debt_equity_ratio__raw"] == pytest.approx(0.03821)
    assert out.loc[0, "debt_equity_ratio__normalized"] == pytest.approx(0.03821)
    assert out.loc[0, "debt_equity_ratio__normalization_applied"] == "IDENTITY"
    assert out.loc[0, "net_margin_vendor_normalized"] == pytest.approx(0.6297)


def test_margin_reconciliation_pass_fail_and_not_testable():
    df = pd.DataFrame({"net_margin_vendor_normalized": [.20, .40, .20],
                       "net_income_margin": [.18, .20, np.nan],
                       "ebit_margin": [np.nan] * 3, "gross_margin": [np.nan] * 3,
                       "ebitda_margin": [np.nan] * 3, "pretax_margin": [np.nan] * 3,
                       "cash_flow_margin": [np.nan] * 3})
    out = margin_reconciliation(df, .05)
    assert out.loc[0, "net_margin_reconciliation_flag"] == "WITHIN_TOLERANCE"
    assert out.loc[1, "net_margin_reconciliation_flag"].startswith("OUTSIDE_TOLERANCE")
    assert out.loc[2, "net_margin_reconciliation_flag"] == "NUMERATOR_OR_SALES_UNAVAILABLE"
    assert not out.columns.duplicated().any()


def test_eps_strategy_specific_negative_base_semantics():
    audit = eps_audit(pd.DataFrame({"F1 Consensus Est.": [1.0, -1.0, 0.0], "F2 Consensus Est.": [2.0, 1.0, 1.0]}))
    assert audit.loc[0, "eps_growth_valid_for_safe"]
    assert not audit.loc[1, "eps_growth_valid_for_safe"]
    assert audit.loc[1, "high_growth_eps_exemption"] and audit.loc[1, "turnaround_eps_exemption"]
    assert "INVALID" in audit.loc[1, "eps_growth_interpretation_flag"]


def test_sector_aware_standard_reit_and_financial_gates():
    df = pd.DataFrame({"Sector": ["Industrials", "Financials", "Real Estate"],
        "Industry": ["Machinery", "Banks - Major Regional", "REIT and Equity Trust - Retail"],
        "debt_equity_ratio": [1.0, 20.0, 5.0], "debt_total_capital_ratio": [.4, .9, .8],
        "Current Ratio": [1.2, .1, .5], "long_term_debt_ebitda": [2.0, np.nan, 8.0],
        "long_term_debt_ebit": [3.0, np.nan, 9.0]})
    out = balance_sheet_gates(df, financial_sectors=("Financials",), max_de=2,
        max_debt_capital=.75, min_current=.75, max_reit_debt_ebitda=10, max_reit_debt_ebit=12)
    assert out["balance_sheet_gate_pass"].tolist() == [True, True, True]
    assert out.loc[1, "balance_sheet_gate_type"] == "SPECIALIST_FINANCIAL"
    assert out.loc[2, "balance_sheet_gate_type"] == "EQUITY_REIT"


def test_tradability_and_short_validation_are_hard_separate_controls():
    df = pd.DataFrame({"Market Cap (mil)": [5000, 2000], "average_daily_dollar_volume": [60e6, 10e6],
                       "Exchange": ["NASDAQ", "OTC"], "COM/ADR/Canadian": ["ADR", "COM"]})
    out = tradability_gates(df, min_market_cap=3000, min_adv=25e6, min_short_adv=50e6,
                            allow_otc=False, allow_adr=True, allow_mlp=True, allow_canadian=True)
    assert out.loc[0, "tradability_gate_pass"] and out.loc[0, "short_tradability_gate_pass"]
    assert not out.loc[1, "tradability_gate_pass"]
    assert out["short_implementation_validation_required"].all()
    assert out["borrow_cost"].isna().all()


def test_unavailable_registry_has_hooks_and_never_numeric_zero():
    required = {"enterprise_value", "ev_ebit", "ev_ebitda", "net_debt", "free_cash_flow",
                "interest_coverage", "roic_nopat", "altman_z", "piotroski_f", "dupont",
                "accruals", "time_series_indicators"}
    assert required <= set(METRIC_AVAILABILITY_REGISTRY)
    registry = availability_registry_frame({"Market Cap (mil)", "EBIT ($mil)"})
    subset = registry[registry["metric_name"].isin(required)]
    assert subset["calculation_function_name"].str.len().gt(0).all()
    assert not subset["currently_active"].any()


def test_coverage_separates_quant_from_underwriting_without_dependency_double_count():
    df = pd.DataFrame({"Raw Signal Coverage": [1.0], "Independent Group Coverage": [1.0],
        "Safe Weight Coverage": [1.0], "High Growth Potential Weight Coverage": [1.0],
        "Turnaround Story Weight Coverage": [1.0], "tradability_gate_pass": [True],
        "Market Cap (mil)": [5000], "P/E (F1)": [20], "PEG Ratio": [1.5],
        "EBIT ($mil)": [100], "EBITDA ($mil)": [120], "Net Margin %": [10],
        "Current ROA (TTM)": [5], "Current ROE (TTM)": [10], "Debt/Equity Ratio": [.2],
        "Debt/Total Capital": [20], "Current Ratio": [2], "Quick Ratio": [1.5],
        "Book Value": [10], "Cash Flow ($mil)": [90], "Avg Volume": [1e6],
        "Exchange": ["NYSE"], "COM/ADR/Canadian": ["COM"]})
    out = coverage_audit(df)
    assert out.loc[0, "quant_ranking_coverage_score"] == 100
    assert out.loc[0, "institutional_underwriting_coverage_score"] < 100


def test_primary_is_not_an_active_strategy_or_generated_system_sheet():
    from src.strategies import STRATEGIES
    from src.sheets_writer import SYSTEM_SHEETS
    assert STRATEGIES == ("Safe", "High Growth Potential", "Turnaround Story")
    assert "Primary Summary" not in SYSTEM_SHEETS
    assert all("Primary" not in name for name in config._CONTROL_NAMES)
