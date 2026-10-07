import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("single_stock_runner", ROOT / "run_single_stock_pipeline.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_stage_commands_are_fixed_and_share_resolved_credentials(tmp_path, monkeypatch):
    credentials = tmp_path / "service.json"
    credentials.write_text("{}")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    args = runner.parse_args([])
    commands = runner.stage_commands(args, tmp_path / "out", credentials)
    assert [stage for stage, *_ in commands] == ["primary", "secondary", "summary"]
    assert all(command[0] == runner.sys.executable for _, _, command, _ in commands)
    assert all(env["GOOGLE_CREDENTIALS_FILE"] == str(credentials) for *_, env in commands)
    assert "--import-zacks" in commands[0][2]
    assert "--import-sec-data" in commands[1][2]


def test_shortlist_comparison_reports_only_membership_changes():
    before = {("Safe", "ABC"), ("Short", "DEF")}
    after = {("Safe", "ABC"), ("Safe", "GHI")}
    report = runner.describe_shortlist_change(before, after)
    assert "Added — Safe: GHI" in report
    assert "Removed — Short: DEF" in report
    assert runner.describe_shortlist_change(before, before) == "Shortlists unchanged."


def test_summary_shortlist_reader_uses_category_memberships(tmp_path):
    path = tmp_path / "AI_master_index.json"
    path.write_text(json.dumps({"records": [{"ticker": "abc", "categories": ["Safe", "Short"]}]}))
    assert runner.shortlist_from_summary(path) == {("Safe", "ABC"), ("Short", "ABC")}


def test_secondary_shortlist_reader_uses_all_csv_memberships(tmp_path):
    report = tmp_path / "run_summary.json"
    report.write_text(json.dumps({"categories": {"Safe": {"top_tickers": ["ONLY_FALLBACK"]}}}))
    (tmp_path / "safe_secondary_summary.csv").write_text("Ticker\nABC\nDEF\n")
    (tmp_path / "short_summary.csv").write_text("Ticker\nGHI\n")
    assert runner.shortlist_from_secondary(report) == {
        ("Safe", "ABC"), ("Safe", "DEF"), ("Short", "GHI")
    }


def test_stop_request_interrupts_a_silent_child(tmp_path, monkeypatch):
    request = tmp_path / "stop.request"
    monkeypatch.setattr(runner, "STOP_REQUEST", request)
    timer = threading.Timer(0.2, request.touch)
    timer.start()
    try:
        with pytest.raises(runner.PipelineStopped):
            runner.run_stage(
                1, 1, "primary", tmp_path,
                [sys.executable, "-c", "import time; time.sleep(5)"],
                dict(os.environ), io.StringIO(),
            )
    finally:
        timer.cancel()
        timer.join()
