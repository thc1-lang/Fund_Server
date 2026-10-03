"""Mixed-frequency regression tests with independent scalar calculation checks."""

import copy
import numpy as np
import pandas as pd
import pytest
from config import MOMENTUM_WORKBOOKS, upstream_book
from correlation import rolling_correlation
from momentum import momentum_zscore
from momentum_pipeline import calculate, correlation_events, fisher_frame, assert_fresh
from calculation_helper import trace
from validation import ValidationError

BOOKS = [b for b in MOMENTUM_WORKBOOKS if b.kind == "correlation_momentum"]


def fixture(book, step=12, n=840, gap=None):
    dates = pd.date_range("1950-01-31", periods=n, freq="ME")
    x = np.where(np.arange(n) % step == 0, np.sin(np.arange(n) / step * 0.31), np.nan)
    y = np.cos(np.arange(n) / step * 0.71)
    if gap is not None:
        y[gap] = np.nan
    fisher = rolling_correlation(x, y, 24)
    cell = lambda v: float(v) if np.isfinite(v) else ""
    raw = [["Date", "X", "Y"]] + [
        [str(d.date()), cell(a), cell(b)] for d, a, b in zip(dates, x, y)
    ]
    source = [[24, "X"], ["Date", "Y"]] + [
        [str(d.date()), cell(v)] for d, v in zip(dates, fisher)
    ]
    output = [[24, "X"], ["Date", "Y"]] + [[str(d.date())] for d in dates]
    controls = [["Momentum Lookback", 3], ["Momentum Z-Score Window", 24]]
    upstream_controls = [[upstream_book(book).control_key, 24]]
    return source, output, controls, raw, upstream_controls


@pytest.mark.parametrize("book", BOOKS, ids=lambda b: b.dataset)
@pytest.mark.parametrize("step", [1, 3, 12])
def test_updates_scalar_oracle_causality_and_missing_data(book, step):
    args = fixture(book, step, gap=step * 35)
    values, report = calculate(book, *args)
    state = {
        "book": book,
        "source": args[0],
        "output": args[1],
        "values": values,
        "report": report,
        "pair_source": args[3],
    }
    headers, table, matched = trace(state, 0)
    assert matched > 0
    for i, row in enumerate(table):
        fresh = i % step == 0 and i != step * 35
        assert row[headers.index("New paired observation")] == (
            "Yes" if fresh else "No"
        )
        if not fresh:
            assert values[i] == [""]
    # A shorter history has exactly the same previously calculated scores.
    short = fixture(book, step, n=720, gap=step * 35)
    previous, _ = calculate(book, *short)
    assert previous == values[:720]
    if step == 1:
        # Dense histories use each valid current pair's Fisher value.
        frame, _ = fisher_frame(args[0])
        f = frame.iloc[:, 0].to_numpy().copy()
        expected = momentum_zscore(f, 3, 24)
        actual = np.array([r[0] if r[0] != "" else np.nan for r in values])
        np.testing.assert_allclose(actual, expected, equal_nan=True)


@pytest.mark.parametrize("book", BOOKS, ids=lambda b: b.dataset)
def test_annual_warmup_requires_real_observations(book):
    args = fixture(book, n=600)
    values, report = calculate(book, *args)
    assert all(
        r == [""] for r in values
    )  # Fewer than 24+3+24 actual paired observations.
    assert report["suppressed_repeated_fisher_cells"] == 0
    assert report["pair_status"][0]["status"] == "No finite momentum history"


def test_unchanged_correlation_on_new_input_still_counts():
    book = BOOKS[0]
    dates = pd.date_range("2000-01-31", periods=20, freq="ME")
    x = np.resize([1.0, 2.0, 4.0, 3.0], 20)
    y = np.resize([2.0, 1.0, 3.0, 4.0], 20)
    f = rolling_correlation(x, y, 4)
    raw = [["Date", "X", "Y"]] + [[str(d.date()), a, b] for d, a, b in zip(dates, x, y)]
    frame = pd.DataFrame({("X", "Y"): f}, index=dates)
    masks, _ = correlation_events(
        book, frame, raw, [[upstream_book(book).control_key, 4]]
    )
    assert masks[("X", "Y")][1:].all()
    assert len(np.unique(f[4:])) == 1  # Not deduplicated by value.


@pytest.mark.parametrize(
    "tamper",
    ["missing_source", "value", "blank", "control", "duplicate_date", "invalid_number"],
)
def test_lineage_fails_closed(tamper):
    book = BOOKS[0]
    args = list(fixture(book, step=1, n=80))
    if tamper == "missing_source":
        args[3] = None
    elif tamper == "value":
        args[0][-1][1] += 1
    elif tamper == "blank":
        args[0][-1][1] = ""
    elif tamper == "control":
        args[4][0][1] = 25
    elif tamper == "duplicate_date":
        args[3][-1][0] = args[3][-2][0]
    elif tamper == "invalid_number":
        args[3][-1][1] = "123"
    with pytest.raises(ValidationError):
        calculate(book, *args)


def test_missing_display_dates_preserve_strict_cutoff():
    book = BOOKS[0]
    args = list(fixture(book, step=3, n=240))
    # Removing an unpaired display row does not move its preceding pair's update.
    del args[0][102]
    del args[1][102]
    values, report = calculate(book, *args)
    headers, table, _ = trace(
        {
            "book": book,
            "source": args[0],
            "output": args[1],
            "values": values,
            "report": report,
            "pair_source": args[3],
        },
        0,
    )
    assert table[99][headers.index("New paired observation")] == "Yes"
    assert table[100][headers.index("New paired observation")] == "No"


def test_concurrent_pair_source_or_controls_abort():
    book = BOOKS[0]
    source, output, ctrl, raw, upctrl = fixture(book, step=1, n=80)

    class Fake:
        calls = 0

        def values(self, *args):
            self.calls += 1
            if self.calls == 1:
                return [source, output[:2]]
            altered = copy.deepcopy(raw)
            altered[-1][1] += 1
            return [altered, upctrl]

    with pytest.raises(ValidationError, match="changed during calculation"):
        assert_fresh(Fake(), book, source, output, raw, upctrl)


def test_undefined_fisher_update_does_not_reappear_as_stale_momentum():
    book = BOOKS[0]
    args = list(fixture(book, step=3, n=240))
    # A long constant X segment makes several correlation windows undefined;
    # later varying inputs recover. Empty values must never count as updates.
    for i in range(80, 165):
        if args[3][i + 1][1] != "":
            args[3][i + 1][1] = 1.0
    from validation import source_frame

    raw, _ = source_frame(args[3])
    f = rolling_correlation(raw.X, raw.Y, 24)
    for row, v in zip(args[0][2:], f):
        row[1] = float(v) if np.isfinite(v) else ""
    values, report = calculate(book, *args)
    headers, table, _ = trace(
        {
            "book": book,
            "source": args[0],
            "output": args[1],
            "values": values,
            "report": report,
            "pair_source": args[3],
        },
        0,
    )
    assert np.isnan(f[155:165]).any()
    for i, v in enumerate(f):
        if not np.isfinite(v):
            assert values[i] == [""]
    assert report["numeric_cells"] > 0
