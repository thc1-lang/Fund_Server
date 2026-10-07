from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import config
from main import run
from src.economic_policy import DIAGNOSTIC_ONLY_SIGNALS
from src.scoring import active_signal_registry, score_expanded
from src.ranking import rank_quant_candidates
from src.sheets_writer import archive_stale_generated_tabs, reconcile_tabs
from src.shorts import short_thesis_gate
from src.strategies import score_strategies
from src.transformations import DERIVED_FEATURES


def test_historical_screen_fails_closed_without_publication_provenance():
    with pytest.raises(ValueError, match="field-level public availability"):
        run(SimpleNamespace(historical_screen_date="2020-03-31"))


def test_trend_fields_remain_in_audit_but_cannot_change_primary_score():
    frame = pd.DataFrame({
        "book_yield": [0.5, 0.5],
        "book_yield__peer_score": [70.0, 70.0],
        "Sales Growth F(0)/F(-1)": [-50.0, 200.0],
        "Sales Growth F(0)/F(-1)__peer_score": [0.0, 100.0],
        "% Price Change (4 Weeks)": [-90.0, 90.0],
        "% Price Change (4 Weeks)__peer_score": [0.0, 100.0],
        "% Change F1 Est. (4 weeks)": [-90.0, 90.0],
        "% Change F1 Est. (4 weeks)__peer_score": [0.0, 100.0],
    })
    registry = active_signal_registry(frame)
    assert not (set(registry) & set(DIAGNOSTIC_ONLY_SIGNALS))
    scores = score_expanded(frame, {"VALUATION": 1}, 0, 0)
    assert scores["Expanded Long Score"].nunique() == 1
    assert "Sales Growth F(0)/F(-1)" in frame


def test_turnaround_is_current_weak_profitability_plus_value_and_survival():
    frame = pd.DataFrame({
        "Ticker": ["SETUP", "QUALITY", "INSOLVENT"],
        "Industry": ["I"] * 3,
        "Source Row": [2, 3, 4],
        "Data Status": ["SUFFICIENT"] * 3,
        "balance_sheet_gate_pass": [True, True, False],
    })
    for family in config.FAMILY_WEIGHTS:
        frame[f"{family} Long Sub-score"] = 65.0
    frame["VALUATION Long Sub-score"] = [75.0, 75.0, 75.0]
    frame["PROFITABILITY_AND_RETURNS Long Sub-score"] = [40.0, 75.0, 40.0]
    result = score_strategies(frame, config.STRATEGY_WEIGHTS, .65, 4, 55, 50)
    assert result["Turnaround Story Eligible"].tolist() == [True, False, False]
    assert result["Turnaround Setup Gate Pass"].tolist() == [True, False, True]


def test_short_thesis_needs_multiple_active_weaknesses():
    frame = pd.DataFrame({
        "VALUATION Short Sub-score": [80.0, 80.0, 80.0],
        "PROFITABILITY_AND_RETURNS Short Sub-score": [65.0, 40.0, 65.0],
        "BALANCE_SHEET_AND_LEVERAGE Short Sub-score": [40.0, 40.0, 40.0],
        "LIQUIDITY_AND_EFFICIENCY Short Sub-score": [np.nan, np.nan, np.nan],
    })
    weights = dict(config.SHORT_FAMILY_WEIGHTS)
    assert short_thesis_gate(frame, 2, 60, weights).tolist() == [True, False, True]
    weights["PROFITABILITY_AND_RETURNS"] = 0
    assert not short_thesis_gate(frame, 2, 60, weights).any()


def test_short_business_type_default_excludes_specialist_accounting():
    assert config.SHORT_ALLOWED_BUSINESS_TYPES == ("STANDARD_OPERATING_COMPANY",)
    assert config.FUNDAMENTAL_ALLOWED_BUSINESS_TYPES == ("STANDARD_OPERATING_COMPANY",)
    types = pd.Series(["STANDARD_OPERATING_COMPANY", "SPECIALIST_FINANCIAL", "EQUITY_REIT"])
    assert types.isin(config.SHORT_ALLOWED_BUSINESS_TYPES).tolist() == [True, False, False]


def test_short_sufficiency_and_confidence_do_not_depend_on_long_weights():
    frame = pd.DataFrame({"book_yield": [0.1], "book_yield__peer_score": [10.0]})
    scores = score_expanded(
        frame, {"GROWTH": 100.0, "VALUATION": 0.0}, 0.7, 0.5,
        short_family_weights={"GROWTH": 0.0, "VALUATION": 100.0},
    )
    assert scores.loc[0, "Data Status"] == "INSUFFICIENT DATA"
    assert scores.loc[0, "Short Data Status"] == "SUFFICIENT"
    assert scores.loc[0, "Short Score Confidence"] == pytest.approx(100.0)
    assert scores.loc[0, "Expanded Short Score"] == pytest.approx(90.0)
    candidate = pd.concat([pd.DataFrame({
        "Ticker": ["TEST"], "Industry": ["I"], "Source Row": [2],
        "short_tradability_gate_pass": [True], "economic_classification_verified": [True],
        "Short Thesis Gate Pass": [True], "Short Business Type Gate Pass": [True],
    }), scores], axis=1)
    ranked = rank_quant_candidates(candidate, 1, 1, 55, 60, min_confidence=85)
    assert ranked.loc[0, "Quantitative Candidate"] == "QUANT SHORT"


def test_v7_migration_preserves_custom_v6_weights():
    try:
        config.apply_control_panel([
            ["System", "MODEL_VERSION", "primary_snapshot_v6"],
            ["Family weight", "WEIGHT_VALUATION", 15],
            ["Family weight", "WEIGHT_GROWTH", 15],
            ["Short weight", "SHORT_WEIGHT_VALUATION", 30],
            ["Short weight", "SHORT_WEIGHT_PROFITABILITY_AND_RETURNS", 20],
        ])
        assert config.FAMILY_WEIGHTS["VALUATION"] == 15
        assert config.SHORT_FAMILY_WEIGHTS["VALUATION"] == 30
    finally:
        config.apply_control_panel([])


def test_stale_generated_tabs_are_archived_only_after_header_check():
    class FakeBook:
        def __init__(self):
            self.requests = []

        def values_batch_get(self, ranges, params):
            cells = {
                "Old": "INDUSTRY DIAGNOSTIC SUMMARY",
                "Old - Helper": "Old — STRATEGY AUDIT HELPER",
            }
            return {"valueRanges": [{"values": [[cells.get(name.split("'!")[0].strip("'"), "CUSTOM")]]} for name in ranges]}

        def batch_update(self, payload):
            self.requests.extend(payload["requests"])

    titles = ["Active", "Active - Helper", "Old", "Old - Helper", "Custom", "Custom - Helper"]
    metadata = {"sheets": [{"properties": {"title": title, "sheetId": i}} for i, title in enumerate(titles)]}
    book = FakeBook()
    archived = archive_stale_generated_tabs(book, metadata, ["Active"])
    assert archived == ["Old", "Old - Helper"]
    assert len(book.requests) == 2
    assert all(request["updateSheetProperties"]["properties"]["hidden"] for request in book.requests)
    assert not reconcile_tabs(["Active"], ["Active", "ARCHIVED 2026-09-26 - Old"]).unexpected


def test_safe_score_cannot_certify_reit_without_specialist_inputs():
    frame = pd.DataFrame({
        "Ticker": ["OPERATING", "REIT"], "Industry": ["I", "I"], "Source Row": [2, 3],
        "Data Status": ["SUFFICIENT", "SUFFICIENT"],
        "Fundamental Business Type Gate Pass": [True, False],
        "balance_sheet_gate_pass": [True, True],
    })
    for family in config.FAMILY_WEIGHTS:
        frame[f"{family} Long Sub-score"] = 70.0
    result = score_strategies(frame, config.STRATEGY_WEIGHTS, .65, 4, 55, 50, require_positive_eps_safe=False)
    assert result["Safe Eligible"].tolist() == [True, False]


def test_obsolete_control_panel_cannot_restore_temporal_weights():
    try:
        config.apply_control_panel([
            ["System", "MODEL_VERSION", "primary_economic_v5"],
            ["Family weight", "WEIGHT_MARKET_BEHAVIOUR", 50],
            ["Family weight", "WEIGHT_ESTIMATES_AND_REVISIONS", 50],
        ])
        assert config.FAMILY_WEIGHTS["MARKET_BEHAVIOUR"] == 0
        assert config.FAMILY_WEIGHTS["ESTIMATES_AND_REVISIONS"] == 0
        assert config.SHORT_FAMILY_WEIGHTS["MARKET_BEHAVIOUR"] == 0
    finally:
        config.apply_control_panel([])


@pytest.mark.parametrize("factor,numerator,denominator", [
    ("long_term_debt_equity", "Long Term Debt ($mil)", "Common Equity ($mil)"),
    ("long_term_debt_ebitda", "Long Term Debt ($mil)", "EBITDA ($mil)"),
    ("long_term_debt_ebit", "Long Term Debt ($mil)", "EBIT ($mil)"),
    ("preferred_common_equity", "Preferred Equity ($mil)", "Common Equity ($mil)"),
    ("receivables_days", "Receivables ($mil)", "Annual Sales ($mil)"),
    ("inventory_days", "Inventory ($mil)", "Cost of Goods Sold ($mil)"),
    ("intangibles_market_cap", "Intangibles ($mil)", "Market Cap (mil)"),
    ("intangibles_equity", "Intangibles ($mil)", "Common Equity ($mil)"),
    ("dividend_payout_ratio", "Dividend ", "12 Mo Trailing EPS"),
])
def test_negative_accounting_numerator_cannot_earn_a_favourable_ratio(factor, numerator, denominator):
    frame = pd.DataFrame({numerator: [-1.0, 0.0], denominator: [10.0, 10.0]})
    ratio = DERIVED_FEATURES[factor].fn(frame)
    assert pd.isna(ratio.iloc[0])
    assert ratio.iloc[1] == 0.0
