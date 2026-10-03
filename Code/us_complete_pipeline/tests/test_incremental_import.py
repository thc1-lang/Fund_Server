from datetime import date

import pytest

import monthly_indicators as m


def serial(value: date) -> int:
    return (value - date(1899, 12, 30)).days


def test_reference_period_dates_remain_the_published_baseline(monkeypatch):
    monkeypatch.setattr(m, "FULL_HISTORY_MODE", True)
    monthly = {
        "observation_date": date(2026, 1, 1),
        "release_date": date(2026, 2, 3),
        "selection_date": date(2026, 1, 1),
        "period_aligned": True,
        "value": 51.2,
    }
    quarterly = {
        "observation_date": date(2026, 1, 1),
        "release_date": date(2026, 4, 25),
        "selection_date": date(2026, 3, 1),
        "period_aligned": True,
        "native_frequency": "Quarterly",
        "value": 123.4,
    }
    annual = {
        "observation_date": date(2025, 1, 1),
        "release_date": date(2026, 2, 1),
        "selection_date": date(2025, 12, 1),
        "period_aligned": True,
        "native_frequency": "Annual",
        "value": 10.5,
    }

    assert m.output_month_records_for_indicator([monthly]) == {
        date(2026, 1, 1): 51.2
    }
    assert m.output_month_records_for_indicator([quarterly]) == {
        date(2026, 3, 1): 123.4
    }
    assert m.output_month_records_for_indicator([annual]) == {
        date(2025, 12, 1): 10.5
    }


def test_incremental_table_fills_blanks_appends_and_preserves_populated_values():
    existing = [
        ["Date", "Indicator A", "Indicator B"],
        [serial(date(2026, 1, 31)), 1.0, ""],
        [serial(date(2026, 2, 28)), "", 2.0],
    ]
    incoming = [
        ["Date", "Indicator A", "Indicator B"],
        [serial(date(2026, 1, 31)), 99.0, 11.0],
        [serial(date(2026, 2, 28)), 22.0, 20.0],
        [serial(date(2026, 3, 31)), 3.0, 4.0],
    ]

    fills, appends, merged = m.prepare_incremental_table_changes(
        existing, incoming, "test wide table"
    )

    assert fills == [(2, 2, 11.0), (3, 1, 22.0)]
    assert appends == [[serial(date(2026, 3, 31)), 3.0, 4.0]]
    assert merged == [
        existing[0],
        [serial(date(2026, 1, 31)), 1.0, 11.0],
        [serial(date(2026, 2, 28)), 22.0, 2.0],
        appends[0],
    ]

    rerun_fills, rerun_appends, rerun = m.prepare_incremental_table_changes(
        merged, incoming, "test wide table"
    )
    assert rerun_fills == []
    assert rerun_appends == []
    assert rerun == merged


def test_incremental_table_rejects_missing_historical_dates():
    existing = [
        ["Date", "Indicator"],
        [serial(date(2026, 1, 31)), 1.0],
        [serial(date(2026, 3, 31)), 3.0],
    ]
    incoming = [
        ["Date", "Indicator"],
        [serial(date(2026, 2, 28)), 2.0],
        [serial(date(2026, 3, 31)), 4.0],
    ]
    with pytest.raises(RuntimeError, match="missing historical date"):
        m.prepare_incremental_table_changes(existing, incoming, "test table")


def test_indicator_sync_only_updates_blank_values_by_default(monkeypatch):
    monkeypatch.setattr(m, "FULL_HISTORY_MODE", False)
    existing = {
        date(2026, 1, 1): {"row": 2, "value": 1.0},
        date(2026, 2, 1): {"row": 3, "value": None},
    }
    incoming = {
        date(2026, 1, 1): 9.0,
        date(2026, 2, 1): 2.0,
        date(2026, 3, 1): 3.0,
    }

    fills, appends = m.prepare_sheet_changes(existing, incoming)

    assert fills == [(3, date(2026, 2, 1), 2.0)]
    assert appends == [(date(2026, 3, 1), 3.0)]


def test_incremental_import_ignores_revisions_and_blank_replacements(monkeypatch):
    monkeypatch.setattr(m, "FULL_HISTORY_MODE", False)
    existing = {date(2026, 1, 1): {"row": 2, "value": 1.0, "raw_value": 1.0}}
    m.validate_overlapping_indicator_values(
        existing,
        {date(2026, 1, 1): 99.0},
        "test indicator",
        allow_value_revisions=False,
    )
    assert m.prepare_sheet_changes(existing, {date(2026, 1, 1): None}) == ([], [])


def test_explicit_full_history_mode_can_revise_values(monkeypatch):
    monkeypatch.setattr(m, "FULL_HISTORY_MODE", True)
    existing = {date(2026, 1, 1): {"row": 2, "value": 1.0}}
    fills, appends = m.prepare_sheet_changes(existing, {date(2026, 1, 1): 9.0})
    assert fills == [(2, date(2026, 1, 1), 9.0)]
    assert appends == []


def test_incremental_value_ranges_are_bounded_and_do_not_clear():
    ranges = m.incremental_table_value_ranges(
        "All data",
        [(2, 1, 10), (2, 2, 20), (4, 40, 30)],
        [[serial(date(2026, 4, 30)), 1, 2]],
        existing_row_count=4,
    )
    assert ranges == [
        {"range": "'All data'!B2:C2", "values": [[10, 20]]},
        {"range": "'All data'!AO4:AO4", "values": [[30]]},
        {
            "range": "'All data'!A5:C5",
            "values": [[serial(date(2026, 4, 30)), 1, 2]],
        },
    ]
