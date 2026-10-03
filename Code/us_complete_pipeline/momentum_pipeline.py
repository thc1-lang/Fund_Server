"""Momentum inputs are IMPORTRANGE results; upstream books are read-only."""

from collections import Counter
import logging
import time
import numpy as np
import pandas as pd
from config import upstream_book, window_value, momentum_lookback
from google_sheets import a1, column_name
from validation import (
    source_frame,
    pair_definitions,
    output_dates,
    parse_date,
    numeric,
    ValidationError,
)
from momentum import momentum_zscore
from spread import spread_series
from correlation import rolling_correlation
from pipeline import as_matrix, comparison

LOG = logging.getLogger(__name__)


def canonical(rows):
    rows = [list(r) for r in rows]
    for r in rows:
        while r and r[-1] == "":
            r.pop()
    while rows and not rows[-1]:
        rows.pop()
    return rows


def fisher_frame(rows):
    if len(rows) < 3 or len(rows[1]) < 1 or rows[1][0] != "Date":
        raise ValidationError(
            "Imported Fisher history must have Date in A2 and resolved pair headers"
        )
    width = max(len(rows[0]), len(rows[1]))
    keys = [
        (rows[0][j] if j < len(rows[0]) else "", rows[1][j] if j < len(rows[1]) else "")
        for j in range(1, width)
    ]
    counts = Counter(keys)
    issues = []
    columns = {}
    dates = []
    body = []
    for i, row in enumerate(rows[2:], 3):
        if not row or row[0] == "":
            if any(v != "" for v in row[1:]):
                raise ValidationError(
                    f"Imported Fisher row {i} has values without date"
                )
            continue
        dates.append(parse_date(row[0]))
        body.append(row)
    if not dates or len(set(dates)) != len(dates):
        raise ValidationError("Empty or duplicate imported Fisher dates")
    for j, key in enumerate(keys, 1):
        if (
            not all(isinstance(v, str) and v and not v.startswith("#") for v in key)
            or counts[key] != 1
        ):
            issues.append(
                f"Imported Fisher column {j+1}, pair {key}: missing or duplicate definition"
            )
            continue
        columns[key] = [numeric(r[j]) if j < len(r) else np.nan for r in body]
        for d, r, v in zip(dates, body, columns[key]):
            if j < len(r) and r[j] != "" and not np.isfinite(v):
                issues.append(
                    f"Imported Fisher {d.date()}, pair {key}: nonnumeric observation {r[j]!r}"
                )
    frame = pd.DataFrame(columns, index=pd.DatetimeIndex(dates)).sort_index()
    if not frame.notna().any().any():
        raise ValidationError("No finite Fisher values; import may be unavailable")
    return frame, issues


def correlation_events(book, frame, pair_source, pair_controls):
    """Verify Fisher lineage and identify new strictly-prior paired observations.

    Dates, never changes in values, define updates. Reindex on the union so a
    missing display row cannot shift the cutoff or create a stale observation.
    """
    if pair_source is None or pair_controls is None:
        raise ValidationError(
            "Correlation momentum requires dated paired source data and upstream controls"
        )
    raw, issues = source_frame(pair_source)
    if issues:
        raise ValidationError("Invalid paired source data: " + str(issues[:5]))
    window = window_value(pair_controls, upstream_book(book).control_key)
    union = raw.index.union(frame.index).sort_values()
    expanded = raw.reindex(union)
    masks = {}
    latest = {}
    for x, y in frame.columns:
        if x not in raw or y not in raw:
            raise ValidationError(f"Missing paired source indicator: {x!r}, {y!r}")
        paired = np.isfinite(raw[x]) & np.isfinite(raw[y])
        paired_dates = raw.index[paired]
        counts = paired_dates.searchsorted(frame.index, side="right")
        masks[(x, y)] = counts > np.r_[0, counts[:-1]]
        latest[(x, y)] = [paired_dates[n - 1] if n else pd.NaT for n in counts]
        expected = (
            pd.Series(
                rolling_correlation(expanded[x], expanded[y], window), index=union
            )
            .reindex(frame.index)
            .to_numpy()
        )
        actual = frame[(x, y)].to_numpy()
        if not np.all(
            np.isclose(actual, expected, rtol=1e-9, atol=1e-10, equal_nan=True)
        ):
            raise ValidationError(
                f"Upstream Fisher history does not match dated paired inputs and controls for {x!r}, {y!r}; rerun correlation before momentum"
            )
    return masks, latest


def calculate(book, source, output, controls, pair_source=None, pair_controls=None):
    m = momentum_lookback(controls)
    w = window_value(controls, book.control_key)
    if book.kind == "spread_momentum":
        frame, issues = source_frame(source)
        pairs, pair_issues = pair_definitions(output, frame)
        series = [
            None if err else spread_series(frame[x], frame[y]) for x, y, err in pairs
        ]
        indicators = len(frame.columns)
    else:
        frame, issues = fisher_frame(source)
        if issues:
            raise ValidationError("Invalid Fisher input: " + str(issues[:5]))
        masks, latest = correlation_events(book, frame, pair_source, pair_controls)
        width = max(map(len, output[:2]), default=0)
        if width < 2:
            raise ValidationError("Missing output pair definitions")
        pairs = []
        series = []
        pair_issues = []
        for j in range(1, width):
            x = output[0][j] if j < len(output[0]) else ""
            y = output[1][j] if j < len(output[1]) else ""
            err = (
                None
                if (x, y) in frame.columns
                else "missing or duplicate imported pair"
            )
            pairs.append((x, y, err))
            series.append(
                None
                if err
                else np.where(masks[(x, y)], frame[(x, y)].to_numpy(), np.nan)
            )
            if err:
                pair_issues.append(f"Column {j+1}, pair {x!r}, {y!r}: {err}")
        indicators = len({v for x, y, _ in pairs for v in (x, y) if v})
    dates = output_dates(output, frame.index)
    result = np.full((len(frame), len(pairs)), np.nan)
    for j, values in enumerate(series):
        if values is None:
            continue
        try:
            result[:, j] = momentum_zscore(values, m, w)
        except Exception as e:
            raise ValidationError(f"{book.kind}, pair {pairs[j][:2]}: {e}") from e
    aligned = pd.DataFrame(result, index=frame.index).reindex(dates).to_numpy()
    finite = np.isfinite(aligned)
    active = dates[finite.any(axis=1)]
    issues += pair_issues
    for issue in issues:
        LOG.warning("%s: %s", book.kind, issue)
    report = {
        "kind": book.kind,
        "dataset": book.dataset,
        "key": book.key,
        "spreadsheet_id": book.spreadsheet_id,
        "output_tab": book.output_tab,
        "window": w,
        "momentum_lookback": m,
        "window_unit": "prior_changes_between_paired_updates",
        "history_cutoff": "strictly_before_target_date",
        "source_dates": len(frame),
        "indicators": indicators,
        "pairs": len(pairs),
        "output_dates": len(dates),
        "first_calculated_date": str(active.min().date()) if len(active) else None,
        "latest_calculated_date": str(active.max().date()) if len(active) else None,
        "numeric_cells": int(finite.sum()),
        "blank_observations": int((~finite).sum()),
        "invalid_indicator_mappings": len(pair_issues),
        "issues": issues,
        "cells_written": 0,
        "comparison": comparison(
            as_matrix([r[1:] for r in output[2:]], aligned.shape), aligned
        ),
    }
    report["momentum_clock"] = (
        "paired_input_updates"
        if book.kind == "correlation_momentum"
        else "valid_current_paired_observations"
    )
    if book.kind == "correlation_momentum":
        report["upstream_fisher_lineage_verified"] = True
        report["method_version"] = "paired_updates_v1"
        report["upstream_correlation_window"] = window_value(
            pair_controls, upstream_book(book).control_key
        )
        report["suppressed_repeated_fisher_cells"] = int(
            sum(np.sum(np.isfinite(frame[key]) & ~mask) for key, mask in masks.items())
        )
        report["pair_status"] = []
        for j, (x, y, err) in enumerate(pairs):
            if err:
                continue
            event_dates = frame.index[np.isfinite(series[j])]
            score_dates = dates[np.isfinite(aligned[:, j])]
            last_input = latest[(x, y)][-1]
            lag_date = event_dates[-m - 1] if len(event_dates) > m else pd.NaT
            report["pair_status"].append(
                {
                    "x": x,
                    "y": y,
                    "valid_update_count": len(event_dates),
                    "last_paired_input_date": (
                        str(last_input.date()) if pd.notna(last_input) else None
                    ),
                    "last_update_date": (
                        str(event_dates[-1].date()) if len(event_dates) else None
                    ),
                    "latest_lag_date": (
                        str(lag_date.date()) if pd.notna(lag_date) else None
                    ),
                    "last_momentum_date": (
                        str(score_dates[-1].date()) if len(score_dates) else None
                    ),
                    "status": (
                        "No finite momentum history"
                        if not len(score_dates)
                        else (
                            "OK"
                            if score_dates[-1] == dates[-1]
                            else "No current momentum score"
                        )
                    ),
                }
            )
    return np.where(finite, aligned.astype(object), "").tolist(), report


def desired_formulas(book, source_width, pair_width):
    up = upstream_book(book)
    bid = up.spreadsheet_id
    last = column_name(pair_width)
    if book.kind == "spread_momentum":
        return [
            (
                book.source_tab,
                "A1",
                f'=IMPORTRANGE("{bid}","\'Final scores data\'!A:{column_name(source_width)}")',
            ),
            (
                "Imported Pair Definitions",
                "A1",
                f'=IMPORTRANGE("{bid}","\'Spread Score\'!A1:{last}2")',
            ),
            (book.output_tab, "A1", f"=ARRAYFORMULA('{book.source_tab}'!A1:A)"),
            (
                book.output_tab,
                "B1",
                f"=ARRAYFORMULA('Imported Pair Definitions'!B1:{last}2)",
            ),
        ]
    formulas = [
        (book.source_tab, "A1", f'=IMPORTRANGE("{bid}","\'Correlation Score\'!A:A")'),
        (book.output_tab, "A1", "='Control Panel'!B3"),
        (book.output_tab, "A2", f"=ARRAYFORMULA('{book.source_tab}'!A2:A)"),
        (book.output_tab, "B1", f"=ARRAYFORMULA('{book.source_tab}'!B1:{last}2)"),
    ]
    for start in range(2, pair_width + 1, 64):
        end = min(start + 63, pair_width)
        cell = column_name(start) + "1"
        segment = f"'Correlation Score'!{cell}:{column_name(end)}"
        formulas.append(
            (
                book.source_tab,
                cell,
                f'=IMPORTRANGE("{bid}","{segment}"&MAX(FILTER(ROW($A:$A),$A:$A<>"")))',
            )
        )
    return formulas


def prepare(sheets, book):
    """Refresh only new-workbook import geometry. Never source values or controls."""
    up = upstream_book(book)
    tab = up.source_tab if book.kind == "spread_momentum" else up.output_tab
    upstream, pairs = sheets.values(
        up.spreadsheet_id, [a1(tab), a1(up.output_tab, "1:2")]
    )
    if not upstream or len(pairs) < 2:
        raise ValidationError("Upstream source or pair definitions unavailable")
    sw = max(map(len, upstream[:2]))
    pw = max(map(len, pairs))
    meta = sheets.metadata(book.spreadsheet_id)
    if meta["properties"]["title"] != book.title:
        LOG.info(
            "Workbook display title is %r; using the configured workbook ID",
            meta["properties"]["title"],
        )
    tabs = {x["properties"]["title"]: x["properties"] for x in meta["sheets"]}
    if tabs[book.output_tab]["sheetId"] != book.output_id:
        raise ValidationError("Momentum output identity changed")
    formulas = desired_formulas(book, sw, pw)
    current = sheets.values(
        book.spreadsheet_id, [a1(t, c) for t, c, _ in formulas], formulas=True
    )
    changes = [
        {"range": a1(t, c), "values": [[f]]}
        for (t, c, f), v in zip(formulas, current)
        if v != [[f]]
    ]
    existing_anchors = sheets.values(
        book.spreadsheet_id, [a1(book.source_tab, "1:1")], formulas=True
    )[0]
    desired_cells = {c for t, c, _ in formulas if t == book.source_tab}
    obsolete = any(
        isinstance(v, str)
        and v.startswith("=")
        and column_name(j + 1) + "1" not in desired_cells
        for j, v in enumerate(existing_anchors[0] if existing_anchors else [])
    )
    upstream_meta = sheets.metadata(up.spreadsheet_id)
    capacity = next(
        x["properties"]["gridProperties"]["rowCount"]
        for x in upstream_meta["sheets"]
        if x["properties"]["title"] == tab
    )
    req = []
    for name, cols, rows in [
        (book.source_tab, sw, capacity),
        (book.output_tab, pw, capacity + 2),
    ] + (
        [("Imported Pair Definitions", pw, 2)] if book.kind == "spread_momentum" else []
    ):
        p = tabs[name]
        grid = p["gridProperties"]
        target = {
            "rowCount": max(rows, grid["rowCount"]),
            "columnCount": max(cols, grid["columnCount"]),
        }
        if any(target[k] != grid[k] for k in target):
            req.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": p["sheetId"],
                            "gridProperties": target,
                        },
                        "fields": "gridProperties.rowCount,gridProperties.columnCount",
                    }
                }
            )
    if req:
        sheets.batch(book.spreadsheet_id, req)
    if changes or obsolete:
        # Clear only the new imported source when its block layout changes, including obsolete blocks.
        source_changed = any(
            v["range"].startswith(a1(book.source_tab) + "!") for v in changes
        )
        if source_changed or obsolete:
            sheets.request(
                "POST",
                book.spreadsheet_id,
                "/values:batchClear",
                json={"ranges": [a1(book.source_tab)]},
            )
            changes = [{"range": a1(t, c), "values": [[f]]} for t, c, f in formulas]
        sheets.request(
            "POST",
            book.spreadsheet_id,
            "/values:batchUpdate",
            json={"valueInputOption": "USER_ENTERED", "data": changes},
        )
    return upstream, pairs


def assert_fresh(sheets, book, source, output, pair_source=None, pair_controls=None):
    up = upstream_book(book)
    tab = up.source_tab if book.kind == "spread_momentum" else up.output_tab
    upstream, pairs = sheets.values(
        up.spreadsheet_id, [a1(tab), a1(up.output_tab, "1:2")]
    )
    if canonical(source) != canonical(upstream):
        raise ValidationError(
            f"{book.kind}: IMPORTRANGE is stale/loading or differs from upstream; existing scores preserved. Wait for imports and rerun."
        )
    if [r[1:] for r in output[:2]] != [r[1:] for r in pairs]:
        raise ValidationError("Imported pair order differs from source")
    if pair_source is not None:
        current, controls = sheets.values(
            up.spreadsheet_id, [a1(up.source_tab), a1(up.control_tab, "A:B")]
        )
        if canonical(current) != canonical(pair_source) or canonical(
            controls
        ) != canonical(pair_controls):
            raise ValidationError(
                "Paired source data or upstream controls changed during calculation; rerun"
            )
        from source_freshness import wait_for_macro

        macro = wait_for_macro(sheets, up)
        if canonical(macro) != canonical(pair_source):
            raise ValidationError(
                "Paired source no longer matches the macro summary; rerun correlation"
            )


def load(sheets, book):
    prepare(sheets, book)
    for attempt in range(6):
        source, output, controls = sheets.values(
            book.spreadsheet_id,
            [a1(book.source_tab), a1(book.output_tab), a1(book.control_tab, "A:B")],
        )
        try:
            assert_fresh(sheets, book, source, output)
            break
        except ValidationError:
            if attempt == 5:
                raise
            LOG.info("%s: waiting 10 seconds for IMPORTRANGE freshness", book.kind)
            time.sleep(10)
    meta = sheets.metadata(book.spreadsheet_id)
    props = next(
        x["properties"]
        for x in meta["sheets"]
        if x["properties"]["title"] == book.output_tab
    )
    pair_source = pair_controls = None
    if book.kind == "correlation_momentum":
        up = upstream_book(book)
        from source_freshness import wait_for_macro

        wait_for_macro(sheets, up)
        pair_source, pair_controls = sheets.values(
            up.spreadsheet_id, [a1(up.source_tab), a1(up.control_tab, "A:B")]
        )
    values, report = calculate(
        book, source, output, controls, pair_source, pair_controls
    )
    if book.kind == "correlation_momentum":
        LOG.info(
            "%s: Fisher lineage verified; excluded %s repeated-window cells; %s pairs have no finite momentum history",
            book.key,
            report["suppressed_repeated_fisher_cells"],
            sum(r["last_momentum_date"] is None for r in report["pair_status"]),
        )
    return {
        "book": book,
        "meta": meta,
        "properties": props,
        "source": source,
        "output": output,
        "controls": controls,
        "values": values,
        "report": report,
        "pair_source": pair_source,
        "pair_controls": pair_controls,
    }
