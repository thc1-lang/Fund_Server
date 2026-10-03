import json
import subprocess
import sys
from pathlib import Path
import pytest
import run_us_pipeline as runner
from config import BASE_WORKBOOKS
from source_freshness import wait_for_macro
from validation import ValidationError


def test_run_order_and_isolated_selectors():
    args = runner.parse_args([])
    commands = runner.build_commands(args)
    assert [Path(cmd[1]).name for _, cmd in commands] == [
        "monthly_indicators.py",
        "us_indicator_analysis.py",
        "run_relationships.py",
    ]
    assert all(
        cmd[0] == sys.executable and Path(cmd[1]).parent == runner.ROOT
        for _, cmd in commands
    )
    for stage in runner.STAGES:
        commands = runner.build_commands(runner.parse_args(["--stage", stage]))
        assert len(commands) == 1
        if stage not in ("importer", "analysis", "relationships"):
            assert commands[0][1][-1] == "--" + stage


def test_failure_stops_later_stages(tmp_path, monkeypatch):
    monkeypatch.setattr("notifications.lifecycle.send_telegram", lambda *a, **kw: True)
    credential = tmp_path / "credentials.json"
    credential.write_text("{}")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "dependencies", lambda: None)
    seen = []

    def fail(index, total, name, *args):
        seen.append(name)
        raise RuntimeError("test importer failure")

    monkeypatch.setattr(runner, "run_stage", fail)
    assert runner.main(["--credentials", str(credential), "--api-key", "test"]) == 1
    assert seen == ["Macro importer"]
    report = json.loads(next((tmp_path / "work").glob("*/summary.json")).read_text())
    assert report["status"] == "failed"
    assert "test" not in json.dumps(report.get("credentials", {}))


class Imports:
    def __init__(self, upstream, imported, origin=None):
        self.upstream = upstream
        self.imported = imported
        self.reads = 0
        self.origin = origin or "19E_Za0DOHMY_9AFSPatK8Cp2vCQypI81QnB3Hz4ve9c"

    def values(self, id, ranges, formulas=False):
        if formulas:
            return [[[f'=IMPORTRANGE("{self.origin}","\'Coincident Score\'!A1:AL")']]]
        self.reads += 1
        return [
            self.imported if id == BASE_WORKBOOKS[0].spreadsheet_id else self.upstream
        ]


def test_freshness_checks_values_not_just_last_date():
    upstream = [["Date", "X"], [45000, 1.2]]
    api = Imports(upstream, [["Date", "X"], [45000, 0.8]])
    with pytest.raises(ValidationError, match="stale"):
        wait_for_macro(api, BASE_WORKBOOKS[0], attempts=1)
    api.imported = [["Date", "X", ""], [45000, 1.2], []]
    assert wait_for_macro(api, BASE_WORKBOOKS[0], attempts=1) == api.imported


def test_wrong_macro_workbook_is_rejected():
    with pytest.raises(ValidationError, match="different macro"):
        wait_for_macro(Imports([[1]], [[1]], "wrong-id"), BASE_WORKBOOKS[0], attempts=1)


def test_removed_preview_flag_is_rejected():
    with pytest.raises(SystemExit):
        runner.parse_args(["--dry-run"])


def test_lock_rejects_second_process(tmp_path):
    from run_lock import acquire_lock

    with (tmp_path / "lock").open("a+") as handle:
        acquire_lock(handle)
        code = (
            "from run_lock import acquire_lock; from pathlib import Path; f=Path("
            + repr(str(tmp_path / "lock"))
            + ').open("a+"); acquire_lock(f)'
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=runner.ROOT, capture_output=True
        )
        assert result.returncode != 0


@pytest.mark.parametrize(
    "stage",
    [
        "all",
        "relationships",
        "spread",
        "correlation",
        "momentum",
        "spread-momentum",
        "correlation-momentum",
    ],
)
def test_dataset_forwarded_to_relationship_stage(stage):
    commands = runner.build_commands(
        runner.parse_args(["--stage", stage, "--dataset", "leading"])
    )
    command = commands[-1][1]
    assert command[command.index("--dataset") + 1] == "leading"
    assert command[1].endswith("run_relationships.py")
