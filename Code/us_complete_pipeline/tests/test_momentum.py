import math
import statistics
import numpy as np
import pandas as pd
import pytest
from momentum import observation_change, momentum_zscore
from momentum_pipeline import (
    calculate,
    fisher_frame,
    desired_formulas,
    canonical,
    assert_fresh,
)
from config import MOMENTUM_WORKBOOKS, momentum_lookback
from validation import ValidationError


def oracle(values, m, w):
    seen = []
    changes = []
    out = []
    for value in values:
        score = math.nan
        if math.isfinite(value):
            if len(seen) >= m:
                change = value - seen[-m]
                if len(changes) >= w:
                    hist = changes[-w:]
                    sd = statistics.stdev(hist)
                    if sd > 0:
                        score = (change - statistics.mean(hist)) / sd
                changes.append(change)
            seen.append(value)
        out.append(score)
    return out


@pytest.mark.parametrize("m,w", [(1, 2), (3, 4), (3, 24), (5, 10)])
@pytest.mark.parametrize(
    "values",
    [
        np.arange(80.0) ** 2,
        -np.arange(80.0) ** 2,
        np.ones(80),
        np.arange(80.0),
        np.sin(np.arange(80.0)),
        np.where(np.arange(80) % 7 == 0, np.nan, np.arange(80.0) ** 2),
    ],
)
def test_independent_oracle(values, m, w):
    np.testing.assert_allclose(
        momentum_zscore(values, m, w), oracle(values, m, w), atol=1e-12, equal_nan=True
    )


def test_manual_m3_sample_std():
    # Squared series: m=3 changes are 9,15,21,27. Prior mean=15, sample sd=6.
    s = np.arange(7.0) ** 2
    np.testing.assert_allclose(observation_change(s, 3)[3:], [9, 15, 21, 27])
    assert momentum_zscore(s, 3, 3)[6] == 2
    assert np.isnan(momentum_zscore(s, 3, 3)[:6]).all()
    assert momentum_zscore(-s, 3, 3)[6] == -2


def test_no_current_or_future_in_normalisation():
    values = np.arange(12.0) ** 2
    original = momentum_zscore(values, 3, 3)
    changed = values.copy()
    changed[6] += 60
    changed[7:] = 999
    result = momentum_zscore(changed, 3, 3)
    np.testing.assert_allclose(result[:6], original[:6], equal_nan=True)
    assert (
        result[6] == 12
    )  # (87 - 15)/6; current change does not alter historical stats.


def test_gaps_are_valid_observations_and_blank_current():
    x = np.array([0, np.nan, 1, 4, np.nan, 9, 16, 25, 36.0])
    ch = observation_change(x, 3)
    np.testing.assert_allclose(ch[[5, 6, 7, 8]], [9, 15, 21, 27])
    assert momentum_zscore(x, 3, 3)[8] == 2
    assert np.isnan(ch[[1, 4]]).all()


def test_fisher_values_used_directly_and_reordered_by_pair():
    from correlation import rolling_correlation
    from config import upstream_book

    dates = pd.date_range("2000-01-01", periods=40, freq="QS")
    x = np.sin(np.arange(40.0))
    y = np.cos(np.arange(40.0) / 3)
    z = -x
    a = rolling_correlation(x, y, 4)
    b = rolling_correlation(y, z, 4)
    cell = lambda v: float(v) if np.isfinite(v) else ""
    source = [[4, "X", "Y"], ["Date", "Y", "Z"]] + [
        [str(d.date()), cell(u), cell(v)] for d, u, v in zip(dates, a, b)
    ]
    raw = [["Date", "X", "Y", "Z"]] + [
        [str(d.date()), u, v, t] for d, u, v, t in zip(dates, x, y, z)
    ]
    output = [[4, "Y", "X"], ["Date", "Z", "Y"]] + [[str(d.date())] for d in dates]
    book = MOMENTUM_WORKBOOKS[1]
    values, report = calculate(
        book,
        source,
        output,
        [["Momentum Lookback", 3], ["Momentum Z-Score Window", 3]],
        raw,
        [[upstream_book(book).control_key, 4]],
    )
    actual = np.array([[np.nan if v == "" else v for v in r] for r in values])
    np.testing.assert_allclose(actual[:, 0], oracle(b, 3, 3), equal_nan=True)
    np.testing.assert_allclose(actual[:, 1], oracle(a, 3, 3), equal_nan=True)
    assert report["pairs"] == 2 and report["source_dates"] == 40


def test_spread_direction_header_matching_and_appended_data():
    dates = pd.date_range("2020-01-01", periods=8, freq="QS")
    source = [["Date", "Y", "X"]] + [
        [str(d.date()), 10, 10 + i * i] for i, d in enumerate(dates)
    ]
    output = [["Date", "X"], [str(dates[0].date()), "Y"]] + [
        [str(d.date())] for d in dates[1:]
    ]
    ctrl = [["Momentum Lookback", 3], ["Momentum Z-Score Window", 3]]
    v, r = calculate(MOMENTUM_WORKBOOKS[0], source, output, ctrl)
    assert v[5] == [2]
    v0, _ = calculate(MOMENTUM_WORKBOOKS[0], source[:-1], output[:-1], ctrl)
    assert len(v) == len(v0) + 1
    assert v[:-1] == v0
    for c in (
        [["Momentum Lookback", 1], ["Momentum Z-Score Window", 3]],
        [["Momentum Lookback", 3], ["Momentum Z-Score Window", 2]],
    ):
        changed, _ = calculate(MOMENTUM_WORKBOOKS[0], source, output, c)
        assert changed != v


def test_duplicate_fisher_pair_rejected_not_substituted():
    with pytest.raises(ValidationError):
        fisher_frame([[24, "X", "X"], ["Date", "Y", "Y"], ["2020-01-01", 1, 2]])


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "bad"])
def test_invalid_lookback(value):
    with pytest.raises(ValueError):
        momentum_lookback([["Momentum Lookback", value]])


def test_imports_dynamic_and_only_existing_score_sources():
    formulas = desired_formulas(MOMENTUM_WORKBOOKS[1], 700, 700)
    assert len(formulas) < 20
    assert any(
        "ZX" in f for _, _, f in formulas
    )  # 700 columns, beyond original pair width.
    assert all("9881" not in f and "3522" not in f for _, _, f in formulas)
    assert "MAX(FILTER" in formulas[-1][2]


def test_stale_import_rejected():
    class Fake:
        def values(self, *args):
            return [
                [["Date", "X"], ["2020-01-01", 2]],
                [["Date", "X"], ["2020-01-01", "Y"]],
            ]

    with pytest.raises(ValidationError, match="stale"):
        assert_fresh(
            Fake(),
            MOMENTUM_WORKBOOKS[0],
            [["Date", "X"], ["2020-01-01", 1]],
            [["Date", "X"], ["2020-01-01", "Y"]],
        )


def test_momentum_only_client_blocks_both_original_workbooks():
    from google_sheets import Sheets
    from config import BASE_WORKBOOKS

    api = Sheets.__new__(Sheets)
    api.write_ids = {b.spreadsheet_id for b in MOMENTUM_WORKBOOKS}
    for book in BASE_WORKBOOKS:
        with pytest.raises(ValueError, match="prohibited"):
            api.request("POST", book.spreadsheet_id, ":batchUpdate", json={})


def test_invalid_mapping_produces_blank_with_pair_log():
    book = MOMENTUM_WORKBOOKS[0]
    source = [["Date", "X", "Y"], ["2020-01-01", 1, 2], ["2020-04-01", 2, 3]]
    output = [["Date", "MISSING", ""], ["2020-01-01", "Y", ""], ["2020-04-01"]]
    values, report = calculate(
        book, source, output, [["Momentum Lookback", 1], ["Momentum Z-Score Window", 2]]
    )
    assert values == [["", ""]]
    assert report["invalid_indicator_mappings"] == 2
    assert "MISSING" in str(report["issues"])
