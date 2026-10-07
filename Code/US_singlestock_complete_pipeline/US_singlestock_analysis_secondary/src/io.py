from __future__ import annotations

import os
import math
from pathlib import Path
from typing import Any

import pandas as pd

import config


def find_credentials() -> str | None:
    configured = os.getenv("GOOGLE_CREDENTIALS_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured:
        return configured
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        for filename in ("google_credentials.json", "service_account.json"):
            candidate = parent / filename
            if candidate.is_file():
                return str(candidate)
    return None


def client(credentials_file: str | None = None):
    import gspread

    resolved = credentials_file or find_credentials()
    if not resolved or not Path(resolved).expanduser().is_file():
        raise RuntimeError("Google service-account credentials were not found in the project parents or environment.")
    return gspread.service_account(filename=str(Path(resolved).expanduser()))


def _parse_bool(value: Any) -> bool:
    text = str(value).strip().upper()
    if text in {"TRUE", "1", "YES", "Y"}:
        return True
    if text in {"FALSE", "0", "NO", "N"}:
        return False
    raise ValueError(f"Expected TRUE/FALSE, got {value!r}")


def default_controls() -> dict[str, Any]:
    controls = dict(config.GENERAL_DEFAULTS)
    for category, cfg in config.CATEGORIES.items():
        slug = cfg["slug"]
        for family, weight in config.FAMILY_WEIGHTS[category].items():
            controls[f"FAMILY_WEIGHT_{slug}_{family}"] = weight
        for metric, weight in config.METRIC_WEIGHTS[category].items():
            controls[f"METRIC_WEIGHT_{slug}_{metric.upper()}"] = weight
    return controls


def read_controls(values: list[list[str]]) -> dict[str, Any]:
    controls = default_controls()
    known = set(controls)
    types = {key: type(value) for key, value in controls.items()}
    incoming_model = next(
        (str(row[2]).strip() for row in values if len(row) >= 3 and str(row[1]).strip() == "MODEL_VERSION"),
        "",
    )
    schema_changed = incoming_model not in {"", str(config.GENERAL_DEFAULTS["MODEL_VERSION"])}
    for row in values:
        if len(row) < 3:
            continue
        key = str(row[1]).strip()
        raw = row[2]
        if key not in known or str(raw).strip() == "":
            continue
        # Preserve every user-editable setting across model upgrades. Only the
        # code-owned schema marker advances; newly introduced controls take defaults.
        if schema_changed and key == "MODEL_VERSION":
            continue
        try:
            if types[key] is bool:
                controls[key] = _parse_bool(raw)
            elif types[key] is int:
                numeric = float(raw)
                if not math.isfinite(numeric) or not numeric.is_integer():
                    raise ValueError("Expected a finite integer")
                controls[key] = int(numeric)
            elif types[key] is float:
                controls[key] = float(raw)
            else:
                controls[key] = str(raw).strip()
        except Exception as exc:
            raise ValueError(f"Invalid Control Panel value for {key}: {raw!r}") from exc
    # Correct known v5 default redundancies without overwriting a user's custom
    # choice. FCF yield and Price/FCF were exact rank duplicates; revenue CAGR
    # and its four-year regression trend were near-duplicates.
    if schema_changed and incoming_model == "secondary_quality_first_v5":
        for cfg in config.CATEGORIES.values():
            key = f"METRIC_WEIGHT_{cfg['slug']}_LATEST_PRICE_FCF"
            # The Control Panel displays/stores the legacy one-third weight as
            # 0.33, so accept its rounded representation as the old default.
            if abs(float(controls[key]) - (1 / 3)) < 0.01:
                controls[key] = 0.0
        legacy_trend_weights = {"HIGH_GROWTH_POTENTIAL": 3.0, "TURNAROUND_STORY": 2.0}
        new_trend_weights = {"HIGH_GROWTH_POTENTIAL": 1.5, "TURNAROUND_STORY": 1.0}
        for slug, legacy in legacy_trend_weights.items():
            key = f"METRIC_WEIGHT_{slug}_REVENUE_FOUR_YEAR_TREND"
            if abs(float(controls[key]) - legacy) < 1e-9:
                controls[key] = new_trend_weights[slug]
    # Upgrade complete legacy default profiles together so their weights still
    # sum to 100. Preserve customised profiles and already-current settings.
    legacy_models = {
        "secondary_trend_point_impact_v3", "secondary_trend_reliability_v4",
        "secondary_quality_first_v5", "secondary_sector_robust_v6",
    }
    if incoming_model in legacy_models:
        supplied = {str(row[1]).strip() for row in values if len(row) >= 3 and str(row[2]).strip()}
        for category, cfg in config.CATEGORIES.items():
            slug = cfg["slug"]
            legacy = config.LEGACY_FAMILY_WEIGHTS[category]
            if all(
                (key := f"FAMILY_WEIGHT_{slug}_{family}") in supplied
                and abs(float(controls[key]) - weight) < 1e-9
                for family, weight in legacy.items()
            ):
                for family, weight in config.FAMILY_WEIGHTS[category].items():
                    controls[f"FAMILY_WEIGHT_{slug}_{family}"] = weight
                # Previously inactive inflection metrics were stored as zero.
                if legacy["INFLECTION"] == 0:
                    for metric, (family, _, _) in config.FEATURE_SPECS.items():
                        key = f"METRIC_WEIGHT_{slug}_{metric.upper()}"
                        if family == "INFLECTION" and float(controls[key]) == 0:
                            controls[key] = config.METRIC_WEIGHTS[category][metric]
    if schema_changed and incoming_model == "secondary_trend_first_v10":
        old_short_weights = {
            "TREND": 50.0, "INFLECTION": 15.0, "OPERATING": 20.0,
            "CASH_FLOW": 10.0, "BALANCE_SHEET": 5.0,
        }
        if all(float(controls[f"SHORT_WEIGHT_{family}"]) == value for family, value in old_short_weights.items()):
            # Migrate the unchanged v10 defaults to a 100-point v11 profile,
            # reserving 15 points for the primary point-in-time signals.
            for family, value in {
                "TREND": 45.0, "INFLECTION": 15.0, "OPERATING": 15.0,
                "CASH_FLOW": 5.0, "BALANCE_SHEET": 5.0, "POINT_IN_TIME": 15.0,
            }.items():
                controls[f"SHORT_WEIGHT_{family}"] = value
    # Upgrade the unchanged v11 screening defaults to the four-year Long gate
    # and a more selective Short input bar. Only replace exact prior defaults;
    # customized weights, floors and caps remain intact.
    if incoming_model in {f"secondary_trend_first_v{version}" for version in range(7, 12)}:
        for key in (
            "MIN_HISTORY_YEARS_SAFE",
            "MIN_HISTORY_YEARS_HIGH_GROWTH_POTENTIAL",
            "MIN_HISTORY_YEARS_TURNAROUND_STORY",
        ):
            if int(controls[key]) == 2:
                controls[key] = 4
        if int(controls["MIN_TREND_OBSERVATIONS"]) == 3:
            controls["MIN_TREND_OBSERVATIONS"] = 4
        if int(controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"]) == 5:
            controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"] = 8
        if abs(float(controls["SHORT_MIN_POINT_IN_TIME_COVERAGE"]) - 0.60) < 1e-9:
            controls["SHORT_MIN_POINT_IN_TIME_COVERAGE"] = 0.80
        # In v11 the 0% CAGR floor was still a strict greater-than check, so
        # False did not actually permit flat growth. Preserve that behavior.
        if controls["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"] is False:
            controls["HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR"] = True
    # v15 changes the display policy to three ranked research candidates per
    # category. Apply the explicitly requested cap on upgrade; subsequent
    # edits to the v15 Control Panel remain user-controlled.
    if schema_changed and incoming_model:
        for cfg in config.CATEGORIES.values():
            key = f"TOP_N_{cfg['slug']}"
            if controls[key] == 20:
                controls[key] = 3
    validate_controls(controls)
    return controls


def validate_controls(controls: dict[str, Any]) -> None:
    errors: list[str] = []
    for key, value in controls.items():
        if isinstance(value, (int, float)) and not math.isfinite(value):
            errors.append(f"{key} must be finite")
        if key.startswith(("TOP_N_", "SHORT_TOP_N", "MIN_HISTORY_YEARS_", "SHORT_MIN_HISTORY_YEARS", "SHORT_MIN_TREND_OBSERVATIONS", "SHORT_MIN_DIRECTION_OBSERVATIONS", "SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS", "MIN_CROSS_SECTION", "SHORT_MIN_CROSS_SECTION")) and float(value) < 1:
            errors.append(f"{key} must be at least 1")
        if (key.startswith(("FAMILY_WEIGHT_", "METRIC_WEIGHT_", "SHORT_WEIGHT_", "SHORT_METRIC_WEIGHT_")) or key == "SHORT_MIN_SCORE" or key == "SHORT_MIN_TREND_SCORE") and float(value) < 0:
            errors.append(f"{key} cannot be negative")
    for key in (
        "MIN_OVERALL_WEIGHT_COVERAGE", "MIN_FAMILY_METRIC_COVERAGE", "PERCENTILE_WEIGHT",
        "THREE_YEAR_TREND_RELIABILITY", "TREND_FIT_RELIABILITY_FLOOR",
        "MIN_SELECTION_WEIGHT_COVERAGE_SAFE", "MIN_SELECTION_WEIGHT_COVERAGE_HIGH_GROWTH_POTENTIAL",
        "MIN_SELECTION_WEIGHT_COVERAGE_TURNAROUND_STORY", "MIN_EFFECTIVE_TREND_RELIABILITY_SAFE",
        "MIN_EFFECTIVE_TREND_RELIABILITY_HIGH_GROWTH_POTENTIAL", "MIN_EFFECTIVE_TREND_RELIABILITY_TURNAROUND_STORY",
        "SAFE_MIN_POSITIVE_HISTORY_SHARE", "TURNAROUND_MIN_FAVOURABLE_TREND_BREADTH",
        "SECTOR_PEER_WEIGHT", "MIN_RECENT_FAVOURABLE_BREADTH",
        "MIN_FAVOURABLE_TREND_BREADTH", "MIN_RECENT_METRIC_COVERAGE",
        "SHORT_MIN_TREND_COVERAGE", "SHORT_MIN_POINT_IN_TIME_COVERAGE", "SHORT_MIN_TREND_RELIABILITY", "SHORT_MIN_WEIGHT_COVERAGE",
        "SHORT_MAX_POSITIVE_REVENUE_GROWTH_SHARE",
        "SHORT_MIN_ADVERSE_TREND_BREADTH", "SHORT_MIN_ADVERSE_RECENT_BREADTH",
    ):
        if not 0 <= float(controls[key]) <= 1:
            errors.append(f"{key} must be between 0 and 1")
    if not 1 <= int(controls["MIN_RECENT_DIRECTION_OBSERVATIONS"]) <= 6:
        errors.append("MIN_RECENT_DIRECTION_OBSERVATIONS must be between 1 and 6")
    if int(controls["MAX_FINANCIAL_AGE_DAYS"]) < 1:
        errors.append("MAX_FINANCIAL_AGE_DAYS must be positive")
    if float(controls["ROBUST_Z_CAP"]) <= 0:
        errors.append("ROBUST_Z_CAP must be positive")
    if float(controls["CROSS_SECTION_SHRINKAGE_STRENGTH"]) < 0:
        errors.append("CROSS_SECTION_SHRINKAGE_STRENGTH cannot be negative")
    if float(controls["SECTOR_PEER_SHRINKAGE_STRENGTH"]) < 0:
        errors.append("SECTOR_PEER_SHRINKAGE_STRENGTH cannot be negative")
    if not 1 <= int(controls["SHORT_MIN_DIRECTION_OBSERVATIONS"]) <= 6:
        errors.append("SHORT_MIN_DIRECTION_OBSERVATIONS must be between 1 and 6")
    if int(controls["SHORT_MIN_CROSS_SECTION_OBSERVATIONS"]) < 2:
        errors.append("SHORT_MIN_CROSS_SECTION_OBSERVATIONS must be at least 2")
    if not 1 <= int(controls["SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS"]) <= 3:
        errors.append("SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS must be between 1 and 3")
    if not 0 <= float(controls["SHORT_MIN_SCORE"]) <= 100 or not 0 <= float(controls["SHORT_MIN_TREND_SCORE"]) <= 100:
        errors.append("Short score thresholds must be between 0 and 100")
    if int(controls["SHORT_MIN_HISTORY_YEARS"]) < 4 or int(controls["SHORT_MIN_TREND_OBSERVATIONS"]) < 4:
        errors.append("Short selection requires at least four fiscal years and trend observations")
    for category, cfg in config.CATEGORIES.items():
        history_key = f"MIN_HISTORY_YEARS_{cfg['slug']}"
        if int(controls[history_key]) < 4:
            errors.append(f"{history_key} must be at least four for Long selection")
    if int(controls["MIN_TREND_OBSERVATIONS"]) < 4:
        errors.append("MIN_TREND_OBSERVATIONS must be at least four for Long selection")
    short_weights = [float(controls[f"SHORT_WEIGHT_{family}"]) for family in ("TREND", "INFLECTION", "OPERATING", "CASH_FLOW", "BALANCE_SHEET", "POINT_IN_TIME")]
    if sum(short_weights) <= 0:
        errors.append("Short family weights need at least one positive value")
    if any(w > 0 and not any(float(controls[f"SHORT_METRIC_WEIGHT_{metric.upper()}"]) > 0 for metric in metrics)
           for w, metrics in zip(short_weights, (
               ("revenue_four_year_trend", "net_margin_four_year_trend", "fcf_margin_four_year_trend", "roa_four_year_trend", "gross_margin_four_year_trend", "debt_to_equity_four_year_trend", "positive_revenue_growth_share"),
               ("revenue_growth_acceleration", "net_margin_change", "fcf_margin_change", "roa_change", "ebit_margin_change", "debt_to_equity_change"),
               ("latest_roa", "latest_roe", "latest_net_margin", "latest_gross_margin", "latest_pre_tax_margin", "revenue_cagr", "latest_revenue_growth"),
               ("latest_fcf_margin", "latest_fcf_yield", "positive_fcf_share"),
               ("latest_current_ratio", "latest_quick_ratio", "latest_working_capital_to_assets", "latest_debt_to_equity", "latest_interest_coverage", "latest_net_debt_to_ebitda"),
               ("primary_short_score", "primary_price_change_4w", "primary_price_change_12w", "primary_f1_estimate_change_4w", "primary_f2_estimate_change_4w"),
           ))):
        errors.append("Each active short family needs at least one positive metric weight")
    if int(controls["MIN_SECTOR_PEER_OBSERVATIONS"]) < 2:
        errors.append("MIN_SECTOR_PEER_OBSERVATIONS must be at least 2")
    for key in ("MISSING_TREND_PENALTY_POINTS", "NO_USABLE_TREND_EXTRA_PENALTY_POINTS"):
        if float(controls[key]) < 0:
            errors.append(f"{key} cannot be negative")
    for key in ("STRONG_FAMILY_SCORE", "MIN_LEAVE_ONE_FAMILY_OUT_SCORE"):
        if not 0 <= float(controls[key]) <= 100:
            errors.append(f"{key} must be between 0 and 100")
    for cfg in config.CATEGORIES.values():
        slug = cfg["slug"]
        if not 0 <= float(controls[f"MIN_SELECTION_SCORE_{slug}"]) <= 100:
            errors.append(f"MIN_SELECTION_SCORE_{slug} must be between 0 and 100")
    if int(controls["MAX_FISCAL_YEAR_LAG"]) < 0 or int(controls["MAX_FISCAL_YEAR_GAP"]) < 1:
        errors.append("Fiscal-year lag must be non-negative and maximum gap must be at least 1")
    for category, cfg in config.CATEGORIES.items():
        slug = cfg["slug"]
        for family in config.FAMILY_WEIGHTS[category]:
            if float(controls[f"FAMILY_WEIGHT_{slug}_{family}"]) > 0 and not any(
                float(controls[f"METRIC_WEIGHT_{slug}_{metric.upper()}"]) > 0
                for metric, (metric_family, _, _) in config.FEATURE_SPECS.items() if metric_family == family
            ):
                errors.append(f"{category}: active {family} family needs at least one positive metric weight")
        if sum(float(controls[f"FAMILY_WEIGHT_{slug}_{f}"]) for f in config.FAMILY_WEIGHTS[category]) <= 0:
            errors.append(f"{category} family weights must contain at least one positive value")
    if errors:
        raise ValueError("; ".join(errors))


def _numeric_cell(value: Any) -> float:
    text = str(value or "").strip().replace(",", "").replace("%", "")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return float("nan")
    return number if math.isfinite(number) else float("nan")


def parse_primary_short_candidates(values: list[list[Any]]) -> pd.DataFrame:
    """Read selected rows from the primary analysis Short tab (row 3 header)."""
    if len(values) < 3:
        raise ValueError("Primary Short tab is missing its row-3 header")
    headers = [str(value).strip() for value in values[2]]
    required = {"Short Rank", "Ticker", "Company Name", "Industry", "Short Score"}
    missing = sorted(required - set(headers))
    if missing:
        raise ValueError(f"Primary Short tab lacks required fields: {missing}")
    idx = {name: headers.index(name) for name in required}
    optional = {
        "Primary 4-Week Price Change (%)": "% Price Change (4 Weeks)",
        "Primary 12-Week Price Change (%)": "% Price Change (12 Weeks)",
        "Primary F1 Estimate Change 4-Week (%)": "% Change F1 Est. (4 weeks)",
        "Primary F2 Estimate Change 4-Week (%)": "% Change F2 Est. (4 weeks)",
    }
    records = []
    for row in values[3:]:
        row = list(row) + [""] * max(0, len(headers) - len(row))
        rank_value = _numeric_cell(row[idx["Short Rank"]])
        if not math.isfinite(rank_value):
            continue
        if rank_value < 1 or not rank_value.is_integer():
            raise ValueError(f"Primary Short tab has invalid Short Rank: {row[idx['Short Rank']]!r}")
        ticker = str(row[idx["Ticker"]]).strip().upper()
        if not ticker:
            raise ValueError(f"Primary Short rank {int(rank_value)} has no ticker")
        signals = {name: _numeric_cell(row[headers.index(header)]) if header in headers else float("nan")
                   for name, header in optional.items()}
        records.append({
            "Ticker": ticker,
            "Primary Short Rank": int(rank_value),
            "Primary Company Name": str(row[idx["Company Name"]]).strip(),
            "Primary Industry": str(row[idx["Industry"]]).strip(),
            "Primary PIT Verification Status": str(row[headers.index("PIT Verification Status")]).strip() if "PIT Verification Status" in headers else "UNAVAILABLE",
            "Primary Latest Verified Availability Date": str(row[headers.index("Latest Verified Information Availability Date")]).strip() if "Latest Verified Information Availability Date" in headers else "",
            "Primary Dataset As Of Note": str(values[1][0]).strip() if len(values) > 1 and values[1] else "",
            "Primary Short Score": _numeric_cell(row[idx["Short Score"]]),
            **signals,
            "primary_short_score": _numeric_cell(row[idx["Short Score"]]),
            "primary_price_change_4w": signals["Primary 4-Week Price Change (%)"],
            "primary_price_change_12w": signals["Primary 12-Week Price Change (%)"],
            "primary_f1_estimate_change_4w": signals["Primary F1 Estimate Change 4-Week (%)"],
            "primary_f2_estimate_change_4w": signals["Primary F2 Estimate Change 4-Week (%)"],
        })
    candidate_columns = [
        "Ticker", "Primary Short Rank", "Primary Company Name", "Primary Industry",
        "Primary PIT Verification Status", "Primary Latest Verified Availability Date", "Primary Dataset As Of Note", "Primary Short Score",
        "Primary 4-Week Price Change (%)", "Primary 12-Week Price Change (%)",
        "Primary F1 Estimate Change 4-Week (%)", "Primary F2 Estimate Change 4-Week (%)",
        "primary_short_score", "primary_price_change_4w", "primary_price_change_12w",
        "primary_f1_estimate_change_4w", "primary_f2_estimate_change_4w",
    ]
    candidates = pd.DataFrame(records, columns=candidate_columns)
    candidates = candidates.sort_values("Primary Short Rank", kind="stable").reset_index(drop=True)
    if candidates["Ticker"].duplicated().any():
        duplicates = candidates.loc[candidates["Ticker"].duplicated(False), "Ticker"].tolist()
        raise ValueError(f"Primary Short tab contains duplicate tickers: {duplicates}")
    if candidates["Primary Short Rank"].duplicated().any():
        raise ValueError("Primary Short tab contains duplicate ranks")
    return candidates


def merge_primary_short_dataset_signals(candidates: pd.DataFrame, short_values: list[list[Any]],
                                        dataset_values: list[list[Any]]) -> pd.DataFrame:
    """Fill omitted Short-display signals from the matching Primary Dataset rows.

    Price and market cap must exactly reconcile with the published Short rows.
    A Dataset refresh without a corresponding Short refresh therefore fails closed.
    """
    if len(short_values) < 3 or not dataset_values:
        raise ValueError("Primary Short or Dataset header is missing")
    short_headers = [str(value).strip() for value in short_values[2]]
    dataset_headers = [str(value).strip() for value in dataset_values[0]]
    signal_headers = {
        "Primary 4-Week Price Change (%)": "% Price Change (4 Weeks)",
        "Primary 12-Week Price Change (%)": "% Price Change (12 Weeks)",
        "Primary F1 Estimate Change 4-Week (%)": "% Change F1 Est. (4 weeks)",
        "Primary F2 Estimate Change 4-Week (%)": "% Change F2 Est. (4 weeks)",
    }
    anchors = ("Market Cap (mil)", "Last Close")
    required_short = {"Ticker", *anchors}
    required_dataset = {"Ticker", *anchors, *signal_headers.values()}
    if not required_short.issubset(short_headers) or not required_dataset.issubset(dataset_headers):
        raise ValueError("Primary Short/Dataset lacks required signal or reconciliation columns")
    if len(set(dataset_headers)) != len(dataset_headers):
        raise ValueError("Primary Dataset contains duplicate column headers")
    short_index = {name: short_headers.index(name) for name in required_short}
    dataset_index = {name: dataset_headers.index(name) for name in required_dataset}
    selected = set(candidates["Ticker"])
    short_rows = {}
    for row in short_values[3:]:
        ticker = str(row[short_index["Ticker"]]).strip().upper() if len(row) > short_index["Ticker"] else ""
        if ticker in selected:
            if ticker in short_rows:
                raise ValueError(f"Duplicate selected ticker in Primary Short: {ticker}")
            short_rows[ticker] = row
    dataset_rows = {}
    for row in dataset_values[1:]:
        ticker = str(row[dataset_index["Ticker"]]).strip().upper() if len(row) > dataset_index["Ticker"] else ""
        if ticker in selected:
            if ticker in dataset_rows:
                raise ValueError(f"Duplicate selected ticker in Primary Dataset: {ticker}")
            dataset_rows[ticker] = row
    if set(short_rows) != selected or set(dataset_rows) != selected:
        raise ValueError(f"Primary Short/Dataset selected ticker mismatch: missing Short={sorted(selected - set(short_rows))}, "
                         f"missing Dataset={sorted(selected - set(dataset_rows))}")
    result = candidates.copy()
    for index, ticker in enumerate(result["Ticker"]):
        short_row, dataset_row = short_rows[ticker], dataset_rows[ticker]
        for name in anchors:
            short_value = _numeric_cell(short_row[short_index[name]] if len(short_row) > short_index[name] else "")
            dataset_value = _numeric_cell(dataset_row[dataset_index[name]] if len(dataset_row) > dataset_index[name] else "")
            if not math.isfinite(short_value) or not math.isfinite(dataset_value) or not math.isclose(
                    short_value, dataset_value, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError(f"Primary Dataset differs from published Short for {ticker} {name}")
        for output, source in signal_headers.items():
            value = _numeric_cell(dataset_row[dataset_index[source]] if len(dataset_row) > dataset_index[source] else "")
            existing = result.at[index, output]
            if math.isfinite(existing) and math.isfinite(value) and not math.isclose(existing, value, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError(f"Primary Dataset signal differs from published Short for {ticker} {source}")
            if math.isfinite(value):
                result.at[index, output] = value
                result.at[index, {
                    "Primary 4-Week Price Change (%)": "primary_price_change_4w",
                    "Primary 12-Week Price Change (%)": "primary_price_change_12w",
                    "Primary F1 Estimate Change 4-Week (%)": "primary_f1_estimate_change_4w",
                    "Primary F2 Estimate Change 4-Week (%)": "primary_f2_estimate_change_4w",
                }[output]] = value
    result["Primary Short Signal Source"] = "Primary Dataset reconciled to published Short market cap and close"
    return result


def validate_primary_mirror(primary_values: list[list[Any]], mirror_values: list[list[Any]],
                            title: str) -> None:
    """Fail when an IMPORTRANGE summary lags the authoritative Primary tab."""
    if len(primary_values) < 3 or len(mirror_values) < 3:
        raise ValueError(f"{title}: missing Primary or mirrored header")
    source, mirror = primary_values[2:], mirror_values[2:]
    if len(source) != len(mirror):
        raise ValueError(f"{title}: Primary/mirror row count differs ({len(source)} versus {len(mirror)})")
    for row_number, (a, b) in enumerate(zip(source, mirror), start=3):
        width = max(len(a), len(b))
        if (list(a) + [""] * (width - len(a))) != (list(b) + [""] * (width - len(b))):
            raise ValueError(f"{title}: Primary/mirror differs at row {row_number}; refresh IMPORTRANGE before screening")


def preflight_and_read(spreadsheet_id: str, credentials_file: str | None = None):
    if spreadsheet_id != config.SPREADSHEET_ID:
        raise ValueError(f"Refusing unexpected spreadsheet ID {spreadsheet_id}")
    gc = client(credentials_file)
    book = gc.open_by_key(spreadsheet_id)
    if book.title != config.EXPECTED_WORKBOOK_TITLE:
        raise ValueError(f"Workbook title mismatch: {book.title!r}")
    primary_book = gc.open_by_key(config.PRIMARY_SHORT_SPREADSHEET_ID)
    if primary_book.title != config.PRIMARY_SHORT_WORKBOOK_TITLE:
        raise ValueError(f"Primary source workbook title mismatch: {primary_book.title!r}")
    try:
        primary_short_values = primary_book.worksheet(config.PRIMARY_SHORT_TAB).get_all_values()
        primary_shortlist = parse_primary_short_candidates(primary_short_values)
        primary_dataset_values = primary_book.worksheet("Dataset").get_all_values()
        primary_shortlist = merge_primary_short_dataset_signals(primary_shortlist, primary_short_values,
                                                                 primary_dataset_values)
        primary_headers = primary_short_values[2]
        primary_shortlist.attrs["source_column_count"] = len(primary_headers)
        primary_shortlist.attrs["source_ticker_column"] = primary_headers.index("Ticker") + 1
        primary_shortlist.attrs["source_rank_column"] = primary_headers.index("Short Rank") + 1
    except Exception as exc:
        raise ValueError(f"Unable to read primary selected Short candidates: {exc}") from exc
    worksheets = {ws.title: ws for ws in book.worksheets()}
    required = {"Control Panel", "Short Data"}
    for cfg in config.CATEGORIES.values():
        required.update(cfg[key] for key in ("data_sheet", "primary_summary_sheet", "analysis_sheet", "secondary_summary_sheet"))
    missing = sorted(required - set(worksheets))
    if missing:
        raise ValueError(f"Required tabs missing: {missing}")

    controls = read_controls(worksheets["Control Panel"].get_all_values())
    datasets: dict[str, pd.DataFrame] = {}
    short_history_datasets: dict[str, pd.DataFrame] = {}
    metadata: dict[str, pd.DataFrame] = {}
    for category, cfg in config.CATEGORIES.items():
        raw = worksheets[cfg["data_sheet"]].get_all_values(value_render_option="UNFORMATTED_VALUE")
        if not raw or "Ticker" not in raw[0]:
            raise ValueError(f"{cfg['data_sheet']} is empty or lacks Ticker")
        frame = _normalise_source_dates(pd.DataFrame(raw[1:], columns=raw[0]))
        datasets[category] = frame.loc[frame["Ticker"].astype(str).str.strip().ne("")].copy()
        short_history_datasets[category] = datasets[category].copy()

        summary = worksheets[cfg["primary_summary_sheet"]].get_all_values()
        authoritative = primary_book.worksheet(cfg["primary_summary_sheet"]).get_all_values()
        validate_primary_mirror(authoritative, summary, cfg["primary_summary_sheet"])
        if len(summary) < 3 or "Ticker" not in summary[2]:
            raise ValueError(f"{cfg['primary_summary_sheet']} does not have the expected row-3 header")
        width = len(summary[2])
        rows = [(row + [""] * width)[:width] for row in summary[3:] if any(str(v).strip() for v in row)]
        metadata[category] = pd.DataFrame(rows, columns=summary[2])
        datasets[category], metadata[category] = restrict_to_primary(datasets[category], metadata[category], category)
    # Short Data is supplied by the SEC importer and later republished with
    # lineage. Its title/note rows make the field header row 3, unlike the
    # imported A1 header, so locate a valid header rather than assuming a row.
    short_values = worksheets["Short Data"].get_all_values(value_render_option="UNFORMATTED_VALUE")
    short_imported_dataset = pd.DataFrame()
    for row_index, row in enumerate(short_values[:5]):
        if "Ticker" in row and "Fiscal Year" in row:
            width = len(row)
            rows = [(item + [""] * width)[:width] for item in short_values[row_index + 1:] if any(str(v).strip() for v in item)]
            short_imported_dataset = _normalise_source_dates(pd.DataFrame(rows, columns=row))
            break
    return book, worksheets, datasets, metadata, controls, short_history_datasets, primary_shortlist, short_imported_dataset


def _normalise_source_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert Sheets date serials back to ISO text after a formatted read."""
    frame = frame.copy()
    for column in ("Fiscal Period End", "Price Date", "Revenue Filed"):
        if column not in frame:
            continue
        text = frame[column].astype(str).str.strip()
        serial = pd.to_numeric(text, errors="coerce")
        is_serial = text.str.fullmatch(r"\d+(?:\.\d*)?").fillna(False) & serial.between(20000, 80000)
        if is_serial.any():
            dates = pd.Timestamp("1899-12-30") + pd.to_timedelta(serial.loc[is_serial], unit="D")
            frame[column] = frame[column].astype(object)
            frame.loc[is_serial, column] = dates.dt.strftime("%Y-%m-%d")
    return frame


def restrict_to_primary(raw: pd.DataFrame, primary: pd.DataFrame, category: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Only current primary selections may enter the secondary peer universe."""
    primary = primary.copy()
    primary["Ticker"] = primary["Ticker"].fillna("").astype(str).str.strip().str.upper()
    primary = primary.loc[primary["Ticker"].ne("")].drop_duplicates("Ticker")
    if primary.empty:
        raise ValueError(f"{category}: primary shortlist is empty")
    raw = raw.copy()
    raw["Ticker"] = raw["Ticker"].fillna("").astype(str).str.strip().str.upper()
    return raw.loc[raw["Ticker"].isin(primary["Ticker"])].copy(), primary


def build_short_universe(
    datasets: dict[str, pd.DataFrame], primary_shortlist: pd.DataFrame,
    imported_short_history: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Use only tickers selected on the primary Short tab and available annual histories."""
    candidates = primary_shortlist.copy()
    candidates["Ticker"] = candidates["Ticker"].fillna("").astype(str).str.strip().str.upper()
    candidates = candidates.loc[candidates["Ticker"].ne("")].drop_duplicates("Ticker")
    candidate_tickers = set(candidates["Ticker"])
    if not candidate_tickers:
        first = next(iter(datasets.values()))
        return first.iloc[0:0].copy(), candidates.assign(**{"History Source Tabs": pd.Series(dtype=str)})
    frames, source_tabs = [], {}
    for category in config.CATEGORIES:
        raw = datasets[category].copy()
        raw["Ticker"] = raw["Ticker"].fillna("").astype(str).str.strip().str.upper()
        matched = raw.loc[raw["Ticker"].isin(candidate_tickers)].copy()
        if not matched.empty:
            frames.append(matched)
            for ticker in matched["Ticker"].dropna().unique():
                source_tabs.setdefault(ticker, []).append(config.CATEGORIES[category]["data_sheet"])
    if imported_short_history is not None and not imported_short_history.empty and "Ticker" in imported_short_history:
        raw = imported_short_history.copy()
        raw["Ticker"] = raw["Ticker"].fillna("").astype(str).str.strip().str.upper()
        matched = raw.loc[raw["Ticker"].isin(candidate_tickers)].copy()
        if not matched.empty:
            frames.append(matched)
            for ticker in matched["Ticker"].dropna().unique():
                source_tabs.setdefault(ticker, []).append("Short Data")
    # Keep the source schema even when none of the primary short names has history yet.
    if frames:
        combined = pd.concat(frames, ignore_index=True)
    else:
        first = next(iter(datasets.values()))
        combined = first.iloc[0:0].copy()
    candidates["History Source Tabs"] = candidates["Ticker"].map(lambda t: ", ".join(source_tabs.get(t, [])))
    if combined.empty:
        return combined, candidates
    combined["_period"] = pd.to_datetime(combined.get("Fiscal Period End"), errors="coerce")
    numeric = [c for c in ("Revenue", "Net Income", "Free Cash Flow", "Net Margin", "Gross Margin", "Return on Assets Ratio", "Debt to Equity Ratio") if c in combined]
    combined["_data_completeness"] = combined[numeric].notna().sum(axis=1)
    combined["_source_order"] = range(len(combined))
    combined = combined.sort_values(["Ticker", "Fiscal Year", "_period", "_data_completeness", "_source_order"], kind="stable", na_position="first")
    combined = combined.drop_duplicates(["Ticker", "Fiscal Year"], keep="last").drop(columns=["_period", "_data_completeness", "_source_order"])
    return combined, candidates


def build_short_data_export(short_history: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    """Add primary-short lineage to the filtered fiscal records shown in Short Data."""
    history = short_history.copy()
    if "Ticker" not in history.columns or "Ticker" not in candidates.columns:
        raise ValueError("Short Data export requires Ticker in both history and candidate rows")
    history["Ticker"] = history["Ticker"].fillna("").astype(str).str.strip().str.upper()
    candidate_columns = [
        column for column in (
            "Ticker", "Primary Short Rank", "Primary Company Name", "Primary Industry", "History Source Tabs",
        ) if column in candidates.columns
    ]
    # Short Data is also read as a source on the next run. Remove previously
    # published lineage (including old merge suffixes) before joining the
    # current primary candidate metadata, so refreshes remain idempotent.
    lineage_fields = candidate_columns[1:]
    history = history.drop(columns=[
        column for column in history
        if any(column == field or column.startswith(f"{field}_") for field in lineage_fields)
    ])
    lineage = candidates[candidate_columns].copy()
    lineage["Ticker"] = lineage["Ticker"].fillna("").astype(str).str.strip().str.upper()
    if lineage["Ticker"].duplicated().any():
        raise ValueError("Short Data export has duplicate primary candidate tickers")
    output = history.merge(lineage, on="Ticker", how="inner", validate="many_to_one")
    leading = [
        "Ticker", "Primary Short Rank", "Primary Company Name", "Primary Industry", "History Source Tabs",
    ]
    ordered = [column for column in leading if column in output.columns]
    ordered += [column for column in history.columns if column != "Ticker" and column in output.columns]
    ordered += [column for column in output.columns if column not in ordered]
    return output.loc[:, ordered]
