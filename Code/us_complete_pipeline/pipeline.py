"""Read, calculate, compare, and verify complete output histories."""

import hashlib
import json
import logging
import numpy as np
import pandas as pd
from config import window_value
from validation import (
    source_frame,
    pair_definitions,
    output_dates,
    numeric,
    ValidationError,
)
from correlation import rolling_correlation
from spread import spread_zscore
from google_sheets import a1

LOG = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def as_matrix(rows, shape):
    result = np.full(shape, np.nan)
    for i, row in enumerate(rows[: shape[0]]):
        for j, value in enumerate(row[: shape[1]]):
            result[i, j] = numeric(value)
    return result


def comparison(old, new):
    common = np.isfinite(old) & np.isfinite(new)
    bad = common & ~np.isclose(old, new, rtol=1e-9, atol=1e-10)
    blank = np.isfinite(old) ^ np.isfinite(new)
    diff = np.abs(old[common] - new[common])
    last_old = max(np.flatnonzero(np.isfinite(old).any(axis=1)), default=-1)
    new_tail = (
        (~np.isfinite(old))
        & np.isfinite(new)
        & (np.arange(len(new))[:, None] > last_old)
    )
    return {
        "existing_numeric": int(np.isfinite(old).sum()),
        "python_numeric": int(np.isfinite(new).sum()),
        "compared_numeric": int(common.sum()),
        "numeric_mismatches": int(bad.sum()),
        "blank_mismatches": int(blank.sum()),
        "added_numeric": int((~np.isfinite(old) & np.isfinite(new)).sum()),
        "removed_numeric": int((np.isfinite(old) & ~np.isfinite(new)).sum()),
        "new_tail_numeric": int(new_tail.sum()),
        "historical_blank_mismatches": int((blank & ~new_tail).sum()),
        "max_absolute_difference": float(diff.max()) if len(diff) else None,
        "mismatch_coordinates": [
            [int(i) + 3, int(j) + 2] for i, j in np.argwhere(bad | blank)[:30]
        ],
    }


def calculate(book, source, output, controls):
    frame, source_issues = source_frame(source)
    window = window_value(controls, book.control_key)
    pairs, pair_issues = pair_definitions(output, frame)
    dates = output_dates(output, frame.index)
    if not len(dates):
        raise ValidationError("No writable output dates")
    result = np.full((len(frame), len(pairs)), np.nan)
    fn = rolling_correlation if book.kind == "correlation" else spread_zscore
    for j, (x, y, error) in enumerate(pairs):
        if error:
            continue
        try:
            result[:, j] = fn(frame[x].to_numpy(), frame[y].to_numpy(), window)
        except Exception as exc:
            raise ValidationError(
                f"{book.kind} column {j+2}, pair {x!r}, {y!r}: {exc}"
            ) from exc
    aligned = pd.DataFrame(result, index=frame.index).reindex(dates).to_numpy()
    old = as_matrix([row[1:] for row in output[2:]], aligned.shape)
    finite = np.isfinite(aligned)
    active = dates[finite.any(axis=1)]
    values = np.where(finite, aligned.astype(object), "").tolist()
    issues = source_issues + pair_issues
    for issue in issues:
        LOG.warning("%s: %s", book.kind, issue)
    report = {
        "kind": book.kind,
        "dataset": book.dataset,
        "key": book.key,
        "spreadsheet_id": book.spreadsheet_id,
        "output_tab": book.output_tab,
        "window": window,
        "window_unit": "valid_paired_observations",
        "history_cutoff": "strictly_before_target_date",
        "source_dates": len(frame),
        "indicators": len(frame.columns),
        "pairs": len(pairs),
        "output_dates": len(dates),
        "source_first_date": str(frame.index.min().date()),
        "source_latest_date": str(frame.index.max().date()),
        "first_calculated_date": str(active.min().date()) if len(active) else None,
        "latest_calculated_date": str(active.max().date()) if len(active) else None,
        "numeric_cells": int(finite.sum()),
        "blank_observations": int((~finite).sum()),
        "invalid_indicator_mappings": len(pair_issues),
        "issues": issues,
        "cells_written": 0,
        "comparison": comparison(old, aligned),
    }
    return values, report


def load(sheets, book):
    meta = sheets.metadata(book.spreadsheet_id)
    if meta["properties"]["title"] != book.title:
        LOG.info(
            "Workbook display title is %r; using the configured workbook ID",
            meta["properties"]["title"],
        )
    tabs = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    for name in (book.source_tab, book.output_tab, book.control_tab):
        if name not in tabs:
            raise ValidationError(f"{book.title}: missing required tab {name!r}")
    if tabs[book.output_tab]["sheetId"] != book.output_id:
        raise ValidationError("Output sheet identity changed")
    from source_freshness import wait_for_macro

    wait_for_macro(sheets, book)
    source, output, controls = sheets.values(
        book.spreadsheet_id,
        [a1(book.source_tab), a1(book.output_tab), a1(book.control_tab, "A:B")],
    )
    # Input tabs are read-only; reserve capacity only in the output tab.
    output_props = tabs[book.output_tab]
    source_capacity = tabs[book.source_tab]["gridProperties"]["rowCount"]
    requests = []
    output_capacity = max(len(source) + 2, source_capacity + 2)
    if output_capacity > output_props["gridProperties"]["rowCount"]:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": output_props["sheetId"],
                        "gridProperties": {"rowCount": output_capacity},
                    },
                    "fields": "gridProperties.rowCount",
                }
            }
        )
        output_props["gridProperties"]["rowCount"] = output_capacity
    if requests:
        sheets.batch(book.spreadsheet_id, requests)
        output = sheets.values(book.spreadsheet_id, [a1(book.output_tab)])[0]
    values, report = calculate(book, source, output, controls)
    return {
        "book": book,
        "meta": meta,
        "properties": tabs[book.output_tab],
        "source": source,
        "output": output,
        "controls": controls,
        "values": values,
        "report": report,
    }


def validate_before_publish(sheets, state):
    b = state["book"]
    if not b.kind.endswith("_momentum"):
        from source_freshness import wait_for_macro

        wait_for_macro(sheets, b)
    # Detect concurrent source, header/date or control edits before replacing anything.
    source, structure, dates, controls = sheets.values(
        b.spreadsheet_id,
        [
            a1(b.source_tab),
            a1(b.output_tab, "1:2"),
            a1(b.output_tab, "A:A"),
            a1(b.control_tab, "A:B"),
        ],
    )
    expected_dates = [[r[0]] if r and r[0] != "" else [] for r in state["output"]]
    while expected_dates and not expected_dates[-1]:
        expected_dates.pop()
    expected_source = state["source"]
    if (
        source != expected_source
        or structure != state["output"][:2]
        or dates != expected_dates
        or controls != state["controls"]
    ):
        raise ValidationError(
            f"{b.kind}: source, dates, pair definitions or controls changed during calculation; rerun"
        )
    formulas = sheets.values(b.spreadsheet_id, [a1(b.output_tab)], formulas=True)[0]
    has_formulas = any(
        isinstance(v, str) and v.startswith("=") for r in formulas[2:] for v in r[1:]
    )
    if has_formulas:
        c = state["report"]["comparison"]
        # Clearing scores on dates without a current pair is an expected correction.
        if (
            c["compared_numeric"] < min(20, state["report"]["numeric_cells"])
            or not c["compared_numeric"]
            or c["numeric_mismatches"]
            or c["historical_blank_mismatches"] > c["removed_numeric"]
        ):
            raise ValidationError(
                f"{b.kind}: existing formula comparison failed; formulas preserved. See audit report: {c}"
            )
    return formulas


def verify(sheets, state):
    b = state["book"]
    rows = sheets.values(b.spreadsheet_id, [a1(b.output_tab)], formulas=True)[0]
    expected = state["values"]
    # Formula-rendered values expose any surviving calculated formula.
    old_structure = state["formulas"]
    header = lambda data: [data[i] if i < len(data) else [] for i in range(2)]
    if header(rows) != header(old_structure):
        raise ValidationError("Readback: pair/header rows changed")
    n = max(len(rows), len(old_structure), len(expected) + 2)
    for i in range(n):
        actual = rows[i] if i < len(rows) else []
        prior = old_structure[i] if i < len(old_structure) else []
        if (actual[0] if actual else "") != (prior[0] if prior else ""):
            raise ValidationError(f"Readback: column A changed at row {i+1}")
        if i < 2:
            continue
        want = expected[i - 2] if i - 2 < len(expected) else []
        for j in range(1, max(len(actual), len(want) + 1)):
            a = actual[j] if j < len(actual) else ""
            e = want[j - 1] if j <= len(want) else ""
            if isinstance(a, str) and a.startswith("="):
                raise ValidationError(
                    f"Readback: formula remains at row {i+1}, column {j+1}"
                )
            if e == "":
                if a != "":
                    raise ValidationError(
                        f"Readback: stale/nonblank cell at row {i+1}, column {j+1}"
                    )
            elif not np.isfinite(numeric(a)) or not np.isclose(
                a, e, rtol=1e-12, atol=1e-12
            ):
                raise ValidationError(
                    f"Readback: numeric mismatch row {i+1}, column {j+1}"
                )
    if b.kind.endswith("_momentum"):
        effective = sheets.values(
            b.spreadsheet_id, [a1(b.output_tab, "1:2"), a1(b.output_tab, "A:A")]
        )
        expected_dates = [[r[0]] if r and r[0] != "" else [] for r in state["output"]]
        while expected_dates and not expected_dates[-1]:
            expected_dates.pop()
        if effective[0] != state["output"][:2] or effective[1] != expected_dates:
            raise ValidationError("Readback: effective imported headers/dates changed")
    return True
