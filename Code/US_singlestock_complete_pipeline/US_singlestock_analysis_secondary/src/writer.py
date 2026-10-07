from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from gspread.utils import rowcol_to_a1

import config
from src.shorts import SHORT_METRICS, SHORT_WEIGHTS


def _scalar(value: Any) -> Any:
    if value is None or (isinstance(value, float) and not np.isfinite(value)) or pd.isna(value):
        return "NO DATA"
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return value


def frame_values(frame: pd.DataFrame, title: str, note: str) -> list[list[Any]]:
    rows = [[title], [note], list(frame.columns)]
    rows += [[_scalar(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    return rows


def write_handoff_tabs(book, worksheets: dict[str, Any], ready: pd.DataFrame,
                       watchlist: pd.DataFrame, short_status: str = "",
                       research: pd.DataFrame | None = None) -> list[str]:
    """Publish the owned decision tables and verify every populated cell."""
    short_note = (" Short model unavailable: current price/estimate-change signals are missing."
                  if short_status == "UNAVAILABLE_MISSING_CURRENT_SIGNALS" else
                  " Primary Short signal capture date is unverified."
                  if short_status == "RESEARCH_ONLY_PIT_AND_SOURCE_DATE_UNVERIFIED" else "")
    definitions = [
        ("Qualified Handoff", ready, f"{len(ready)} top-ranked names have completed the external data review. "
         "An empty table means source provenance, Primary timing or current TTM evidence remains incomplete." + short_note),
        ("Failed Watchlist", watchlist, f"{len(watchlist)} primary candidates need external data review. "
         "The table does not apply model pass/fail gates." + short_note),
    ]
    if research is not None:
        current_data = int(research["Research Status"].eq("CURRENT-DATA RESEARCH").sum())
        definitions.append(("Research Candidates", research,
                            f"{len(research)} top-ranked category candidates; {current_data} have a traceable, "
                            "integrity-clean current annual source with complete latest essential fields. "
                            "Research only: Primary vendor point-in-time status and current TTM remain unverified." + short_note))
    updated = []
    for title, frame, note in definitions:
        marker = f"{title.upper()} — SECONDARY DECISION TABLE"
        values = frame_values(frame, marker, note)
        width = max(map(len, values))
        height = len(values)
        padded = [row + [""] * (width - len(row)) for row in values]
        ws = worksheets.get(title)
        if ws is None:
            ws = book.add_worksheet(title=title, rows=max(4, height), cols=width)
            worksheets[title] = ws
        elif ws.acell("A1").value != marker:
            raise ValueError(f"Refusing to replace non-generated tab {title!r}")
        ws.resize(rows=max(4, height), cols=width)
        ws.update(padded, "A1", value_input_option="RAW")
        requests = [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 3}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 2, "endRowIndex": 3},
                            "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .9, "blue": .95},
                                                          "textFormat": {"bold": True}, "wrapStrategy": "WRAP"}},
                            "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                 "startIndex": 9, "endIndex": 10},
                                           "properties": {"pixelSize": 460}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                 "startIndex": 14, "endIndex": 15},
                                           "properties": {"pixelSize": 560}, "fields": "pixelSize"}},
        ]
        if height > 3:
            requests.append({"setBasicFilter": {"filter": {"range": {"sheetId": ws.id,
                                                                      "startRowIndex": 2, "endRowIndex": height,
                                                                      "endColumnIndex": width}}}})
        book.batch_update({"requests": requests})
        actual = ws.get(f"A1:{rowcol_to_a1(height, width)}", value_render_option="UNFORMATTED_VALUE")
        actual = [list(row) + [""] * (width - len(row)) for row in actual]
        if actual != padded:
            raise RuntimeError(f"Full-cell read-back mismatch in {title}")
        updated.append(title)
    return updated


def write_current_evidence_tab(book, worksheets: dict[str, Any], evidence: pd.DataFrame) -> str:
    """Publish dated source evidence with a marker guard and complete read-back."""
    title = "Current Evidence"
    marker = "CURRENT EVIDENCE — SECONDARY DECISION TABLE"
    note = (f"{len(evidence)} model-qualified category entries, "
            f"{int(evidence['Selected in Summary'].eq('YES').sum())} selected for the capped Summary tabs; "
            f"{int(evidence['Current Evidence Status'].eq('CURRENT_REVIEW_EVIDENCE_COMPLETE').sum())} "
            "have complete current research evidence. SEC accepted filings and completed daily market bars; "
            "manual review required before any order. Historical model performance is unverified.")
    values = frame_values(evidence, marker, note)
    width, height = len(evidence.columns), len(values)
    padded = [row + [""] * (width - len(row)) for row in values]
    ws = worksheets.get(title)
    if ws is None:
        ws = book.add_worksheet(title=title, rows=max(4, height), cols=width)
        worksheets[title] = ws
    elif ws.acell("A1").value != marker:
        raise ValueError(f"Refusing to replace non-generated tab {title!r}")
    ws.resize(rows=max(4, height), cols=width)
    ws.update(padded, "A1", value_input_option="RAW")
    requests = [
        {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 3}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 2, "endRowIndex": 3},
                        "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .9, "blue": .95},
                                                      "textFormat": {"bold": True}, "wrapStrategy": "WRAP"}},
                        "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                 "startIndex": 4, "endIndex": 5},
                                       "properties": {"pixelSize": 650}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                 "startIndex": 7, "endIndex": 8},
                                       "properties": {"pixelSize": 360}, "fields": "pixelSize"}},
    ]
    if height > 3:
        requests.append({"setBasicFilter": {"filter": {"range": {"sheetId": ws.id,
                                                                  "startRowIndex": 2, "endRowIndex": height,
                                                                  "endColumnIndex": width}}}})
    book.batch_update({"requests": requests})
    actual = ws.get(f"A1:{rowcol_to_a1(height, width)}", value_render_option="UNFORMATTED_VALUE")
    actual = [list(row) + [""] * (width - len(row)) for row in actual]
    if actual != padded:
        raise RuntimeError(f"Full-cell read-back mismatch in {title}")
    return title


def delete_retired_output_tabs(book, worksheets: dict[str, Any]) -> list[str]:
    """Remove legacy qualification/review tabs from the published workbook."""
    retired = ("Qualified Handoff", "Failed Watchlist", "Research Candidates", "Current Evidence")
    deleted = []
    for title in retired:
        worksheet = worksheets.get(title)
        if worksheet is None:
            continue
        book.del_worksheet(worksheet)
        worksheets.pop(title, None)
        deleted.append(title)
    return deleted

def short_process_audit_values(
    primary_shortlist: pd.DataFrame,
    short_analysis: pd.DataFrame,
    short_summary: pd.DataFrame,
    controls: dict[str, Any],
) -> list[list[Any]]:
    """Build the per-primary-candidate audit columns shown beside the live import."""
    primary = primary_shortlist.copy()
    primary["Ticker"] = primary["Ticker"].fillna("").astype(str).str.strip().str.upper()
    primary["Primary Short Rank"] = pd.to_numeric(primary["Primary Short Rank"], errors="coerce")
    primary = primary.sort_values("Primary Short Rank", kind="stable")

    analysis = short_analysis.copy()
    analysis["Ticker"] = analysis["Ticker"].fillna("").astype(str).str.strip().str.upper()
    if analysis["Ticker"].duplicated().any():
        raise ValueError("Short audit: duplicate tickers in secondary analysis")
    by_ticker = analysis.set_index("Ticker", drop=False).to_dict(orient="index")
    missing = [ticker for ticker in primary["Ticker"] if ticker not in by_ticker]
    if missing:
        raise ValueError(f"Short audit: missing secondary rows for primary candidates {missing}")
    selected = set(short_summary.get("Ticker", pd.Series(dtype=object)).fillna("").astype(str).str.upper())

    headers = [
        "Secondary Ticker", "Primary Short Rank", "Primary Company Name", "Primary Industry",
        "Primary Short Score", "Primary 4-Week Price Change (%)", "Primary 12-Week Price Change (%)",
        "Primary F1 Estimate Change 4-Week (%)", "Primary F2 Estimate Change 4-Week (%)",
        "Primary Short Signal Source", "Primary Dataset As Of Note",
        "History Source Tabs", "Fiscal Years Found", "Trend Observations",
        "Latest Fiscal Year", "Latest Fiscal Period End", "Max Fiscal Year Gap",
        "Financial Age Days", "Source Error Count",
    ]
    for family, metrics in SHORT_METRICS.items():
        for metric in metrics:
            headers.extend([f"{metric} Input", f"{metric} Score"])
            if metric.endswith("_four_year_trend"):
                headers.extend([f"{metric} Observations", f"{metric} R Squared"])
    for family in SHORT_METRICS:
        family_key = family.upper()
        headers.extend([
            f"{family_key} Family Score", f"{family_key} Metric Coverage", f"{family_key} Weight",
            f"{family_key} Raw Score Contribution", f"{family_key} Final Points Contribution",
        ])
    headers.extend([
        "Applicable Family Weight", "Overall Family Weight Coverage", "Raw Short Score",
        "Trend Reliability", "Final Short Score", "Four-Year Adverse Observations",
        "Four-Year Adverse Breadth", "Recent Adverse Observations", "Recent Adverse Breadth",
        "Weak Fundamental Domains",
    ])
    headers.extend(["Secondary Rank", "Top Ranked Display", "Data Status"])

    rows: list[list[Any]] = [headers]
    primary_by_ticker = primary.set_index("Ticker", drop=False).to_dict(orient="index")
    for ticker in primary["Ticker"]:
        item = by_ticker[ticker]
        primary_item = primary_by_ticker[ticker]

        def value(column: str) -> Any:
            return _scalar(item.get(column))

        row: list[Any] = [ticker]
        for column in (
            "Primary Short Rank", "Primary Company Name", "Primary Industry", "Primary Short Score",
            "Primary 4-Week Price Change (%)", "Primary 12-Week Price Change (%)",
            "Primary F1 Estimate Change 4-Week (%)", "Primary F2 Estimate Change 4-Week (%)",
            "Primary Short Signal Source", "Primary Dataset As Of Note",
        ):
            row.append(_scalar(primary_item.get(column)))
        for column in (
            "History Source Tabs", "history_years", "trend_observations", "latest_fiscal_year",
            "latest_fiscal_period_end", "max_fiscal_year_gap", "financial_age_days", "source_error_count",
        ):
            row.append(value(column))

        for family, metrics in SHORT_METRICS.items():
            for metric in metrics:
                row.extend([value(metric), value(f"SHORT_{metric}_score")])
                if metric.endswith("_four_year_trend"):
                    row.extend([value(f"{metric}_observations"), value(f"{metric}_r2")])

        family_scores: dict[str, float] = {}
        family_weights: dict[str, float] = {}
        for family in SHORT_METRICS:
            score_column = f"SHORT_{family.upper()}_score"
            score = pd.to_numeric(pd.Series([item.get(score_column)]), errors="coerce").iloc[0]
            weight = float(controls[SHORT_WEIGHTS[family]])
            if weight > 0 and pd.notna(score):
                family_scores[family] = float(score)
                family_weights[family] = weight
        applicable_weight = sum(family_weights.values())
        reliability = pd.to_numeric(pd.Series([item.get("short_trend_reliability")]), errors="coerce").iloc[0]
        reliability = float(reliability) if pd.notna(reliability) else float("nan")
        for family in SHORT_METRICS:
            family_key = family.upper()
            score = family_scores.get(family, float("nan"))
            weight = float(controls[SHORT_WEIGHTS[family]])
            raw_contribution = score * weight / applicable_weight if applicable_weight and pd.notna(score) else float("nan")
            final_contribution = ((score - 50.0) * weight / applicable_weight * reliability
                                  if applicable_weight and pd.notna(score) and pd.notna(reliability) else float("nan"))
            row.extend([
                _scalar(item.get(f"SHORT_{family_key}_score")),
                _scalar(item.get(f"SHORT_{family_key}_coverage")),
                weight,
                _scalar(raw_contribution),
                _scalar(final_contribution),
            ])

        row.extend([
            _scalar(applicable_weight if applicable_weight else float("nan")),
            value("short_weight_coverage"), value("short_raw_score"), value("short_trend_reliability"),
            value("short_score"), value("short_adverse_trend_observations"),
            value("short_adverse_trend_breadth"), value("short_adverse_recent_observations"),
            value("short_adverse_recent_breadth"), value("short_weak_fundamental_domains"),
        ])
        rank = pd.to_numeric(pd.Series([item.get("short_rank")]), errors="coerce").iloc[0]
        if pd.notna(rank):
            data_status = "Rankable four-year score"
        elif pd.to_numeric(pd.Series([item.get("history_years")]), errors="coerce").iloc[0] < 4:
            data_status = "Insufficient annual history"
        elif pd.to_numeric(pd.Series([item.get("source_error_count")]), errors="coerce").iloc[0] > 0:
            data_status = "Source error recorded"
        else:
            data_status = "Score unavailable"
        row.extend([
            value("short_rank"),
            "YES" if ticker in selected else "NO",
            data_status,
        ])
        rows.append(row)
    return rows


def write_short_process_sheet(
    book,
    worksheet,
    primary_shortlist: pd.DataFrame,
    short_analysis: pd.DataFrame,
    short_summary: pd.DataFrame,
    controls: dict[str, Any],
    as_of: str,
    short_note: str,
    audit_values: list[list[Any]] | None = None,
) -> None:
    """Show the live primary Short import and every secondary decision stage on one tab."""
    source_width = int(primary_shortlist.attrs.get("source_column_count", 50))
    ticker_column = int(primary_shortlist.attrs.get("source_ticker_column", 2))
    rank_column = int(primary_shortlist.attrs.get("source_rank_column", 1))
    source_end_col = rowcol_to_a1(1, source_width).rstrip("1")
    audit_start_col = rowcol_to_a1(1, source_width + 1).rstrip("1")
    audit_values = audit_values or short_process_audit_values(primary_shortlist, short_analysis, short_summary, controls)
    audit_headers, audit_rows = audit_values[0], audit_values[1:]
    total_width = source_width + len(audit_headers)
    last_col = rowcol_to_a1(1, total_width).rstrip("1")
    table_end_row = 5 + len(audit_rows)
    existing_row_count = int(worksheet.row_count)
    existing_width = int(worksheet.col_count)
    row_count = max(existing_row_count, table_end_row + 5)

    primary = primary_shortlist.copy()
    primary["Ticker"] = primary["Ticker"].fillna("").astype(str).str.strip().str.upper()
    primary = primary.sort_values("Primary Short Rank", kind="stable")
    tickers = primary["Ticker"].tolist()
    history_count = int(pd.to_numeric(short_analysis.get("history_years"), errors="coerce").fillna(0).gt(0).sum())
    selected_tickers = short_summary.get("Ticker", pd.Series(dtype=object)).fillna("").astype(str).str.upper().tolist()
    weight_summary = ", ".join(
        f"{family.replace('_', ' ').title()} {float(controls[key]):g}%"
        for family, key in SHORT_WEIGHTS.items()
    )
    status = (
        f"Top ranked research names (maximum {int(controls['SHORT_TOP_N'])}): {', '.join(selected_tickers) if selected_tickers else 'NONE'}"
        f" | Primary candidates: {len(tickers)} | With annual history: {history_count}"
        f" | Rankable scores: {int(pd.to_numeric(short_analysis.get('short_rank'), errors='coerce').notna().sum())} | Displayed: {len(selected_tickers)} | As of: {as_of}"
        f" | Weights: {weight_summary}"
    )
    note = (
        f"{short_note} Primary columns A:{source_end_col} are a live IMPORTRANGE from the primary Short tab. "
        "The separate Short Data, Short Analysis, and Short Secondary Summary tabs show the filtered fiscal inputs, "
        "candidate audit, and top ranked research names. Secondary audit columns to the right reflect this run and refresh when rerun."
    )
    source_tab = config.PRIMARY_SHORT_TAB.replace("'", "''")
    import_range = f"'{source_tab}'!A3:{source_end_col}"
    formula = (
        f'=QUERY(IMPORTRANGE("{config.PRIMARY_SHORT_SPREADSHEET_ID}","{import_range}"),'
        f'"select * where Col{rank_column} is not null",1)'
    )
    last_used_row = max(5, table_end_row)
    old_width = max(existing_width, total_width)
    book.batch_update({"requests": [
        {
            "unmergeCells": {"range": {
                "sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": existing_row_count,
                "startColumnIndex": 0, "endColumnIndex": existing_width,
            }}
        },
        {"clearBasicFilter": {"sheetId": worksheet.id}},
    ]})
    book.batch_update({"requests": [{
        "updateSheetProperties": {
            "properties": {"sheetId": worksheet.id, "gridProperties": {
                "rowCount": row_count, "columnCount": old_width,
                "frozenRowCount": 5, "frozenColumnCount": 2,
            }},
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)",
        }
    }]})
    old_last_col = rowcol_to_a1(1, old_width).rstrip("1")
    book.values_batch_clear({"ranges": [f"'Short'!A1:{old_last_col}{row_count}"]})
    book.values_batch_update({"valueInputOption": "USER_ENTERED", "data": [
        {"range": "'Short'!A1:A4", "majorDimension": "ROWS", "values": [
            ["SHORT"], ["SOURCE"], ["RESULT"], ["FROZEN IDENTIFIERS"],
        ]},
        {"range": "'Short'!C1:C4", "majorDimension": "ROWS", "values": [
            ["Primary selection → four-year secondary Short screener"], [note], [status], ["PRIMARY SHORT ANALYSIS"],
        ]},
        {"range": "'Short'!A5", "values": [[formula]]},
        {"range": f"'Short'!{audit_start_col}4", "majorDimension": "ROWS", "values": [
            ["SECONDARY FOUR-YEAR SCREEN — PER-CANDIDATE AUDIT"], audit_headers, *audit_rows,
        ]},
    ]})

    requests = [
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 2, "endColumnIndex": total_width}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": 2,
                                  "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": 2,
                                  "startColumnIndex": 2, "endColumnIndex": total_width}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 2, "endRowIndex": 3,
                                  "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 2, "endRowIndex": 3,
                                  "startColumnIndex": 2, "endColumnIndex": total_width}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 3, "endRowIndex": 4,
                                  "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 3, "endRowIndex": 4,
                                  "startColumnIndex": 2, "endColumnIndex": source_width}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 3, "endRowIndex": 4,
                                  "startColumnIndex": source_width, "endColumnIndex": total_width}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": .10, "green": .24, "blue": .40},
                                                         "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontFamily": "Arial", "bold": True, "fontSize": 10},
                                                         "verticalAlignment": "BOTTOM", "wrapStrategy": "OVERFLOW_CELL"}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": 2,
                                  "startColumnIndex": 0, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1},
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": False},
                                                         "wrapStrategy": "OVERFLOW_CELL", "verticalAlignment": "BOTTOM"}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 2, "endRowIndex": 3,
                                  "startColumnIndex": 0, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .90, "blue": .95},
                                                         "wrapStrategy": "WRAP", "verticalAlignment": "BOTTOM",
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": True}}},
                         "fields": "userEnteredFormat(backgroundColor,wrapStrategy,verticalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 3, "endRowIndex": 4,
                                  "startColumnIndex": 0, "endColumnIndex": source_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .90, "blue": .95},
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": True}}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 3, "endRowIndex": 4,
                                  "startColumnIndex": source_width, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .90, "blue": .95},
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": True}}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 4, "endRowIndex": 5,
                                  "startColumnIndex": 0, "endColumnIndex": source_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .90, "blue": .95},
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": True}, "wrapStrategy": "WRAP",
                                                         "verticalAlignment": "BOTTOM"}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 4, "endRowIndex": 5,
                                  "startColumnIndex": source_width, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": .85, "green": .90, "blue": .95},
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": True}, "wrapStrategy": "WRAP",
                                                         "verticalAlignment": "BOTTOM"}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 5, "endRowIndex": last_used_row,
                                  "startColumnIndex": 0, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.##########"},
                                                         "verticalAlignment": "BOTTOM"}},
                         "fields": "userEnteredFormat(numberFormat,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 5, "endRowIndex": last_used_row,
                                  "startColumnIndex": 0, "endColumnIndex": total_width},
                         "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1},
                                                         "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": False}}},
                         "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 5, "endRowIndex": last_used_row,
                                  "startColumnIndex": 0, "endColumnIndex": 1},
                         "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
                         "fields": "userEnteredFormat.numberFormat"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
                                       "properties": {"pixelSize": 22}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 2, "endIndex": 3},
                                       "properties": {"pixelSize": 28}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 3, "endIndex": 4},
                                       "properties": {"pixelSize": 22}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 4, "endIndex": 5},
                                       "properties": {"pixelSize": 64}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 5, "endIndex": last_used_row},
                                       "properties": {"pixelSize": 21}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": source_width},
                                       "properties": {"pixelSize": 130}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 90}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                                       "properties": {"pixelSize": 85}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                                       "properties": {"pixelSize": 210}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 5},
                                       "properties": {"pixelSize": 190}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": source_width, "endIndex": total_width},
                                       "properties": {"pixelSize": 115}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": total_width - 1, "endIndex": total_width},
                                       "properties": {"pixelSize": 190}, "fields": "pixelSize"}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": worksheet.id, "startRowIndex": 4,
                                                   "endRowIndex": 5 + len(audit_rows), "startColumnIndex": 0,
                                                   "endColumnIndex": total_width}}}},
    ]
    integer_headers = {
        "Fiscal Years Found", "Trend Observations", "Latest Fiscal Year", "Max Fiscal Year Gap",
        "Financial Age Days", "Source Error Count", "Secondary Rank", "Weak Fundamental Domains",
        "Four-Year Adverse Observations", "Recent Adverse Observations",
    }
    for index, header in enumerate(audit_headers):
        lower = header.lower()
        if "(%)" in header:
            number_format = {"type": "NUMBER", "pattern": '0.0"%"'}
        elif header in integer_headers or header.endswith(" Observations"):
            number_format = {"type": "NUMBER", "pattern": "0"}
        elif ("coverage" in lower and header != "Applicable Family Weight") or any(
            token in lower for token in ("reliability", "breadth", "r squared")
        ):
            number_format = {"type": "PERCENT", "pattern": "0.0%"}
        elif header.endswith(" Input"):
            metric = header.removesuffix(" Input")
            is_percentage_metric = (
                any(token in metric for token in (
                    "_roa", "_roe", "_margin", "_growth", "_cagr", "_yield", "_share",
                )) and "debt_to_equity" not in metric
                and not metric.startswith("primary_")
            )
            if is_percentage_metric:
                number_format = {"type": "PERCENT", "pattern": "0.0%"}
            else:
                continue
        elif "_four_year_trend" in header and "debt_to_equity" not in header:
            number_format = {"type": "PERCENT", "pattern": "0.0%"}
        else:
            continue
        requests.append({"repeatCell": {
            "range": {"sheetId": worksheet.id, "startRowIndex": 5, "endRowIndex": last_used_row,
                      "startColumnIndex": source_width + index, "endColumnIndex": source_width + index + 1},
            "cell": {"userEnteredFormat": {"numberFormat": number_format}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    book.batch_update({"requests": requests})

    formula_readback = worksheet.acell("A5", value_render_option="FORMULA").value
    if formula_readback != formula:
        raise RuntimeError("Short sheet IMPORTRANGE formula failed read-back")
    imported = worksheet.get(f"A5:{source_end_col}{last_used_row}", value_render_option="UNFORMATTED_VALUE")
    imported_tickers = [str(row[ticker_column - 1]).strip().upper() for row in imported[1:] if len(row) >= ticker_column]
    audit_ticker_index = audit_headers.index("Secondary Ticker")
    audit_tickers = [str(row[audit_ticker_index]).strip().upper() for row in audit_rows]
    if imported_tickers != tickers or audit_tickers != tickers:
        raise RuntimeError("Short sheet primary import and secondary audit rows do not reconcile")


def control_rows(controls: dict[str, Any]) -> list[list[Any]]:
    descriptions = {
        "MODEL_VERSION": "Model/control schema marker; updated by code.",
        "CURRENT_MAX_CLOSE_AGE_DAYS": "Maximum calendar age of the last completed US daily close used for current research evidence.",
        "CURRENT_MIN_DOLLAR_VOLUME_20D": "Minimum mean daily close times volume over the last 20 completed sessions, in quote currency.",
        "CURRENT_MIN_PRICE": "Minimum completed daily close for current research evidence, in quote currency.",
        "CURRENT_MAX_REPORT_AGE_DAYS": "Maximum calendar age of the latest SEC financial report period end.",
        "CURRENT_MAX_SHARE_AGE_DAYS": "Maximum calendar age of the SEC common-share count used for estimated current market cap.",
        "MIN_OVERALL_WEIGHT_COVERAGE": "General family-weight coverage floor; the category selection floor also applies, so the higher threshold governs.",
        "MIN_FAMILY_METRIC_COVERAGE": "Minimum valid metric-weight coverage required to admit a family.",
        "MIN_CROSS_SECTION_OBSERVATIONS": "Minimum valid stocks required to score one metric.",
        "CROSS_SECTION_SHRINKAGE_STRENGTH": "Peer scores are shrunk toward neutral by n/(n+strength), reducing false precision in sparse metrics.",
        "SECTOR_PEER_WEIGHT": "Maximum blend weight on reliability-weighted within-sector scoring; the remainder stays category-relative.",
        "MIN_SECTOR_PEER_OBSERVATIONS": "Minimum valid observations required before a sector can influence one metric.",
        "SECTOR_PEER_SHRINKAGE_STRENGTH": "Shrinkage strength applied to small within-sector metric samples.",
        "MIN_TREND_OBSERVATIONS": "Minimum four unique annual observations required for a regression-style trend; fewer observations remain NO DATA.",
        "MISSING_TREND_PENALTY_POINTS": "Maximum points deducted when the TREND family has no valid metrics; partial trend coverage receives a proportional deduction.",
        "NO_USABLE_TREND_EXTRA_PENALTY_POINTS": "Additional deduction when effective trend reliability is exactly zero, preventing non-trend candidates from ranking as trend-backed winners.",
        "THREE_YEAR_TREND_RELIABILITY": "Reliability multiplier for a trend based on three rather than four unique fiscal years.",
        "TREND_FIT_RELIABILITY_FLOOR": "Minimum fit multiplier; R-squared scales the remainder so noisy trends have less authority without being discarded.",
        "PERCENTILE_WEIGHT": "Blend weight on cross-sectional percentile; remainder is capped robust-z.",
        "ROBUST_Z_CAP": "Absolute robust-z mapped to the 0/100 endpoints.",
        "REQUIRE_BLANK_ERROR_FIELD": "Exclude a stock when any retained source-year Error field is populated.",
        "SAFE_REQUIRE_POSITIVE_NET_INCOME": "Safe eligibility requires latest Net Income above zero.",
        "SAFE_REQUIRE_POSITIVE_FREE_CASH_FLOW": "Safe eligibility requires latest Free Cash Flow above zero.",
        "SAFE_MIN_POSITIVE_HISTORY_SHARE": "Safe candidates require this share of retained fiscal rows to have confirmed positive Net Income and Free Cash Flow; missing years are not positive evidence.",
        "HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR": "Require Revenue CAGR above zero in addition to the configurable inclusive minimum floor.",
        "HIGH_GROWTH_MIN_REVENUE_CAGR": "Inclusive minimum Revenue CAGR floor; the separate positive-growth switch can require a value above zero.",
        "HIGH_GROWTH_REQUIRE_POSITIVE_FREE_CASH_FLOW": "Require positive latest-period Free Cash Flow for High Growth selection.",
        "TURNAROUND_REQUIRE_INFLECTION_GATE": "Require the inflection family to pass its threshold.",
        "TURNAROUND_MIN_INFLECTION_SCORE": "Minimum 0-100 inflection family score for Turnaround eligibility.",
        "TURNAROUND_MIN_FAVOURABLE_TREND_BREADTH": "Minimum multi-year directional breadth; improving=1, flat=0.5, deteriorating=0.",
        "STRONG_FAMILY_SCORE": "Family score threshold used by the evidence-breadth gate.",
        "MIN_LEAVE_ONE_FAMILY_OUT_SCORE": "Worst recomputed score after removing any one active family; guards against one-theme rankings.",
        "MAX_FISCAL_YEAR_LAG": "Maximum latest-fiscal-year lag versus the freshest company in the category.",
        "MAX_FISCAL_YEAR_GAP": "Maximum allowed gap between adjacent fiscal observations.",
        "MAX_FINANCIAL_AGE_DAYS": "Maximum age of latest fiscal period end versus the analysis date; future/missing dates fail.",
        "MIN_RECENT_DIRECTION_OBSERVATIONS": "Minimum available latest-year comparisons among revenue, margins, ROA and debt/equity (six measures).",
        "MIN_RECENT_FAVOURABLE_BREADTH": "Minimum latest-year directional breadth: improving=1, flat=0.5, deteriorating=0; missing excluded.",
        "MIN_FAVOURABLE_TREND_BREADTH": "Minimum multi-year directional breadth, independent of peer ranking; flat trends count half.",
        "MIN_RECENT_METRIC_COVERAGE": "Minimum inflection metric-weight coverage when the family is active; missing momentum cannot be renormalised away.",
        "SHORT_MIN_HISTORY_YEARS": "Require at least four unique annual observations for a short candidate.",
        "SHORT_MIN_TREND_OBSERVATIONS": "Require four observations for each short trend regression.",
        "SHORT_MIN_SCORE": "Diagnostic qualification threshold for final short score after trend reliability adjustment; top ranked display can include failures.",
        "SHORT_MIN_TREND_SCORE": "Minimum adverse trend-family score.",
        "SHORT_MIN_TREND_COVERAGE": "Minimum weighted coverage of available adverse trend metrics.",
        "SHORT_MIN_POINT_IN_TIME_COVERAGE": "Require 80% weighted coverage of current primary Short score, recent price momentum and estimate revisions; with equal weights this means at least four of five signals.",
        "SHORT_MIN_CROSS_SECTION_OBSERVATIONS": "Require at least eight primary Short candidates with a valid metric before that metric can affect peer ranking.",
        "SHORT_MIN_WEIGHT_COVERAGE": "Minimum total short model family-weight coverage for qualification.",
        "SHORT_MIN_TREND_RELIABILITY": "Minimum history- and fit-weighted short trend reliability.",
        "SHORT_MIN_DIRECTION_OBSERVATIONS": "Minimum available adverse direction checks among multi-year or recent measures (maximum six, the number of recent measures).",
        "SHORT_MAX_POSITIVE_REVENUE_GROWTH_SHARE": "A four-year revenue-growth share below this cutoff counts as adverse; 0.50 means positive growth occurred in fewer than half of observed annual changes.",
        "SHORT_MIN_ADVERSE_TREND_BREADTH": "Minimum share of observed four-year trends moving adversely.",
        "SHORT_MIN_ADVERSE_RECENT_BREADTH": "Minimum share of observed latest-year changes moving adversely.",
        "SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS": "Require independent weak operating, cash-flow and balance-sheet evidence domains.",
        "SHORT_TOP_N": "Maximum number of scored four-year Short research names displayed after the minimum data checks.",
    }
    rows: list[list[Any]] = [["CONTROL PANEL — EDIT VALUE COLUMN, THEN RERUN"], ["Source Data tabs are read-only; scores are ranked by the configured Top N after weights are renormalised over valid inputs and the explicit trend-data penalty is applied."], ["Section", "Parameter", "Value", "Description"]]
    hidden_gate_controls = {
        "MIN_OVERALL_WEIGHT_COVERAGE", "STRONG_FAMILY_SCORE", "MIN_LEAVE_ONE_FAMILY_OUT_SCORE",
        "MAX_FISCAL_YEAR_LAG", "MAX_FINANCIAL_AGE_DAYS", "MIN_RECENT_DIRECTION_OBSERVATIONS",
        "MIN_RECENT_FAVOURABLE_BREADTH", "MIN_FAVOURABLE_TREND_BREADTH", "MIN_RECENT_METRIC_COVERAGE",
        "SAFE_REQUIRE_POSITIVE_NET_INCOME", "SAFE_REQUIRE_POSITIVE_FREE_CASH_FLOW", "SAFE_MIN_POSITIVE_HISTORY_SHARE",
        "HIGH_GROWTH_REQUIRE_POSITIVE_REVENUE_CAGR", "HIGH_GROWTH_MIN_REVENUE_CAGR",
        "HIGH_GROWTH_REQUIRE_POSITIVE_FREE_CASH_FLOW", "TURNAROUND_REQUIRE_INFLECTION_GATE",
        "TURNAROUND_MIN_INFLECTION_SCORE", "TURNAROUND_MIN_FAVOURABLE_TREND_BREADTH",
        "SHORT_MIN_SCORE", "SHORT_MIN_TREND_SCORE", "SHORT_MIN_TREND_COVERAGE",
        "SHORT_MIN_POINT_IN_TIME_COVERAGE", "SHORT_MIN_WEIGHT_COVERAGE", "SHORT_MIN_TREND_RELIABILITY",
        "SHORT_MIN_DIRECTION_OBSERVATIONS", "SHORT_MIN_ADVERSE_TREND_BREADTH",
        "SHORT_MIN_ADVERSE_RECENT_BREADTH", "SHORT_MIN_WEAK_FUNDAMENTAL_DOMAINS",
    }
    for key in config.GENERAL_DEFAULTS:
        if key.startswith("SHORT_METRIC_WEIGHT_"):
            continue
        if (key.startswith("CURRENT_") or key in hidden_gate_controls or key.startswith(("MIN_SCORE_", "MIN_SELECTION_SCORE_",
                                                           "MIN_SELECTION_WEIGHT_COVERAGE_",
                                                           "MIN_EFFECTIVE_TREND_RELIABILITY_",
                                                           "MIN_STRONG_FAMILIES_",
                                                           "MIN_STRONG_EVIDENCE_DOMAINS_"))):
            continue
        if key.startswith("CURRENT_"):
            section = "Current research evidence"
        elif key.startswith("SHORT_"):
            section = "Short strategy"
        elif key.startswith("TOP_N_"):
            section = "Selection cap"
        elif key.startswith("MIN_HISTORY") or key in {"MAX_FISCAL_YEAR_GAP", "SHORT_MIN_HISTORY_YEARS", "SHORT_MIN_TREND_OBSERVATIONS"}:
            section = "Data requirements"
        else:
            section = "Model"
        desc = descriptions.get(key, "Editable model parameter.")
        if key.startswith("MIN_HISTORY_YEARS_"):
            desc = "Require at least four unique fiscal-year observations for this Long category."
        elif key.startswith("TOP_N_"):
            desc = "Number of highest-scoring names shown in the Secondary Summary."
        elif key.startswith("SHORT_WEIGHT_"):
            family = key.removeprefix("SHORT_WEIGHT_").replace("_", " ").title()
            desc = f"Relative share of the pre-reliability Short score assigned to {family}; zero disables this family."
        rows.append([section, key, _scalar(controls[key]), desc])
    for category, cfg in config.CATEGORIES.items():
        slug = cfg["slug"]
        for family in config.FAMILY_WEIGHTS[category]:
            key = f"FAMILY_WEIGHT_{slug}_{family}"
            rows.append([f"{category} family weight", key, controls[key], "Relative family weight; 0 disables the family."])
        for metric, (family, direction, description) in config.FEATURE_SPECS.items():
            key = f"METRIC_WEIGHT_{slug}_{metric.upper()}"
            rows.append([f"{category} metric weight", key, controls[key], f"{family}; {direction} is better; {description}."])
    short_metrics = sorted({metric for family in SHORT_METRICS.values() for metric in family})
    short_metric_directions = {
        metric: direction
        for family in SHORT_METRICS.values()
        for metric, direction in family.items()
    }
    for metric in short_metrics:
        key = f"SHORT_METRIC_WEIGHT_{metric.upper()}"
        direction_text = "lower raw values rank higher for the Short score" if short_metric_directions[metric] == "lower" else "higher raw values rank higher for the Short score"
        rows.append(["Short metric weight", key, controls[key], f"Relative weight; {direction_text}, as shown in Secondary Model Registry; zero disables it."])
    rows += [["Interpretation", "Missing data", "Renormalised", "Missing values are never converted to zero; score coverage and reliability remain visible in analysis."],
             ["Interpretation", "Short input", "Primary Short tab", "Only tickers currently selected on the primary workbook Short tab enter the secondary Short screen. Four-year history is required; primary Short score, four- and twelve-week price changes and estimate revisions are point-in-time inputs."],
             ["Interpretation", "Primary Short freshness", "Unverified", "The imported primary Short table has no source as-of timestamp. Validate price/revision freshness before relying on the default 15% point-in-time Short family; borrow availability, cost and squeeze risk are not modeled."],
             ["Interpretation", "Ranking", "Separate cohorts", "Long scores compare within each current primary category shortlist; Short scores compare only the tickers on the live primary Short tab."],
             ["Interpretation", "Output", "Top N scored", "Display the highest scored names with valid fiscal history and no source error."]]
    return rows


def short_primary_summary_formula() -> str:
    """Live-import the complete primary Short summary into its own tab."""
    source_tab = config.PRIMARY_SHORT_TAB.replace("'", "''")
    return f'=IMPORTRANGE("{config.PRIMARY_SHORT_SPREADSHEET_ID}","{source_tab}!A:AX")'


def write_short_primary_summary(book, worksheet) -> None:
    """Refresh the formula-backed primary Short candidate summary."""
    last_col = rowcol_to_a1(1, 50).rstrip("1")  # The primary Short table spans A:AX.
    title = worksheet.title.replace("'", "''")
    book.values_batch_clear({"ranges": [f"'{title}'!A1:{last_col}{worksheet.row_count}"]})
    book.values_batch_update({"valueInputOption": "USER_ENTERED", "data": [{
        "range": f"'{title}'!A1",
        "majorDimension": "ROWS",
        "values": [[short_primary_summary_formula()]],
    }]})
    book.batch_update({"requests": [
        {"clearBasicFilter": {"sheetId": worksheet.id}},
        {"updateSheetProperties": {"properties": {"sheetId": worksheet.id, "gridProperties": {"frozenRowCount": 3}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 50}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.10, "green": 0.24, "blue": 0.40}, "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontFamily": "Arial", "fontSize": 10, "bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 50}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.85, "green": 0.90, "blue": 0.95}, "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": True}, "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 58}, "fields": "pixelSize"}},
    ]})


def write_outputs(
    book, worksheets: dict[str, Any], controls: dict[str, Any], analyses: dict[str, pd.DataFrame],
    summaries: dict[str, pd.DataFrame], registry: pd.DataFrame, run_audit: pd.DataFrame,
    shorts: pd.DataFrame, short_note: str = "", short_only: bool = False,
    short_analysis: pd.DataFrame | None = None, primary_shortlist: pd.DataFrame | None = None,
    short_as_of: str = "", short_data: pd.DataFrame | None = None,
) -> list[str]:
    if short_analysis is None or primary_shortlist is None or short_data is None:
        raise ValueError("Short process output requires filtered fiscal data, the live primary shortlist and full secondary analysis")
    required_outputs = [
        ("Secondary Model Registry", 220, 10), ("Short", 100, 195),
        ("Short Data", 100, 160), ("Short Analysis", 100, 180),
        ("Short Secondary Summary", 100, 40), ("Short Summary", 100, 50),
    ]
    if not short_only:
        required_outputs.append(("Secondary Run Audit", 30, 24))
    for title, rows, cols in required_outputs:
        if title not in worksheets:
            worksheets[title] = book.add_worksheet(title=title, rows=rows, cols=cols)
    payloads: list[tuple[str, list[list[Any]]]] = [("Control Panel", control_rows(controls))]
    for category, cfg in config.CATEGORIES.items():
        analysis_note = "AUDIT: latest four unique fiscal years → trend slopes and fit → metric/family point contributions → reliability penalty → final score; missing data remain NO DATA."
        analysis_display = analyses[category].drop(columns=[
            column for column in analyses[category]
            if column in {"eligible", "gate_fail_reasons"} or column.endswith("_gate_pass")
        ])
        payloads.append((cfg["analysis_sheet"], frame_values(analysis_display, f"{category.upper()} — SECONDARY MULTI-YEAR AUDIT", analysis_note)))
        summary_note = "Top score-ranked research candidates, up to the configured cap, among valid four-year source histories. Research ranking does not verify Primary timing, current TTM or trading suitability."
        payloads.append((cfg["secondary_summary_sheet"], frame_values(summaries[category], f"{category.upper()} — SECONDARY SCREEN", summary_note)))
    source_tabs = ", ".join(cfg["data_sheet"] for cfg in config.CATEGORIES.values())
    history_candidates = int(short_data["Ticker"].nunique()) if "Ticker" in short_data else 0
    data_note = (
        f"Filtered annual source records for the current primary Short candidates from {source_tabs}. "
        f"This run contains {len(short_data)} fiscal rows across {history_candidates} candidates as of {short_as_of}."
    )
    if history_candidates == 0:
        data_note += " No candidate has source history here; see Short Analysis for the candidate-level data status."
    audit_values = short_process_audit_values(primary_shortlist, short_analysis, shorts, controls)
    audit_frame = pd.DataFrame(audit_values[1:], columns=audit_values[0])
    analysis_note = (
        "Every primary Short candidate is shown with its point-in-time primary signals, fiscal history, four-year trend inputs, "
        "family contributions, coverage, data status and final score. Missing history is shown as NO DATA."
    )
    summary_note = (
        f"{short_note} This sheet ranks scored four-year Short candidates; display does not assess trade readiness."
    )
    payloads.extend([
        ("Short Data", frame_values(short_data, "SHORT — FILTERED ANNUAL SOURCE DATA", data_note)),
        ("Short Analysis", frame_values(audit_frame, "SHORT — SECONDARY FOUR-YEAR ANALYSIS", analysis_note)),
        ("Short Secondary Summary", frame_values(shorts, "SHORT — SECONDARY SUMMARY", summary_note)),
    ])
    payloads.append(("Secondary Model Registry", frame_values(
        registry, "SECONDARY MODEL REGISTRY", "Every scored metric, economic direction, family and live Control Panel weight."
    )))
    payloads.append(("Secondary Run Audit", frame_values(
        run_audit, "SECONDARY RUN AUDIT", "Run-level data quality, coverage, selection and exact contribution reconciliation evidence."
    )))

    if short_only:
        payloads = [item for item in payloads if item[0] in {
            "Control Panel", "Secondary Model Registry", "Short Data", "Short Analysis", "Short Secondary Summary",
        }]
    clear_ranges = [
        f"'{title.replace(chr(39), chr(39)*2)}'!A1:{rowcol_to_a1(worksheets[title].row_count, worksheets[title].col_count)}"
        for title, _ in payloads
    ]
    book.values_batch_clear({"ranges": clear_ranges})
    data = []
    requests = []
    for title, values in payloads:
        ws = worksheets[title]
        width = max(len(row) for row in values)
        padded = [row + [""] * (width - len(row)) for row in values]
        if width > ws.col_count or len(padded) > ws.row_count:
            requests.append({"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"rowCount": max(ws.row_count, len(padded) + 20), "columnCount": max(ws.col_count, width)}}, "fields": "gridProperties(rowCount,columnCount)"}})
        data.append({"range": f"'{title.replace(chr(39), chr(39)*2)}'!A1", "majorDimension": "ROWS", "values": padded})
    if requests:
        book.batch_update({"requests": requests})
    book.values_batch_update({"valueInputOption": "USER_ENTERED", "data": data})

    format_requests = []
    for title, values in payloads:
        ws = worksheets[title]
        width = max(len(row) for row in values)
        height = len(values)
        headers = values[2] if len(values) > 2 else []
        if title == "Control Panel":
            column_widths = [180, 450, 220, 520]
        elif title.endswith("Summary"):
            column_widths = [70, 80, 170, 560, 105, 230, 160, 80] + [115] * max(0, width - 8)
        else:
            column_widths = [90, 85, 95, 115, 105] + [115] * max(0, width - 5)
        if title.startswith("Short "):
            column_widths = [115] * width
            for index, header in enumerate(headers):
                label = str(header)
                if label in {"Ticker", "Secondary Ticker"}:
                    column_widths[index] = 90
                elif label == "Primary Short Rank" or label == "Rank":
                    column_widths[index] = 90
                elif label in {"Primary Company Name", "Company Name"}:
                    column_widths[index] = 200
                elif label in {"Primary Industry", "Industry"}:
                    column_widths[index] = 190
                elif label == "History Source Tabs":
                    column_widths[index] = 220
                elif label == "Failure Reasons":
                    column_widths[index] = 250
                elif label == "Short Thesis Evidence":
                    column_widths[index] = 280
                elif label == "Research Checks":
                    column_widths[index] = 360
                elif label == "Quantitative Reason":
                    column_widths[index] = 560
        format_requests += [
            {"clearBasicFilter": {"sheetId": ws.id}},
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 3}}, "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": width}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.10, "green": 0.24, "blue": 0.40}, "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontFamily": "Arial", "fontSize": 10, "bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": width}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}, "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": False}, "wrapStrategy": "OVERFLOW_CELL"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": width}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.85, "green": 0.90, "blue": 0.95}, "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True}, "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": ws.id, "startRowIndex": 2, "endRowIndex": height, "startColumnIndex": 0, "endColumnIndex": width}}}},
        ]
        if title == "Control Panel" and height > 3:
            # Rebuilding a shorter panel can move interpretation rows into old
            # control rows. Clear prior validations across the panel before
            # applying the current control-specific rules below.
            format_requests.append({"setDataValidation": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1000, "startColumnIndex": 0, "endColumnIndex": 26}}})
            format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 3, "endRowIndex": height, "startColumnIndex": 2, "endColumnIndex": 3}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1.0, "green": 0.96, "blue": 0.78}}}, "fields": "userEnteredFormat.backgroundColor"}})
            percentages = {
                "MIN_OVERALL_WEIGHT_COVERAGE", "MIN_FAMILY_METRIC_COVERAGE", "PERCENTILE_WEIGHT",
                "THREE_YEAR_TREND_RELIABILITY", "TREND_FIT_RELIABILITY_FLOOR",
                "MIN_SELECTION_WEIGHT_COVERAGE_SAFE", "MIN_SELECTION_WEIGHT_COVERAGE_HIGH_GROWTH_POTENTIAL",
                "MIN_SELECTION_WEIGHT_COVERAGE_TURNAROUND_STORY", "MIN_EFFECTIVE_TREND_RELIABILITY_SAFE",
                "MIN_EFFECTIVE_TREND_RELIABILITY_HIGH_GROWTH_POTENTIAL", "MIN_EFFECTIVE_TREND_RELIABILITY_TURNAROUND_STORY",
                "SAFE_MIN_POSITIVE_HISTORY_SHARE", "TURNAROUND_MIN_FAVOURABLE_TREND_BREADTH",
                "SECTOR_PEER_WEIGHT",
            }
            percentages.update({"MIN_RECENT_FAVOURABLE_BREADTH", "MIN_FAVOURABLE_TREND_BREADTH", "MIN_RECENT_METRIC_COVERAGE", "SHORT_MIN_TREND_COVERAGE", "SHORT_MIN_POINT_IN_TIME_COVERAGE", "SHORT_MIN_WEIGHT_COVERAGE", "SHORT_MIN_TREND_RELIABILITY", "SHORT_MIN_ADVERSE_TREND_BREADTH", "SHORT_MIN_ADVERSE_RECENT_BREADTH", "SHORT_MAX_POSITIVE_REVENUE_GROWTH_SHARE"})
            boolean_keys = {key for key, value in controls.items() if isinstance(value, bool)}
            integer_keys = {key for key, value in controls.items() if isinstance(value, int) and not isinstance(value, bool)}
            float_keys = {key for key, value in controls.items() if isinstance(value, float)}
            nonnegative_keys = {
                "CROSS_SECTION_SHRINKAGE_STRENGTH", "STRONG_FAMILY_SCORE",
                "MIN_LEAVE_ONE_FAMILY_OUT_SCORE", "MAX_FISCAL_YEAR_LAG",
                "SECTOR_PEER_SHRINKAGE_STRENGTH",
            }
            for row_index, row in enumerate(values[3:], start=3):
                key = str(row[1]) if len(row) > 1 else ""
                condition = None
                if key == "SHORT_MIN_DIRECTION_OBSERVATIONS":
                    condition = {"type": "NUMBER_BETWEEN", "values": [{"userEnteredValue": "1"}, {"userEnteredValue": "6"}]}
                elif key == "SHORT_MIN_CROSS_SECTION_OBSERVATIONS":
                    condition = {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "2"}]}
                elif key in {
                    "MIN_HISTORY_YEARS_SAFE", "MIN_HISTORY_YEARS_HIGH_GROWTH_POTENTIAL",
                    "MIN_HISTORY_YEARS_TURNAROUND_STORY", "MIN_TREND_OBSERVATIONS",
                    "SHORT_MIN_HISTORY_YEARS", "SHORT_MIN_TREND_OBSERVATIONS",
                }:
                    condition = {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "4"}]}
                elif key in boolean_keys:
                    condition = {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "TRUE"}, {"userEnteredValue": "FALSE"}]}
                elif key in percentages:
                    condition = {"type": "NUMBER_BETWEEN", "values": [{"userEnteredValue": "0"}, {"userEnteredValue": "1"}]}
                elif key in integer_keys and key != "MAX_FISCAL_YEAR_LAG":
                    condition = {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "1"}]}
                elif key.startswith(("FAMILY_WEIGHT_", "METRIC_WEIGHT_", "SHORT_WEIGHT_", "SHORT_METRIC_WEIGHT_", "MIN_SCORE_", "MIN_SELECTION_SCORE_", "SHORT_MIN_SCORE", "SHORT_MIN_TREND_SCORE")) or key in {
                    "MISSING_TREND_PENALTY_POINTS", "NO_USABLE_TREND_EXTRA_PENALTY_POINTS",
                } | nonnegative_keys:
                    condition = {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "0"}]}
                if condition:
                    format_requests.append({"setDataValidation": {"range": {"sheetId": ws.id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 2, "endColumnIndex": 3}, "rule": {"condition": condition, "strict": True, "showCustomUi": True}}})
                if key in integer_keys:
                    format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 2, "endColumnIndex": 3}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}}, "fields": "userEnteredFormat.numberFormat"}})
                elif key in percentages:
                    format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 2, "endColumnIndex": 3}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}}, "fields": "userEnteredFormat.numberFormat"}})
                elif key in float_keys:
                    format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 2, "endColumnIndex": 3}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}}, "fields": "userEnteredFormat.numberFormat"}})
        if height > 3 and title != "Control Panel":
            # Reset stale formats before applying header-driven formats. Summary
            # columns can move when a new audit family is added.
            format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 3, "endRowIndex": height, "startColumnIndex": 0, "endColumnIndex": width}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.##########"}}}, "fields": "userEnteredFormat.numberFormat"}})
            if title.startswith("Short "):
                format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 3, "endRowIndex": height, "startColumnIndex": 0, "endColumnIndex": width}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}, "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "fontFamily": "Arial", "fontSize": 10, "bold": False}, "verticalAlignment": "BOTTOM"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}})
        run_start = 0
        for index in range(1, len(column_widths) + 1):
            if index == len(column_widths) or column_widths[index] != column_widths[run_start]:
                format_requests.append({"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": run_start, "endIndex": index}, "properties": {"pixelSize": column_widths[run_start]}, "fields": "pixelSize"}})
                run_start = index
        for index, header in enumerate(headers):
            label = str(header)
            lower = label.lower()
            format_type = "NUMBER"
            if label in {"Rank", "Primary Short Rank", "Fiscal Year", "History Years", "Fiscal Years Found", "Trend Observations", "Latest Fiscal Year", "Strong Family Count", "Max Fiscal Year Gap", "Financial Age Days", "Weak Fundamental Domains", "Shortlist Candidates", "Eligible Shorts", "Selected Shorts"} or lower in {
                "history_years", "trend_observations", "latest_fiscal_year", "strong_family_count",
                "max_fiscal_year_gap", "source_error_count",
            }:
                pattern = "0"
            elif label == "Market Cap (mil)":
                pattern = "#,##0"
            elif "(%)" in label:
                format_type = "NUMBER"
                pattern = '0.0"%"'
            elif label in {"Fiscal Period End", "Price Date", "Revenue Filed", "Analysis As Of", "Generated At"} or lower.endswith("_date") or "period_end" in lower:
                format_type = "DATE"
                pattern = "yyyy-mm-dd"
            elif any(token in lower for token in ("score", "contribution", "weighted_points", "final_score", "penalty")):
                pattern = "0.00"
            elif any(token in lower for token in ("interest_coverage", "cash_conversion", "debt / equity", "debt_to_equity", "net_debt_to_ebitda", "ratio")):
                pattern = "0.00"
            elif any(token in lower for token in ("coverage", "breadth", "reliability", "cagr", "growth", "roa", "roe", "margin", "yield", "_share", "four_year_trend", "4y trend", "sector peer weight")):
                pattern = "0.0%"
            else:
                continue
            format_requests.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 3, "endRowIndex": height, "startColumnIndex": index, "endColumnIndex": index + 1}, "cell": {"userEnteredFormat": {"numberFormat": {"type": format_type, "pattern": pattern}}}, "fields": "userEnteredFormat.numberFormat"}})
        if title.startswith("Short "):
            format_requests.extend([
                {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
                {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 22}, "fields": "pixelSize"}},
                {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 58}, "fields": "pixelSize"}},
                {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 3, "endIndex": max(4, height)}, "properties": {"pixelSize": 21}, "fields": "pixelSize"}},
            ])
        if title.endswith("Summary") and "Quantitative Reason" in headers and height > 3:
            reason_index = headers.index("Quantitative Reason")
            format_requests.extend([
                {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 3, "endRowIndex": height,
                                          "startColumnIndex": reason_index, "endColumnIndex": reason_index + 1},
                                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
                {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 3,
                                                         "endIndex": height}, "properties": {"pixelSize": 150}, "fields": "pixelSize"}},
            ])
    book.batch_update({"requests": format_requests})

    write_short_process_sheet(
        book, worksheets["Short"], primary_shortlist, short_analysis, shorts,
        controls, short_as_of, short_note, audit_values=audit_values,
    )
    write_short_primary_summary(book, worksheets["Short Summary"])

    # Bounded read-back: title, header, first data row and final populated row.
    updated = []
    for title, values in payloads:
        ws = worksheets[title]
        checks = ws.batch_get(["A1", "A3", f"A{len(values)}"])
        if not checks[0] or not checks[1]:
            raise RuntimeError(f"Read-back verification failed for {title}")
        if title.endswith("Summary") and "Quantitative Reason" in values[2]:
            reason_checks = ws.batch_get(["D3"] + (["D4"] if len(values) > 3 else []))
            if reason_checks[0] != [["Quantitative Reason"]] or (
                len(values) > 3 and reason_checks[1] != [[values[3][3]]]
            ):
                raise RuntimeError(f"Read-back verification failed for {title} column D reasons")
        updated.append(title)
    updated.append("Short")
    imported_formula = worksheets["Short Summary"].get("A1", value_render_option="FORMULA")
    if not imported_formula or imported_formula[0][0] != short_primary_summary_formula():
        raise RuntimeError("Read-back verification failed for Short Summary IMPORTRANGE")
    updated.append("Short Summary")
    return updated
