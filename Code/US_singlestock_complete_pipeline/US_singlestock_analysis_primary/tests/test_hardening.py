from copy import deepcopy
import numpy as np
import pandas as pd
import pytest

import config
from src.economic_policy import classify_business, DIAGNOSTIC_ONLY_SIGNALS
from src.institutional import balance_sheet_gates, eps_audit
from src.peer_statistics import peer_enrich, apply_liquidity_policy
from src.ranking import rank_quant_candidates
from src.scoring import active_signal_registry, score_expanded
from src.sheets_writer import verify_cell_values, build_strategy_summary
from src.strategies import score_strategies


@pytest.fixture(autouse=True)
def reset_controls():
    config.apply_control_panel([])
    yield
    config.apply_control_panel([])


def balance(frame):
    return balance_sheet_gates(frame, financial_sectors=("Finance",), max_de=2,
        max_debt_capital=.75, min_current=.75, max_reit_debt_ebitda=10, max_reit_debt_ebit=12)


@pytest.mark.parametrize("weight", [0, .6, 1])
def test_constant_cross_section_is_neutral_and_missing_row_stays_missing(weight):
    frame = pd.DataFrame({"Sector": ["S"]*11, "Industry": ["I"]*11, "x": [0.]*10+[np.nan]})
    result = peer_enrich(frame, "x", percentile_weight=weight)
    assert result.loc[:9, "x__peer_score"].eq(50).all()
    assert pd.isna(result.loc[10, "x__peer_score"])


def test_negative_peg_is_unavailable_not_cheap():
    frame = pd.DataFrame({"Sector": ["S"]*6, "Industry": ["I"]*6, "PEG Ratio": [-10, 1, 2, 3, 4, 5]})
    result = peer_enrich(frame, "PEG Ratio")
    assert pd.isna(result.loc[0, "PEG Ratio__peer_score"])
    assert result["PEG Ratio__sector_valid_count"].eq(5).all()


def test_unknown_dividend_does_not_create_neutral_evidence():
    frame = pd.DataFrame({"Sector": ["Industrials"]*3, "Dividend ": [np.nan, 0, 1],
        "12 Mo Trailing EPS": [np.nan, np.nan, -1], "dividend_payout_ratio": [np.nan]*3})
    result = apply_liquidity_policy(frame, (), -1, 0, 2, 3)
    assert pd.isna(result.loc[0, "payout_sustainability__directional_score"])
    assert result.loc[1, "payout_sustainability__directional_score"] == 50
    assert result.loc[2, "payout_sustainability__directional_score"] == 0


def test_finance_sector_does_not_bypass_equity_reit_gate():
    frame = pd.DataFrame({"Ticker": ["REIT", "BANK", "UNKNOWN", "MREIT"], "Sector": ["Finance"]*4,
        "Industry": ["REIT and Equity Trust - Retail", "Banks - West", "Financial - Miscellaneous Services", "Mortgage REIT"],
        "long_term_debt_ebitda": [11., 11., 1., 1.], "debt_equity_ratio": [1.]*4,
        "debt_total_capital_ratio": [.5]*4, "Current Ratio": [1.]*4})
    result = balance(frame)
    assert result["balance_sheet_gate_pass"].tolist() == [False, True, False, False]
    assert result.loc[0, "balance_sheet_gate_type"] == "EQUITY_REIT"
    assert "DEBT_EBITDA" in result.loc[0, "balance_sheet_gate_fail_reasons"]
    assert "SPECIALIST" in result.loc[3, "balance_sheet_gate_fail_reasons"]


def test_classification_override_is_explicit_and_validated():
    frame = pd.DataFrame({"Ticker": ["MINER"], "Sector": ["Finance"], "Industry": ["Financial - Miscellaneous Services"]})
    assert not classify_business(frame).loc[0, "economic_classification_verified"]
    out = classify_business(frame, {"MINER": "STANDARD_OPERATING_COMPANY"})
    assert out.loc[0, "ordinary_liquidity_applicable"]
    assert out.loc[0, "economic_classification_note"] == "CONTROL_PANEL_OVERRIDE"
    with pytest.raises(ValueError):
        config.apply_control_panel([["", "BUSINESS_TYPE_OVERRIDES_JSON", '{"MINER":"INVENTED"}']])
    assert config.BUSINESS_TYPE_OVERRIDES == {}


def test_generic_reit_label_is_not_assumed_to_be_equity_or_mortgage():
    out = classify_business(pd.DataFrame({"Industry": ["REIT and Equity Trust"], "Sector": ["Finance"]}))
    assert not out.loc[0, "economic_classification_verified"]


def test_custom_financial_sector_requires_review_without_bypassing_specific_industry():
    frame = pd.DataFrame({"Sector": ["Custom Financials"]*2,
                          "Industry": ["Miscellaneous", "REIT - Retail"]})
    out = balance_sheet_gates(frame, financial_sectors=("Custom Financials",), max_de=2,
        max_debt_capital=.75, min_current=.75, max_reit_debt_ebitda=10, max_reit_debt_ebit=12)
    assert out["economic_business_type"].tolist() == ["UNCLASSIFIED_FINANCIAL", "EQUITY_REIT"]
    assert not out["ordinary_liquidity_applicable"].any()
    assert not out["balance_sheet_gate_pass"].any()


def strategy_frame():
    frame = pd.DataFrame({"Ticker": ["LOSS", "PROFIT"], "Industry": ["I"]*2,
        "Source Row": [2, 3], "Data Status": ["SUFFICIENT"]*2,
        "F1 Consensus Est.": [-1., 1.], "F2 Consensus Est.": [-2., 2.], "balance_sheet_gate_pass": [True]*2})
    for family in config.FAMILY_WEIGHTS:
        frame[f"{family} Long Sub-score"] = 80.
    frame["PROFITABILITY_AND_RETURNS Long Sub-score"] = 40.
    return pd.concat([frame, eps_audit(frame)], axis=1)


def test_positive_eps_switches_reject_losses_and_remain_optional():
    frame = strategy_frame()
    positive = score_strategies(frame, config.STRATEGY_WEIGHTS, .75, 5, 60, 50, True, True, True)
    permissive = score_strategies(frame, config.STRATEGY_WEIGHTS, .75, 5, 60, 50, True, False, False)
    for name in ("High Growth Potential", "Turnaround Story"):
        assert positive[f"{name} Eligible"].tolist() == [False, True]
        assert permissive[f"{name} Eligible"].tolist() == [True, True]


def candidates():
    return pd.DataFrame({"Ticker": ["BLOCKED", "FIRST", "SECOND"], "Industry": ["I"]*3,
        "Source Row": [2, 3, 4], "Data Status": ["SUFFICIENT"]*3, "Score Confidence": [95.]*3,
        "Expanded Long Score": [90., 80., 70.], "Expanded Short Score": [10., 20., 30.],
        "tradability_gate_pass": [False, True, True], "short_tradability_gate_pass": [False, True, True],
        "balance_sheet_gate_pass": [True]*3})


def test_gates_precede_industry_and_global_caps():
    out = rank_quant_candidates(candidates(), 1, 1, 60, 60, 1, 1, 85)
    assert out.loc[out["Quantitative Candidate"].eq("QUANT LONG"), "Ticker"].tolist() == ["FIRST"]
    assert out.loc[0, "Expanded Long Rank"] == 1  # research evidence is retained


def test_book_gate_is_applied_when_requested_and_not_when_disabled():
    frame = candidates().assign(tradability_gate_pass=True, book_price_divergence=[.5, 0., np.nan])
    out = rank_quant_candidates(frame, 1, 1, 60, 60, 1, 1, 85, max_book_divergence=0)
    assert out.loc[out["Quantitative Candidate"].eq("QUANT LONG"), "Ticker"].tolist() == ["FIRST"]


def test_short_selection_requires_tradability_but_does_not_claim_borrow():
    frame = candidates()
    frame["Expanded Long Score"], frame["Expanded Short Score"] = frame["Expanded Short Score"].copy(), frame["Expanded Long Score"].copy()
    frame["short_implementation_validation_required"] = True
    frame["Short Thesis Gate Pass"] = True
    frame["Short Business Type Gate Pass"] = True
    out = rank_quant_candidates(frame, 1, 1, 60, 60, 1, 1, 85)
    assert out.loc[out["Quantitative Candidate"].eq("QUANT SHORT"), "Ticker"].tolist() == ["FIRST"]
    assert out["Implementation Candidate"].eq("").all()


def test_diagnostic_accounting_ratios_cannot_enter_active_registry():
    frame = pd.DataFrame({name: [10.] for name in DIAGNOSTIC_ONLY_SIGNALS})
    frame["Net Margin %"] = 20.
    registry = active_signal_registry(frame)
    assert not set(DIAGNOSTIC_ONLY_SIGNALS) & registry.keys()
    assert "Net Margin %" in registry


def test_disabled_family_cannot_change_confidence_or_score():
    frame = pd.DataFrame({"PEG Ratio": [2.], "PEG Ratio__peer_score": [20.],
        "Market Cap (mil)": [1.], "Market Cap (mil)__peer_score": [np.nan]})
    before = score_expanded(frame, {"VALUATION": 1., "TRADABILITY": 0.}, 0, 0)
    frame["Market Cap (mil)__peer_score"] = 100.
    after = score_expanded(frame, {"VALUATION": 1., "TRADABILITY": 0.}, 0, 0)
    assert before["Score Confidence"].equals(after["Score Confidence"])
    assert before["Expanded Long Score"].equals(after["Expanded Long Score"])


def test_publication_verifier_detects_score_corruption_despite_correct_title():
    expected = [["TITLE"], ["Ticker", "Score"], ["ABC", 70.]]
    verify_cell_values("Test", expected, [["TITLE", ""], ["Ticker", "Score"], ["ABC", 70.00000000001]])
    with pytest.raises(RuntimeError, match="B3"):
        verify_cell_values("Test", expected, [["TITLE"], ["Ticker", "Score"], ["ABC", 71.]])
    with pytest.raises(RuntimeError):
        verify_cell_values("Test", [[False]], [[0]])


def test_strategy_summary_filters_before_cap_and_exposes_research_rank():
    frame = pd.DataFrame({"Ticker": ["BAD", "GOOD"], "Company Name": ["Bad", "Good"],
        "Market Cap (mil)": [1e4]*2, "Industry": ["I"]*2, "Sector": ["S"]*2,
        "Exchange": ["NYSE"]*2, "COM/ADR/Canadian": ["COM"]*2, "Data Status": ["SUFFICIENT"]*2,
        "Safe Score": [90., 80.], "Safe Universe Rank": [1, 2], "Safe Within-Industry Rank": [1, 2],
        "Safe Implementation Eligible": [False, True], "Safe Weight Coverage": [1.]*2,
        "Safe Components Available": [8]*2, "Safe Components Missing": [0]*2})
    out = build_strategy_summary(frame, "Safe", {}, 1, 60)
    assert out.Ticker.tolist() == ["GOOD"]
    assert out["Universe Rank"].tolist() == [1]
    assert out["Research Universe Rank"].tolist() == [2]


def test_v4_migration_preserves_custom_controls_and_aligns_short_views():
    config.apply_control_panel([["", "MODEL_VERSION", "institutional_three_strategy_v4"],
        ["", "WEIGHT_VALUATION", 19], ["", "MIN_LONG_SCORE", 64], ["", "MIN_SHORT_SCORE", 90],
        ["", "SHORT_DISPLAY_TOP_N", 0]])
    assert config.FAMILY_WEIGHTS["VALUATION"] == 19
    assert config.FAMILY_WEIGHTS["TRADABILITY"] == config.FAMILY_WEIGHTS["RISK_AND_STABILITY"] == 0
    assert config.MIN_LONG_SCORE == config.MIN_SHORT_SCORE == 64
    assert config.MAX_QUANT_SHORTS_TOTAL == 0
