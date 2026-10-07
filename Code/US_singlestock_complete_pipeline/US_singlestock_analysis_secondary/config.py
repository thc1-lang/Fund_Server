from __future__ import annotations

SPREADSHEET_ID = "1vVb8EfJnsbPufiVH3ckznzu2TAOL3xW1WTdfceLALeA"
EXPECTED_WORKBOOK_TITLE = "Equities Secondary Quantitative Analysis - code interface"
PRIMARY_SHORT_SPREADSHEET_ID = "1T2jn-zW7TIDMM5FK0Ih_WEP5puv1nkRHaSKHB3TK_Ms"
PRIMARY_SHORT_WORKBOOK_TITLE = "Equities Primary Quantitative Analysis - code interface"
PRIMARY_SHORT_TAB = "Short"

CATEGORIES = {
    "Safe": {
        "data_sheet": "Safe Data",
        "primary_summary_sheet": "Safe Summary",
        "analysis_sheet": "Safe Analysis",
        "secondary_summary_sheet": "Safe Secondary Summary",
        "slug": "SAFE",
    },
    "High Growth Potential": {
        "data_sheet": "High Growth Potential Data",
        "primary_summary_sheet": "High Growth Potential Summary",
        "analysis_sheet": "High Growth Potential Analysis",
        "secondary_summary_sheet": "High Growth Potential Secondary Summary",
        "slug": "HIGH_GROWTH_POTENTIAL",
    },
    "Turnaround Story": {
        "data_sheet": "Turnaround Story Data",
        "primary_summary_sheet": "Turnaround Story Summary",
        "analysis_sheet": "Turnaround Story Analysis",
        "secondary_summary_sheet": "Turnaround Story Secondary Summary",
        "slug": "TURNAROUND_STORY",
    },
}

GENERAL_DEFAULTS = {
    "MODEL_VERSION": "secondary_trend_first_v22",
    "CURRENT_MAX_CLOSE_AGE_DAYS": 5,
    "CURRENT_MIN_DOLLAR_VOLUME_20D": 10000000.0,
    "CURRENT_MIN_PRICE": 5.0,
    "CURRENT_MAX_REPORT_AGE_DAYS": 180,
    "CURRENT_MAX_SHARE_AGE_DAYS": 180,
    "SHORT_TOP_N": 3,
    "SHORT_MIN_CROSS_SECTION_OBSERVATIONS": 8,
    "SHORT_MIN_HISTORY_YEARS": 4,
    "SHORT_MIN_TREND_OBSERVATIONS": 4,
    "SHORT_MIN_SCORE": 70.0,
    "SHORT_MIN_TREND_SCORE": 65.0,
    "SHORT_MIN_TREND_COVERAGE": 0.80,
    "SHORT_MIN_POINT_IN_TIME_COVERAGE": 0.80,
    "SHORT_MIN_WEIGHT_COVERAGE": 0.85,
    "SHORT_MIN_TREND_RELIABILITY": 0.75,
    "SHORT_MIN_DIRECTION_OBSERVATIONS": 4,
    "SHORT_MAX_POSITIVE_REVENUE_GROWTH_SHARE": 0.50,
    "SHORT_MIN_ADVERSE_TREND_BREADTH": 0.67,
    "SHORT_MIN_ADVERSE_RECENT_BREADTH": 0.67,
    "SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS": 2,
    "SHORT_WEIGHT_TREND": 45.0,
    "SHORT_WEIGHT_INFLECTION": 15.0,
    "SHORT_WEIGHT_OPERATING": 15.0,
    "SHORT_WEIGHT_CASH_FLOW": 5.0,
    "SHORT_WEIGHT_BALANCE_SHEET": 5.0,
    "SHORT_WEIGHT_POINT_IN_TIME": 15.0,
    **{f"SHORT_METRIC_WEIGHT_{metric.upper()}": 1.0 for metrics in {
        "operating": ("latest_roa", "latest_roe", "latest_net_margin", "latest_gross_margin", "latest_pre_tax_margin", "revenue_cagr", "latest_revenue_growth"),
        "cash_flow": ("latest_fcf_margin", "latest_fcf_yield", "positive_fcf_share"),
        "balance_sheet": ("latest_current_ratio", "latest_quick_ratio", "latest_working_capital_to_assets", "latest_debt_to_equity", "latest_interest_coverage", "latest_net_debt_to_ebitda"),
        "trend": ("revenue_four_year_trend", "net_margin_four_year_trend", "fcf_margin_four_year_trend", "roa_four_year_trend", "gross_margin_four_year_trend", "debt_to_equity_four_year_trend", "positive_revenue_growth_share"),
        "inflection": ("revenue_growth_acceleration", "net_margin_change", "fcf_margin_change", "roa_change", "ebit_margin_change", "debt_to_equity_change"),
        "point_in_time": ("primary_short_score", "primary_price_change_4w", "primary_price_change_12w", "primary_f1_estimate_change_4w", "primary_f2_estimate_change_4w"),
    }.values() for metric in metrics},
    "TOP_N_SAFE": 3,
    "TOP_N_HIGH_GROWTH_POTENTIAL": 3,
    "TOP_N_TURNAROUND_STORY": 3,
    "MIN_HISTORY_YEARS_SAFE": 4,
    "MIN_HISTORY_YEARS_HIGH_GROWTH_POTENTIAL": 4,
    "MIN_HISTORY_YEARS_TURNAROUND_STORY": 4,
    "MIN_SCORE_SAFE": 0.0,
    "MIN_SCORE_HIGH_GROWTH_POTENTIAL": 0.0,
    "MIN_SCORE_TURNAROUND_STORY": 0.0,
    "MIN_SELECTION_SCORE_SAFE": 50.0,
    "MIN_SELECTION_SCORE_HIGH_GROWTH_POTENTIAL": 50.0,
    "MIN_SELECTION_SCORE_TURNAROUND_STORY": 50.0,
    "MIN_SELECTION_WEIGHT_COVERAGE_SAFE": 0.75,
    "MIN_SELECTION_WEIGHT_COVERAGE_HIGH_GROWTH_POTENTIAL": 0.75,
    "MIN_SELECTION_WEIGHT_COVERAGE_TURNAROUND_STORY": 0.75,
    "MIN_EFFECTIVE_TREND_RELIABILITY_SAFE": 0.50,
    "MIN_EFFECTIVE_TREND_RELIABILITY_HIGH_GROWTH_POTENTIAL": 0.50,
    "MIN_EFFECTIVE_TREND_RELIABILITY_TURNAROUND_STORY": 0.50,
    "STRONG_FAMILY_SCORE": 50.0,
    "MIN_STRONG_FAMILIES_SAFE": 2,
    "MIN_STRONG_FAMILIES_HIGH_GROWTH_POTENTIAL": 2,
    "MIN_STRONG_FAMILIES_TURNAROUND_STORY": 2,
    "MIN_LEAVE_ONE_FAMILY_OUT_SCORE": 40.0,
    "MAX_FISCAL_YEAR_LAG": 1,
    "MAX_FINANCIAL_AGE_DAYS": 730,
    "MIN_RECENT_DIRECTION_OBSERVATIONS": 3,
    "MIN_RECENT_FAVOURABLE_BREADTH": 0.50,
    "MIN_FAVOURABLE_TREND_BREADTH": 0.50,
    "MIN_RECENT_METRIC_COVERAGE": 0.60,
    "MAX_FISCAL_YEAR_GAP": 1,
    "MIN_OVERALL_WEIGHT_COVERAGE": 0.50,
    "MIN_FAMILY_METRIC_COVERAGE": 0.50,
    "MIN_CROSS_SECTION_OBSERVATIONS": 10,
    "CROSS_SECTION_SHRINKAGE_STRENGTH": 10.0,
    "SECTOR_PEER_WEIGHT": 0.35,
    "MIN_SECTOR_PEER_OBSERVATIONS": 4,
    "SECTOR_PEER_SHRINKAGE_STRENGTH": 5.0,
    "MIN_TREND_OBSERVATIONS": 4,
    "MISSING_TREND_PENALTY_POINTS": 15.0,
    "NO_USABLE_TREND_EXTRA_PENALTY_POINTS": 20.0,
    "THREE_YEAR_TREND_RELIABILITY": 0.75,
    "TREND_FIT_RELIABILITY_FLOOR": 0.50,
    "PERCENTILE_WEIGHT": 0.70,
    "ROBUST_Z_CAP": 2.50,
    "REQUIRE_BLANK_ERROR_FIELD": True,
    "SAFE_REQUIRE_POSITIVE_NET_INCOME": True,
    "SAFE_REQUIRE_POSITIVE_FREE_CASH_FLOW": True,
    "SAFE_MIN_POSITIVE_HISTORY_SHARE": 0.75,
    "HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR": True,
    "HIGH_GROWTH_MIN_REVENUE_CAGR": 0.0,
    "HIGH_GROWTH_REQUIRE_POSITIVE_FREE_CASH_FLOW": True,
    "TURNAROUND_REQUIRE_INFLECTION_GATE": True,
    "TURNAROUND_MIN_INFLECTION_SCORE": 40.0,
    "TURNAROUND_MIN_FAVOURABLE_TREND_BREADTH": 0.50,
    "MIN_STRONG_EVIDENCE_DOMAINS_SAFE": 2,
    "MIN_STRONG_EVIDENCE_DOMAINS_HIGH_GROWTH_POTENTIAL": 2,
    "MIN_STRONG_EVIDENCE_DOMAINS_TURNAROUND_STORY": 2,
}

LEGACY_FAMILY_WEIGHTS = {
    "Safe": {
        "PROFITABILITY": 15.0,
        "CASH_FLOW": 15.0,
        "BALANCE_SHEET": 15.0,
        "STABILITY": 10.0,
        "VALUATION": 10.0,
        "GROWTH": 0.0,
        "INFLECTION": 0.0,
        "TREND": 35.0,
    },
    "High Growth Potential": {
        "PROFITABILITY": 10.0,
        "CASH_FLOW": 5.0,
        "BALANCE_SHEET": 5.0,
        "STABILITY": 5.0,
        "VALUATION": 10.0,
        "GROWTH": 25.0,
        "INFLECTION": 0.0,
        "TREND": 40.0,
    },
    "Turnaround Story": {
        "PROFITABILITY": 8.0,
        "CASH_FLOW": 7.0,
        "BALANCE_SHEET": 10.0,
        "STABILITY": 0.0,
        "VALUATION": 5.0,
        "GROWTH": 5.0,
        "INFLECTION": 30.0,
        "TREND": 35.0,
    },
}


# Secondary selection assigns 80% to trajectory and 20% to level safeguards.
# Weights are design choices, not empirically optimised return forecasts.
FAMILY_WEIGHTS = {
    "Safe": {
        "PROFITABILITY": 5.0, "CASH_FLOW": 5.0, "BALANCE_SHEET": 5.0,
        "STABILITY": 3.0, "VALUATION": 2.0, "GROWTH": 0.0,
        "INFLECTION": 20.0, "TREND": 60.0,
    },
    "High Growth Potential": {
        "PROFITABILITY": 5.0, "CASH_FLOW": 5.0, "BALANCE_SHEET": 5.0,
        "STABILITY": 0.0, "VALUATION": 5.0, "GROWTH": 5.0,
        "INFLECTION": 20.0, "TREND": 55.0,
    },
    "Turnaround Story": {
        "PROFITABILITY": 5.0, "CASH_FLOW": 5.0, "BALANCE_SHEET": 5.0,
        "STABILITY": 0.0, "VALUATION": 5.0, "GROWTH": 0.0,
        "INFLECTION": 30.0, "TREND": 50.0,
    },
}

# Families that share an economic driver count as one independent evidence
# domain for breadth gating. This prevents Growth and Trend, for example, from
# appearing to be two independent confirmations of the same revenue signal.
EVIDENCE_DOMAINS = {
    "EARNINGS_QUALITY": ("PROFITABILITY", "CASH_FLOW"),
    "FINANCIAL_RESILIENCE": ("BALANCE_SHEET", "STABILITY"),
    "VALUATION": ("VALUATION",),
    "GROWTH_TREND": ("GROWTH", "TREND", "INFLECTION"),
}

# The direction is an economic model choice and is displayed on the Control Panel.
# Metric and family weights are editable; zero disables an item.
FEATURE_SPECS = {
    "latest_roa": ("PROFITABILITY", "higher", "Latest Return on Assets Ratio"),
    "latest_roe": ("PROFITABILITY", "higher", "Latest Return on Equity Ratio"),
    "latest_net_margin": ("PROFITABILITY", "higher", "Latest Net Margin"),
    "latest_gross_margin": ("PROFITABILITY", "higher", "Latest Gross Margin"),
    "latest_pre_tax_margin": ("PROFITABILITY", "higher", "Latest Pre-Tax Profit Ratio"),
    "latest_sales_to_assets": ("PROFITABILITY", "higher", "Latest Sales to Assets Ratio"),
    "latest_fcf_margin": ("CASH_FLOW", "higher", "Latest Free Cash Flow / Revenue"),
    "latest_fcf_yield": ("CASH_FLOW", "higher", "Latest Free Cash Flow Yield"),
    "positive_fcf_share": ("CASH_FLOW", "higher", "Share of reported years with positive FCF"),
    "cash_conversion": ("CASH_FLOW", "higher", "Latest Operating Cash Flow / positive Net Income"),
    "latest_current_ratio": ("BALANCE_SHEET", "higher", "Latest Current Ratio"),
    "latest_quick_ratio": ("BALANCE_SHEET", "higher", "Latest Quick Ratio"),
    "latest_working_capital_to_assets": ("BALANCE_SHEET", "higher", "Latest Working Capital to Total Assets Ratio"),
    "latest_debt_to_equity": ("BALANCE_SHEET", "lower", "Latest non-negative Debt to Equity Ratio"),
    "latest_interest_coverage": ("BALANCE_SHEET", "higher", "Latest positive Interest Coverage Ratio"),
    "latest_net_debt_to_ebitda": ("BALANCE_SHEET", "lower", "(Debt - Cash) / positive EBITDA"),
    "positive_net_income_share": ("STABILITY", "higher", "Share of reported years with positive Net Income"),
    "revenue_growth_stability": ("STABILITY", "lower", "Standard deviation of annual revenue growth"),
    "latest_ev_ebitda": ("VALUATION", "lower", "Latest positive EV / EBITDA"),
    "latest_ev_ebit": ("VALUATION", "lower", "Latest positive EV / EBIT"),
    "latest_price_fcf": ("VALUATION", "lower", "Latest positive Price / FCF per share"),
    "revenue_cagr": ("GROWTH", "higher", "Revenue CAGR from first to latest positive observation"),
    "latest_revenue_growth": ("GROWTH", "higher", "Latest annual Revenue growth"),
    "revenue_growth_acceleration": ("INFLECTION", "higher", "Latest revenue growth minus prior growth"),
    "net_margin_change": ("INFLECTION", "higher", "Latest Net Margin minus prior Net Margin"),
    "fcf_margin_change": ("INFLECTION", "higher", "Latest FCF margin minus prior FCF margin"),
    "roa_change": ("INFLECTION", "higher", "Latest ROA minus prior ROA"),
    "ebit_margin_change": ("INFLECTION", "higher", "Latest EBIT margin minus prior EBIT margin"),
    "debt_to_equity_change": ("INFLECTION", "lower", "Latest non-negative Debt/Equity minus prior year"),
    "revenue_four_year_trend": ("TREND", "higher", "Log-linear annual Revenue trend over up to four fiscal years"),
    "net_margin_four_year_trend": ("TREND", "higher", "Annual Net Margin slope over up to four fiscal years"),
    "fcf_margin_four_year_trend": ("TREND", "higher", "Annual Free Cash Flow margin slope over up to four fiscal years"),
    "roa_four_year_trend": ("TREND", "higher", "Annual Return on Assets slope over up to four fiscal years"),
    "gross_margin_four_year_trend": ("TREND", "higher", "Annual Gross Margin slope over up to four fiscal years"),
    "debt_to_equity_four_year_trend": ("TREND", "lower", "Annual non-negative Debt to Equity slope over up to four fiscal years"),
    "positive_revenue_growth_share": ("TREND", "higher", "Positive annual Revenue changes / possible annual transitions; missing is nonconfirming"),
    "trend_direction_breadth": ("TREND", "higher", "Share of available trend measures moving in the economically favourable direction"),
}

METRIC_WEIGHTS = {
    category: {metric: (1.0 if FAMILY_WEIGHTS[category].get(family, 0) > 0 else 0.0)
               for metric, (family, _, _) in FEATURE_SPECS.items()}
    for category in CATEGORIES
}

# Avoid double counting close substitutes by splitting their default weight.
for category in CATEGORIES:
    for metric in ("latest_current_ratio", "latest_quick_ratio"):
        METRIC_WEIGHTS[category][metric] *= 0.5
    for metric in ("latest_ev_ebitda", "latest_ev_ebit", "latest_price_fcf"):
        METRIC_WEIGHTS[category][metric] *= 1 / 3
    # Closely related profitability/efficiency ratios receive deliberately
    # modest weights to broaden the evidence without dominating ROA/net margin.
    for metric in ("latest_gross_margin", "latest_pre_tax_margin", "latest_sales_to_assets"):
        METRIC_WEIGHTS[category][metric] *= 0.5
    METRIC_WEIGHTS[category]["latest_working_capital_to_assets"] *= 0.5
    # FCF yield and Price/FCF are exact monotonic inverses in the live data.
    # Keep the directly interpretable yield and disable the duplicate multiple.
    METRIC_WEIGHTS[category]["latest_price_fcf"] = 0.0

# Category-specific trend emphasis. These weights operate inside the TREND
# family; the family itself is the largest component of every v3 strategy.
TREND_METRIC_WEIGHTS = {
    "Safe": {
        "revenue_four_year_trend": 1.5,
        "net_margin_four_year_trend": 2.0,
        "fcf_margin_four_year_trend": 2.0,
        "roa_four_year_trend": 2.0,
        "gross_margin_four_year_trend": 1.0,
        "debt_to_equity_four_year_trend": 2.0,
        "positive_revenue_growth_share": 0.5,
        "trend_direction_breadth": 1.0,
    },
    "High Growth Potential": {
        "revenue_four_year_trend": 3.0,
        "net_margin_four_year_trend": 1.5,
        "fcf_margin_four_year_trend": 1.5,
        "roa_four_year_trend": 1.0,
        "gross_margin_four_year_trend": 1.0,
        "debt_to_equity_four_year_trend": 0.5,
        "positive_revenue_growth_share": 1.5,
        "trend_direction_breadth": 1.5,
    },
    "Turnaround Story": {
        "revenue_four_year_trend": 2.0,
        "net_margin_four_year_trend": 2.0,
        "fcf_margin_four_year_trend": 2.0,
        "roa_four_year_trend": 1.5,
        "gross_margin_four_year_trend": 1.0,
        "debt_to_equity_four_year_trend": 1.0,
        "positive_revenue_growth_share": 0.5,
        "trend_direction_breadth": 1.5,
    },
}
for category, weights in TREND_METRIC_WEIGHTS.items():
    METRIC_WEIGHTS[category].update(weights)

# Revenue CAGR and the log-linear revenue trend are nearly identical over four
# annual observations. Retain both lineages but halve the trend metric's legacy
# default weight where the separate GROWTH family is active.
METRIC_WEIGHTS["High Growth Potential"]["revenue_four_year_trend"] = 1.5
METRIC_WEIGHTS["Turnaround Story"]["revenue_four_year_trend"] = 1.0
