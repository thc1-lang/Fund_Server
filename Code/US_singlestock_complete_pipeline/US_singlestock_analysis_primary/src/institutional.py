"""Institutional availability, audit, gate and coverage controls.

Unavailable metrics are metadata, never numeric substitutes.  These controls are
kept outside peer scoring so missing underwriting fields cannot dilute quant ranks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import numpy as np
import pandas as pd
from .economic_policy import classify_business


@dataclass(frozen=True)
class MetricAvailability:
    metric_name: str
    availability_status: str
    unavailable_reason: str
    required_source_fields: tuple[str, ...]
    optional_source_fields: tuple[str, ...] = ()
    future_ready_calculation_hook: bool = True
    calculation_function_name: str = ""
    activation_condition: str = "all required source fields are present and valid"
    affects_quant_score: bool = False
    affects_underwriting_readiness: bool = True


METRIC_AVAILABILITY_REGISTRY: dict[str, MetricAvailability] = {
    "enterprise_value": MetricAvailability("enterprise_value", "UNAVAILABLE_SOURCE_DATA", "No complete enterprise-value field or genuine cash-and-equivalents balance.", ("Market Cap (mil)", "total_debt", "cash_and_equivalents"), ("preferred_equity", "minority_interest"), calculation_function_name="calculate_enterprise_value"),
    "ev_ebit": MetricAvailability("ev_ebit", "UNAVAILABLE_SOURCE_DATA", "Enterprise value unavailable.", ("enterprise_value", "EBIT ($mil)"), calculation_function_name="calculate_ev_ebit"),
    "ev_ebitda": MetricAvailability("ev_ebitda", "UNAVAILABLE_SOURCE_DATA", "Enterprise value unavailable.", ("enterprise_value", "EBITDA ($mil)"), calculation_function_name="calculate_ev_ebitda"),
    "net_debt": MetricAvailability("net_debt", "UNAVAILABLE_SOURCE_DATA", "No genuine cash-and-equivalents balance.", ("total_debt", "cash_and_equivalents"), calculation_function_name="calculate_net_debt"),
    "free_cash_flow": MetricAvailability("free_cash_flow", "UNAVAILABLE_SOURCE_DATA", "Operating cash flow is undefined and capital expenditure is absent.", ("operating_cash_flow", "capital_expenditure"), calculation_function_name="calculate_free_cash_flow"),
    "free_cash_flow_yield": MetricAvailability("free_cash_flow_yield", "UNAVAILABLE_SOURCE_DATA", "Free cash flow unavailable.", ("free_cash_flow", "Market Cap (mil)"), calculation_function_name="calculate_free_cash_flow_yield"),
    "interest_coverage": MetricAvailability("interest_coverage", "UNAVAILABLE_SOURCE_DATA", "No interest-expense field.", ("EBIT ($mil)", "interest_expense"), calculation_function_name="calculate_interest_coverage"),
    "roic_nopat": MetricAvailability("roic_nopat", "UNAVAILABLE_SOURCE_DATA", "Insufficient tax and invested-capital detail.", ("EBIT ($mil)", "effective_tax_rate", "invested_capital"), calculation_function_name="calculate_roic_nopat"),
    "altman_z": MetricAvailability("altman_z", "UNAVAILABLE_SOURCE_DATA", "Missing total assets, retained earnings and model-variant inputs.", ("total_assets", "retained_earnings", "working_capital", "EBIT ($mil)", "Market Cap (mil)", "total_liabilities", "Annual Sales ($mil)"), calculation_function_name="calculate_altman_z"),
    "piotroski_f": MetricAvailability("piotroski_f", "UNAVAILABLE_SOURCE_DATA", "Insufficient multi-period financial-statement inputs.", ("multi_period_financial_statements",), calculation_function_name="calculate_piotroski_f"),
    "dupont": MetricAvailability("dupont", "UNAVAILABLE_SOURCE_DATA", "No total-assets field and insufficient components.", ("Net Income  ($mil)", "Annual Sales ($mil)", "total_assets", "Common Equity ($mil)"), calculation_function_name="calculate_dupont"),
    "accruals": MetricAvailability("accruals", "UNAVAILABLE_SOURCE_DATA", "No defined operating cash flow and average total assets.", ("Net Income  ($mil)", "operating_cash_flow", "average_total_assets"), calculation_function_name="calculate_accruals"),
    "time_series_indicators": MetricAvailability("time_series_indicators", "UNAVAILABLE_SOURCE_DATA", "No underlying historical price series.", ("historical_prices",), calculation_function_name="calculate_time_series_indicators"),
    "cet1_ratio": MetricAvailability("cet1_ratio", "FUTURE_FIELD_REQUIRED", "Specialist financial-sector capital field absent.", ("cet1_ratio",), calculation_function_name="use_vendor_cet1_ratio"),
    "financial_leverage_ratio": MetricAvailability("financial_leverage_ratio", "FUTURE_FIELD_REQUIRED", "Specialist financial-sector leverage field absent.", ("regulatory_leverage_ratio",), calculation_function_name="use_vendor_regulatory_leverage_ratio"),
    "npl_ratio": MetricAvailability("npl_ratio", "FUTURE_FIELD_REQUIRED", "Non-performing-loan field absent.", ("npl_ratio",), calculation_function_name="use_vendor_npl_ratio"),
    "deposit_stability": MetricAvailability("deposit_stability", "FUTURE_FIELD_REQUIRED", "Deposit composition/history absent.", ("deposit_history",), calculation_function_name="calculate_deposit_stability"),
    "net_interest_margin": MetricAvailability("net_interest_margin", "FUTURE_FIELD_REQUIRED", "Net-interest-margin field absent.", ("net_interest_margin",), calculation_function_name="use_vendor_net_interest_margin"),
    "borrow_available": MetricAvailability("borrow_available", "UNAVAILABLE_SOURCE_DATA", "Borrow availability is not in the source workbook.", ("borrow_available",), calculation_function_name="use_borrow_availability"),
    "borrow_cost": MetricAvailability("borrow_cost", "UNAVAILABLE_SOURCE_DATA", "Borrow cost is not in the source workbook.", ("borrow_cost",), calculation_function_name="use_borrow_cost"),
    "short_interest_percent_float": MetricAvailability("short_interest_percent_float", "UNAVAILABLE_SOURCE_DATA", "Short-interest data is absent.", ("short_interest_percent_float",), calculation_function_name="use_short_interest"),
    "days_to_cover": MetricAvailability("days_to_cover", "UNAVAILABLE_SOURCE_DATA", "Short-interest and compatible volume data are incomplete.", ("short_interest_shares", "average_daily_volume"), calculation_function_name="calculate_days_to_cover"),
    "short_squeeze_risk_score": MetricAvailability("short_squeeze_risk_score", "UNAVAILABLE_SOURCE_DATA", "Borrow and short-interest inputs are absent.", ("borrow_cost", "short_interest_percent_float", "days_to_cover"), calculation_function_name="calculate_short_squeeze_risk"),
}


def availability_registry_frame(source_columns: set[str] | None = None) -> pd.DataFrame:
    source_columns = source_columns or set()
    rows = []
    for spec in METRIC_AVAILABILITY_REGISTRY.values():
        present = tuple(field for field in spec.required_source_fields if field in source_columns)
        missing = tuple(field for field in spec.required_source_fields if field not in source_columns)
        row = asdict(spec)
        row.update({
            "required_source_fields": " | ".join(spec.required_source_fields),
            "optional_source_fields": " | ".join(spec.optional_source_fields),
            "current_source_fields_present": " | ".join(present),
            "missing_required_fields": " | ".join(missing),
            "currently_active": not missing and spec.availability_status in {"AVAILABLE", "DERIVED"},
        })
        rows.append(row)
    return pd.DataFrame(rows)


def margin_reconciliation(df: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    specs = {
        "gross_margin": (None, "gross_margin"),
        "ebitda_margin": (None, "ebitda_margin"),
        "ebit_margin": ("operating_margin_vendor_normalized", "ebit_margin"),
        "pretax_margin": (None, "pretax_margin"),
        "net_margin": ("net_margin_vendor_normalized", "net_income_margin"),
        "cash_flow_margin": (None, "cash_flow_margin"),
    }
    statuses = []
    for name, (vendor_col, derived_col) in specs.items():
        vendor_source = df[vendor_col] if vendor_col and vendor_col in df else pd.Series(np.nan, index=df.index)
        derived_source = df[derived_col] if derived_col in df else pd.Series(np.nan, index=df.index)
        vendor = pd.to_numeric(vendor_source, errors="coerce")
        derived = pd.to_numeric(derived_source, errors="coerce")
        testable = vendor.notna() & derived.notna()
        diff = (vendor - derived).abs().where(testable)
        rel = (diff / derived.abs()).where(testable & derived.ne(0))
        status = pd.Series("RECONCILIATION_NOT_TESTABLE", index=df.index, dtype="object")
        if vendor_col is None:
            status[:] = "VENDOR_MARGIN_UNAVAILABLE"
        else:
            status.loc[vendor.isna()] = "VENDOR_MARGIN_UNAVAILABLE"
            status.loc[vendor.notna() & derived.isna()] = "NUMERATOR_OR_SALES_UNAVAILABLE"
            status.loc[testable & diff.le(tolerance)] = "WITHIN_TOLERANCE"
            status.loc[testable & diff.gt(tolerance)] = "OUTSIDE_TOLERANCE_VENDOR_DEFINITION_OR_PERIOD_MISMATCH"
        out[f"{name}_reconciliation_vendor_value"] = vendor
        out[f"{name}_reconciliation_derived_value"] = derived
        out[f"{name}_absolute_difference"] = diff
        out[f"{name}_relative_difference"] = rel
        out[f"{name}_reconciliation_flag"] = status
        out[f"{name}_reconciliation_explanation"] = ("Different definitions: EBIT versus vendor operating margin; derived measure is diagnostic only" if name == "ebit_margin" else status)
        statuses.append(status)
    status_frame = pd.concat(statuses, axis=1)
    out["margin_reconciliation_status"] = np.select(
        [status_frame.eq("OUTSIDE_TOLERANCE_VENDOR_DEFINITION_OR_PERIOD_MISMATCH").any(axis=1),
         status_frame.eq("WITHIN_TOLERANCE").any(axis=1)],
        ["REVIEW_REQUIRED", "TESTED"], default="NOT_TESTABLE")
    return out


def eps_audit(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    f1 = pd.to_numeric(df.get("F1 Consensus Est."), errors="coerce")
    f2 = pd.to_numeric(df.get("F2 Consensus Est."), errors="coerce")
    negative = f1.le(0) | f2.le(0)
    conventional = f1.gt(0) & f2.gt(0)
    out["eps_base_negative_flag"] = f1.le(0)
    out["eps_growth_interpretation_flag"] = np.select(
        [f1.isna() | f2.isna(), f1.eq(0), negative],
        ["MISSING_FORWARD_EPS", "ZERO_BASE_NOT_MEANINGFUL", "NEGATIVE_BASE_CONVENTIONAL_GROWTH_INVALID"],
        default="CONVENTIONAL_GROWTH_VALID")
    out["eps_growth_valid_for_safe"] = conventional
    out["eps_growth_valid_for_high_growth"] = f1.notna() & f2.notna()
    out["eps_growth_valid_for_turnaround"] = f1.notna() & f2.notna()
    out["high_growth_eps_exemption"] = negative & f1.notna() & f2.notna()
    out["turnaround_eps_exemption"] = negative & f1.notna() & f2.notna()
    return out


def balance_sheet_gates(df: pd.DataFrame, *, financial_sectors: tuple[str, ...], max_de: float,
                        max_debt_capital: float, min_current: float, max_reit_debt_ebitda: float,
                        max_reit_debt_ebit: float, business_type_overrides: dict[str, str] | None = None) -> pd.DataFrame:
    out = classify_business(df, business_type_overrides, financial_sectors=financial_sectors)
    kind = out["economic_business_type"]
    financial = kind.eq("SPECIALIST_FINANCIAL")
    mortgage = kind.eq("MORTGAGE_FINANCE")
    reit = kind.isin(["EQUITY_REIT", "REAL_ESTATE"])
    def num(name):
        return pd.to_numeric(df.get(name, pd.Series(np.nan, index=df.index)), errors="coerce")
    de, dc = num("debt_equity_ratio"), num("debt_total_capital_ratio")
    current = num("current_ratio_normalized") if "current_ratio_normalized" in df else num("Current Ratio")
    debt_ebitda, debt_ebit = num("long_term_debt_ebitda"), num("long_term_debt_ebit")
    equity = num("Common Equity ($mil)")
    reasons: list[str] = []
    passes: list[bool] = []
    for i in df.index:
        fail = []
        if not out.loc[i, "economic_classification_verified"]:
            passes.append(False)
            reasons.append("BUSINESS_CLASSIFICATION_REQUIRES_REVIEW")
            continue
        if mortgage.loc[i]:
            passes.append(False)
            reasons.append("MORTGAGE_FINANCE_SPECIALIST_CAPITAL_DATA_UNAVAILABLE")
            continue
        if financial.loc[i]:
            passes.append(True)
            reasons.append("")
            continue
        if reit.loc[i]:
            tested = False
            if pd.notna(debt_ebitda.loc[i]):
                tested = True
                if debt_ebitda.loc[i] > max_reit_debt_ebitda: fail.append("DEBT_EBITDA_ABOVE_LIMIT")
            elif pd.notna(debt_ebit.loc[i]):
                tested = True
                if debt_ebit.loc[i] > max_reit_debt_ebit: fail.append("DEBT_EBIT_ABOVE_LIMIT")
            if not tested: fail.append("REIT_LEVERAGE_NOT_TESTABLE")
        else:
            if pd.notna(equity.loc[i]) and equity.loc[i] <= 0:
                fail.append("NONPOSITIVE_EQUITY_REQUIRES_REVIEW")
            leverage_tested = False
            if pd.notna(de.loc[i]):
                leverage_tested = True
                if de.loc[i] < 0 or de.loc[i] >= max_de: fail.append("DEBT_EQUITY_OUTSIDE_LIMIT")
            if pd.notna(dc.loc[i]):
                leverage_tested = True
                if dc.loc[i] < 0 or dc.loc[i] >= max_debt_capital: fail.append("DEBT_TOTAL_CAPITAL_OUTSIDE_LIMIT")
            if not leverage_tested: fail.append("LEVERAGE_NOT_TESTABLE")
            if pd.notna(current.loc[i]) and current.loc[i] < min_current: fail.append("CURRENT_RATIO_BELOW_SURVIVABILITY_FLOOR")
        passes.append(not fail)
        reasons.append(" | ".join(fail))
    base_pass = pd.Series(passes, index=df.index, dtype=bool)
    out["balance_sheet_gate_type"] = kind
    out["balance_sheet_gate_pass"] = base_pass
    out["balance_sheet_gate_fail_reasons"] = reasons
    out["financial_sector_leverage_exemption"] = financial | mortgage
    out["reit_sector_leverage_exemption"] = reit
    # Exceptions relax current profitability, never hard solvency failures.
    out["high_growth_balance_sheet_exemption"] = False
    out["balance_sheet_gate_status"] = np.where(financial, "SPECIALIST_METRICS_UNAVAILABLE_RESEARCH_EXEMPTION", np.where(base_pass, "PASS_AVAILABLE_CHECKS", "FAIL_OR_UNTESTABLE"))
    out["turnaround_balance_sheet_exemption"] = False
    return out


def tradability_gates(df: pd.DataFrame, *, min_market_cap: float, min_adv: float, min_short_adv: float,
                      allow_otc: bool, allow_adr: bool, allow_mlp: bool, allow_canadian: bool) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    cap = pd.to_numeric(df.get("Market Cap (mil)"), errors="coerce")
    adv = pd.to_numeric(df.get("average_daily_dollar_volume"), errors="coerce")
    exchange = df.get("Exchange", pd.Series("", index=df.index)).astype(str).str.strip().str.upper()
    security = df.get("COM/ADR/Canadian", pd.Series("", index=df.index)).astype(str).str.strip().str.upper()
    long_reasons, short_reasons = [], []
    for i in df.index:
        common = []
        if not np.isfinite(cap.loc[i]) or cap.loc[i] < min_market_cap: common.append("MARKET_CAP_BELOW_MINIMUM_OR_MISSING")
        if not allow_otc and ("OTC" in exchange.loc[i] or "OTC" in security.loc[i]): common.append("OTC_NOT_ALLOWED")
        if not allow_adr and security.loc[i] in {"ADR", "ADS", "ASR"}: common.append("ADR_NOT_ALLOWED")
        if not allow_mlp and security.loc[i] == "MLP": common.append("MLP_NOT_ALLOWED")
        if not allow_canadian and security.loc[i] in {"CDN", "CANADIAN"}: common.append("CANADIAN_NOT_ALLOWED")
        lr = list(common)
        sr = list(common)
        if not np.isfinite(adv.loc[i]) or adv.loc[i] < min_adv: lr.append("DOLLAR_VOLUME_BELOW_MINIMUM_OR_MISSING")
        if not np.isfinite(adv.loc[i]) or adv.loc[i] < min_short_adv: sr.append("SHORT_DOLLAR_VOLUME_BELOW_MINIMUM_OR_MISSING")
        long_reasons.append(" | ".join(lr)); short_reasons.append(" | ".join(sr))
    out["tradability_gate_fail_reasons"] = long_reasons
    out["tradability_gate_pass"] = pd.Series(long_reasons, index=df.index).eq("")
    out["short_tradability_gate_fail_reasons"] = short_reasons
    out["short_tradability_gate_pass"] = pd.Series(short_reasons, index=df.index).eq("")
    for metric in ("borrow_available", "borrow_cost", "short_interest_percent_float", "days_to_cover", "short_squeeze_risk_score"):
        out[metric] = np.nan
    out["borrow_data_available"] = False
    out["short_squeeze_data_available"] = False
    out["short_implementation_validation_required"] = True
    return out


def coverage_audit(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    raw = pd.to_numeric(df.get("Raw Signal Coverage"), errors="coerce") * 100
    strategy_cols = [f"{s} Weight Coverage" for s in ("Safe", "High Growth Potential", "Turnaround Story") if f"{s} Weight Coverage" in df]
    strategy = df[strategy_cols].mean(axis=1) * 100 if strategy_cols else pd.Series(np.nan, index=df.index)
    quant = pd.to_numeric(df.get("Independent Group Coverage"), errors="coerce") * 100
    # Dependency groups prevent EV and FCF chains from being triple-counted.
    groups = {
        "valuation": ["Market Cap (mil)", "P/E (F1)", "PEG Ratio"],
        "profitability": ["EBIT ($mil)", "EBITDA ($mil)", "Net Margin %", "Current ROA (TTM)", "Current ROE (TTM)"],
        "balance_sheet": ["Debt/Equity Ratio", "Debt/Total Capital", "Current Ratio", "Quick Ratio", "Book Value"],
        "cash_flow": ["Cash Flow ($mil)"],
        "implementation": ["Market Cap (mil)", "Avg Volume", "Exchange", "COM/ADR/Canadian"],
        "ev_dependency": ["cash_and_equivalents"],
        "fcf_dependency": ["operating_cash_flow", "capital_expenditure"],
        "interest_dependency": ["interest_expense"],
        "advanced_dependency": ["total_assets", "multi_period_financial_statements"],
        "time_series_dependency": ["historical_prices"],
    }
    group_scores = []
    for fields in groups.values():
        present = [field for field in fields if field in df]
        if not present:
            group_scores.append(pd.Series(0.0, index=df.index))
        else:
            group_scores.append(df[present].notna().mean(axis=1) * 100)
    underwriting = pd.concat(group_scores, axis=1).mean(axis=1)
    out["raw_factor_coverage_score"] = raw.clip(0, 100)
    out["strategy_scoring_coverage_score"] = strategy.clip(0, 100)
    out["quant_ranking_coverage_score"] = quant.clip(0, 100)
    out["institutional_underwriting_coverage_score"] = underwriting.clip(0, 100)
    out["valuation_coverage_score"] = df[[c for c in groups["valuation"] if c in df]].notna().mean(axis=1) * 100
    out["ev_valuation_coverage_score"] = 0.0
    out["cash_flow_coverage_score"] = df[[c for c in groups["cash_flow"] if c in df]].notna().mean(axis=1) * 100
    out["fcf_coverage_score"] = 0.0
    out["balance_sheet_coverage_score"] = df[[c for c in groups["balance_sheet"] if c in df]].notna().mean(axis=1) * 100
    out["interest_coverage_available"] = False
    out["advanced_quality_metrics_available"] = False
    out["time_series_metrics_available"] = False
    out["data_quality_score_0_100"] = (0.6 * out["quant_ranking_coverage_score"] + 0.4 * underwriting).clip(0, 100)
    out["implementation_readiness_status"] = np.where(df.get("tradability_gate_pass", False), "LONG_IMPLEMENTATION_GATE_PASS", "IMPLEMENTATION_BLOCKED")
    out["institutional_underwriting_readiness"] = pd.cut(underwriting, [-1, 60, 75, 90, 101], labels=["SCREENING_QUALITY_ONLY", "MATERIALLY_INCOMPLETE", "ANALYST_REVIEW_REQUIRED", "CLOSE_TO_MEMO_READINESS"], right=False).astype(str)
    out["data_quality_summary"] = "Quant ranking coverage is measured separately from institutional underwriting; unavailable EV, FCF, interest, advanced-quality and price-history metrics are not scored as zero."
    return out
