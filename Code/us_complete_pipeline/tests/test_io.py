import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pytest
from config import WORKBOOKS
from google_sheets import Sheets, column_name
from pipeline import verify, validate_before_publish
from validation import ValidationError


def test_batch_write_preserves_headers_and_dates_and_clears_stale(tmp_path):
    api = Sheets.__new__(Sheets)
    calls = []
    api.request = (
        lambda method, id, suffix="", **kw: calls.append((method, suffix, kw)) or {}
    )
    matrix = [[float(i)] * 3 for i in range(5)]
    count = api.publish(
        WORKBOOKS[0],
        {"gridProperties": {"rowCount": 20, "columnCount": 8}},
        matrix,
        tmp_path / "before.gz",
        {},
    )
    assert count == 15
    assert calls[0][1] == "/values:batchClear"
    assert calls[0][2]["json"]["ranges"] == ["'Correlation Score'!B3:H20"]
    assert calls[1][2]["json"]["data"][0]["range"] == "'Correlation Score'!B3:D7"
    assert (tmp_path / "before.gz").exists()


def test_large_matrix_is_batched(tmp_path):
    api = Sheets.__new__(Sheets)
    calls = []
    api.request = lambda method, id, suffix="", **kw: calls.append((suffix, kw)) or {}
    matrix = [[""] * 666 for _ in range(200)]
    api.publish(
        WORKBOOKS[1],
        {"gridProperties": {"rowCount": 250, "columnCount": 667}},
        matrix,
        tmp_path / "before.gz",
        {},
    )
    writes = [c for c in calls if c[0] == "/values:batchUpdate"]
    assert len(writes) == 3
    assert all(
        sum(len(r) for r in w[1]["json"]["data"][0]["values"]) <= 50000 for w in writes
    )


def test_verification_detects_stale_output_and_preserves_structure():
    class Fake:
        def values(self, *args, **kwargs):
            return [self.rows]

    api = Fake()
    state = {
        "book": WORKBOOKS[1],
        "values": [[""], [2.0]],
        "formulas": [["Date", "X"], [43831, "Y"], [43862, "=FORMULA()"], [43891]],
    }
    api.rows = [["Date", "X"], [43831, "Y"], [43862], [43891, 2.0]]
    assert verify(api, state)
    api.rows.append(["", 123])
    with pytest.raises(ValidationError, match="stale"):
        verify(api, state)
    api.rows = [["Date", "wrong"], [43831, "Y"], [43862], [43891, 2.0]]
    with pytest.raises(ValidationError, match="header"):
        verify(api, state)


def test_source_change_blocks_write(monkeypatch):
    monkeypatch.setattr("source_freshness.wait_for_macro", lambda *args: None)

    class Fake:
        def values(self, *args, **kwargs):
            return [
                [["changed"]],
                [["", "X"], ["Date", "Y"]],
                [[], ["Date"], [43831]],
                [["Correlation Window", 3]],
            ]

    state = {
        "book": WORKBOOKS[0],
        "source": [["Date", "X", "Y"]],
        "output": [["", "X"], ["Date", "Y"], [43831]],
        "controls": [["Correlation Window", 3]],
    }
    with pytest.raises(ValidationError, match="changed"):
        validate_before_publish(Fake(), state)


def test_column_names_have_no_fixed_pair_limit():
    assert column_name(667) == "YQ"
    assert column_name(703) == "AAA"


def test_spread_preserves_source_and_resizes_only_output(monkeypatch):
    from pipeline import load

    book = WORKBOOKS[1]
    source = [
        ["Date", "X", "Y"],
        ["2020-01-01", 1, 0],
        ["2020-02-01", 2, 0],
        ["2020-03-01", 4, 0],
        ["2020-04-01", 7, 0],
    ]
    output = [["", "X"], ["Date", "Y"]] + [[r[0]] for r in source[1:]]

    class Fake:
        def __init__(self):
            self.writes = []
            self.reads = []

        def metadata(self, id):
            assert id == book.spreadsheet_id
            return {
                "properties": {"title": book.title},
                "sheets": [
                    {
                        "properties": {
                            "title": name,
                            "sheetId": sid,
                            "gridProperties": {"rowCount": 5, "columnCount": 3},
                        }
                    }
                    for name, sid in [
                        (book.source_tab, 1545232009),
                        (book.output_tab, book.output_id),
                        (book.control_tab, 2026091101),
                    ]
                ],
            }

        def values(self, id, ranges, **kwargs):
            assert id == book.spreadsheet_id
            self.reads.extend(ranges)
            if len(ranges) == 1:
                return [output]
            return [source, output, [[book.control_key, 3]]]

        def batch(self, id, requests):
            assert id == book.spreadsheet_id
            for request in requests:
                assert (
                    request["updateSheetProperties"]["properties"]["sheetId"]
                    == book.output_id
                )
            self.writes.extend(requests)

    monkeypatch.setattr("source_freshness.wait_for_macro", lambda *args: source)
    api = Fake()
    state = load(api, book)
    assert state["source"] == source
    assert state["values"][-1][0] == pytest.approx(
        (7 - 7 / 3) / np.std([1, 2, 4], ddof=1)
    )
    assert "source_refresh" not in state
    assert all("Coincident" not in r for r in api.reads)
    assert api.writes


def test_formula_only_header_snapshot_omits_blank_second_row():
    # Sheets FORMULA rendering omits spill values; trailing blank rows may be absent.
    class Fake:
        def values(self, *args, **kwargs):
            return [[["=DATES()", "=PAIRS()"], [], ["", 1.0]]]

    state = {
        "book": WORKBOOKS[0],
        "values": [[1.0]],
        "formulas": [["=DATES()", "=PAIRS()"]],
    }
    assert verify(Fake(), state)
