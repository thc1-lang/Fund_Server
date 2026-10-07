"""Model control panel. Environment variables override backtestable defaults."""
from __future__ import annotations

import os
import json
import math
from copy import deepcopy


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _tuple(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


def _parse_bool(raw: object, parameter: str) -> bool:
    value = str(raw).strip().casefold()
    if value in {"true", "1", "yes", "y"}:
        return True
    if value in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"{parameter} must be true/false, yes/no, or 1/0; got {raw!r}")


def _parse_weight_map(raw: object, parameter: str) -> dict[str, float]:
    parsed = json.loads(str(raw or "{}"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{parameter} must be a JSON object")
    return {str(key): float(value) for key, value in parsed.items()}

SOURCE_SPREADSHEET_ID = os.getenv("SOURCE_SPREADSHEET_ID", "")
SOURCE_TAB = os.getenv("SOURCE_TAB", "Dataset")
# The supplied workbook is both source and destination. `--live` is the
# explicit write authorization, so repetitive environment variables are unnecessary.
DESTINATION_SPREADSHEET_ID = os.getenv("DESTINATION_SPREADSHEET_ID", SOURCE_SPREADSHEET_ID)
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")

MODEL_VERSION = "institutional_three_strategy_v4"

# Selection and coverage controls.
TOP_N_LONG = _int("TOP_N_LONG", 3)
TOP_N_SHORT = _int("TOP_N_SHORT", 3)
MAX_QUANT_LONGS_TOTAL = _int("MAX_QUANT_LONGS_TOTAL", 10)
MAX_QUANT_SHORTS_TOTAL = _int("MAX_QUANT_SHORTS_TOTAL", 10)
MIN_CANDIDATE_CONFIDENCE = _float("MIN_CANDIDATE_CONFIDENCE", 85.0)
MIN_VALID_FACTOR_OBSERVATIONS = _int("MIN_VALID_FACTOR_OBSERVATIONS", 5)
MIN_FACTOR_COVERAGE = _float("MIN_FACTOR_COVERAGE", 0.70)
MIN_FAMILY_COVERAGE = _float("MIN_FAMILY_COVERAGE", 0.50)
MIN_LONG_SCORE = _float("MIN_LONG_SCORE", 55.0)
MIN_SHORT_SCORE = _float("MIN_SHORT_SCORE", 55.0)

# Peer model controls. There is no whole-US fallback and no winsorisation.
# Industry weight = n_industry / (n_industry + PEER_SHRINKAGE_STRENGTH).
PEER_SHRINKAGE_STRENGTH = _float("PEER_SHRINKAGE_STRENGTH", 8.0)
PEER_Z_SCORE_CAP = _float("PEER_Z_SCORE_CAP", 3.0)
PEER_PERCENTILE_WEIGHT = _float("PEER_PERCENTILE_WEIGHT", 0.50)

# Balance-sheet and liquidity controls.
MAX_STANDARD_DEBT_EQUITY_RATIO = _float("MAX_STANDARD_DEBT_EQUITY_RATIO", 2.0)
MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO = _float("MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO", 0.75)
MIN_STANDARD_CURRENT_RATIO = _float("MIN_STANDARD_CURRENT_RATIO", 0.75)
MAX_REIT_DEBT_EBITDA = _float("MAX_REIT_DEBT_EBITDA", 10.0)
MAX_REIT_DEBT_EBIT = _float("MAX_REIT_DEBT_EBIT", 12.0)
MAX_BOOK_PRICE_DIVERGENCE = None if os.getenv("MAX_BOOK_PRICE_DIVERGENCE") in (None, "", "none", "None") else _float("MAX_BOOK_PRICE_DIVERGENCE", 0.15)
LIQUIDITY_DISTRESS_Z_THRESHOLD = _float("LIQUIDITY_DISTRESS_Z_THRESHOLD", -1.0)
LIQUIDITY_DISTRESS_FLOOR_SCORE = _float("LIQUIDITY_DISTRESS_FLOOR_SCORE", 0.0)
EXCESS_WORKING_CAPITAL_Z_THRESHOLD = _float("EXCESS_WORKING_CAPITAL_Z_THRESHOLD", 2.0)
PAYOUT_WARNING_RATIO = _float("PAYOUT_WARNING_RATIO", 0.80)
PAYOUT_DISTRESS_RATIO = _float("PAYOUT_DISTRESS_RATIO", 1.50)
FINANCIAL_SECTORS = _tuple("FINANCIAL_SECTORS", "Finance,Financials,Financial Services")

# Score confidence and neutral shrinkage controls.
CONFIDENCE_FACTOR_WEIGHT = _float("CONFIDENCE_FACTOR_WEIGHT", 0.60)
CONFIDENCE_FAMILY_WEIGHT = _float("CONFIDENCE_FAMILY_WEIGHT", 0.40)

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ALLOW_CREATE_INDUSTRY_TABS = os.getenv("ALLOW_CREATE_INDUSTRY_TABS", "false").lower() == "true"
ALLOW_SAME_SOURCE_AND_DESTINATION = os.getenv("ALLOW_SAME_SOURCE_AND_DESTINATION", "false").lower() == "true"
FULL_AUDIT_TO_SHEETS = os.getenv("FULL_AUDIT_TO_SHEETS", "false").lower() == "true"

ALLOWED_SECURITY_TYPES = _tuple("ALLOWED_SECURITY_TYPES", "COM,ADR,CDN,MLP,ADS,ASR")
EXCLUDED_SECURITY_TYPES = ("ETF", "CEF")

DATA_SOURCE_LABEL = os.getenv("DATA_SOURCE_LABEL")
DATA_SOURCE_URL = os.getenv("DATA_SOURCE_URL")
DATA_AS_OF = os.getenv("DATA_AS_OF")

MARGIN_RECONCILIATION_ABS_TOLERANCE = _float("MARGIN_RECONCILIATION_ABS_TOLERANCE", 0.05)
PEG1_MAPPING_TENTATIVE = True
EG2_REQUIRE_POSITIVE_EPS_SAFE = os.getenv("EG2_REQUIRE_POSITIVE_EPS_SAFE", "true").lower() == "true"
EG2_REQUIRE_POSITIVE_EPS_HIGH_GROWTH = os.getenv("EG2_REQUIRE_POSITIVE_EPS_HIGH_GROWTH", "false").lower() == "true"
EG2_REQUIRE_POSITIVE_EPS_TURNAROUND = os.getenv("EG2_REQUIRE_POSITIVE_EPS_TURNAROUND", "false").lower() == "true"
PB_VALIDATION_TOLERANCE = _float("PB_VALIDATION_TOLERANCE", 0.15)
MARKET_CAP_PER_SHARE_TOLERANCE = _float("MARKET_CAP_PER_SHARE_TOLERANCE", 0.20)

# Implementation gates are separate from investment-attractiveness scores.
MIN_MARKET_CAP_MIL = _float("MIN_MARKET_CAP_MIL", 3000.0)
MIN_AVG_DAILY_DOLLAR_VOLUME = _float("MIN_AVG_DAILY_DOLLAR_VOLUME", 25_000_000.0)
MIN_AVG_DAILY_DOLLAR_VOLUME_SHORT = _float("MIN_AVG_DAILY_DOLLAR_VOLUME_SHORT", 50_000_000.0)
REQUIRE_OPTIONABLE_FOR_OPTIONS_STRATEGY = os.getenv("REQUIRE_OPTIONABLE_FOR_OPTIONS_STRATEGY", "true").lower() == "true"
ALLOW_OTC = os.getenv("ALLOW_OTC", "false").lower() == "true"
ALLOW_ADR = os.getenv("ALLOW_ADR", "true").lower() == "true"
ALLOW_MLP = os.getenv("ALLOW_MLP", "true").lower() == "true"
ALLOW_CANADIAN = os.getenv("ALLOW_CANADIAN", "true").lower() == "true"

FAMILY_WEIGHTS = {
    "VALUATION": 14.0,
    "GROWTH": 12.0,
    "PROFITABILITY_AND_RETURNS": 16.0,
    "BALANCE_SHEET_AND_LEVERAGE": 14.0,
    "LIQUIDITY_AND_EFFICIENCY": 8.0,
    "EARNINGS_QUALITY": 6.0,
    "ESTIMATES_AND_REVISIONS": 10.0,
    "SHAREHOLDER_AND_YIELD": 4.0,
    "MARKET_BEHAVIOUR": 6.0,
    "BROKER_AND_TARGET": 3.0,
    "TRADABILITY": 2.0,
    "RISK_AND_STABILITY": 5.0,
}
if os.getenv("FAMILY_WEIGHTS_JSON"):
    FAMILY_WEIGHTS.update({k: float(v) for k, v in json.loads(os.environ["FAMILY_WEIGHTS_JSON"]).items()})
SIGNAL_WEIGHTS = {k: float(v) for k, v in json.loads(os.getenv("SIGNAL_WEIGHTS_JSON", "{}")).items()}
DUPLICATE_GROUP_WEIGHTS = {k: float(v) for k, v in json.loads(os.getenv("DUPLICATE_GROUP_WEIGHTS_JSON", "{}")).items()}

# Strategy scores reuse the existing economically-directed family sub-scores.  Zero
# weights are allowed; negative weights are deliberately prohibited because they
# would silently invert a family's economic meaning.
STRATEGY_WEIGHTS = {
    "Safe": {
        "VALUATION": 14.0,
        "GROWTH": 4.0,
        "PROFITABILITY_AND_RETURNS": 18.0,
        "BALANCE_SHEET_AND_LEVERAGE": 20.0,
        "LIQUIDITY_AND_EFFICIENCY": 10.0,
        "EARNINGS_QUALITY": 8.0,
        "ESTIMATES_AND_REVISIONS": 4.0,
        "SHAREHOLDER_AND_YIELD": 5.0,
        "MARKET_BEHAVIOUR": 0.0,
        "BROKER_AND_TARGET": 0.0,
        "TRADABILITY": 5.0,
        "RISK_AND_STABILITY": 12.0,
    },
    "High Growth Potential": {
        "VALUATION": 15.0,
        "GROWTH": 25.0,
        "PROFITABILITY_AND_RETURNS": 15.0,
        "BALANCE_SHEET_AND_LEVERAGE": 7.0,
        "LIQUIDITY_AND_EFFICIENCY": 5.0,
        "EARNINGS_QUALITY": 7.0,
        "ESTIMATES_AND_REVISIONS": 12.0,
        "SHAREHOLDER_AND_YIELD": 0.0,
        "MARKET_BEHAVIOUR": 7.0,
        "BROKER_AND_TARGET": 3.0,
        "TRADABILITY": 2.0,
        "RISK_AND_STABILITY": 2.0,
    },
    "Turnaround Story": {
        "VALUATION": 12.0,
        "GROWTH": 22.0,
        "PROFITABILITY_AND_RETURNS": 10.0,
        "BALANCE_SHEET_AND_LEVERAGE": 7.0,
        "LIQUIDITY_AND_EFFICIENCY": 5.0,
        "EARNINGS_QUALITY": 7.0,
        "ESTIMATES_AND_REVISIONS": 22.0,
        "SHAREHOLDER_AND_YIELD": 0.0,
        "MARKET_BEHAVIOUR": 10.0,
        "BROKER_AND_TARGET": 3.0,
        "TRADABILITY": 0.0,
        "RISK_AND_STABILITY": 2.0,
    },
}
if os.getenv("STRATEGY_WEIGHTS_JSON"):
    supplied = json.loads(os.environ["STRATEGY_WEIGHTS_JSON"])
    for strategy, weights in supplied.items():
        if strategy in STRATEGY_WEIGHTS and isinstance(weights, dict):
            STRATEGY_WEIGHTS[strategy].update({str(k): float(v) for k, v in weights.items()})

MIN_STRATEGY_WEIGHT_COVERAGE = _float("MIN_STRATEGY_WEIGHT_COVERAGE", 0.65)
MIN_STRATEGY_COMPONENTS = _int("MIN_STRATEGY_COMPONENTS", 4)
MIN_STRATEGY_SCORE = _float("MIN_STRATEGY_SCORE", 55.0)
STRATEGY_SUMMARY_TOP_N = _int("STRATEGY_SUMMARY_TOP_N", 50)
TURNAROUND_MIN_INFLECTION_SCORE = _float("TURNAROUND_MIN_INFLECTION_SCORE", 55.0)
TURNAROUND_MIN_INFLECTION_COMPONENTS = _int("TURNAROUND_MIN_INFLECTION_COMPONENTS", 2)

# Exported with every run so the full backtestable state is auditable.
_CONTROL_NAMES = (
        "TOP_N_LONG", "TOP_N_SHORT", "MAX_QUANT_LONGS_TOTAL",
        "MAX_QUANT_SHORTS_TOTAL", "MIN_CANDIDATE_CONFIDENCE", "MIN_VALID_FACTOR_OBSERVATIONS",
        "MIN_FACTOR_COVERAGE", "MIN_FAMILY_COVERAGE", "MIN_LONG_SCORE",
        "MIN_SHORT_SCORE", "PEER_SHRINKAGE_STRENGTH", "PEER_Z_SCORE_CAP",
        "PEER_PERCENTILE_WEIGHT", "MAX_STANDARD_DEBT_EQUITY_RATIO",
        "MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO", "MIN_STANDARD_CURRENT_RATIO",
        "MAX_REIT_DEBT_EBITDA", "MAX_REIT_DEBT_EBIT",
        "MAX_BOOK_PRICE_DIVERGENCE", "LIQUIDITY_DISTRESS_Z_THRESHOLD",
        "LIQUIDITY_DISTRESS_FLOOR_SCORE", "EXCESS_WORKING_CAPITAL_Z_THRESHOLD",
        "PAYOUT_WARNING_RATIO", "PAYOUT_DISTRESS_RATIO",
        "FINANCIAL_SECTORS", "CONFIDENCE_FACTOR_WEIGHT",
        "CONFIDENCE_FAMILY_WEIGHT",
        "PB_VALIDATION_TOLERANCE", "MARKET_CAP_PER_SHARE_TOLERANCE",
        "MARGIN_RECONCILIATION_ABS_TOLERANCE",
        "EG2_REQUIRE_POSITIVE_EPS_SAFE", "EG2_REQUIRE_POSITIVE_EPS_HIGH_GROWTH",
        "EG2_REQUIRE_POSITIVE_EPS_TURNAROUND", "ALLOWED_SECURITY_TYPES", "FAMILY_WEIGHTS",
        "MIN_MARKET_CAP_MIL", "MIN_AVG_DAILY_DOLLAR_VOLUME", "MIN_AVG_DAILY_DOLLAR_VOLUME_SHORT",
        "REQUIRE_OPTIONABLE_FOR_OPTIONS_STRATEGY", "ALLOW_OTC", "ALLOW_ADR", "ALLOW_MLP", "ALLOW_CANADIAN",
        "SIGNAL_WEIGHTS", "DUPLICATE_GROUP_WEIGHTS",
        "STRATEGY_WEIGHTS", "MIN_STRATEGY_WEIGHT_COVERAGE",
        "MIN_STRATEGY_COMPONENTS", "MIN_STRATEGY_SCORE", "STRATEGY_SUMMARY_TOP_N",
        "TURNAROUND_MIN_INFLECTION_SCORE", "TURNAROUND_MIN_INFLECTION_COMPONENTS",
)


def _model_controls() -> dict:
    return {name: globals()[name] for name in _CONTROL_NAMES}


def validate_model_controls() -> None:
    """Fail early when environment or Control Panel values are unsafe or incoherent."""
    errors: list[str] = []

    def between(name: str, lower: float, upper: float) -> None:
        value = float(globals()[name])
        if not math.isfinite(value) or not lower <= value <= upper:
            errors.append(f"{name} must be between {lower} and {upper}; got {value}")

    numeric_controls = (
        TOP_N_LONG, TOP_N_SHORT, MAX_QUANT_LONGS_TOTAL, MAX_QUANT_SHORTS_TOTAL,
        MIN_CANDIDATE_CONFIDENCE, MIN_VALID_FACTOR_OBSERVATIONS,
        MIN_FACTOR_COVERAGE, MIN_FAMILY_COVERAGE, MIN_LONG_SCORE, MIN_SHORT_SCORE,
        PEER_SHRINKAGE_STRENGTH, PEER_Z_SCORE_CAP, PEER_PERCENTILE_WEIGHT,
        MAX_STANDARD_DEBT_EQUITY_RATIO, MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO,
        MIN_STANDARD_CURRENT_RATIO, MAX_REIT_DEBT_EBITDA, MAX_REIT_DEBT_EBIT,
        LIQUIDITY_DISTRESS_Z_THRESHOLD,
        LIQUIDITY_DISTRESS_FLOOR_SCORE, EXCESS_WORKING_CAPITAL_Z_THRESHOLD,
        PAYOUT_WARNING_RATIO, PAYOUT_DISTRESS_RATIO,
        CONFIDENCE_FACTOR_WEIGHT, CONFIDENCE_FAMILY_WEIGHT,
        PB_VALIDATION_TOLERANCE, MARKET_CAP_PER_SHARE_TOLERANCE,
        MIN_STRATEGY_WEIGHT_COVERAGE, MIN_STRATEGY_COMPONENTS, MIN_STRATEGY_SCORE,
        STRATEGY_SUMMARY_TOP_N, TURNAROUND_MIN_INFLECTION_SCORE,
        TURNAROUND_MIN_INFLECTION_COMPONENTS, MARGIN_RECONCILIATION_ABS_TOLERANCE,
        MIN_MARKET_CAP_MIL, MIN_AVG_DAILY_DOLLAR_VOLUME, MIN_AVG_DAILY_DOLLAR_VOLUME_SHORT,
    )
    if any(not math.isfinite(float(value)) for value in numeric_controls):
        errors.append("Numeric model controls must all be finite")

    if TOP_N_LONG < 1 or TOP_N_SHORT < 1:
        errors.append("TOP_N_LONG and TOP_N_SHORT must both be at least 1")
    if MAX_QUANT_LONGS_TOTAL < 1 or MAX_QUANT_SHORTS_TOTAL < 1:
        errors.append("MAX_QUANT_LONGS_TOTAL and MAX_QUANT_SHORTS_TOTAL must both be at least 1")
    if MIN_VALID_FACTOR_OBSERVATIONS < 2:
        errors.append("MIN_VALID_FACTOR_OBSERVATIONS must be at least 2")
    for name in ("MIN_FACTOR_COVERAGE", "MIN_FAMILY_COVERAGE"):
        between(name, 0, 1)
    between("MIN_STRATEGY_WEIGHT_COVERAGE", 0, 1)
    for name in ("MIN_LONG_SCORE", "MIN_SHORT_SCORE", "MIN_CANDIDATE_CONFIDENCE"):
        between(name, 0, 100)
    for name in ("MIN_STRATEGY_SCORE", "TURNAROUND_MIN_INFLECTION_SCORE"):
        between(name, 0, 100)
    if MIN_STRATEGY_COMPONENTS < 1:
        errors.append("MIN_STRATEGY_COMPONENTS must be at least 1")
    if TURNAROUND_MIN_INFLECTION_COMPONENTS < 1:
        errors.append("TURNAROUND_MIN_INFLECTION_COMPONENTS must be at least 1")
    if STRATEGY_SUMMARY_TOP_N < 1:
        errors.append("STRATEGY_SUMMARY_TOP_N must be at least 1")
    if PEER_SHRINKAGE_STRENGTH < 0:
        errors.append("PEER_SHRINKAGE_STRENGTH cannot be negative")
    if PEER_Z_SCORE_CAP <= 0:
        errors.append("PEER_Z_SCORE_CAP must be positive")
    between("PEER_PERCENTILE_WEIGHT", 0, 1)
    for name in ("MAX_STANDARD_DEBT_EQUITY_RATIO", "MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO",
                 "MIN_STANDARD_CURRENT_RATIO", "MAX_REIT_DEBT_EBITDA", "MAX_REIT_DEBT_EBIT"):
        if globals()[name] <= 0:
            errors.append(f"{name} must be positive")
    if MAX_BOOK_PRICE_DIVERGENCE is not None and (
        not math.isfinite(MAX_BOOK_PRICE_DIVERGENCE) or MAX_BOOK_PRICE_DIVERGENCE < 0
    ):
        errors.append("MAX_BOOK_PRICE_DIVERGENCE must be finite and non-negative")
    if not -PEER_Z_SCORE_CAP <= LIQUIDITY_DISTRESS_Z_THRESHOLD <= 0:
        errors.append("LIQUIDITY_DISTRESS_Z_THRESHOLD must be between -PEER_Z_SCORE_CAP and 0")
    between("LIQUIDITY_DISTRESS_FLOOR_SCORE", 0, 50)
    if EXCESS_WORKING_CAPITAL_Z_THRESHOLD < 0:
        errors.append("EXCESS_WORKING_CAPITAL_Z_THRESHOLD cannot be negative")
    if PAYOUT_WARNING_RATIO < 0 or PAYOUT_DISTRESS_RATIO <= PAYOUT_WARNING_RATIO:
        errors.append("PAYOUT_DISTRESS_RATIO must be greater than non-negative PAYOUT_WARNING_RATIO")
    if CONFIDENCE_FACTOR_WEIGHT < 0 or CONFIDENCE_FAMILY_WEIGHT < 0:
        errors.append("Confidence weights cannot be negative")
    if CONFIDENCE_FACTOR_WEIGHT + CONFIDENCE_FAMILY_WEIGHT <= 0:
        errors.append("At least one confidence weight must be positive")
    if PB_VALIDATION_TOLERANCE < 0 or MARKET_CAP_PER_SHARE_TOLERANCE < 0 or MARGIN_RECONCILIATION_ABS_TOLERANCE < 0:
        errors.append("Validation tolerances cannot be negative")
    if not ALLOWED_SECURITY_TYPES:
        errors.append("ALLOWED_SECURITY_TYPES cannot be empty")
    for name, weights in (
        ("FAMILY_WEIGHTS", FAMILY_WEIGHTS),
        ("SIGNAL_WEIGHTS", SIGNAL_WEIGHTS),
        ("DUPLICATE_GROUP_WEIGHTS", DUPLICATE_GROUP_WEIGHTS),
    ):
        if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
            errors.append(f"{name} must contain only finite, non-negative weights")
    if not any(weight > 0 for weight in FAMILY_WEIGHTS.values()):
        errors.append("At least one FAMILY_WEIGHTS value must be positive")
    expected_families = set(FAMILY_WEIGHTS)
    for strategy, weights in STRATEGY_WEIGHTS.items():
        if set(weights) != expected_families:
            errors.append(f"{strategy} strategy weights must cover exactly the existing score families")
        if any(not math.isfinite(float(weight)) or float(weight) < 0 for weight in weights.values()):
            errors.append(f"{strategy} strategy weights must be finite and non-negative")
        if not any(float(weight) > 0 for weight in weights.values()):
            errors.append(f"{strategy} must have at least one positive weight")
    if errors:
        raise ValueError("Invalid model controls: " + "; ".join(errors))


validate_model_controls()
_BASE_CONTROLS = deepcopy(_model_controls())
MODEL_CONTROLS = deepcopy(_BASE_CONTROLS)


def _restore_base_controls() -> None:
    global FAMILY_WEIGHTS, SIGNAL_WEIGHTS, DUPLICATE_GROUP_WEIGHTS, STRATEGY_WEIGHTS
    for name, value in _BASE_CONTROLS.items():
        globals()[name] = deepcopy(value)
    FAMILY_WEIGHTS = deepcopy(_BASE_CONTROLS["FAMILY_WEIGHTS"])
    SIGNAL_WEIGHTS = deepcopy(_BASE_CONTROLS["SIGNAL_WEIGHTS"])
    DUPLICATE_GROUP_WEIGHTS = deepcopy(_BASE_CONTROLS["DUPLICATE_GROUP_WEIGHTS"])
    STRATEGY_WEIGHTS = deepcopy(_BASE_CONTROLS["STRATEGY_WEIGHTS"])

CONTROL_PANEL_SPECS = [
    ("Selection", "TOP_N_LONG", "Maximum selected longs per industry."),
    ("Selection", "TOP_N_SHORT", "Maximum selected shorts per industry."),
    ("Selection", "MAX_QUANT_LONGS_TOTAL", "Maximum quantitative long candidates across the universe."),
    ("Selection", "MAX_QUANT_SHORTS_TOTAL", "Maximum quantitative short candidates across the universe."),
    ("Selection", "MIN_CANDIDATE_CONFIDENCE", "Minimum 0-100 score confidence for a quantitative candidate."),
    ("Selection", "MIN_LONG_SCORE", "Minimum 0-100 peer-relative score for a long candidate."),
    ("Selection", "MIN_SHORT_SCORE", "Minimum 0-100 peer-relative score for a short candidate."),
    ("Peer model", "MIN_VALID_FACTOR_OBSERVATIONS", "Minimum valid sector observations; below this the factor is unscored—never compared with the whole US universe."),
    ("Peer model", "PEER_SHRINKAGE_STRENGTH", "Industry shrinkage k: industry weight = n/(n+k)."),
    ("Peer model", "PEER_Z_SCORE_CAP", "Absolute z-score mapped to the 0/100 score endpoints."),
    ("Peer model", "PEER_PERCENTILE_WEIGHT", "Weight on robust empirical percentile; the remainder uses capped robust z (classical z fallback when MAD is zero)."),
    ("Coverage", "MIN_FACTOR_COVERAGE", "Minimum scored-factor coverage required for a final score."),
    ("Coverage", "MIN_FAMILY_COVERAGE", "Minimum within-family group coverage required to admit that family."),
    ("Strategy coverage", "MIN_STRATEGY_WEIGHT_COVERAGE", "Minimum proportion of positive configured strategy weight that must have a valid family sub-score."),
    ("Strategy coverage", "MIN_STRATEGY_COMPONENTS", "Minimum number of positively weighted family components required for a strategy score."),
    ("Strategy selection", "MIN_STRATEGY_SCORE", "Minimum final 0-100 score shown in a strategy summary."),
    ("Strategy selection", "STRATEGY_SUMMARY_TOP_N", "Maximum stocks shown on each cross-universe strategy summary."),
    ("Turnaround gate", "TURNAROUND_MIN_INFLECTION_SCORE", "Minimum weighted 0-100 Growth/Estimates/Market inflection score required for Turnaround eligibility."),
    ("Turnaround gate", "TURNAROUND_MIN_INFLECTION_COMPONENTS", "Minimum valid inflection families required; Growth or Estimates must also score at least the inflection threshold."),
    ("Liquidity", "LIQUIDITY_DISTRESS_Z_THRESHOLD", "Low-tail peer z below which current/quick/cash ratios become distress evidence."),
    ("Liquidity", "LIQUIDITY_DISTRESS_FLOOR_SCORE", "Long score at the worst capped liquidity distress point."),
    ("Liquidity", "EXCESS_WORKING_CAPITAL_Z_THRESHOLD", "High-tail working-capital/sales z that raises the separate excess flag."),
    ("Payout risk", "PAYOUT_WARNING_RATIO", "Payout ratio above which sustainability score declines from neutral."),
    ("Payout risk", "PAYOUT_DISTRESS_RATIO", "Payout ratio receiving the zero sustainability score; dividends with non-positive EPS also score zero."),
    ("Liquidity", "FINANCIAL_SECTORS", "Comma-separated sectors excluded from ordinary liquidity-ratio scoring."),
    ("Confidence", "CONFIDENCE_FACTOR_WEIGHT", "Relative weight on factor coverage in confidence."),
    ("Confidence", "CONFIDENCE_FAMILY_WEIGHT", "Relative weight on family coverage in confidence."),
    ("Balance-sheet gates", "MAX_STANDARD_DEBT_EQUITY_RATIO", "Maximum debt/equity for a standard operating company when that measure is valid."),
    ("Balance-sheet gates", "MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO", "Maximum debt/total capital for a standard operating company."),
    ("Balance-sheet gates", "MIN_STANDARD_CURRENT_RATIO", "Minimum current ratio for standard-company survivability when available."),
    ("Balance-sheet gates", "MAX_REIT_DEBT_EBITDA", "Maximum long-term debt/EBITDA for REIT and real-estate gates."),
    ("Balance-sheet gates", "MAX_REIT_DEBT_EBIT", "Maximum long-term debt/EBIT fallback for REIT and real-estate gates."),
    ("Risk gates", "MAX_BOOK_PRICE_DIVERGENCE", "Optional hard candidate gate: maximum absolute (price-book)/price divergence; 0 requires an exact match, blank disables."),
    ("EPS policy", "EG2_REQUIRE_POSITIVE_EPS_SAFE", "Require positive F1 and F2 EPS for Safe."),
    ("EPS policy", "EG2_REQUIRE_POSITIVE_EPS_HIGH_GROWTH", "Require positive F1 and F2 EPS for High Growth Potential."),
    ("EPS policy", "EG2_REQUIRE_POSITIVE_EPS_TURNAROUND", "Require positive F1 and F2 EPS for Turnaround Story."),
    ("Implementation", "MIN_MARKET_CAP_MIL", "Minimum market capitalisation in USD millions."),
    ("Implementation", "MIN_AVG_DAILY_DOLLAR_VOLUME", "Minimum average daily dollar volume for long implementation."),
    ("Implementation", "MIN_AVG_DAILY_DOLLAR_VOLUME_SHORT", "Minimum average daily dollar volume for short implementation."),
    ("Implementation", "REQUIRE_OPTIONABLE_FOR_OPTIONS_STRATEGY", "Require listed options only when an options implementation is requested."),
    ("Implementation", "ALLOW_OTC", "Permit OTC-listed securities."),
    ("Implementation", "ALLOW_ADR", "Permit ADR/ADS securities."),
    ("Implementation", "ALLOW_MLP", "Permit MLP securities."),
    ("Implementation", "ALLOW_CANADIAN", "Permit Canadian securities."),
    ("Validation", "MARGIN_RECONCILIATION_ABS_TOLERANCE", "Absolute vendor-versus-derived margin tolerance in decimal units."),
    ("Data policy", "ALLOWED_SECURITY_TYPES", "Comma-separated eligible security types."),
    ("Validation", "PB_VALIDATION_TOLERANCE", "Allowed relative P/B reconstruction difference before a flag."),
    ("Validation", "MARKET_CAP_PER_SHARE_TOLERANCE", "Allowed market-cap/share-price reconstruction difference before a flag."),
] + [
    ("Family weight", f"WEIGHT_{family}", f"Relative weight of {family.replace('_', ' ').lower()}.")
    for family in FAMILY_WEIGHTS
] + [
    ("Advanced weights", "SIGNAL_WEIGHTS_JSON", "Optional JSON object of individual signal weights; blank or {} uses 1.0."),
    ("Advanced weights", "DUPLICATE_GROUP_WEIGHTS_JSON", "Optional JSON object of duplicate-group weights; blank or {} uses 1.0."),
]


def control_panel_rows() -> list[list[object]]:
    rows: list[list[object]] = [["System", "MODEL_VERSION", MODEL_VERSION,
                                 "Model/control schema marker; updated automatically."]]
    for section, parameter, description in CONTROL_PANEL_SPECS:
        if parameter.startswith("WEIGHT_"):
            value = FAMILY_WEIGHTS[parameter.removeprefix("WEIGHT_")]
        elif parameter == "SIGNAL_WEIGHTS_JSON":
            value = json.dumps(SIGNAL_WEIGHTS, sort_keys=True)
        elif parameter == "DUPLICATE_GROUP_WEIGHTS_JSON":
            value = json.dumps(DUPLICATE_GROUP_WEIGHTS, sort_keys=True)
        else:
            value = globals()[parameter]
            if isinstance(value, tuple):
                value = ",".join(value)
            elif value is None:
                value = ""
        rows.append([section, parameter, value, description])
    return rows


def strategy_weight_rows() -> list[list[object]]:
    """Matrix displayed separately on Control Panel for direct manual editing."""
    rows = []
    for family in FAMILY_WEIGHTS:
        rows.append([
            family,
            STRATEGY_WEIGHTS["Safe"][family],
            STRATEGY_WEIGHTS["High Growth Potential"][family],
            STRATEGY_WEIGHTS["Turnaround Story"][family],
        ])
    return rows


def apply_control_panel(rows: list[list[object]]) -> dict:
    """Apply editable Google/Excel Control Panel values to this run."""
    global MODEL_CONTROLS, FAMILY_WEIGHTS, SIGNAL_WEIGHTS, DUPLICATE_GROUP_WEIGHTS, STRATEGY_WEIGHTS
    _restore_base_controls()
    aliases = {"MIN_PEER_OBSERVATIONS": "MIN_VALID_FACTOR_OBSERVATIONS"}
    values = {str(r[1]).strip(): r[2] for r in rows if len(r) >= 3 and str(r[1]).strip()}
    migrate_weights = str(values.get("MODEL_VERSION", "")).strip() != MODEL_VERSION
    for old, new in aliases.items():
        if new not in values and old in values:
            values[new] = values[old]
    try:
        for parameter, raw in values.items():
            if parameter == "MODEL_VERSION":
                continue
            if migrate_weights and parameter == "FINANCIAL_SECTORS":
                continue
            matched_strategy = None
            for strategy in STRATEGY_WEIGHTS:
                prefix = "STRATEGY_WEIGHT_" + strategy.upper().replace(" ", "_") + "_"
                if parameter.startswith(prefix):
                    matched_strategy = (strategy, parameter.removeprefix(prefix))
                    break
            if matched_strategy:
                if migrate_weights:
                    continue
                strategy, family = matched_strategy
                if family not in STRATEGY_WEIGHTS[strategy]:
                    raise ValueError(f"Unknown strategy family {family!r} for {strategy}")
                if raw in (None, ""):
                    raise ValueError(f"{parameter} cannot be blank; use 0 to disable a component")
                STRATEGY_WEIGHTS[strategy][family] = float(raw)
                continue
            if parameter.startswith("WEIGHT_"):
                if migrate_weights:
                    continue
                family = parameter.removeprefix("WEIGHT_")
                if family in FAMILY_WEIGHTS and raw not in (None, ""):
                    FAMILY_WEIGHTS[family] = float(raw)
                continue
            if parameter in {"SIGNAL_WEIGHTS_JSON", "DUPLICATE_GROUP_WEIGHTS_JSON"}:
                parsed = _parse_weight_map(raw, parameter)
                if parameter == "SIGNAL_WEIGHTS_JSON":
                    SIGNAL_WEIGHTS = parsed
                else:
                    DUPLICATE_GROUP_WEIGHTS = parsed
                continue
            if parameter not in _CONTROL_NAMES or raw in (None, ""):
                continue
            if parameter == "MAX_BOOK_PRICE_DIVERGENCE" and str(raw).strip().casefold() in {"none", "null"}:
                globals()[parameter] = None
                continue
            current = globals()[parameter]
            if isinstance(current, bool):
                value = _parse_bool(raw, parameter)
            elif isinstance(current, int):
                numeric = float(raw)
                if not numeric.is_integer():
                    raise ValueError(f"{parameter} must be a whole number; got {raw!r}")
                value = int(numeric)
            elif isinstance(current, tuple):
                value = tuple(x.strip() for x in str(raw).split(",") if x.strip())
            else:
                value = float(raw)
            globals()[parameter] = value
        validate_model_controls()
    except (TypeError, ValueError, json.JSONDecodeError):
        _restore_base_controls()
        MODEL_CONTROLS = deepcopy(_BASE_CONTROLS)
        raise
    MODEL_CONTROLS = deepcopy(_model_controls())
    return MODEL_CONTROLS
