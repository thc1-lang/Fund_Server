"""Independent current-state Short thesis and selection."""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

# Compatibility alias for callers; score_shorts reads current config each time.
SHORT_WEIGHTS = config.short_weights()


def short_rules() -> str:
    return (f"Short screening rules: minimum Short score {config.MIN_SHORT_SCORE:g}; "
            f"minimum confidence {config.MIN_CANDIDATE_CONFIDENCE:g}; top {config.TOP_N_LONG} per industry; "
            f"display up to {config.SHORT_DISPLAY_TOP_N}. At least {config.SHORT_MIN_WEAK_FAMILIES} weak families "
            f"score {config.SHORT_WEAK_FAMILY_SCORE:g}+ with a non-valuation weakness. "
            "Change MIN_SHORT_SCORE, SHORT_DISPLAY_TOP_N, SHORT_WEIGHT_* and thesis controls in Control Panel, then rerun. "
            f"Eligible business types: {','.join(config.SHORT_ALLOWED_BUSINESS_TYPES)}. "
            "Configured tradability and business-classification checks are required before display. Borrow remains unverified until independently checked.")


def short_thesis_gate(df: pd.DataFrame, min_families: int, weak_score: float,
                      weights: dict[str, float]) -> pd.Series:
    families = ("VALUATION", "PROFITABILITY_AND_RETURNS", "BALANCE_SHEET_AND_LEVERAGE", "LIQUIDITY_AND_EFFICIENCY")
    weak = pd.DataFrame({family: pd.to_numeric(df.get(f"{family} Short Sub-score", pd.Series(np.nan, index=df.index)), errors="coerce").ge(weak_score) & (weights.get(family, 0) > 0)
                         for family in families}, index=df.index)
    return weak.sum(axis=1).ge(min_families) & weak.drop(columns="VALUATION").any(axis=1)


def score_shorts(df: pd.DataFrame) -> pd.DataFrame:
    """Apply shared eligibility gates and the separately configured Short score floor."""
    from .ranking import rank_quant_candidates
    score = pd.to_numeric(df["Expanded Short Score"], errors="coerce")
    raw = pd.to_numeric(df["Expanded Raw Short Score"], errors="coerce")
    sufficient = df.get("Short Data Status", df["Data Status"]).eq("SUFFICIENT")
    ranked = rank_quant_candidates(
        df, config.TOP_N_LONG, config.TOP_N_LONG, config.MIN_LONG_SCORE, config.MIN_SHORT_SCORE,
        config.MAX_QUANT_LONGS_TOTAL, config.SHORT_DISPLAY_TOP_N, config.MIN_CANDIDATE_CONFIDENCE,
        config.MAX_BOOK_PRICE_DIVERGENCE,
    )
    rank = ranked["Short Selection Rank"]
    confidence = pd.to_numeric(df.get("Short Score Confidence", df["Score Confidence"]), errors="coerce")
    gates = pd.DataFrame({
        "Data": sufficient & score.notna(),
        "Confidence": confidence.ge(config.MIN_CANDIDATE_CONFIDENCE),
        "Score": score.ge(config.MIN_SHORT_SCORE),
        "Tradability": df.get("short_tradability_gate_pass", pd.Series(True, index=df.index)).fillna(False),
        "Classification": df.get("economic_classification_verified", pd.Series(True, index=df.index)).fillna(False),
        "Thesis": df.get("Short Thesis Gate Pass", pd.Series(False, index=df.index)).fillna(False),
        "Business type": df.get("Short Business Type Gate Pass", pd.Series(False, index=df.index)).fillna(False),
        "Industry rank": rank.le(config.TOP_N_LONG),
    }).fillna(False)
    out = pd.DataFrame({"Short Score": score, "Short Raw Score": raw,
        "Short Weight Coverage": df["Short Composite Weight Coverage"],
        "Short Within-Industry Rank": rank, "Short Eligible": ranked["Short Candidate Eligible"]}, index=df.index)
    for family, weight in config.short_weights().items():
        out[f"Short {family} Score"] = df.get(f"{family} Short Sub-score", 100 - df[f"{family} Long Sub-score"])
        out[f"Short {family} Weight"] = weight
        out[f"Short {family} Contribution"] = df[f"Quant Composite {family} Short Contribution Points"]
    for gate in gates:
        out[f"Short {gate} Pass"] = gates[gate]
    out["Short Rejection Reasons"] = gates.apply(lambda row: " | ".join(row.index[~row]), axis=1)
    selected = df.loc[out["Short Eligible"], ["Ticker", "Source Row"]].copy()
    selected["score"] = score.loc[selected.index]
    selected["confidence"] = confidence.loc[selected.index]
    selected["ticker_sort"] = selected["Ticker"].fillna("").astype(str).str.casefold()
    selected = selected.sort_values(["score", "confidence", "ticker_sort", "Source Row"], ascending=[False, False, True, True], kind="stable")
    out["Short Universe Rank"] = np.nan
    out.loc[selected.index, "Short Universe Rank"] = np.arange(1, len(selected) + 1)
    selected = selected.head(config.SHORT_DISPLAY_TOP_N)
    out["Short Selected"] = out.index.isin(selected.index)
    out["Short Rank"] = np.nan
    out.loc[selected.index, "Short Rank"] = np.arange(1, len(selected) + 1)
    return out


def short_sheet_values(df: pd.DataFrame, as_of: str | None = None) -> list[list[object]]:
    from .sheets_writer import _candidate_reason, _sheet_values
    selected = df.loc[df["Short Selected"]].sort_values("Short Rank").copy()
    selected["Shortlist Reason"] = selected.apply(lambda row: _candidate_reason(row, "Short"), axis=1)
    cols = ["Short Rank", "Ticker", "Company Name", "Market Cap (mil)", "Shortlist Reason", "Industry", "Short Score", "Short Raw Score", "Short Score Confidence", "Short Weight Coverage", "Short Data Status", "PIT Verification Status", "Latest Verified Information Availability Date",
        "Short Thesis Gate Pass", "Short Business Type Gate Pass", "Net Margin %", "debt_equity_ratio", "Current Ratio", "earnings_yield_pe_valid", "average_daily_dollar_volume", "Last Close", "short_tradability_gate_pass", "short_tradability_gate_fail_reasons", "short_implementation_validation_required", "economic_business_type", "margin_reconciliation_status"]
    for family, weight in config.short_weights().items():
        if weight > 0:
            cols += [f"Short {family} Score", f"Short {family} Weight", f"Short {family} Contribution"]
    rows = [["SHORT — QUANTITATIVE RESEARCH CANDIDATES"],
        [f"Dataset as-of: {as_of or 'NOT PROVIDED — freshness unverified'}. Borrow availability/cost, squeeze risk and catalysts require validation. Scores are not probabilities."], cols]
    rows += selected.reindex(columns=cols).values.tolist()
    if selected.empty:
        rows.append(["Display count is set to zero." if config.SHORT_DISPLAY_TOP_N == 0 else "No stocks meet the shared quantitative selection criteria."])
    rows += [[""], ["SELECTION RULES", short_rules()],
        ["SCORE METHOD", "Separate Short family weights reward current valuation excess and financial weakness. A current-state thesis gate is required. Family contributions sum to raw Short score; final = 50 + confidence/100 * (raw - 50)."],
        ["INTERPRETATION", "Research screen, not an execution instruction or validated trading edge. Thresholds were not optimised on historical returns."]]
    return _sheet_values(rows)


def write_short_sheet(book, df: pd.DataFrame, as_of: str | None = None) -> str:
    """Only create/update Short; read back every populated cell and formatting."""
    from .sheets_writer import _retry_google_request
    from gspread.utils import rowcol_to_a1
    values = short_sheet_values(df, as_of)
    width = max(map(len, values))
    values = [row + [""] * (width - len(row)) for row in values]
    sheets = {w.title: w for w in book.worksheets()}
    ws = sheets.get("Short")
    if ws is None:
        ws = book.add_worksheet(title="Short", rows=max(30, len(values)), cols=width)
    sid = ws.id
    requests = [{"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"rowCount": max(30, len(values)), "columnCount": width, "frozenRowCount": 3, "frozenColumnCount": 0}}, "fields": "gridProperties"}},
        {"unmergeCells": {"range": {"sheetId": sid}}},
        {"repeatCell": {"range": {"sheetId": sid}, "cell": {}, "fields": "userEnteredValue,userEnteredFormat"}}]
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": len(values)},
        "properties": {"pixelSize": 36}, "fields": "pixelSize",
    }})
    for r in (0, 1):
        requests.append({"mergeCells": {"range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1, "startColumnIndex": 0, "endColumnIndex": width}, "mergeType": "MERGE_ALL"}})
    for r in range(len(values)-3, len(values)):
        requests.append({"mergeCells": {"range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1, "startColumnIndex": 1, "endColumnIndex": min(7, width)}, "mergeType": "MERGE_ALL"}})
    requests += [{"repeatCell": {"range": {"sheetId": sid, "endRowIndex": len(values), "endColumnIndex": width}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP", "textFormat": {"fontSize": 10}}}, "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": .96, "green": .72, "blue": .72}, "textFormat": {"bold": True,"fontSize": 14}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 3}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": .98, "green": .86, "blue": .86}, "textFormat": {"bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid,"dimension":"COLUMNS","startIndex":0,"endIndex":width},"properties":{"pixelSize":145},"fields":"pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid,"dimension":"COLUMNS","startIndex":2,"endIndex":3},"properties":{"pixelSize":240},"fields":"pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid,"dimension":"COLUMNS","startIndex":4,"endIndex":5},"properties":{"pixelSize":560},"fields":"pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid,"dimension":"ROWS","startIndex":3,"endIndex":3 + int(df['Short Selected'].sum())},"properties":{"pixelSize":150},"fields":"pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid,"dimension":"ROWS","startIndex":2,"endIndex":3},"properties":{"pixelSize":75},"fields":"pixelSize"}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 3 + int(df['Short Selected'].sum()), "endColumnIndex": width}}}}]
    requests.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": len(values)-3, "endIndex": len(values)}, "properties": {"pixelSize": 220}, "fields": "pixelSize"}})
    count = int(df['Short Selected'].sum())
    if count:
        requests.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":3,"endRowIndex":3+count,"startColumnIndex":4,"endColumnIndex":5},"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat.wrapStrategy"}})
        requests.append({"repeatCell":{"range":{"sheetId":sid,"startRowIndex":3,"endRowIndex":3+count,"startColumnIndex":5,"endColumnIndex":width},"cell":{"userEnteredFormat":{"numberFormat":{"type":"NUMBER","pattern":"#,##0.00"}}},"fields":"userEnteredFormat.numberFormat"}})
    _retry_google_request(lambda: book.batch_update({"requests": requests}))
    _retry_google_request(lambda: ws.update(values, "A1", value_input_option="RAW"))
    actual = _retry_google_request(lambda: ws.get(f"A1:{rowcol_to_a1(len(values),width)}", value_render_option="UNFORMATTED_VALUE"))
    actual = [list(row) + [""] * (width-len(row)) for row in actual]
    if actual != values:
        raise RuntimeError("Short full-cell read-back mismatch")
    meta = book.fetch_sheet_metadata()
    props = next(s for s in meta['sheets'] if s['properties']['sheetId']==sid)
    assert props['properties']['gridProperties']['frozenRowCount']==3
    assert props['basicFilter']['range']['startRowIndex']==2
    return ws.url


def short_control_validation_requests(sheet_id, rows):
    """Keep the same input validation after a normal full workbook refresh."""
    requests = []
    for i, row in enumerate(rows):
        if len(row) < 3 or not str(row[1]).startswith('SHORT_'):
            continue
        name, value = row[1:3]
        requests.append({'setDataValidation': {'range': {'sheetId': sheet_id, 'startRowIndex': i, 'endRowIndex': i+1, 'startColumnIndex': 0, 'endColumnIndex': 4}}})
        if isinstance(value, bool):
            condition = {'type': 'BOOLEAN'}
        elif isinstance(value, int):
            condition = {'type': 'CUSTOM_FORMULA', 'values': [{'userEnteredValue': f'=AND(ISNUMBER(C{i+1}),C{i+1}>=0,C{i+1}=INT(C{i+1}))'}]}
        elif isinstance(value, float):
            if name.endswith('_PCT'):
                condition = {'type': 'CUSTOM_FORMULA', 'values': [{'userEnteredValue': f'=ISNUMBER(C{i+1})'}]}
            elif name == 'SHORT_MIN_WEIGHT_COVERAGE':
                condition = {'type': 'NUMBER_BETWEEN', 'values': [{'userEnteredValue': '0'}, {'userEnteredValue': '1'}]}
            elif 'SCORE' in name or name == 'SHORT_MIN_CONFIDENCE':
                condition = {'type': 'NUMBER_BETWEEN', 'values': [{'userEnteredValue': '0'}, {'userEnteredValue': '100'}]}
            else:
                condition = {'type': 'NUMBER_GREATER_THAN_EQ', 'values': [{'userEnteredValue': '0'}]}
        else:
            continue
        requests.append({'setDataValidation': {'range': {'sheetId': sheet_id, 'startRowIndex': i, 'endRowIndex': i+1, 'startColumnIndex': 2, 'endColumnIndex': 3}, 'rule': {'condition': condition, 'strict': True, 'showCustomUi': True}}})
    return requests
