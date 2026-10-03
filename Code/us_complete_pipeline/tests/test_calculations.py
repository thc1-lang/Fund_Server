import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import pytest
from config import WORKBOOKS, window_value
from validation import (
    source_frame,
    pair_definitions,
    parse_date,
    output_dates,
    ValidationError,
)
from correlation import rolling_correlation, fisher_transform
from spread import spread_series, rolling_statistics, spread_zscore
from pipeline import calculate, comparison


def test_exact_headers_and_missing_mapping():
    frame, _ = source_frame([["Date", "X", "Y"], ["2020-01-01", 1, 2]])
    pairs, issues = pair_definitions(
        [["Date", "X", "x", "X", ""], ["", "Y", "Y", "", ""]], frame
    )
    assert pairs[0][2] is None
    assert len(issues) == 3


def test_duplicate_header_invalidates_mapping():
    frame, issues = source_frame([["Date", "X", "X", "Y"], ["2020-01-01", 1, 2, 3]])
    assert list(frame.columns) == ["Y"] and issues
    assert pair_definitions([["", "X"], ["", "Y"]], frame)[0][0][2]


def test_dates_are_actual_dates_and_source_sorted():
    frame, _ = source_frame(
        [
            ["Date", "X", "Y"],
            ["31/03/2020", 3, 1],
            ["2020-01-31", 1, 3],
            ["29/02/2020", 2, 2],
        ]
    )
    assert list(frame["X"]) == [1, 2, 3]
    assert parse_date(43861) == pd.Timestamp("2020-01-31")


@pytest.mark.parametrize("value", ["01/02/03", "invalid", "2020-02-30", True, 43861.5])
def test_bad_dates_fail(value):
    with pytest.raises(ValidationError):
        parse_date(value)


def test_duplicate_dates_fail():
    with pytest.raises(ValidationError):
        source_frame([["Date", "X"], ["2020-01-01", 1], ["2020-01-01", 2]])


def test_pearson_and_fisher_manual():
    # Prior x=(1,2,3), y=(1,3,2): r=1/2.
    result = rolling_correlation([1, 2, 3, 100], [1, 3, 2, -100], 3)
    assert np.isnan(result[:3]).all()
    assert result[3] == pytest.approx(0.5 * np.log(3))
    assert fisher_transform([0, 0.5, -0.5])[1] == pytest.approx(np.arctanh(0.5))


def test_perfect_and_constant_correlation_blank():
    assert np.isnan(rolling_correlation([1, 2, 3, 4], [2, 4, 6, 8], 3)).all()
    assert np.isnan(rolling_correlation([1, 1, 1, 1], [2, 4, 6, 8], 3)).all()
    assert np.isnan(fisher_transform([1, -1, 1.1, np.inf])).all()


def test_directional_spread_and_aligned_series():
    np.testing.assert_equal(spread_series([1, 2, np.nan], [3, 1, 5]), [-2, 1, np.nan])


def test_sample_mean_std_and_zscore_manual():
    spread = [1, 2, 3, 5]
    mean, std = rolling_statistics(spread, 3)
    assert mean[-1] == 2 and std[-1] == 1
    assert spread_zscore(spread, [0, 0, 0, 0], 3)[-1] == 3


def test_missing_values_are_skipped_in_observation_windows():
    x = [1, 2, np.nan, 4, 5, 6, 7]
    result = spread_zscore(x, np.zeros(7), 3)
    assert np.isnan(result[:4]).all()
    assert result[4] == pytest.approx(
        (5 - np.mean([1, 2, 4])) / np.std([1, 2, 4], ddof=1)
    )
    assert result[-1] == pytest.approx(2)
    c = rolling_correlation(x, [1, 3, 2, 6, 4, 8, 5], 3)
    assert np.isnan(c[:4]).all()
    assert c[4] == pytest.approx(np.arctanh(np.corrcoef([1, 2, 4], [1, 3, 6])[0, 1]))


@pytest.mark.parametrize("kind", ["correlation", "spread"])
def test_missing_current_input_blanks_only_affected_relationship(kind):
    dates = pd.date_range("2026-01-31", periods=8, freq="ME")
    x = [1, 2, 4, 3, 5, 7, 6, 8]
    y = [2, 5, 1, 4, "", 3, 6, 2]
    z = [3, 1, 5, 2, 6, 4, 7, 8]
    source = [["Date", "X", "Y", "Z"]] + [
        [str(d.date()), a, b, c] for d, a, b, c in zip(dates, x, y, z)
    ]
    output = [["", "X", "X"], ["Date", "Y", "Z"]] + [
        [str(d.date())] for d in dates
    ]
    book = next(b for b in WORKBOOKS if b.kind == kind)
    values, _ = calculate(book, source, output, [[book.control_key, 3]])
    assert values[4][0] == ""
    assert values[4][1] != ""
    assert values[5][0] != ""


@pytest.mark.parametrize("kind", ["correlation", "spread"])
def test_blank_is_missing_but_numeric_zero_is_a_valid_pair_value(kind):
    source = [
        ["Date", "X", "Y"],
        ["2026-01-31", 1, 2],
        ["2026-02-28", 2, 5],
        ["2026-03-31", 4, 1],
        ["2026-04-30", 3, ""],
    ]
    output = [["", "X"], ["Date", "Y"]] + [[r[0]] for r in source[1:]]
    book = next(b for b in WORKBOOKS if b.kind == kind)
    frame, _ = source_frame(source)
    assert np.isnan(frame.loc["2026-04-30", "Y"])
    blank, _ = calculate(book, source, output, [[book.control_key, 3]])
    assert blank[-1] == [""]
    source[-1][2] = 0
    frame, _ = source_frame(source)
    assert frame.loc["2026-04-30", "Y"] == 0
    zero, _ = calculate(book, source, output, [[book.control_key, 3]])
    assert zero[-1][0] != ""


def test_numeric_strings_bools_and_errors_are_missing():
    frame, issues = source_frame(
        [
            ["Date", "X"],
            ["2020-01-01", 0],
            ["2020-02-01", "2"],
            ["2020-03-01", True],
            ["2020-04-01", "#N/A"],
        ]
    )
    assert frame.iloc[0, 0] == 0 and frame.iloc[1:, 0].isna().all() and len(issues) == 3


def test_insufficient_history_and_zero_std():
    assert np.isnan(spread_zscore([1, 2], [0, 0], 3)).all()
    assert np.isnan(spread_zscore([1, 1, 1, 5], [0, 0, 0, 0], 3)).all()


def test_no_lookahead_correlation_and_spread():
    x = np.array([1.0, 2, 3, 5, 4, 9])
    y = np.array([1.0, 3, 2, 1, 5, 8])
    initial = rolling_correlation(x, y, 3)
    changed = x.copy()
    changed[3:] = [999, 1000, -999]
    assert rolling_correlation(changed, y, 3)[3] == pytest.approx(initial[3])
    mu, sd = rolling_statistics(x - y, 3)
    mu2, sd2 = rolling_statistics(changed - y, 3)
    assert mu[3] == mu2[3] and sd[3] == sd2[3]
    # Current spread affects only numerator at t.
    assert spread_zscore(changed, y, 3)[3] == pytest.approx(
        (changed[3] - y[3] - mu[3]) / sd[3]
    )


def fixture(n):
    dates = (
        pd.date_range("2020-01-01", periods=n, freq="MS").strftime("%Y-%m-%d").tolist()
    )
    source = [["Date", "X", "Y"]] + [
        [d, float(i * i), float(i % 3)] for i, d in enumerate(dates)
    ]
    output = [["", "X"], ["Date", "Y"]] + [[d] for d in dates]
    return source, output


def test_append_data_automatically_extends_history():
    book = WORKBOOKS[1]
    source, output = fixture(6)
    first, _ = calculate(book, source, output, [[book.control_key, 3]])
    source2, output2 = fixture(7)
    extended, _ = calculate(book, source2, output2, [[book.control_key, 3]])
    assert len(extended) == 7 and first == extended[:6] and extended[-1][0] != ""


def test_output_is_joined_by_date_not_position():
    book = WORKBOOKS[1]
    source, output = fixture(6)
    complete, _ = calculate(book, source, output, [[book.control_key, 3]])
    # A2 contains earliest date (actual Spread Score layout).
    shifted = [output[0], [source[1][0], "Y"]] + output[3:]
    actual, _ = calculate(book, source, shifted, [[book.control_key, 3]])
    assert actual == complete[1:]


def test_output_missing_unsorted_or_duplicate_dates_fail():
    source, output = fixture(5)
    frame, _ = source_frame(source)
    with pytest.raises(ValidationError):
        output_dates(output[:-1], frame.index)
    with pytest.raises(ValidationError):
        output_dates(output[:2] + list(reversed(output[2:])), frame.index)
    with pytest.raises(ValidationError):
        output_dates(output + [output[-1]], frame.index)


def test_control_window_changes_calculations():
    source, output = fixture(6)
    book = WORKBOOKS[1]
    a, _ = calculate(book, source, output, [[book.control_key, 3]])
    b, _ = calculate(book, source, output, [[book.control_key, 4]])
    assert a[3][0] != "" and b[3][0] == ""


@pytest.mark.parametrize("v", [1, 0, -1, 2.5, True, "oops", float("inf")])
def test_bad_window(v):
    with pytest.raises(ValueError):
        window_value([["Window", v]], "Window")


def test_duplicate_setting_rejected():
    with pytest.raises(ValueError):
        window_value([["Window", 3], ["Window", 3]], "Window")


def test_missing_mapping_blanks_only_affected_pair():
    source, output = fixture(6)
    output[0].append("missing")
    output[1].append("Y")
    result, report = calculate(
        WORKBOOKS[1], source, output, [[WORKBOOKS[1].control_key, 3]]
    )
    assert all(row[1] == "" for row in result)
    assert result[-1][0] != "" and report["invalid_indicator_mappings"] == 1


def test_upstream_write_prohibited_before_network():
    from google_sheets import Sheets

    api = Sheets.__new__(Sheets)
    with pytest.raises(ValueError, match="prohibited"):
        api.request(
            "POST",
            "19E_Za0DOHMY_9AFSPatK8Cp2vCQypI81QnB3Hz4ve9c",
            ":batchUpdate",
            json={},
        )


def test_new_tail_and_historical_blank_mismatch_distinguished():
    old = np.array([[np.nan], [1.0], [np.nan]])
    new = np.array([[2.0], [1.0], [3.0]])
    c = comparison(old, new)
    assert c["new_tail_numeric"] == 1 and c["historical_blank_mismatches"] == 1


@pytest.mark.parametrize("window", [2, 3, 5, 24])
def test_every_rolling_row_against_independent_prior_pair_oracle(window):
    import statistics

    rng = np.random.default_rng(42)
    x = rng.normal(size=60)
    y = 0.3 * x + rng.normal(size=60)
    x[[1, 4, 6, 20, 39]] = np.nan
    y[[2, 5, 16, 29, 45]] = np.nan
    c = rolling_correlation(x, y, window)
    mu, sd = rolling_statistics(x - y, window)
    z = spread_zscore(x, y, window)
    for t in range(len(x)):
        pairs = [
            (x[i], y[i]) for i in range(t) if np.isfinite(x[i]) and np.isfinite(y[i])
        ][-window:]
        if len(pairs) < window:
            assert np.isnan([c[t], mu[t], sd[t], z[t]]).all()
            continue
        xx, yy = zip(*pairs)
        h = [a - b for a, b in pairs]
        if window > 2 and np.isfinite(x[t]) and np.isfinite(y[t]):
            assert c[t] == pytest.approx(
                np.arctanh(statistics.correlation(xx, yy))
            )
        else:
            assert np.isnan(c[t])
        assert mu[t] == pytest.approx(statistics.mean(h))
        assert sd[t] == pytest.approx(statistics.stdev(h))
        if np.isfinite(x[t]) and np.isfinite(y[t]):
            assert z[t] == pytest.approx(
                (x[t] - y[t] - statistics.mean(h)) / statistics.stdev(h)
            )
        else:
            assert np.isnan(z[t])
        # Changing current and future data must not change ANY historical statistic at t.
        changed_x = x.copy()
        changed_y = y.copy()
        changed_x[t + 1 :] = 1000
        changed_y[t + 1 :] = -1000
        np.testing.assert_allclose(
            rolling_correlation(changed_x, changed_y, window)[: t + 1],
            c[: t + 1],
            equal_nan=True,
        )
        mm, ss = rolling_statistics(changed_x - changed_y, window)
        np.testing.assert_allclose(mm[: t + 1], mu[: t + 1], equal_nan=True)
        np.testing.assert_allclose(ss[: t + 1], sd[: t + 1], equal_nan=True)


def test_quarterly_observations_in_monthly_grid_and_gaps():
    dates = pd.date_range("2010-01-31", periods=85, freq="ME")
    rows = [["Date", "X", "Y"]]
    k = 0
    for i, d in enumerate(dates):
        if i % 3 == 2 and i != 20:
            k += 1
            rows.append([str(d.date()), k * k, k % 4])
        else:
            rows.append([str(d.date()), "", ""])
    output = [["", "X"], ["Date", "Y"]] + [[r[0]] for r in rows[1:]]
    result, report = calculate(
        WORKBOOKS[1], rows, output, [[WORKBOOKS[1].control_key, 24]]
    )
    valid = [r for r in rows[1:] if r[1] != ""]
    assert len(valid) > 24
    first_date = valid[24][0]
    assert report["first_calculated_date"] == first_date
    h = [r[1] - r[2] for r in valid[:24]]
    pos = next(i for i, r in enumerate(rows[1:]) if r[0] == first_date)
    assert result[pos][0] == pytest.approx(
        (valid[24][1] - valid[24][2] - np.mean(h)) / np.std(h, ddof=1)
    )
    # Removing blank months entirely gives the same quarterly-date scores.
    compact = [rows[0]] + valid
    compact_output = output[:2] + [[r[0]] for r in valid]
    expected, _ = calculate(
        WORKBOOKS[1], compact, compact_output, [[WORKBOOKS[1].control_key, 24]]
    )
    actual = [result[i] for i, r in enumerate(rows[1:]) if r[1] != ""]
    assert actual == expected
