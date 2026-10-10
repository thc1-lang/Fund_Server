import json
import os
import subprocess
import sys

import numpy as np
import pytest

from notifications import changes, lifecycle


@pytest.fixture
def collecting(monkeypatch):
    monkeypatch.delenv("US_PIPELINE_CHANGE_REPORT", raising=False)
    token = changes.begin()
    try:
        yield
    finally:
        changes.end(token)


def test_compare_distinguishes_new_revised_removed_and_unchanged():
    assert changes.compare([[1, "", 5, 4]], [["1", 3, "", 6]]) == {
        "added": 1,
        "updated": 1,
        "removed": 1,
    }
    assert changes.compare([[1.0, None]], [[1.00000000001, ""]]) == {
        "added": 0,
        "updated": 0,
        "removed": 0,
    }


def test_subprocess_journal_is_read_by_parent(tmp_path, collecting):
    journal = tmp_path / "changes.jsonl"
    environment = dict(os.environ, US_PIPELINE_CHANGE_REPORT=str(journal))
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from notifications.changes import record; record('Source data', 'GDP', added=2)",
        ],
        env=environment,
        check=True,
    )
    assert changes.events(journal)[0]["added"] == 2
    assert changes.events() == []


def test_unknown_is_not_reported_as_unchanged_or_refreshed():
    text = "\n".join(
        changes.describe([{"category": "Analysis", "name": "GDP", "unknown": True}])
    )
    assert "without a reliable change comparison" in text
    assert "no value changes" not in text
    assert "refreshed" not in text


def test_only_start_and_end_with_real_metrics(monkeypatch, tmp_path):
    monkeypatch.delenv("US_PIPELINE_NOTIFICATIONS_MANAGED", raising=False)
    monkeypatch.delenv("US_PIPELINE_CHANGE_REPORT", raising=False)
    calls = []
    now = [0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        lifecycle,
        "send_telegram",
        lambda text, title, **kw: calls.append((title, text)),
    )

    def action():
        lifecycle.set_run_details(log_dir=tmp_path, stages=["Import", "Analysis"])
        lifecycle.stage_event(1, 2, "Import", "started")
        changes.record("Source data", "GDP", added=3, updated=1)
        lifecycle.stage_event(1, 2, "Import", "finished", seconds=100)
        lifecycle.stage_event(2, 2, "Analysis", "started")
        changes.record("Indicator analysis", "GDP")
        lifecycle.stage_event(2, 2, "Analysis", "finished", seconds=95)
        now[0] = 195
        return 0

    assert lifecycle.run_notified("Pipeline", action, argv=["--dataset", "all"]) == 0
    assert len(calls) == 2
    assert "Entire US pipeline" in calls[0][1]
    assert "Duration: 3m 15s" in calls[1][1]
    assert "3 added, 1 updated values" in calls[1][1]
    assert "Affected: GDP" in calls[1][1]
    assert "no value changes detected in tracked ranges" in calls[1][1]
    assert "Stages completed: 2/2" in calls[1][1]


def test_analysis_tracks_changed_values_after_successful_write(collecting):
    import us_indicator_analysis as analysis

    class Workbook:
        def values_batch_get(self, ranges, params):
            return {"valueRanges": [{"values": [[1, "", 8]]} for _ in ranges]}

        def values_batch_update(self, payload):
            return {}

    assert (
        analysis.batch_update_values(
            Workbook(), [{"range": "'GDP'!C2:E2", "values": [[1, 4, 9]]}]
        )
        == 1
    )
    result = changes.events()[0]
    assert result["added"] == 1 and result["updated"] == 1


def test_comparison_read_failure_does_not_prevent_write(collecting):
    import us_indicator_analysis as analysis

    calls = []

    class Workbook:
        def values_batch_get(self, ranges, params):
            raise ValueError("Comparison unavailable")

        def values_batch_update(self, payload):
            calls.append(payload)

    analysis.batch_update_values(Workbook(), [{"range": "'GDP'!C2", "values": [[1]]}])
    assert len(calls) == 1
    assert changes.events()[0]["unknown"] is True


def test_failed_write_does_not_claim_successful_changes(collecting, monkeypatch):
    import us_indicator_analysis as analysis

    monkeypatch.setattr(analysis, "call_with_backoff", lambda fn, **kw: fn())

    class Workbook:
        def values_batch_get(self, ranges, params):
            return {"valueRanges": [{"values": [[1]]}]}

        def values_batch_update(self, payload):
            raise ValueError("Write failed")

    with pytest.raises(ValueError):
        analysis.batch_update_values(
            Workbook(), [{"range": "'GDP'!C2", "values": [[2]]}]
        )
    assert changes.events() == []


def test_relationship_diff_counts_actual_additions_and_removals():
    from pipeline import comparison

    diff = comparison(np.array([[1.0, np.nan, 3.0]]), np.array([[2.0, 4.0, np.nan]]))
    assert diff["numeric_mismatches"] == 1
    assert diff["added_numeric"] == 1
    assert diff["removed_numeric"] == 1


def test_import_preview_does_not_claim_changes(collecting):
    from monthly_indicators import print_sync_summary, sync_result_row

    result = sync_result_row("GDP", "GDP", None, 1, 2, None, "DRY RUN: would append")
    print_sync_summary([result], validate_only=True)
    assert changes.events() == []


def test_import_backfill_is_reported_as_an_added_value(collecting):
    from monthly_indicators import print_sync_summary, sync_result_row

    result = sync_result_row(
        "Central bank liquidity growth",
        "Central bank liquidity growth",
        None,
        2,
        0,
        None,
        source_change_counts={"added": 2, "updated": 0, "removed": 0},
    )
    print_sync_summary([result])
    assert changes.events() == [
        {
            "category": "Source data",
            "name": "Central bank liquidity growth",
            "added": 2,
            "updated": 0,
            "removed": 0,
            "unknown": False,
            "note": "",
        }
    ]


def test_unwritable_journal_does_not_block_calculations(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "US_PIPELINE_CHANGE_REPORT", str(tmp_path / "missing" / "changes.jsonl")
    )
    changes.record("Source data", "GDP", added=1)
