"""Readable, independently reconstructed histories for any selected output pair."""

import math
import statistics as stats
from datetime import datetime, timezone
import numpy as np
from validation import source_frame, output_dates, ValidationError
from momentum_pipeline import fisher_frame, canonical
from google_sheets import a1, column_name

TAB = "Calculation Helper"
HELPER_ID = 2026091401


def pair_labels(output):
    width = max(map(len, output[:2]), default=0)
    return [
        f'{column_name(j+1)} | {output[0][j] if j<len(output[0]) else ""} | {output[1][j] if j<len(output[1]) else ""}'
        for j in range(1, width)
    ]


def clean(v):
    if isinstance(v, (float, np.floating)) and not math.isfinite(v):
        return ""
    return v


def trace(state, pair_index):
    """Independent scalar oracle, checked against the production matrix by date."""
    book = state["book"]
    kind = book.kind
    output = state["output"]
    w = state["report"]["window"]
    m = state["report"].get("momentum_lookback")
    labels = pair_labels(output)
    if not 0 <= pair_index < len(labels):
        raise ValidationError("Helper pair selection outside output matrix")
    j = pair_index + 1
    x = output[0][j] if j < len(output[0]) else ""
    y = output[1][j] if j < len(output[1]) else ""
    if kind == "correlation_momentum":
        frame, _ = fisher_frame(state["source"])
        valid_mapping = (x, y) in frame.columns
        values = (
            frame[(x, y)].to_numpy() if valid_mapping else np.full(len(frame), np.nan)
        )
        xv = yv = np.full(len(frame), np.nan)
        if state.get("pair_source") is None:
            raise ValidationError("Helper requires dated paired inputs")
        raw, _ = source_frame(state["pair_source"])
        paired_dates = (
            [
                d
                for d, a, b in zip(raw.index, raw[x], raw[y])
                if math.isfinite(a) and math.isfinite(b)
            ]
            if x in raw and y in raw
            else []
        )
        input_count = 0
        previous_count = 0
    else:
        frame, _ = source_frame(state["source"])
        valid_mapping = x in frame and y in frame
        xv = frame[x].to_numpy() if x in frame else np.full(len(frame), np.nan)
        yv = frame[y].to_numpy() if y in frame else np.full(len(frame), np.nan)
        values = xv - yv
    date_index = output_dates(output, frame.index)
    production = {d: state["values"][i][pair_index] for i, d in enumerate(date_index)}
    common = ["Date", "Current X", "Current Y"]
    if kind == "correlation":
        headers = common + [
            "Prior pair count",
            "Window first date",
            "Window last date",
            "Mean X",
            "Mean Y",
            "Sample std X",
            "Sample std Y",
            "Sample covariance",
            "Pearson r",
            "Fisher score",
            "Output score",
            "Status",
        ]
    elif kind == "spread":
        headers = common + [
            "Signed spread",
            "Prior spread count",
            "Window first date",
            "Window last date",
            "Prior mean",
            "Prior sample std",
            "Numerator",
            "Calculated z-score",
            "Output score",
            "Status",
        ]
    else:
        headers = (
            common + ["Signed spread"]
            if kind == "spread_momentum"
            else ["Date", "Fisher value"]
        ) + [
            "Lag observation date",
            "Lag value",
            "Signed change",
            "Prior change count",
            "Window first date",
            "Window last date",
            "Prior mean change",
            "Prior sample std",
            "Numerator",
            "Calculated z-score",
            "Output score",
            "Status",
        ]
    if kind == "correlation_momentum":
        headers += [
            "Last paired input date",
            "Input age days",
            "New paired observation",
        ]
    history = []
    seen = []
    table = []
    matched = 0
    for i, date in enumerate(frame.index):
        fresh = True
        last_input = ""
        input_age = ""
        if kind == "correlation_momentum":
            while input_count < len(paired_dates) and paired_dates[input_count] <= date:
                input_count += 1
            fresh = input_count > previous_count
            previous_count = input_count
            if input_count:
                last_input = str(paired_dates[input_count - 1].date())
                input_age = int((date - paired_dates[input_count - 1]).days)
        d = str(date.date())
        score = math.nan
        status = "Insufficient history"
        h = history[-w:]
        count = len(h)
        first = h[0][0] if h else ""
        last = h[-1][0] if h else ""
        if kind == "correlation":
            mx = my = sx = sy = cov = r = math.nan
            if count == w and math.isfinite(xv[i]) and math.isfinite(yv[i]):
                a = [v[1] for v in h]
                b = [v[2] for v in h]
                mx = stats.mean(a)
                my = stats.mean(b)
                sx = stats.stdev(a)
                sy = stats.stdev(b)
                cov = sum((aa - mx) * (bb - my) for aa, bb in zip(a, b)) / (w - 1)
                if sx > 0 and sy > 0:
                    r = cov / (sx * sy)
                    if abs(r) < 1 - 1e-14:
                        score = math.atanh(r)
                        status = "OK"
                    else:
                        status = "Perfect/near-perfect correlation"
                else:
                    status = "Zero input variance"
            row = [d, xv[i], yv[i], count, first, last, mx, my, sx, sy, cov, r, score]
            if math.isfinite(xv[i]) and math.isfinite(yv[i]):
                history.append((d, float(xv[i]), float(yv[i])))
            else:
                status = "Missing current pair"
        else:
            display_val = float(values[i])
            val = display_val if fresh else math.nan
            change = math.nan
            lagdate = ""
            lagval = math.nan
            if m is not None and math.isfinite(val) and len(seen) >= m:
                lagdate, lagval = seen[-m]
                change = val - lagval
            target = val if m is None else change
            mean = sd = numerator = math.nan
            if count == w:
                mean = stats.mean(v[1] for v in h)
                sd = stats.stdev(v[1] for v in h)
                if math.isfinite(target):
                    numerator = target - mean
                    if sd > 0:
                        score = numerator / sd
                        status = "OK"
                    else:
                        status = "Zero historical standard deviation"
            if not math.isfinite(val):
                status = "Missing current observation"
            elif m is not None and not math.isfinite(change):
                status = "Insufficient lag history"
            if m is None:
                row = [
                    d,
                    xv[i],
                    yv[i],
                    val,
                    count,
                    first,
                    last,
                    mean,
                    sd,
                    numerator,
                    score,
                ]
            else:
                row = (
                    [d, xv[i], yv[i], val]
                    if kind == "spread_momentum"
                    else [d, display_val]
                ) + [
                    lagdate,
                    lagval,
                    change,
                    count,
                    first,
                    last,
                    mean,
                    sd,
                    numerator,
                    score,
                ]
            if math.isfinite(target):
                history.append((d, target))
            if math.isfinite(val):
                seen.append((d, val))
        if not valid_mapping:
            status = "Invalid pair mapping"
            score = math.nan
        published = production.get(date, "")
        if date in production:
            if published == "":
                if math.isfinite(score):
                    raise ValidationError(
                        f"Helper disagrees with blank output: {labels[pair_index]}, {d}"
                    )
            elif not math.isfinite(score) or not math.isclose(
                score, published, rel_tol=1e-9, abs_tol=1e-10
            ):
                raise ValidationError(
                    f"Helper differs from output: {labels[pair_index]}, {d}: {score} vs {published}"
                )
            else:
                matched += 1
        if kind == "correlation_momentum" and not fresh:
            status = "No new paired observation"
        row.extend([published, status])
        if kind == "correlation_momentum":
            row.extend([last_input, input_age, "Yes" if fresh else "No"])
        table.append([clean(v) for v in row])
    return headers, table, matched


def prepare(sheets, state):
    labels = pair_labels(state["output"])
    if not labels:
        raise ValidationError("No helper pairs")
    tabs = {x["properties"]["title"]: x["properties"] for x in state["meta"]["sheets"]}
    selected = ""
    if TAB in tabs:
        row = sheets.values(state["book"].spreadsheet_id, [a1(TAB, "B2")])[0]
        selected = row[0][0] if row and row[0] else ""
    # Fail clearly when the selected pair was removed/reordered, rather than show another pair.
    if selected and selected not in labels:
        raise ValidationError(
            "Helper pair no longer exists; choose a valid pair or clear Calculation Helper!B2"
        )
    selected = selected or labels[0]
    idx = labels.index(selected)
    headers, table, matched = trace(state, idx)
    return {
        "selection": selected,
        "labels": labels,
        "headers": headers,
        "table": table,
        "matched": matched,
        "old_selection": selected if TAB in tabs else None,
    }


def publish(sheets, state, helper, run_dir):
    book = state["book"]
    bid = book.spreadsheet_id
    meta = sheets.metadata(bid)
    props = next(
        (x["properties"] for x in meta["sheets"] if x["properties"]["title"] == TAB),
        None,
    )
    required = max(len(helper["table"]) + 6, len(helper["labels"]) + 1)
    req = []
    if props is None:
        req.append(
            {
                "addSheet": {
                    "properties": {
                        "sheetId": HELPER_ID,
                        "title": TAB,
                        "gridProperties": {
                            "rowCount": required,
                            "columnCount": 22,
                            "frozenRowCount": 5,
                            "frozenColumnCount": 1,
                        },
                    }
                }
            }
        )
        sid = HELPER_ID
    else:
        sid = props["sheetId"]
        old = sheets.values(bid, [a1(TAB)])[0]
        current = old[1][1] if len(old) > 1 and len(old[1]) > 1 else ""
        if current and current != helper["selection"]:
            raise ValidationError("Helper selection changed during run; rerun")
        sheets.backup(
            run_dir / (book.key + "_helper_before.json.gz"),
            {"spreadsheet_id": bid, "sheet": TAB, "values": old},
        )
        grid = props["gridProperties"]
        req.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "gridProperties": {
                            "rowCount": max(required, grid["rowCount"]),
                            "columnCount": max(22, grid["columnCount"]),
                        },
                    },
                    "fields": "gridProperties.rowCount,gridProperties.columnCount",
                }
            }
        )
    sheets.batch(bid, req)
    n = len(helper["headers"])
    end = column_name(n)
    rows = [
        ["Calculation audit", book.title],
        ["Selected pair", helper["selection"]],
        ["Calculated pair", helper["selection"]],
        [
            "Window",
            state["report"]["window"],
            "Lookback",
            state["report"].get("momentum_lookback", "N/A"),
            "Refreshed UTC",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "",
            "",
            "Select B2, then rerun Python. Momentum windows count paired updates, not calendar months. Lag and window dates show actual horizons.",
        ],
        helper["headers"],
    ] + helper["table"]
    sheets.request(
        "POST",
        bid,
        "/values:batchClear",
        json={"ranges": [a1(TAB, "A1:V1"), a1(TAB, "A3:V"), a1(TAB, "U2:V2")]},
    )
    sheets.write_values(
        bid,
        [
            {"range": a1(TAB, "A1"), "values": rows},
            {
                "range": a1(TAB, "U1"),
                "values": [["Available pairs"]]
                + [[label] for label in helper["labels"]],
            },
        ],
    )
    format_req = [
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 1,
                    "endRowIndex": 2,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [
                            {
                                "userEnteredValue": f"='{TAB}'!$U$2:$U${len(helper['labels'])+1}"
                            }
                        ],
                    },
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 0,
                    "endRowIndex": len(rows),
                    "startColumnIndex": 0,
                    "endColumnIndex": n,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"fontFamily": "Arial", "fontSize": 10},
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat.textFormat,userEnteredFormat.verticalAlignment",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 4,
                    "endRowIndex": 5,
                    "startColumnIndex": 0,
                    "endColumnIndex": n,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85},
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold,userEnteredFormat.wrapStrategy",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 1,
                    "endRowIndex": 2,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1, "green": 0.9, "blue": 0.8}
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sid,
                    "startRowIndex": 5,
                    "endRowIndex": len(rows),
                    "startColumnIndex": 1,
                    "endColumnIndex": n - 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "0.000000"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": n,
                },
                "properties": {"pixelSize": 145},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "ROWS",
                    "startIndex": 4,
                    "endIndex": 5,
                },
                "properties": {"pixelSize": 52},
                "fields": "pixelSize",
            }
        },
    ]
    # Keep merges wholly to the right of the frozen date column. Repair partial setup safely.
    existing_merges = next(
        (
            x.get("merges", [])
            for x in meta["sheets"]
            if x["properties"]["title"] == TAB
        ),
        [],
    )
    for row in (0, 1, 2):
        r = {
            "sheetId": sid,
            "startRowIndex": row,
            "endRowIndex": row + 1,
            "startColumnIndex": 1,
            "endColumnIndex": 5,
        }
        if r not in existing_merges:
            format_req.append({"mergeCells": {"range": r, "mergeType": "MERGE_ALL"}})
    r = {
        "sheetId": sid,
        "startRowIndex": 3,
        "endRowIndex": 4,
        "startColumnIndex": 5,
        "endColumnIndex": 8,
    }
    if r not in existing_merges:
        format_req.append({"mergeCells": {"range": r, "mergeType": "MERGE_ALL"}})
    for j, label in enumerate(helper["headers"]):
        if label.endswith("count"):
            format_req.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 5,
                            "endRowIndex": len(rows),
                            "startColumnIndex": j,
                            "endColumnIndex": j + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "NUMBER", "pattern": "0"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
    sheets.batch(bid, format_req)
    actual = sheets.values(bid, [a1(TAB, f"A5:{end}{len(rows)}")])[0]
    if canonical(actual) != canonical([helper["headers"]] + helper["table"]):
        raise ValidationError("Helper readback mismatch")
    state["report"]["helper"] = {
        "tab": TAB,
        "pair": helper["selection"],
        "dates": len(helper["table"]),
        "numeric_checks": helper["matched"],
        "verified": True,
    }
