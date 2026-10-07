from __future__ import annotations
import math
import time
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import pandas as pd
import numpy as np
from gspread.exceptions import APIError
from gspread.utils import absolute_range_name, rowcol_to_a1
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from .scoring import RAW_SIGNALS
from .schema import UNAVAILABLE_CALCULATIONS
from .transformations import DERIVED_FEATURES
from .institutional import METRIC_AVAILABILITY_REGISTRY


@dataclass
class TabManifest:
    matched: list[str]
    missing: list[str]
    unexpected: list[str]


def _sheet_values(rows: list[list[object]]) -> list[list[object]]:
    """Convert pandas/NumPy scalars into values accepted by the Sheets JSON API."""
    values = []
    for row in rows:
        converted = []
        for value in row:
            if value is None or (not isinstance(value, (list, tuple, dict)) and pd.isna(value)):
                converted.append("NO DATA")
                continue
            # NumPy scalar types expose item(), which returns the native Python value.
            if hasattr(value, "item"):
                value = value.item()
            if isinstance(value, float) and not math.isfinite(value):
                value = "NO DATA"
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            converted.append(value)
        values.append(converted)
    return values


def _retry_after_seconds(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    try:
        return max(float(value), 0)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0)
        except (TypeError, ValueError, OverflowError):
            return fallback


def _retry_google_request(call, *, attempts: int = 6):
    """Retry quota, transient server, timeout, and connection failures."""
    delay = 5
    for attempt in range(attempts):
        try:
            return call()
        except APIError as exc:
            if exc.response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
            retry_after = exc.response.headers.get("Retry-After")
            time.sleep(_retry_after_seconds(retry_after, delay))
            delay = min(delay * 2, 60)
        except (RequestsConnectionError, RequestsTimeout):
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 60)


def _control_panel_values(rows: list[list[object]], strategy_rows: list[list[object]]) -> list[list[object]]:
    values = [["CONTROL PANEL — EDIT VALUE COLUMN, THEN RERUN", "", "", ""], ["", "", "", ""],
              ["Section", "Parameter", "Value", "Description"]] + rows
    values += [["", "", "", ""],
               ["STRATEGY WEIGHTING CONTROLS — NON-NEGATIVE; USE 0 TO DISABLE", "", "", ""],
               ["Metric / Score", "Safe Weight", "High Growth Potential Weight", "Turnaround Story Weight"]]
    values += strategy_rows
    values.append(["Total Weight"] + [sum(float(row[column]) for row in strategy_rows) for column in (1, 2, 3)])
    values += [["Interpretation", "Weights are relative and automatically normalised over valid components.", "", ""],
               ["Missing data", "Missing components are excluded, subject to the minimum coverage controls above.", "", ""],
               ["Negative weights", "Not permitted. A zero weight disables a component; malformed or blank matrix cells fail the run.", "", ""]]
    return _sheet_values(values)


def helper_sheet_name(industry: object) -> str:
    """Google titles are capped at 100 characters; retain a stable audited mapping."""
    base = str(industry)
    suffix = " - Helper"
    if len(base + suffix) <= 100:
        return base + suffix
    digest = sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[:100 - len(suffix) - 10]} {digest}{suffix}"


def _industry_values(table: pd.DataFrame) -> list[list[object]]:
    long = table.loc[table["Quantitative Candidate"].eq("QUANT LONG")].sort_values("Expanded Long Score", ascending=False)
    short = table.loc[table["Quantitative Candidate"].eq("QUANT SHORT")].sort_values("Expanded Short Score", ascending=False)
    table = table.sort_values(["Expanded Net Quant Score", "Expanded Long Score"], ascending=[False, False])
    visible = [c for c in ["Ticker", "Company Name", "Exchange", "COM/ADR/Canadian", "Sector", "Industry",
                           "Expanded Long Rank", "Expanded Long Score", "Expanded Short Rank", "Expanded Short Score",
                           "Expanded Net Quant Score", "Score Confidence", "Independent Group Coverage",
                           "Expanded Family Coverage", "Safe Score", "High Growth Potential Score", "Turnaround Story Score",
                           "Positive Peer Evidence Count", "Negative Peer Evidence Count", "Excessive Working Capital Flag",
                           "Unsustainable Payout Flag", "Implementation Feasibility Note", "Data Status"] if c in table]
    display_headers = [{
        "Expanded Long Rank": "Quant Long Rank",
        "Expanded Long Score": "Quant Long Score (Reliability Adjusted)",
        "Expanded Short Rank": "Quant Short Rank",
        "Expanded Short Score": "Quant Short Score (Reliability Adjusted)",
        "Expanded Net Quant Score": "Quant Net Score",
        "Expanded Family Coverage": "Quant Family Weight Coverage",
    }.get(c, c) for c in visible]
    diagnostic = build_industry_summary(table).iloc[0]
    diagnostic_rows = [["INDUSTRY DIAGNOSTIC SUMMARY"], ["Metric", "Value"]] + [[k, v] for k, v in diagnostic.items()]
    rows = diagnostic_rows + [[""], ["QUANT LONG CANDIDATES"], display_headers] + long[visible].fillna("NO DATA").values.tolist()
    rows += [[""], ["QUANT SHORT CANDIDATES"], display_headers] + short[visible].fillna("NO DATA").values.tolist()
    rows += [[""], ["FULL INDUSTRY RANKING"], display_headers] + table[visible].fillna("NO DATA").values.tolist()
    end_col = max(len(row) for row in rows)
    return _sheet_values([row + [""] * (end_col - len(row)) for row in rows])


SUMMARY_METRICS = {
    "Safe": ["debt_equity_ratio", "roe_valid", "Current ROA (TTM)", "Net Margin %",
             "earnings_yield_pe_valid", "cash_flow_margin", "cash_conversion"],
    "High Growth Potential": ["This Yr`s Est.d Growth (F(1)/F(0))", "eg2_growth_pct",
                              "eps_growth_acceleration", "Long-Term Growth Consensus Est.",
                              "Sales Growth F(0)/F(-1)", "% Change F1 Est. (4 weeks)",
                              "% Change F2 Est. (4 weeks)", "growth_adjusted_pe_f1"],
    "Turnaround Story": ["eps_growth_acceleration", "% Change F1 Est. (4 weeks)",
                         "% Change F2 Est. (4 weeks)", "Last EPS Surprise (%)",
                         "Avg EPS Surprise (Last 4 Qtrs)", "momentum_acceleration",
                         "Sales Growth F(0)/F(-1)", "forward_pe_change"],
}


def _risk_flags(row: pd.Series) -> str:
    flags = []
    if row.get("Data Status") != "SUFFICIENT":
        flags.append(str(row.get("Data Status", "INSUFFICIENT DATA")))
    if row.get("Excessive Working Capital Flag") is True:
        flags.append("EXCESS WORKING CAPITAL")
    if row.get("Unsustainable Payout Flag") is True:
        flags.append("UNSUSTAINABLE PAYOUT")
    if row.get("Financial Company Liquidity Exclusion") is True:
        flags.append("FINANCIAL LIQUIDITY EXCLUDED")
    for label, column in (
        ("CURRENT RATIO DISTRESS", "liquidity_current_ratio__distress_flag"),
        ("QUICK RATIO DISTRESS", "liquidity_quick_ratio__distress_flag"),
        ("CASH RATIO DISTRESS", "liquidity_cash_ratio__distress_flag"),
        ("P/B RECONCILIATION", "P/B Reconciliation Flag"),
        ("MARKET CAP/SHARE RECONCILIATION", "Market Cap Per Share Flag"),
        ("RATING PERCENT RECONCILIATION", "Rating Percent Reconciliation Flag"),
    ):
        value = row.get(column)
        if value is True or (isinstance(value, str) and value.strip() not in {"", "OK", "False"}):
            flags.append(label)
    if not bool(row.get("Safe Balance-Sheet Gate Pass", True)):
        flags.append("SAFE LEVERAGE GATE FAIL")
    return "; ".join(flags) if flags else "NONE"


def _quant_reason(row: pd.Series, strategy: str, weights: dict[str, float]) -> str:
    contributors = []
    for family, weight in weights.items():
        points = row.get(f"{strategy} {family} Contribution Points")
        score = row.get(f"{family} Long Sub-score")
        if weight > 0 and pd.notna(points) and pd.notna(score):
            contributors.append((float(points), family.replace("_", " ").title(), float(score)))
    contributors.sort(reverse=True)
    lead = ", ".join(f"{name} {score:.1f} ({points:.1f} pts)" for points, name, score in contributors[:3])
    coverage = 100 * float(row.get(f"{strategy} Weight Coverage", 0) or 0)
    if strategy == "Turnaround Story":
        lead += f"; inflection {float(row.get('Turnaround Inflection Score', np.nan)):.1f}"
    return f"{lead}; {coverage:.0f}% weighted coverage"


def _quant_composite_reason(row: pd.Series) -> str:
    direction = "Long" if row.get("Quantitative Candidate") == "QUANT LONG" else "Short"
    suffix = f" {direction} Contribution Points"
    contributors = []
    for column, value in row.items():
        if column.startswith("Quant Composite ") and column.endswith(suffix) and pd.notna(value):
            family = column[len("Quant Composite "):-len(suffix)].replace("_", " ").title()
            contributors.append((float(value), family))
    contributors.sort(reverse=True)
    lead = ", ".join(f"{family} {points:.1f} pts" for points, family in contributors[:3])
    confidence = float(row.get("Score Confidence", 0) or 0)
    raw_col = f"Expanded Raw {direction} Score"
    final_col = f"Expanded {direction} Score"
    raw = float(row.get(raw_col, np.nan))
    final = float(row.get(final_col, np.nan))
    return f"{lead}; raw {raw:.1f} adjusted to {final:.1f} at {confidence:.0f}% confidence"


def build_strategy_summary(
    all_rows: pd.DataFrame, strategy: str, weights: dict[str, float], top_n: int, min_score: float
) -> pd.DataFrame:
    score_col = f"{strategy} Score"
    selected = all_rows.loc[pd.to_numeric(all_rows[score_col], errors="coerce").ge(min_score)].copy()
    selected = selected.sort_values(
        [f"{strategy} Universe Rank", score_col, "Ticker"], ascending=[True, False, True], kind="stable"
    ).head(top_n)
    out = pd.DataFrame(index=selected.index)
    out["Universe Rank"] = selected[f"{strategy} Universe Rank"]
    out["Within-Industry Rank"] = selected[f"{strategy} Within-Industry Rank"]
    for column in (
        "Ticker", "Company Name", "Market Cap (mil)", "Industry", "Sector", "Exchange", "COM/ADR/Canadian"
    ):
        out[column] = selected[column]
    out["Final Strategy Score"] = selected[score_col]
    for family, weight in weights.items():
        if weight > 0:
            label = family.replace("_", " ").title()
            out[f"{label} Score"] = selected[f"{family} Long Sub-score"]
            out[f"{label} Contribution"] = selected[f"{strategy} {family} Contribution Points"]
    for metric in SUMMARY_METRICS[strategy]:
        if metric in selected:
            out[metric] = selected[metric]
    out["Weight Coverage"] = selected[f"{strategy} Weight Coverage"]
    out["Components Available"] = selected[f"{strategy} Components Available"]
    out["Components Missing"] = selected[f"{strategy} Components Missing"]
    out["Existing Data Status"] = selected["Data Status"]
    out["Quantitative Candidate"] = selected.get("Quantitative Candidate")
    out["Implementation Eligible"] = selected.get(f"{strategy} Implementation Eligible")
    out["Tradability Gate Pass"] = selected.get("tradability_gate_pass")
    out["Tradability Gate Reasons"] = selected.get("tradability_gate_fail_reasons")
    out["Balance-Sheet Gate Pass"] = selected.get(f"{strategy} Balance-Sheet Gate Pass", selected.get("balance_sheet_gate_pass"))
    if strategy == "Turnaround Story":
        out["Inflection Score"] = selected["Turnaround Inflection Score"]
        out["Inflection Gate"] = selected["Turnaround Inflection Gate Pass"]
    out["Warning / Risk Flags"] = selected.apply(_risk_flags, axis=1)
    out["Concise Quantitative Reason"] = selected.apply(lambda row: _quant_reason(row, strategy, weights), axis=1)
    return out.reset_index(drop=True)


def _strategy_summary_values(all_rows: pd.DataFrame, strategy: str, weights: dict[str, float], top_n: int, min_score: float) -> list[list[object]]:
    summary = build_strategy_summary(all_rows, strategy, weights, top_n, min_score)
    note = "Peer-relative family scores; missing components are weight-renormalised; only eligible stocks are ranked."
    rows = [[f"{strategy.upper()} — CROSS-UNIVERSE STOCK SELECTION", ""], [note, ""], list(summary.columns)]
    rows += summary.fillna("NO DATA").values.tolist()
    return _sheet_values(rows)


def build_helper_table(table: pd.DataFrame, strategy_weights: dict[str, dict[str, float]]) -> pd.DataFrame:
    """Complete row audit plus friendly factor labels and narrative diagnostics.

    The model frame is deliberately copied in full.  This prevents a new calculation
    from being silently omitted from Google Sheets when the scoring pipeline evolves.
    """
    columns: dict[str, pd.Series] = {column: table[column] for column in table.columns}
    columns["Warning / Risk Flags"] = table.apply(_risk_flags, axis=1)
    for strategy, weights in strategy_weights.items():
        columns[f"{strategy} Quantitative Reason"] = table.apply(lambda row, s=strategy, w=weights: _quant_reason(row, s, w), axis=1)
    helper = pd.DataFrame(columns, index=table.index)
    return helper.sort_values(["Safe Universe Rank", "High Growth Potential Universe Rank", "Turnaround Story Universe Rank"], na_position="last")


def build_metric_registry(strategy_weights: dict[str, dict[str, float]]) -> pd.DataFrame:
    """Human-readable governance table for every available and unavailable calculation."""
    rows = []
    for metric, (family, direction, duplicate) in RAW_SIGNALS.items():
        if metric in DERIVED_FEATURES:
            continue
        treatment = "Target/risk policy" if direction == "target_range" else "Monotonic peer-relative score"
        rows.append({
            "Metric / Score": metric, "Source Type": "Raw source metric", "Raw Inputs": metric,
            "Formula / Transformation": "Source value; numeric cleaning; industry/sector shrinkage; robust percentile/z blend",
            "Units": "Source units", "Valid When": "Finite numeric value", "Category": family,
            "Economic Direction": direction, "Scoring Treatment": treatment,
            "Duplicate Exposure Group": duplicate,
            "Peer Relative": "YES" if direction in {"higher_better", "lower_better"} else "NO",
        })
    for metric, spec in DERIVED_FEATURES.items():
        raw_scoring = RAW_SIGNALS.get(metric)
        category = raw_scoring[0] if raw_scoring else spec.family
        direction = raw_scoring[1] if raw_scoring else spec.direction
        duplicate = raw_scoring[2] if raw_scoring else spec.duplicate_group
        rows.append({
            "Metric / Score": metric, "Source Type": "Derived metric", "Raw Inputs": " | ".join(spec.inputs),
            "Formula / Transformation": spec.formula, "Units": spec.units, "Valid When": spec.valid_when,
            "Category": category or "AUDIT / CONFIDENCE ONLY", "Economic Direction": direction,
            "Scoring Treatment": ("Monotonic peer-relative score" if category and direction in {"higher_better", "lower_better"}
                                  else "Audited but not independently scored"),
            "Duplicate Exposure Group": duplicate,
            "Peer Relative": "YES" if category and direction in {"higher_better", "lower_better"} else "NO",
        })
    policy_metrics = (
        ("liquidity_current_ratio", "Current Ratio", "LIQUIDITY_AND_EFFICIENCY",
         "Asymmetric peer-z low-tail distress policy; high liquidity is neutral", "Finite ratio; non-financial sector", "YES"),
        ("liquidity_quick_ratio", "Quick Ratio", "LIQUIDITY_AND_EFFICIENCY",
         "Asymmetric peer-z low-tail distress policy; high liquidity is neutral", "Finite ratio; non-financial sector", "YES"),
        ("liquidity_cash_ratio", "Cash Ratio", "LIQUIDITY_AND_EFFICIENCY",
         "Asymmetric peer-z low-tail distress policy; high liquidity is neutral", "Finite ratio; non-financial sector", "YES"),
        ("working_capital_efficiency", "working_capital_to_sales", "LIQUIDITY_AND_EFFICIENCY",
         "Neutral except for a declining score above the excessive-working-capital z threshold", "Finite ratio; non-financial sector", "YES"),
        ("payout_sustainability", "Dividend | 12 Mo Trailing EPS", "SHAREHOLDER_AND_YIELD",
         "Neutral through warning ratio; declines to zero at distress ratio; dividend with non-positive EPS scores zero", "Dividend/EPS inputs available", "NO"),
    )
    for metric, raw, category, formula, valid_when, peer_relative in policy_metrics:
        rows.append({
            "Metric / Score": metric, "Source Type": "Policy-adjusted metric", "Raw Inputs": raw,
            "Formula / Transformation": formula,
            "Units": "0–100 score", "Valid When": valid_when,
            "Category": category, "Economic Direction": "higher_better",
            "Scoring Treatment": "Downside distress penalty", "Duplicate Exposure Group": metric,
            "Peer Relative": peer_relative,
        })
    for metric, availability in METRIC_AVAILABILITY_REGISTRY.items():
        rows.append({
            "Metric / Score": metric, "Source Type": "Unavailable calculation", "Raw Inputs": " | ".join(availability.required_source_fields),
            "Formula / Transformation": availability.unavailable_reason, "Units": "N/A", "Valid When": availability.activation_condition,
            "Category": "NOT SCORED", "Economic Direction": "N/A", "Scoring Treatment": "Excluded",
            "Duplicate Exposure Group": "N/A", "Peer Relative": "NO",
            "Availability Status": availability.availability_status,
            "Missing Required Fields": " | ".join(availability.required_source_fields),
            "Calculation Function": availability.calculation_function_name,
            "Currently Active": False,
            "Affects Quant Score": availability.affects_quant_score,
            "Affects Underwriting Readiness": availability.affects_underwriting_readiness,
        })
    registry = pd.DataFrame(rows)
    for strategy, weights in strategy_weights.items():
        registry[f"{strategy} Weight"] = registry["Category"].map(weights).fillna(0.0)
        registry[f"{strategy} Influence"] = np.where(
            registry[f"{strategy} Weight"].gt(0), "Via category sub-score", "Not used"
        )
    registry["Audit Note"] = np.where(
        registry["Source Type"].eq("Unavailable calculation"),
        "Shown to document a known data limitation; never imputed or invented.",
        "Raw and all intermediate per-stock values are exposed on the industry helper tab.",
    )
    return registry.sort_values(["Source Type", "Category", "Metric / Score"], kind="stable").reset_index(drop=True)


def _metric_registry_values(strategy_weights: dict[str, dict[str, float]]) -> list[list[object]]:
    registry = build_metric_registry(strategy_weights)
    rows = [["METRIC REGISTRY — FORMULAS, DIRECTIONS AND STRATEGY USE"],
            ["Weights are category-level controls from the Control Panel; raw values and every intermediate calculation remain visible in each helper."],
            list(registry.columns)]
    rows += registry.fillna("NO DATA").values.tolist()
    return _sheet_values(rows)


def _helper_values(industry: str, table: pd.DataFrame, strategy_weights: dict[str, dict[str, float]]) -> list[list[object]]:
    audit = build_helper_table(table, strategy_weights)
    return _prepared_helper_values(industry, audit)


def _prepared_helper_values(industry: str, audit: pd.DataFrame) -> list[list[object]]:
    note = ("AUDIT: peer score = configured blend of shrunk percentile and capped robust-z mapping; correlated signals aggregate through groups into families. "
            "Raw score = sum(valid family weighted numerators) / applicable weight; published quant composite = 50 + confidence × (raw − 50). Strategy scores reuse the same audited family numerators with strategy weights.")
    rows = [[f"{industry} — STRATEGY AUDIT HELPER"], [note], list(audit.columns)]
    rows += audit.fillna("NO DATA").values.tolist()
    return _sheet_values(rows)


def _format_request(sheet_id: int, start_row: int, end_row: int, end_col: int, background, *, start_col=0, bold=False, font_size=10, foreground=None, wrap="WRAP"):
    text_format = {"bold": bold, "fontSize": font_size}
    if foreground:
        text_format["foregroundColorStyle"] = {"rgbColor": foreground}
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": start_row, "endRowIndex": end_row,
                  "startColumnIndex": start_col, "endColumnIndex": end_col},
        "cell": {"userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": background},
            "textFormat": text_format,
            "wrapStrategy": wrap,
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy,verticalAlignment)",
    }}


def _column_width_request(sheet_id: int, start_col: int, end_col: int, pixels: int):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": start_col, "endIndex": end_col},
        "properties": {"pixelSize": pixels},
        "fields": "pixelSize",
    }}


def _section_ranges(values: list[list[object]]) -> dict[str, tuple[int, int, int]]:
    """Return dynamic title/header/data boundaries for each industry section."""
    markers = {}
    for index, row in enumerate(values):
        if row and row[0] in {"QUANT LONG CANDIDATES", "QUANT SHORT CANDIDATES", "FULL INDUSTRY RANKING"}:
            markers[row[0]] = index
    ordered = ["QUANT LONG CANDIDATES", "QUANT SHORT CANDIDATES", "FULL INDUSTRY RANKING"]
    ranges = {}
    for position, marker in enumerate(ordered):
        title = markers[marker]
        next_title = markers[ordered[position + 1]] if position + 1 < len(ordered) else len(values) + 1
        # Every section has a blank separator immediately before the next title.
        data_end = max(title + 2, next_title - 1)
        ranges[marker] = (title, title + 1, data_end)
    return ranges


def _layout_requests(payloads, sheet_metadata):
    sheet_info = {
        sheet["properties"]["title"]: sheet
        for sheet in sheet_metadata.get("sheets", [])
    }
    cleanup, styles = [], []
    white = {"red": 1, "green": 1, "blue": 1}
    black = {"red": 0.12, "green": 0.12, "blue": 0.12}
    blue = {"red": 0.84, "green": 0.91, "blue": 0.97}
    gray = {"red": 0.91, "green": 0.92, "blue": 0.93}
    green_title = {"red": 0.72, "green": 0.88, "blue": 0.75}
    green_header = {"red": 0.85, "green": 0.95, "blue": 0.87}
    green_data = {"red": 0.95, "green": 0.99, "blue": 0.96}
    red_title = {"red": 0.96, "green": 0.72, "blue": 0.72}
    red_header = {"red": 0.98, "green": 0.86, "blue": 0.86}
    red_data = {"red": 1, "green": 0.96, "blue": 0.96}

    missing_sheets = [title for title, _, _ in payloads if title not in sheet_info]
    if missing_sheets:
        raise ValueError(f"Cannot format missing destination tabs: {missing_sheets}")

    for title, values, _ in payloads:
        info = sheet_info[title]
        props = info["properties"]
        sheet_id = props["sheetId"]
        used_rows = len(values)
        used_cols = max(len(row) for row in values)
        grid = props.get("gridProperties", {})
        current_rows = int(grid.get("rowCount", 0))
        current_cols = int(grid.get("columnCount", 0))
        # Generated tabs are owned by this writer.  Exact grids remove stale rows
        # and preserve enough workbook capacity for the complete helper audit.
        desired_rows = max(used_rows, 1)
        desired_cols = max(used_cols, 1)
        expanded_grid = {}
        expanded_fields = []
        if desired_rows != current_rows:
            expanded_grid["rowCount"] = desired_rows
            expanded_fields.append("gridProperties.rowCount")
        if desired_cols != current_cols:
            expanded_grid["columnCount"] = desired_cols
            expanded_fields.append("gridProperties.columnCount")
        if expanded_grid:
            cleanup.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": expanded_grid},
                "fields": ",".join(expanded_fields),
            }})
        cleanup.append({"unmergeCells": {"range": {
            "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": desired_rows,
            "startColumnIndex": 0, "endColumnIndex": desired_cols,
        }}})
        for _ in info.get("conditionalFormats", []):
            cleanup.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})

        styles.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": used_rows,
                      "startColumnIndex": 0, "endColumnIndex": used_cols},
            "cell": {"userEnteredFormat": {
                "backgroundColorStyle": {"rgbColor": white},
                "textFormat": {"bold": False, "italic": False, "foregroundColorStyle": {"rgbColor": black}},
                "wrapStrategy": "OVERFLOW_CELL",
            }},
            "fields": "userEnteredFormat(backgroundColorStyle,textFormat,numberFormat,wrapStrategy)",
        }})

        frozen_rows = 3 if title in {"Control Panel", "Metric Registry"} or title.endswith(" Summary") or title.endswith(" - Helper") else 2
        styles.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": frozen_rows}},
            "fields": "gridProperties.frozenRowCount",
        }})

        if title == "Control Panel":
            styles.append({"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                                        "startColumnIndex": 0, "endColumnIndex": min(4, used_cols)},
                                                   "mergeType": "MERGE_ALL"}})
            styles.append(_format_request(sheet_id, 0, 1, used_cols, blue, bold=True, font_size=14))
            styles.append(_format_request(sheet_id, 2, 3, used_cols, gray, bold=True))
            if used_rows > 3:
                styles.append(_format_request(sheet_id, 3, used_rows, used_cols, white))
                styles.append(_format_request(sheet_id, 3, used_rows, 3, {"red": 1, "green": 0.96, "blue": 0.80}, start_col=2))
            weight_header = next((i for i, row in enumerate(values) if row and row[0] == "Metric / Score"), None)
            total_row = next((i for i, row in enumerate(values) if row and row[0] == "Total Weight"), None)
            if weight_header is not None and total_row is not None:
                styles.append(_format_request(sheet_id, weight_header - 1, weight_header, used_cols, blue, bold=True))
                styles.append(_format_request(sheet_id, weight_header, weight_header + 1, used_cols, gray, bold=True))
                styles.append(_format_request(sheet_id, weight_header + 1, total_row, 4,
                                              {"red": 1, "green": 0.96, "blue": 0.80}, start_col=1))
                styles.append(_format_request(sheet_id, total_row, total_row + 1, used_cols, gray, bold=True))
                styles.append({"setDataValidation": {
                    "range": {"sheetId": sheet_id, "startRowIndex": weight_header + 1, "endRowIndex": total_row,
                              "startColumnIndex": 1, "endColumnIndex": 4},
                    "rule": {"condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "0"}]},
                             "strict": True, "showCustomUi": True},
                }})
        elif title == "Metric Registry":
            for merge_row in (0, 1):
                styles.append({"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": merge_row,
                                                            "endRowIndex": merge_row + 1, "startColumnIndex": 0,
                                                            "endColumnIndex": min(8, used_cols)},
                                                       "mergeType": "MERGE_ALL"}})
            styles.append(_format_request(sheet_id, 0, 1, used_cols, blue, bold=True, font_size=14))
            styles.append(_format_request(sheet_id, 1, 2, used_cols, white, font_size=10))
            styles.append(_format_request(sheet_id, 2, 3, used_cols, gray, bold=True))
            if used_rows > 3:
                styles.append(_format_request(sheet_id, 3, used_rows, used_cols, white))
        elif title in STRATEGY_SUMMARY_SHEETS:
            for merge_row in (0, 1):
                styles.append({"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": merge_row,
                                                            "endRowIndex": merge_row + 1, "startColumnIndex": 0,
                                                            "endColumnIndex": min(8, used_cols)},
                                                       "mergeType": "MERGE_ALL"}})
            styles.append(_format_request(sheet_id, 0, 1, used_cols, blue, bold=True, font_size=14))
            styles.append(_format_request(sheet_id, 1, 2, used_cols, white, font_size=10))
            styles.append(_format_request(sheet_id, 2, 3, used_cols, gray, bold=True))
            if used_rows > 3:
                styles.append(_format_request(sheet_id, 3, used_rows, used_cols, white))
                headers = values[2]
                for column, header in enumerate(headers):
                    if header in {"Universe Rank", "Within-Industry Rank", "Components Available", "Components Missing"}:
                        pattern = "0"
                    elif header == "Weight Coverage":
                        pattern = "0.0%"
                    elif header == "Market Cap (mil)":
                        pattern = "#,##0"
                    elif header.endswith(("Score", "Contribution")) or header in SUMMARY_METRICS.get(title.removesuffix(" Summary"), []):
                        pattern = "0.00"
                    else:
                        continue
                    styles.append({"repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 3, "endRowIndex": used_rows,
                                  "startColumnIndex": column, "endColumnIndex": column + 1},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                        "fields": "userEnteredFormat.numberFormat",
                    }})
        elif title.endswith(" - Helper"):
            for merge_row in (0, 1):
                styles.append({"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": merge_row,
                                                            "endRowIndex": merge_row + 1, "startColumnIndex": 0,
                                                            "endColumnIndex": min(8, used_cols)},
                                                       "mergeType": "MERGE_ALL"}})
            styles.append(_format_request(sheet_id, 0, 1, used_cols, blue, bold=True, font_size=14))
            styles.append(_format_request(sheet_id, 1, 2, used_cols, white, font_size=10))
            styles.append(_format_request(sheet_id, 2, 3, used_cols, gray, bold=True, wrap="WRAP"))
            if used_rows > 3:
                styles.append(_format_request(sheet_id, 3, used_rows, used_cols, white, wrap="OVERFLOW_CELL"))
        else:
            styles.append(_format_request(sheet_id, 0, 1, used_cols, blue, bold=True, font_size=14))
            styles.append(_format_request(sheet_id, 1, 2, used_cols, gray, bold=True))
            styles.append(_format_request(sheet_id, 2, _section_ranges(values)["QUANT LONG CANDIDATES"][0] - 1, 1, white, bold=True))
            section_styles = {
                "QUANT LONG CANDIDATES": (green_title, green_header, green_data),
                "QUANT SHORT CANDIDATES": (red_title, red_header, red_data),
                "FULL INDUSTRY RANKING": (blue, gray, white),
            }
            for marker, (title_color, header_color, data_color) in section_styles.items():
                title_row, header_row, data_end = _section_ranges(values)[marker]
                styles.append(_format_request(sheet_id, title_row, title_row + 1, used_cols, title_color, bold=True))
                styles.append(_format_request(sheet_id, header_row, header_row + 1, used_cols, header_color, bold=True))
                if data_end > header_row + 1:
                    styles.append(_format_request(sheet_id, header_row + 1, data_end, used_cols, data_color))

        # Auto-fit every populated column on every run, then cap verbose text fields
        # so one long company name or note cannot make the sheet awkward to scan.
        if not title.endswith(" - Helper"):
            styles.append({"autoResizeDimensions": {"dimensions": {
                "sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": used_cols,
            }}})
        if title == "Control Panel":
            caps = [(0, 1, 190), (1, 2, 220), (2, 3, 160), (3, 4, 360)]
        elif title == "Metric Registry":
            caps = [(0, 1, 230), (1, 3, 260), (3, 4, 360), (4, 11, 150),
                    (11, used_cols, 130)]
        elif title in STRATEGY_SUMMARY_SHEETS:
            caps = [(0, 2, 120), (2, 4, 210), (4, 8, 140), (used_cols - 2, used_cols - 1, 260),
                    (used_cols - 1, used_cols, 420)]
        elif title.endswith(" - Helper"):
            caps = [(0, 1, 90), (1, 2, 90), (2, 3, 210), (3, used_cols, 115)]
        else:
            caps = [(0, 1, 230), (1, 2, 220), (4, 5, 170), (5, 6, 220),
                    (6, 17, 135), (17, 18, 280), (18, 19, 150)]
        for start_col, end_col, pixels in caps:
            if start_col < used_cols:
                styles.append(_column_width_request(sheet_id, start_col, min(end_col, used_cols), pixels))
    return cleanup, styles


def _run_request_chunks(book, requests, size: int = 400):
    for start in range(0, len(requests), size):
        chunk = requests[start:start + size]
        _retry_google_request(lambda chunk=chunk: book.batch_update({"requests": chunk}))


STRATEGY_SUMMARY_SHEETS = ("Safe Summary", "High Growth Potential Summary", "Turnaround Story Summary")
SYSTEM_SHEETS = ("Dataset", "Control Panel", "Metric Registry", *STRATEGY_SUMMARY_SHEETS)


def ensure_analysis_sheets(book, industries, sheet_metadata, helper_column_count: int = 900, industry_sizes: dict[str, int] | None = None):
    """Create generated sheets once, remove deprecated summaries, and order tabs."""
    industries = [str(x) for x in industries]
    industry_sizes = industry_sizes or {}
    existing = {s["properties"]["title"]: s for s in sheet_metadata.get("sheets", [])}
    requests = []
    allocated_cells = sum(
        int(s["properties"].get("gridProperties", {}).get("rowCount", 0))
        * int(s["properties"].get("gridProperties", {}).get("columnCount", 0))
        for s in sheet_metadata.get("sheets", [])
    )
    for deprecated in ("Primary Summary", "Secondary Summary"):
        if deprecated in existing:
            props = existing[deprecated]["properties"].get("gridProperties", {})
            allocated_cells -= int(props.get("rowCount", 0)) * int(props.get("columnCount", 0))
            requests.append({"deleteSheet": {"sheetId": existing[deprecated]["properties"]["sheetId"]}})
    if "Metric Registry" not in existing:
        allocated_cells += 150 * 20
        requests.append({"addSheet": {"properties": {
            "title": "Metric Registry", "gridProperties": {"rowCount": 150, "columnCount": 20}
        }}})
    for title in STRATEGY_SUMMARY_SHEETS:
        if title not in existing:
            allocated_cells += 60 * 40
            requests.append({"addSheet": {"properties": {
                "title": title, "gridProperties": {"rowCount": 60, "columnCount": 40}
            }}})
    for industry in industries:
        title = helper_sheet_name(industry)
        if title not in existing:
            row_count = max(10, int(industry_sizes.get(industry, 0)) + 3)
            allocated_cells += row_count * max(30, helper_column_count)
            requests.append({"addSheet": {"properties": {
                "title": title,
                "gridProperties": {"rowCount": row_count, "columnCount": max(30, helper_column_count)},
            }}})
    if allocated_cells > 9_800_000:
        raise RuntimeError(
            f"Projected workbook grid allocation is {allocated_cells:,} cells; refusing to approach the 10,000,000-cell cap"
        )
    _run_request_chunks(book, requests, size=100)
    metadata = _retry_google_request(book.fetch_sheet_metadata)
    titles_in_order = [s["properties"]["title"] for s in metadata["sheets"]]
    industry_set = set(industries)
    industry_order = [title for title in titles_in_order if title in industry_set]
    industry_order += sorted(industry_set - set(industry_order))
    paired = [name for industry in industry_order for name in (industry, helper_sheet_name(industry))]
    generated = set(paired) | set(SYSTEM_SHEETS) | {"Primary Summary", "Secondary Summary"}
    other_system = [title for title in titles_in_order if title not in generated and title not in industry_set]
    desired = [title for title in SYSTEM_SHEETS if title in titles_in_order or title in STRATEGY_SUMMARY_SHEETS]
    desired += other_system + paired
    reorder = []
    by_title = {s["properties"]["title"]: s["properties"]["sheetId"] for s in metadata["sheets"]}
    for title in reversed([t for t in desired if t in by_title]):
        reorder.append({"updateSheetProperties": {
            "properties": {"sheetId": by_title[title], "index": 0}, "fields": "index"
        }})
    _run_request_chunks(book, reorder, size=300)
    return _retry_google_request(book.fetch_sheet_metadata)


def validate_generated_topology(sheet_metadata, industries) -> None:
    titles = [s["properties"]["title"] for s in sheet_metadata.get("sheets", [])]
    for summary in STRATEGY_SUMMARY_SHEETS:
        if titles.count(summary) != 1:
            raise RuntimeError(f"Expected exactly one {summary!r}; found {titles.count(summary)}")
    if titles.count("Metric Registry") != 1:
        raise RuntimeError(f"Expected exactly one 'Metric Registry'; found {titles.count('Metric Registry')}")
    for deprecated in ("Primary Summary", "Secondary Summary"):
        if deprecated in titles:
            raise RuntimeError(f"Deprecated {deprecated} still exists")
    for industry in map(str, industries):
        helper = helper_sheet_name(industry)
        if titles.count(helper) != 1:
            raise RuntimeError(f"Expected exactly one helper for {industry!r}; found {titles.count(helper)}")
        if titles.index(helper) != titles.index(industry) + 1:
            raise RuntimeError(f"Helper {helper!r} is not immediately after {industry!r}")


def _payload_chunks(payloads, max_cells: int = 150_000):
    chunk, cells = [], 0
    for payload in payloads:
        payload_cells = len(payload[1]) * max(len(row) for row in payload[1])
        if chunk and cells + payload_cells > max_cells:
            yield chunk
            chunk, cells = [], 0
        chunk.append(payload)
        cells += payload_cells
    if chunk:
        yield chunk


def write_analysis_batch(
    *, book, control_rows: list[list[object]], strategy_rows: list[list[object]],
    strategy_weights: dict[str, dict[str, float]], quant_weights: dict[str, float],
    industry_tables, all_rows: pd.DataFrame,
    sheet_metadata, strategy_summary_top_n: int, min_strategy_score: float,
) -> int:
    """Write and verify the complete live analysis using quota-efficient batch calls."""
    groups = [(str(industry), table.copy()) for industry, table in industry_tables]
    payloads = [("Control Panel", _control_panel_values(control_rows, strategy_rows), "CONTROL PANEL — EDIT VALUE COLUMN, THEN RERUN")]
    payloads.append(("Metric Registry", _metric_registry_values(strategy_weights),
                     "METRIC REGISTRY — FORMULAS, DIRECTIONS AND STRATEGY USE"))
    payloads += [(industry, _industry_values(table), "INDUSTRY DIAGNOSTIC SUMMARY") for industry, table in groups]
    if groups:
        prepared_helper = build_helper_table(all_rows, strategy_weights)
        payloads += [
            (helper_sheet_name(industry), _prepared_helper_values(industry, prepared_helper.loc[prepared_helper["Industry"].eq(industry)]),
             f"{industry} — STRATEGY AUDIT HELPER")
            for industry, _ in groups
        ]
    for strategy, title in zip(("Safe", "High Growth Potential", "Turnaround Story"), STRATEGY_SUMMARY_SHEETS):
        payloads.append((title, _strategy_summary_values(
            all_rows, strategy, strategy_weights[strategy], strategy_summary_top_n, min_strategy_score
        ), f"{strategy.upper()} — CROSS-UNIVERSE STOCK SELECTION"))

    current_cells = sum(
        int(s["properties"].get("gridProperties", {}).get("rowCount", 0))
        * int(s["properties"].get("gridProperties", {}).get("columnCount", 0))
        for s in sheet_metadata.get("sheets", [])
    )
    metadata_by_title = {s["properties"]["title"]: s["properties"] for s in sheet_metadata.get("sheets", [])}
    projected_cells = current_cells
    for title, values, _ in payloads:
        props = metadata_by_title[title]
        grid = props.get("gridProperties", {})
        projected_cells -= int(grid.get("rowCount", 0)) * int(grid.get("columnCount", 0))
        projected_cells += len(values) * max(len(row) for row in values)
    if projected_cells > 9_800_000:
        raise RuntimeError(
            f"Projected workbook grid allocation is {projected_cells:,} cells; refusing to approach the 10,000,000-cell cap"
        )

    cleanup_requests, style_requests = _layout_requests(payloads, sheet_metadata)
    _run_request_chunks(book, cleanup_requests)

    sheet_info = {sheet["properties"]["title"]: sheet["properties"] for sheet in sheet_metadata["sheets"]}
    clear_ranges = []
    for sheet, values, _ in payloads:
        column_count = max(len(row) for row in values)
        last_column = "".join(filter(str.isalpha, rowcol_to_a1(1, column_count)))
        clear_ranges.append(absolute_range_name(sheet, f"A:{last_column}"))
    for start in range(0, len(clear_ranges), 100):
        ranges = clear_ranges[start:start + 100]
        _retry_google_request(lambda ranges=ranges: book.values_batch_clear(body={"ranges": ranges}))
    for chunk in _payload_chunks(payloads):
        data = [
            {"range": absolute_range_name(sheet, "A1"), "majorDimension": "ROWS", "values": values}
            for sheet, values, _ in chunk
        ]
        _retry_google_request(lambda data=data: book.values_batch_update(body={"valueInputOption": "RAW", "data": data}))
    _run_request_chunks(book, style_requests)

    for start in range(0, len(payloads), 100):
        payload_chunk = payloads[start:start + 100]
        verification = _retry_google_request(lambda payload_chunk=payload_chunk: book.values_batch_get(
            [absolute_range_name(sheet, "A1") for sheet, _, _ in payload_chunk]
        ))
        returned = verification.get("valueRanges", [])
        if len(returned) != len(payload_chunk):
            raise RuntimeError(f"Batch read-back returned {len(returned)} of {len(payload_chunk)} requested tabs")
        for (sheet, _, expected), result in zip(payload_chunk, returned):
            values = result.get("values", [])
            actual = values[0][0] if values and values[0] else None
            if actual != expected:
                raise RuntimeError(f"Read-back verification failed for {sheet}: expected {expected!r}, got {actual!r}")
    return len(payloads)


def reconcile_tabs(industries, destination_tabs, protected=("Dataset", "Control Panel", "Metric Registry", "Primary Summary", "Secondary Summary", "Industry Summary", "USA Summary", *STRATEGY_SUMMARY_SHEETS)) -> TabManifest:
    industries, tabs = set(industries), set(destination_tabs)
    helper_tabs = {helper_sheet_name(industry) for industry in industries}
    return TabManifest(sorted(industries & tabs), sorted(industries - tabs), sorted(tabs - industries - helper_tabs - set(protected)))


def validate_destination(source_id: str, destination_id: str | None, allow_same: bool) -> None:
    if not destination_id:
        raise ValueError("DESTINATION_SPREADSHEET_ID is required before any Google write")
    if source_id == destination_id and not allow_same:
        raise ValueError("Destination equals source; set ALLOW_SAME_SOURCE_AND_DESTINATION only after explicit approval")
def build_quant_summary(all_rows: pd.DataFrame) -> pd.DataFrame:
    """One compact summary row per selected direction and security, grouped by industry."""
    columns = [
        "Industry", "Quantitative Candidate", "Direction Rank", "Ticker", "Company Name", "Sector",
        "Exchange", "COM/ADR/Canadian", "Quant Direction Score", "Raw Direction Score",
        "Score Confidence", "Independent Group Coverage", "Expanded Family Coverage",
        "Safe Score", "High Growth Potential Score", "Turnaround Story Score",
        "Positive Peer Evidence Count", "Negative Peer Evidence Count",
        "Warning / Risk Flags", "Implementation Feasibility Note", "Data Status",
        "Quantitative Reason",
    ]
    selected = all_rows.loc[all_rows["Quantitative Candidate"].ne("")].copy()
    if selected.empty:
        return pd.DataFrame(columns=columns)
    selected["Direction Rank"] = selected["Expanded Long Rank"].where(
        selected["Quantitative Candidate"].eq("QUANT LONG"), selected["Expanded Short Rank"]
    )
    is_long = selected["Quantitative Candidate"].eq("QUANT LONG")
    selected["Quant Direction Score"] = selected["Expanded Long Score"].where(is_long, selected["Expanded Short Score"])
    raw_long = selected["Expanded Raw Long Score"] if "Expanded Raw Long Score" in selected else selected["Expanded Long Score"]
    raw_short = selected["Expanded Raw Short Score"] if "Expanded Raw Short Score" in selected else selected["Expanded Short Score"]
    selected["Raw Direction Score"] = raw_long.where(is_long, raw_short)
    selected["Warning / Risk Flags"] = selected.apply(_risk_flags, axis=1)
    selected["Quantitative Reason"] = selected.apply(_quant_composite_reason, axis=1)
    columns = [c for c in columns if c in selected]
    return selected[columns].sort_values(["Industry", "Quantitative Candidate", "Direction Rank"])


def build_industry_summary(all_rows: pd.DataFrame) -> pd.DataFrame:
    """Decision-useful industry diagnostics, one row per industry."""
    rows = []
    for industry, group in all_rows.groupby("Industry", sort=True):
        long_top = group.sort_values("Expanded Long Score", ascending=False).iloc[0]
        short_top = group.sort_values("Expanded Short Score", ascending=False).iloc[0]
        rows.append({
            "Industry": industry,
            "Sector": group["Sector"].mode().iloc[0] if group["Sector"].notna().any() else "NO DATA",
            "Eligible Stocks": int(len(group)),
            "Sufficient Data Stocks": int(group["Data Status"].eq("SUFFICIENT").sum()),
            "Quant Longs": int(group["Quantitative Candidate"].eq("QUANT LONG").sum()),
            "Quant Shorts": int(group["Quantitative Candidate"].eq("QUANT SHORT").sum()),
            "Median Long Score": group["Expanded Long Score"].median(),
            "Median Short Score": group["Expanded Short Score"].median(),
            "Median Net Score": group["Expanded Net Quant Score"].median(),
            "Net Score Std": group["Expanded Net Quant Score"].std(ddof=0),
            "Average Confidence": group["Score Confidence"].mean(),
            "Top Long Ticker": long_top.get("Ticker", "NO DATA"),
            "Top Long Score": long_top.get("Expanded Long Score"),
            "Top Short Ticker": short_top.get("Ticker", "NO DATA"),
            "Top Short Score": short_top.get("Expanded Short Score"),
            "Liquidity Distress Flags": int(sum(group.get(c, pd.Series(False, index=group.index)).fillna(False).sum() for c in (
                "liquidity_current_ratio__distress_flag", "liquidity_quick_ratio__distress_flag", "liquidity_cash_ratio__distress_flag"
            ))),
            "Excess Working Capital Flags": int(group.get("Excessive Working Capital Flag", pd.Series(False, index=group.index)).fillna(False).sum()),
            "Financial Liquidity Exclusions": int(group.get("Financial Company Liquidity Exclusion", pd.Series(False, index=group.index)).fillna(False).sum()),
            "Optionable Count (Feasibility Only)": int(group.get("Optionability Implementation Flag", pd.Series(False, index=group.index)).fillna(False).sum()),
        })
    return pd.DataFrame(rows)
