import json
import time

import pytest

from notifications import control
from run_lock import acquire_lock


@pytest.fixture
def controller(tmp_path, monkeypatch):
    replies = []
    launches = []
    monkeypatch.setattr(control, "WORK", tmp_path)
    monkeypatch.setattr(
        control, "send_telegram", lambda text, title: replies.append(text)
    )
    monkeypatch.setattr(control, "task_state", lambda: "Ready")
    monkeypatch.setattr(
        control, "start_pipeline", lambda stage="all": launches.append(stage)
    )
    return control.Controller("-100123", "123, 456", "test_bot"), replies, launches


def update(text="/run", uid=1, user=123, chat=-100123, age=0):
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"},
            "from": {"id": user, "is_bot": False},
            "date": time.time() - age,
            "text": text,
        },
    }


def test_run_and_replay_protection(controller):
    handler, replies, launches = controller
    handler.handle(update())
    handler.handle(update())
    restarted = control.Controller("-100123", "123,456", "test_bot")
    restarted.handle(update())
    assert len(launches) == 1
    assert replies == []  # Only the pipeline itself sends the start alert.


@pytest.mark.parametrize(
    "message",
    [
        update(user=999),
        update(chat=999),
        update("/run@other_bot"),
        update("/run", age=180),
        update("/run ; calc.exe"),
        {"update_id": 1, "edited_message": update()["message"]},
    ],
)
def test_untrusted_and_stale_commands_cannot_launch(controller, message):
    handler, _, launches = controller
    handler.handle(message)
    assert not launches


def test_running_task_blocks_launch(controller, monkeypatch):
    handler, replies, launches = controller
    monkeypatch.setattr(control, "task_state", lambda: "Running")
    handler.handle(update())
    assert not launches
    assert "Already running" in replies[0]


def test_manual_run_lock_blocks_launch(controller):
    handler, replies, launches = controller
    with (control.WORK / "complete_pipeline.lock").open("a+") as lock:
        acquire_lock(lock)
        handler.handle(update())
    assert not launches
    assert "Already running" in replies[0]


def test_rapid_commands_do_not_launch_twice(controller):
    handler, _, launches = controller
    handler.handle(update())
    handler.handle(update(uid=2))
    assert len(launches) == 1


def test_status_and_help_do_not_launch(controller):
    handler, replies, launches = controller
    handler.handle(update("/status"))
    handler.handle(update("/help@test_bot", uid=2))
    assert "idle" in replies[0]
    assert "/run" in replies[1]
    assert not launches


@pytest.mark.parametrize("command", ["/us_cot_import", "/cot", "us-cot-import"])
def test_cot_commands_launch_scheduled_runner(controller, monkeypatch, command):
    handler, replies, launches = controller
    cot_launches = []
    monkeypatch.setattr(control, "cot_busy", lambda: False)
    monkeypatch.setattr(control, "start_cot_import", lambda: cot_launches.append(True))
    handler.handle(update(command))
    assert cot_launches == [True]
    assert not replies and not launches


def test_cot_status_command_reports_without_launch(controller, monkeypatch):
    handler, replies, launches = controller
    monkeypatch.setattr(control, "cot_status", lambda: "COT import: idle (task: Ready).")
    handler.handle(update("/us_cot_status"))
    assert replies == ["COT import: idle (task: Ready)."]
    assert not launches


def test_cot_command_rejects_duplicate_run(controller, monkeypatch):
    handler, replies, launches = controller
    monkeypatch.setattr(control, "cot_busy", lambda: True)
    monkeypatch.setattr(control, "cot_status", lambda: "COT import: running.")
    monkeypatch.setattr(
        control, "start_cot_import", lambda: pytest.fail("duplicate COT run launched")
    )
    handler.handle(update("/cot"))
    assert "Already running" in replies[0]
    assert not launches


@pytest.mark.parametrize(
    ("command", "stage"),
    [
        ("/us_stock_run", "all"),
        ("/us-stock-primary", "primary"),
        ("/us_single_stock_secondary", "secondary"),
        ("us-stock-summary", "summary"),
    ],
)
def test_single_stock_commands_launch_only_fixed_stages(controller, monkeypatch, command, stage):
    handler, replies, launches = controller
    single_stock_launches = []
    monkeypatch.setattr(control, "single_stock_busy", lambda: False)
    monkeypatch.setattr(
        control, "start_single_stock_pipeline",
        lambda requested: single_stock_launches.append(requested),
    )
    handler.handle(update(command))
    assert single_stock_launches == [stage]
    assert not replies and not launches


def test_single_stock_status_and_stop_do_not_launch(controller, monkeypatch):
    handler, replies, launches = controller
    monkeypatch.setattr(control, "single_stock_status", lambda: "Single-stock pipeline: idle.")
    monkeypatch.setattr(control, "stop_single_stock_pipeline", lambda: "Stop requested.")
    handler.handle(update("/us_stock_status"))
    handler.handle(update("/us_stock_stop", uid=2))
    assert replies == ["Single-stock pipeline: idle.", "Stop requested."]
    assert not launches


def test_single_stock_dispatch_uses_only_fixed_launcher(monkeypatch):
    import types

    calls = []
    monkeypatch.setattr(control, "credential", lambda key: "configured")
    monkeypatch.setattr(
        control.subprocess,
        "Popen",
        lambda command, **kw: calls.append((command, kw))
        or types.SimpleNamespace(poll=lambda: 0),
    )
    monkeypatch.setattr(control.Path, "is_file", lambda path: True)
    control.start_single_stock_pipeline("summary")
    assert calls[0][0] == [
        "cmd.exe", "/d", "/c", str(control.SINGLE_STOCK_LAUNCHER), "--stage", "summary",
    ]
    with pytest.raises(ValueError):
        control.start_single_stock_pipeline("summary & calc.exe")


@pytest.mark.parametrize("command", ["/backup", "/fund_backup", "fund-server-backup"])
def test_backup_commands_start_the_existing_backup_task(controller, monkeypatch, command):
    handler, replies, launches = controller
    backup_launches = []
    monkeypatch.setattr(control, "backup_busy", lambda: False)
    monkeypatch.setattr(control, "start_backup", lambda: backup_launches.append(True))
    handler.handle(update(command))
    assert backup_launches == [True]
    assert not replies and not launches


def test_backup_command_rejects_duplicate_run(controller, monkeypatch):
    handler, replies, launches = controller
    monkeypatch.setattr(control, "backup_busy", lambda: True)
    monkeypatch.setattr(
        control, "start_backup", lambda: pytest.fail("duplicate backup run launched")
    )
    handler.handle(update("/backup"))
    assert "already running" in replies[0].lower()
    assert not launches


def test_start_backup_uses_the_existing_scheduled_task(monkeypatch):
    calls = []
    monkeypatch.setattr(control, "powershell", lambda script: calls.append(script))
    control.start_backup()
    assert calls == [
        "Start-ScheduledTask -TaskName 'Fund Server GitHub Backup'"
    ]


@pytest.mark.parametrize("user", [123, 456])
def test_both_group_users_can_request_status(controller, user):
    handler, replies, launches = controller
    handler.handle(update("/status", user=user))
    assert len(replies) == 1 and "idle" in replies[0]
    assert not launches


@pytest.mark.parametrize("user", [123, 456])
def test_both_group_users_can_use_stage_commands(controller, user):
    handler, replies, launches = controller
    handler.handle(update("/analysis", user=user))
    assert launches == ["analysis"]
    assert not replies


@pytest.mark.parametrize("user,chat", [(999, -100123), (123, 123), (456, 456), (123, -100999), (456, -100999)])
def test_group_only_identity_checks(controller, user, chat):
    handler, replies, launches = controller
    handler.handle(update("/status", user=user, chat=chat))
    assert not replies and not launches


def test_bot_forward_and_wrong_chat_type_are_ignored(controller):
    handler, replies, launches = controller
    bot = update("/status")
    bot["message"]["from"]["is_bot"] = True
    handler.handle(bot)
    forwarded = update("/status", uid=2)
    forwarded["message"]["forward_origin"] = {"type": "user"}
    handler.handle(forwarded)
    wrong_type = update("/status", uid=3)
    wrong_type["message"]["chat"]["type"] = "private"
    handler.handle(wrong_type)
    assert not replies and not launches


def test_allowed_user_ids_validation():
    assert control.parse_allowed_user_ids(" 123 , 456,123, 00123 ") == frozenset({"123", "456"})
    for raw in ("", "0", "-1", "123,", ",123", "123,abc", "1.0", "1 2"):
        with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
            control.parse_allowed_user_ids(raw)


def test_chat_id_never_authorises_user(tmp_path):
    handler = control.Controller("-100123", "456", "test_bot", tmp_path / "offset.json")
    assert "100123" not in handler.user_ids
    assert "456" in handler.user_ids
    with pytest.raises(ValueError, match="negative group ID"):
        control.Controller("123", "123", "test_bot", tmp_path / "other.json")


def test_legacy_single_user_setting_is_not_used(monkeypatch):
    monkeypatch.setattr(control.sys, "platform", "win32")
    settings = {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-100123", "TELEGRAM_ALLOWED_USER_ID": "123"}
    monkeypatch.setattr(control, "credential", lambda key: settings.get(key, ""))
    with pytest.raises(RuntimeError, match="TELEGRAM_ALLOWED_USER_IDS"):
        control.main()


def test_status_reports_progress_and_stale_run(controller):
    run = control.WORK / "20260919T123000Z"
    run.mkdir()
    (run / "summary.json").write_text(
        json.dumps(
            {
                "status": "running",
                "dataset": "all",
                "current_stage": "2/3 Analysis",
                "stages": [{"stage": "Import", "seconds": 12}],
            }
        )
    )
    message = control.status()
    assert "2/3 Analysis" in message
    assert "Import: 12s" in message
    assert "may have been interrupted" in message


def test_task_failure_is_reported_without_raw_error(controller, monkeypatch):
    handler, replies, _ = controller

    def fail(stage="all"):
        raise RuntimeError("secret-token")

    monkeypatch.setattr(control, "start_pipeline", fail)
    handler.handle(update())
    assert "Could not complete" in replies[0]
    assert "secret-token" not in replies[0]


def test_dispatch_uses_only_fixed_launcher(monkeypatch):
    import types

    calls = []
    monkeypatch.setattr(control, "credential", lambda key: "configured")
    monkeypatch.setattr(
        control.subprocess,
        "Popen",
        lambda command, **kw: calls.append((command, kw))
        or types.SimpleNamespace(poll=lambda: 0),
    )
    monkeypatch.setattr(control.Path, "is_file", lambda path: True)
    control.start_pipeline()
    assert calls[0][0] == [
        "cmd.exe",
        "/d",
        "/c",
        str(control.LAUNCHER),
        "--stage",
        "all",
    ]
    assert calls[0][1]["stdin"] == control.subprocess.DEVNULL


@pytest.mark.skipif(control.sys.platform != "win32", reason="Windows launcher")
def test_real_windows_launcher_with_harmless_job(tmp_path, monkeypatch):
    launcher = tmp_path / "test_job.bat"
    marker = tmp_path / "launched.txt"
    launcher.write_text('@echo off\necho launched>"' + str(marker) + '"\n')
    monkeypatch.setattr(control, "LAUNCHER", launcher)
    monkeypatch.setattr(control, "ROOT", tmp_path)
    monkeypatch.setattr(control, "credential", lambda key: "")
    monkeypatch.setattr(control, "_child", None)
    control.start_pipeline()
    assert control._child.wait(timeout=10) == 0
    assert marker.read_text().strip() == "launched"


def test_runner_lock_skips_before_calculations(tmp_path, monkeypatch):
    import run_us_pipeline as runner
    from notifications import telegram

    replies = []
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(
        runner, "_run", lambda args: pytest.fail("Must not enter calculations")
    )
    monkeypatch.setattr(telegram, "send_telegram", lambda *args: replies.append(args))
    (tmp_path / "work").mkdir()
    with (tmp_path / "work" / "complete_pipeline.lock").open("a+") as lock:
        acquire_lock(lock)
        assert runner.main([]) == 0
    assert "already running" in replies[0][1]


def test_status_skips_other_summary_formats(controller):
    for name, report in [
        ("20260919T120000Z", {"status": "completed", "stages": []}),
        ("20260919T130000Z", [{"other": "analysis output"}]),
    ]:
        folder = control.WORK / name
        folder.mkdir()
        (folder / "summary.json").write_text(json.dumps(report))
    assert "Recorded status: completed" in control.status()


@pytest.mark.parametrize("name,stage", list(control.STAGE_COMMANDS.items()))
@pytest.mark.parametrize("style", ["hyphen", "slash", "underscore"])
def test_every_requested_stage_alias(controller, name, stage, style):
    handler, _, launches = controller
    command = "us-macroanalysis-" + name
    if style == "slash":
        command = "/" + command
    elif style == "underscore":
        command = "/" + command.replace("-", "_")
    handler.handle(update(command))
    assert launches == [stage]


def test_menu_names_meet_telegram_limits(controller):
    import re

    for item in control.MENU:
        assert re.fullmatch("[a-z0-9_]{1,32}", item["command"])
    handler, _, launches = controller
    handler.handle(update("/us_macro_correlation_momentum"))
    assert launches == ["correlation-momentum"]


@pytest.mark.parametrize(
    "name", [
        "pythonstopall", "us-macroanalysis-stop", "us-macroanalysis-logs",
        "us-cot-import", "us-cot-status",
    ]
)
def test_new_commands_reject_other_users(controller, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorised command caused a side effect")

    monkeypatch.setattr(control, "stop_all_python", forbidden)
    monkeypatch.setattr(control, "stop_pipeline", forbidden)
    monkeypatch.setattr(control, "api", forbidden)
    handler, replies, launches = controller
    handler.handle(update(name, user=999))
    assert not replies and not launches


def test_stop_is_bound_to_the_active_run(controller):
    run_id = "a" * 32
    (control.WORK / "active-pipeline.json").write_text(json.dumps({"run_id": run_id}))
    with (control.WORK / "complete_pipeline.lock").open("a+") as lock:
        acquire_lock(lock)
        reply = control.stop_pipeline()
    assert "Stop requested" in reply
    assert (control.WORK / f"stop-{run_id}.request").exists()
    assert "No active" in control.stop_pipeline()


def test_stop_rejects_invalid_run_identity(controller):
    (control.WORK / "active-pipeline.json").write_text(
        json.dumps({"run_id": "../../bad"})
    )
    with (control.WORK / "complete_pipeline.lock").open("a+") as lock:
        acquire_lock(lock)
        assert "No processes were stopped" in control.stop_pipeline()


def test_kill_all_requires_elevation_and_acknowledgement(controller, monkeypatch):
    handler, replies, _ = controller
    kills = []
    monkeypatch.setattr(control, "is_admin", lambda: False)
    monkeypatch.setattr(control, "stop_all_python", lambda: kills.append(True))
    handler.handle(update("pythonstopall"))
    assert not kills
    assert "administrator" in replies[-1]
    monkeypatch.setattr(control, "is_admin", lambda: True)
    monkeypatch.setattr(control, "send_telegram", lambda *args: False)
    handler.handle(update("pythonstopall", uid=2))
    assert not kills
    monkeypatch.setattr(control, "send_telegram", lambda *args: True)
    handler.handle(update("pythonstopall", uid=3))
    assert kills == [True]
    handler.handle(update("pythonstopall", uid=3))
    assert kills == [True]


def test_log_watch_edits_one_message_and_can_stop(controller, monkeypatch, tmp_path):
    handler, _, _ = controller
    calls = []
    path = tmp_path / "pipeline.log"
    path.write_text("first output secret-key")
    monkeypatch.setattr(control, "LOG_PATH", path)
    monkeypatch.setattr(control, "credential", lambda key: "secret-key")
    monkeypatch.setattr(
        control,
        "api",
        lambda method, **kw: calls.append((method, kw)) or {"message_id": 42},
    )
    handler.handle(update("us-macroanalysis-logs"))
    assert calls[0][0] == "sendMessage"
    assert "secret-key" not in calls[0][1]["text"]
    path.write_text("new output")
    handler.log_watch["next"] = 0
    handler.update_logs()
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["message_id"] == 42
    assert "new output" in calls[-1][1]["text"]
    handler.handle(update("/logs", uid=2))
    assert handler.log_watch is None


def test_log_watch_continues_indefinitely_and_checks_every_30_seconds(
    controller, monkeypatch
):
    handler, _, _ = controller
    calls = []
    now = [100.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        control,
        "api",
        lambda *args, **kw: calls.append((args, kw)) or {"message_id": 42},
    )
    monkeypatch.setattr(control, "log_tail", lambda: str(now[0]))
    handler.toggle_logs()
    assert handler.log_watch["next"] == 130
    now[0] = 129
    handler.update_logs()
    assert len(calls) == 1
    now[0] = 130
    handler.update_logs()
    assert len(calls) == 2
    assert handler.log_watch["next"] == 160
    now[0] = 1000000
    handler.update_logs()
    assert handler.log_watch is not None
    assert handler.log_watch["next"] == 1000030


@pytest.mark.skipif(control.sys.platform != "win32", reason="Windows process tree")
def test_pipeline_stop_terminates_only_its_test_stage(tmp_path, monkeypatch):
    import io
    import os
    import sys
    import threading
    import run_us_pipeline as runner

    request = tmp_path / "stop.request"
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "STOP_REQUEST", request)
    trigger = threading.Timer(1.5, request.touch)
    trigger.start()
    try:
        with pytest.raises(runner.PipelineStopped):
            runner.run_stage(
                1,
                1,
                "Harmless test sleeper",
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('ready'); time.sleep(10)",
                ],
                dict(os.environ),
                io.StringIO(),
            )
    finally:
        trigger.cancel()
        trigger.join()


def test_stage_allowlist_rejects_shell_text(monkeypatch):
    monkeypatch.setattr(
        control.subprocess,
        "Popen",
        lambda *args, **kw: pytest.fail("Launched invalid stage"),
    )
    with pytest.raises(ValueError):
        control.start_pipeline("all & calc.exe")
