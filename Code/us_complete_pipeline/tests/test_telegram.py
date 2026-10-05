import io
import json
import os
import runpy
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import run_us_pipeline as runner
from notifications import telegram
from notifications import lifecycle


@pytest.fixture
def alerts(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lifecycle, "send_telegram", lambda *a, **kw: calls.append((a, kw))
    )
    monkeypatch.delenv("US_PIPELINE_NOTIFICATIONS_MANAGED", raising=False)
    return calls


def test_success(alerts, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda args: 0)
    assert runner.main(["--stage", "analysis"]) == 0
    assert [a[1] for a, kw in alerts] == [
        "Indicator analysis started",
        "Indicator analysis finished",
    ]


@pytest.mark.parametrize("error", [ValueError("setup failed"), KeyboardInterrupt()])
def test_setup_failure_and_interrupt(alerts, monkeypatch, error):
    def fail(args):
        raise error

    monkeypatch.setattr(runner, "_run", fail)
    with pytest.raises(type(error)):
        runner.main([])
    assert [a[1] for a, kw in alerts] == [
        "Pipeline started",
        (
            "Pipeline stopped"
            if isinstance(error, KeyboardInterrupt)
            else "Pipeline failed"
        ),
    ]
    assert type(error).__name__ in alerts[-1][0][0]


def test_stage_failure_alert_once(alerts, monkeypatch, tmp_path):
    credential = tmp_path / "credentials.json"
    credential.write_text("{}")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "dependencies", lambda: None)

    def fail(*args):
        raise RuntimeError("actual stage exception")

    monkeypatch.setattr(runner, "run_stage", fail)
    assert runner.main(["--credentials", str(credential), "--api-key", "fake"]) == 1
    assert [a[1] for a, kw in alerts if "— stage" not in a[1]] == [
        "Pipeline started",
        "Pipeline failed",
    ]
    assert "actual stage exception" in alerts[-1][0][0]


def test_self_test_does_not_notify(alerts, monkeypatch):
    monkeypatch.setattr(runner, "_run", lambda args: 0)
    assert runner.main(["--self-test"]) == 0
    assert alerts == []


def test_child_exception_included(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="child failure detail"):
        runner.run_stage(
            1,
            1,
            "Test stage",
            [sys.executable, "-c", 'raise ValueError("child failure detail")'],
            dict(os.environ),
            io.StringIO(),
        )


def test_delivery_redaction_limits(monkeypatch):
    monkeypatch.setattr(telegram.sys, "platform", "linux")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-user")

    def deliver(request, timeout):
        payload = parse_qs(request.data.decode())
        assert timeout == 10
        assert request.full_url == "https://api.telegram.org/botfake-token/sendMessage"
        assert payload["chat_id"] == ["fake-user"]
        assert request.get_method() == "POST"
        assert len(payload["text"][0]) == 4096
        assert "fake-token" not in payload["text"][0]
        assert "fake-user" not in payload["text"][0]
        assert payload["text"][0].startswith("[redacted] <plain>\n")
        assert "fred-secret" not in payload["text"][0]
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(telegram, "urlopen", deliver)
    assert telegram.send_telegram(
        "fake-token fred-secret " + "x" * 5000,
        title="fake-user <plain>",
        secrets=("fred-secret",),
    )


def test_group_notifications_use_group_chat_id(monkeypatch):
    monkeypatch.setattr(telegram.sys, "platform", "linux")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    captured = []

    def deliver(request, timeout):
        captured.append(parse_qs(request.data.decode()))
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(telegram, "urlopen", deliver)
    assert telegram.send_telegram("Pipeline started")
    assert captured[0]["chat_id"] == ["-1001234567890"]


@pytest.mark.parametrize(
    "mode", ["missing", "missing_token", "timeout", "rejected", "invalid_json"]
)
def test_delivery_problems_nonfatal(monkeypatch, mode):
    monkeypatch.setattr(telegram.sys, "platform", "linux")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-user")
    if mode == "missing":
        monkeypatch.delenv("TELEGRAM_CHAT_ID")

    if mode == "missing_token":
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")

    def deliver(*a, **kw):
        if mode in ("missing", "missing_token"):
            pytest.fail("Missing credentials must not make a request")
        if mode == "timeout":
            raise TimeoutError("network error")
        if mode == "invalid_json":
            return io.BytesIO(b"invalid JSON")
        return io.BytesIO(b'{"ok":false}')

    monkeypatch.setattr(telegram, "urlopen", deliver)
    assert not telegram.send_telegram("test")


def test_portable_sources_and_notebook_match(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = root / "portable" / "US_PIPELINE_COLAB.py"
    if not script.exists():
        pytest.skip("Portable export verification runs in the source project")
    embedded = runpy.run_path(str(script), run_name="portable_test")
    embedded["materialize"](tmp_path)
    for name in embedded["SOURCES"]:
        assert "\\" not in name, "Embedded paths must also work on macOS and Linux"
        assert (tmp_path / name).read_text(encoding="utf-8") == (root / name).read_text(
            encoding="utf-8"
        )
    notebook = json.loads(script.with_suffix(".ipynb").read_text(encoding="utf-8"))
    assert "".join(notebook["cells"][0]["source"]) == script.read_text(encoding="utf-8")
    assert '"TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN"' in script.read_text(
        encoding="utf-8"
    )


def test_colab_secrets_forwarded_without_duplicate_alerts(monkeypatch, tmp_path):
    import subprocess
    import types

    root = Path(__file__).resolve().parents[1]
    script = root / "portable" / "US_PIPELINE_COLAB.py"
    if not script.exists():
        pytest.skip("Portable export verification runs in the source project")
    embedded = runpy.run_path(str(script), run_name="portable_test")
    credential = tmp_path / "credential.json"
    credential.write_text("{}")
    secrets = {
        "TELEGRAM_CHAT_ID": "colab-user",
        "TELEGRAM_BOT_TOKEN": "colab-token",
        "GOOGLE_APPLICATION_CREDENTIALS": str(credential),
        "FRED_API_KEY": "fake-fred",
    }
    for key in secrets:
        monkeypatch.delenv(key, raising=False)
    colab = types.ModuleType("google.colab")
    colab.userdata = types.SimpleNamespace(get=secrets.get)
    monkeypatch.setitem(sys.modules, "google.colab", colab)
    monkeypatch.chdir(tmp_path)
    captured = []

    def launch(command, env):
        captured.append(env)
        return types.SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(subprocess, "Popen", launch)
    embedded["run_embedded"](notebook=True)
    assert len(captured) == 1
    assert captured[0]["TELEGRAM_CHAT_ID"] == "colab-user"
    assert captured[0]["TELEGRAM_BOT_TOKEN"] == "colab-token"


@pytest.mark.parametrize("stage,name", list(lifecycle.STAGE_NAMES.items()))
def test_each_launcher_stage_has_tailored_title(alerts, monkeypatch, stage, name):
    monkeypatch.setattr(runner, "_run", lambda args: 0)
    assert runner.main(["--stage", stage]) == 0
    assert [a[1] for a, kw in alerts] == [name + " started", name + " finished"]


@pytest.mark.parametrize("failed", [False, True])
def test_direct_indicators_on_mac(alerts, monkeypatch, tmp_path, failed):
    import types
    import monthly_indicators as indicators

    monkeypatch.setattr(sys, "argv", ["monthly_indicators.py"])
    monkeypatch.setattr(lifecycle.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lifecycle.platform, "node", lambda: "Test-Mac")
    monkeypatch.setattr(indicators, "ERROR_LOG_PATH", tmp_path / "errors.log")
    monkeypatch.setattr(indicators.logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(
        indicators,
        "parse_args",
        lambda: types.SimpleNamespace(audit_google_sheet=False),
    )

    def run(args):
        if failed:
            raise ValueError("Indicator download failed")
        return 0

    monkeypatch.setattr(indicators, "run", run)
    assert indicators.main() == (1 if failed else 0)
    assert [a[1] for a, kw in alerts] == [
        "Indicators started",
        "Indicators failed" if failed else "Indicators finished",
    ]
    assert "Mac (Test-Mac)" in alerts[0][0][0]
    if failed:
        assert "Indicator download failed" in alerts[-1][0][0]


@pytest.mark.parametrize(
    "flag,name",
    [
        ("--correlation", "Correlation"),
        ("--spread", "Spread"),
        ("--momentum", "Momentum"),
        ("--spread-momentum", "Spread momentum"),
        ("--correlation-momentum", "Correlation momentum"),
    ],
)
@pytest.mark.parametrize("failed", [False, True])
def test_direct_relationship_entrypoint(
    alerts, monkeypatch, tmp_path, flag, name, failed
):
    import run_relationships as relationships

    monkeypatch.setattr(relationships, "ROOT", tmp_path)
    monkeypatch.setattr(relationships, "select_workbooks", lambda *a: [])
    monkeypatch.setattr(relationships, "credentials_path", lambda *a: "unused")

    def sheets(*a, **kw):
        if failed:
            raise ValueError("Cannot open workbook")
        return object()

    monkeypatch.setattr(relationships, "Sheets", sheets)
    assert relationships.main([flag, "--dataset", "leading"]) == (1 if failed else 0)
    assert [a[1] for a, kw in alerts] == [
        name + " started",
        name + (" failed" if failed else " finished"),
    ]
    assert "Dataset: Leading" in alerts[0][0][0]
    if failed:
        assert "Cannot open workbook" in alerts[-1][0][0]


def test_direct_analysis_reports_batch_error(alerts, monkeypatch):
    import us_indicator_analysis as analysis

    monkeypatch.setattr(analysis, "open_workbook", lambda *a: object())
    monkeypatch.setattr(analysis, "build_indicator_specs", lambda *a: [object()])
    monkeypatch.setattr(analysis, "filter_indicator_specs", lambda specs, *a: specs)
    monkeypatch.setattr(
        analysis,
        "process_indicators_batch",
        lambda *a: (
            [analysis.BatchIndicatorResult("GDP", False, error="Missing data")],
            None,
        ),
    )
    monkeypatch.setattr(analysis, "print_batch_summary", lambda *a: None)
    assert analysis.main(["--all"]) == 1
    assert [a[1] for a, kw in alerts] == [
        "Indicator analysis started",
        "Indicator analysis failed",
    ]
    assert "GDP: Missing data" in alerts[-1][0][0]


def test_open_workbook_uses_quota_backoff(monkeypatch):
    import us_indicator_analysis as analysis

    opened = object()
    calls = []

    class Client:
        def open_by_key(self, spreadsheet_id):
            calls.append(("open", spreadsheet_id))
            return opened

    fake_gspread = types.SimpleNamespace(
        service_account=lambda *, filename: calls.append(("credentials", filename))
        or Client()
    )
    monkeypatch.setitem(sys.modules, "gspread", fake_gspread)
    backoff_calls = []

    def backoff(fn, **kwargs):
        backoff_calls.append(kwargs)
        return fn()

    monkeypatch.setattr(analysis, "call_with_backoff", backoff)

    result = analysis.open_workbook(analysis.EngineConfig("sheet-id", "credentials.json"))

    assert result is opened
    assert calls == [("credentials", "credentials.json"), ("open", "sheet-id")]
    assert backoff_calls == [{"operation_name": "open_workbook"}]


def test_parent_suppresses_child_notifications(alerts, monkeypatch, tmp_path):
    credential = tmp_path / "credentials.json"
    credential.write_text("{}")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "dependencies", lambda: None)

    def stage(index, total, name, command, env, log):
        assert env["US_PIPELINE_NOTIFICATIONS_MANAGED"] == "1"
        return {"stage": name}

    monkeypatch.setattr(runner, "run_stage", stage)
    assert runner.main(["--credentials", str(credential), "--api-key", "fake"]) == 0
    assert [a[1] for a, kw in alerts if "— stage" not in a[1]] == [
        "Pipeline started",
        "Pipeline finished",
    ]
    assert len(alerts) == 2
    assert "Stages completed: 3/3" in alerts[-1][0][0]
    assert "Details: /status or /logs" in alerts[-1][0][0]
    alerts.clear()
    monkeypatch.setenv("US_PIPELINE_NOTIFICATIONS_MANAGED", "1")
    assert lifecycle.run_notified("Indicators", lambda: 0) == 0
    assert alerts == []


@pytest.mark.parametrize("flag", ["--help", "--self-test"])
def test_direct_entrypoint_help_and_tests_are_quiet(alerts, monkeypatch, flag):
    @lifecycle.notify_entrypoint("Indicators")
    def fake(argv):
        return 0

    assert fake([flag]) == 0
    assert alerts == []


@pytest.mark.parametrize("exit_code", [0, 2, "Credentials missing"])
def test_system_exit_preserved_and_reported(alerts, exit_code):
    def action():
        raise SystemExit(exit_code)

    with pytest.raises(SystemExit) as result:
        lifecycle.run_notified("Indicators", action)
    assert result.value.code == exit_code
    assert alerts[-1][0][1] == (
        "Indicators finished" if exit_code == 0 else "Indicators failed"
    )


def test_run_metadata_and_context_reset(alerts):
    lifecycle.run_notified("Indicators", lambda: 0, argv=["--dataset", "leading"])
    start, finish = [args[0] for args, _ in alerts]
    for label in ("Scope:", "Dataset:", "Run "):
        assert label in start and label in finish
    assert "Started:" in start
    assert "Duration:" in finish and "Finished:" in finish
    assert start.splitlines()[-1] == finish.splitlines()[-1]
    assert lifecycle._active.get() is None


def test_long_error_retains_end_and_redacts(monkeypatch):
    monkeypatch.setattr(
        telegram,
        "credential",
        lambda name: "token-secret" if name.endswith("TOKEN") else "chat-secret",
    )
    payloads = []

    def deliver(request, timeout):
        payloads.append(parse_qs(request.data.decode())["text"][0])
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(telegram, "urlopen", deliver)
    assert telegram.send_telegram(
        "x" * 6000 + " final error token-secret", "Run details"
    )
    assert len(payloads[0]) == 4096
    assert payloads[0].startswith("Run details")
    assert payloads[0].endswith("final error [redacted]")


def test_windows_persistent_credentials(monkeypatch):
    import types

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(telegram.sys, "platform", "win32")

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        types.SimpleNamespace(
            HKEY_CURRENT_USER=1,
            OpenKey=lambda *args: Key(),
            QueryValueEx=lambda key, name: ("saved-token", 1),
        ),
    )
    assert telegram.credential("TELEGRAM_BOT_TOKEN") == "saved-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "override")
    assert telegram.credential("TELEGRAM_BOT_TOKEN") == "override"
