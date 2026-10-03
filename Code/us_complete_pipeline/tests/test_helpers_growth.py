import copy
import numpy as np
import pandas as pd
import pytest
from config import WORKBOOKS, BASE_WORKBOOKS, MOMENTUM_WORKBOOKS
from pipeline import calculate, load
from momentum_pipeline import (
    calculate as momentum_calculate,
    prepare as prepare_imports,
    desired_formulas,
)
from calculation_helper import trace, pair_labels
from google_sheets import a1


def fixture(book, n=50):
    dates = pd.date_range("2000-03-31", periods=n, freq="QE")
    source = [["Date", "X", "Y"]] + [
        [str(d.date()), float(i * i + 2), float(i % 5)] for i, d in enumerate(dates)
    ]
    output = [["", "X"], ["Date", "Y"]] + [[str(d.date())] for d in dates]
    controls = [[book.control_key, 4]]
    if book.kind.endswith("_momentum"):
        controls.append(["Momentum Lookback", 3])
    if book.kind == "correlation_momentum":
        from correlation import rolling_correlation

        f = rolling_correlation(
            [r[1] for r in source[1:]], [r[2] for r in source[1:]], 4
        )
        source = [[4, "X"], ["Date", "Y"]] + [
            [str(d.date()), float(v) if np.isfinite(v) else ""]
            for d, v in zip(dates, f)
        ]
    return source, output, controls


@pytest.mark.parametrize("book", WORKBOOKS, ids=lambda b: b.kind)
def test_helper_independent_scores_and_cutoff(book):
    source, output, controls = fixture(book)
    fn = momentum_calculate if book.kind.endswith("_momentum") else calculate
    extra = {}
    if book.kind == "correlation_momentum":
        from config import upstream_book

        up = upstream_book(book)
        raw, _, _ = fixture(up)
        extra = {"pair_source": raw, "pair_controls": [[up.control_key, 4]]}
    values, report = fn(book, source, output, controls, **extra)
    state = {
        "book": book,
        "source": source,
        "output": output,
        "values": values,
        "report": report,
        **extra,
    }
    headers, table, matched = trace(state, 0)
    assert len(table) == 50 and matched > 20
    endcol = headers.index("Window last date")
    for row in table:
        if row[endcol]:
            assert row[endcol] < row[0]
    assert pair_labels(output) == ["B | X | Y"]
    wrong = copy.deepcopy(state)
    idx = next(i for i, r in enumerate(values) if r[0] != "")
    wrong["values"][idx][0] += 1
    with pytest.raises(ValueError, match="Helper differs"):
        trace(wrong, 0)


@pytest.mark.parametrize(
    "book",
    [b for b in WORKBOOKS if b.kind != "correlation_momentum"],
    ids=lambda b: b.kind,
)
def test_append_quarterly_data_and_gap_all_scores(book):
    source, out, ctrl = fixture(book, 50)
    fn = momentum_calculate if book.kind.endswith("_momentum") else calculate
    old, oldreport = fn(book, source, out, ctrl)
    extended, extendedout, _ = fixture(book, 52)
    # New dated row can contain a gap without zero imputation.
    extended[-2][1] = ""
    new, report = fn(book, extended, extendedout, ctrl)
    assert len(new) == len(old) + 2
    assert new[: len(old)] == old
    assert report["source_dates"] == oldreport["source_dates"] + 2
    if book.kind != "correlation":
        assert new[-2][0] == ""
    assert new[-1][0] != ""


@pytest.mark.parametrize("book", BASE_WORKBOOKS, ids=lambda b: b.kind)
def test_base_grid_edge_expands_before_date_validation(book, monkeypatch):
    source, out, ctrl = fixture(book, 50)
    monkeypatch.setattr("source_freshness.wait_for_macro", lambda *args: source)

    class Fake:
        expanded = False

        def metadata(self, id):
            return {
                "properties": {"title": book.title + " renamed"},
                "sheets": [
                    {
                        "properties": {
                            "title": name,
                            "sheetId": sid,
                            "gridProperties": {
                                "rowCount": 52 if name == book.source_tab else 10,
                                "columnCount": 3,
                            },
                        }
                    }
                    for name, sid in [
                        (book.source_tab, 123),
                        (book.output_tab, book.output_id),
                        (book.control_tab, 456),
                    ]
                ],
            }

        def values(self, id, ranges, **kw):
            if len(ranges) == 1:
                return [out if self.expanded else out[:10]]
            return [source, out[:10], ctrl]

        def batch(self, id, requests):
            assert len(requests) == 1
            p = requests[0]["updateSheetProperties"]["properties"]
            assert p["sheetId"] == book.output_id and p["gridProperties"][
                "rowCount"
            ] >= len(out)
            self.expanded = True

    api = Fake()
    state = load(api, book)
    assert api.expanded and len(state["values"]) == 50


@pytest.mark.parametrize("book", MOMENTUM_WORKBOOKS, ids=lambda b: b.kind)
def test_momentum_import_and_output_capacity_grow(book):
    from config import upstream_book

    up = upstream_book(book)
    is_spread = book.kind == "spread_momentum"
    source, out, ctrl = fixture(book, 50)
    pairs = [["", "X"], ["Date", "Y"]]
    sw = max(map(len, source[:2]))
    pw = 2
    formulas = desired_formulas(book, sw, pw)
    names = [book.source_tab, book.output_tab] + (
        ["Imported Pair Definitions"] if is_spread else []
    )

    class Fake:
        def __init__(self):
            self.writes = []

        def metadata(self, id):
            if id == up.spreadsheet_id:
                return {
                    "sheets": [
                        {
                            "properties": {
                                "title": up.source_tab if is_spread else up.output_tab,
                                "gridProperties": {"rowCount": 100},
                            }
                        }
                    ]
                }
            return {
                "properties": {"title": book.title + " renamed"},
                "sheets": [
                    {
                        "properties": {
                            "title": name,
                            "sheetId": (
                                book.output_id if name == book.output_tab else 123 + i
                            ),
                            "gridProperties": {
                                "rowCount": 10,
                                "columnCount": sw if name == book.source_tab else pw,
                            },
                        }
                    }
                    for i, name in enumerate(names)
                ],
            }

        def values(self, id, ranges, formulas=False):
            if id == up.spreadsheet_id:
                return [source, pairs]
            if len(ranges) == 1:
                return [
                    [
                        [
                            v
                            for t, c, v in desired_formulas(book, sw, pw)
                            if t == book.source_tab
                        ][0]
                    ]
                ]
            return [[[f]] for _, _, f in desired_formulas(book, sw, pw)]

        def batch(self, id, req):
            assert id == book.spreadsheet_id
            self.writes += req

    fake = Fake()
    prepare_imports(fake, book)
    assert len(fake.writes) == 2
    assert all(
        v["updateSheetProperties"]["properties"]["gridProperties"]["rowCount"] >= 100
        for v in fake.writes
    )


def test_helper_publication_roundtrip_and_frozen_boundary(tmp_path):
    from calculation_helper import publish, trace, pair_labels

    book = WORKBOOKS[0]
    source, out, ctrl = fixture(book)
    values, report = calculate(book, source, out, ctrl)
    state = {
        "book": book,
        "source": source,
        "output": out,
        "values": values,
        "report": report,
    }
    headers, table, matched = trace(state, 0)
    helper = {
        "selection": pair_labels(out)[0],
        "labels": pair_labels(out),
        "headers": headers,
        "table": table,
        "matched": matched,
    }

    class Fake:
        def metadata(self, id):
            return {"sheets": []}

        def batch(self, id, requests):
            for req in requests:
                if "mergeCells" in req:
                    r = req["mergeCells"]["range"]
                    assert (
                        r["startColumnIndex"] >= 1
                    )  # Never merge across frozen date column A.

        def request(self, *args, **kwargs):
            pass

        def write_values(self, id, data):
            self.rows = data[0]["values"]

        def values(self, id, ranges):
            return [self.rows[4:]]

    publish(Fake(), state, helper, tmp_path)
    assert state["report"]["helper"]["verified"]
