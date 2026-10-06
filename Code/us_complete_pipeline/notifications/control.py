"""Authorised Telegram commands for the existing Windows scheduled pipeline."""

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from notifications.telegram import credential, send_telegram
from run_lock import acquire_lock

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
SERVER_ROOT = ROOT.parents[1]
TASK = "US complete pipeline"
LAUNCHER = SERVER_ROOT / "Scripts" / "run_us_pipeline.bat"
_child = None
BACKUP_TASK = "Fund Server GitHub Backup"
COT_TASK = "US COT report"
COT_ROOT = SERVER_ROOT / "Code" / "us_cot_import"
COT_WORK = COT_ROOT / "work"
COT_LAUNCHER = SERVER_ROOT / "Scripts" / "run_us_COT.cmd"
COT_LOG_PATH = SERVER_ROOT / "Logs" / "us_COT.log"
_cot_child = None
STAGE_COMMANDS = {
    "run": "all",
    "indicators": "importer",
    "analysis": "analysis",
    "relationships": "relationships",
    "spread": "spread",
    "correlation": "correlation",
    "momentum": "momentum",
    "spread-momentum": "spread-momentum",
    "correlation-momentum": "correlation-momentum",
}
LOG_PATH = SERVER_ROOT / "Logs" / "us_complete_pipeline.log"
HELP = (
    "Commands (hyphens or underscores, with or without /):\n"
    + "\n".join(
        "us-macroanalysis-" + name
        for name in (*STAGE_COMMANDS, "status", "logs", "stop")
    )
    + "\npythonstopall — stop ALL Python processes (including this bot; requires administrator listener)."
    + "\nShortcuts: /run /status /logs /stop /help."
    + "\nCOT: /us_cot_import (or /cot) runs the COT import; /us_cot_status (or /cot_status) checks it."
    + "\n/backup starts the safe Fund Server GitHub backup; this chat receives only its completion or failure notice."
    + "\n/logs toggles a live log view every 30 seconds with no time limit."
    + "\nRuns update Google Sheets. Stops do not roll back partial writes. Daily schedule stays enabled."
)
MENU = [
    {
        "command": (
            "us_macro_" if name == "correlation-momentum" else "us_macroanalysis_"
        )
        + name.replace("-", "_"),
        "description": description,
    }
    for name, description in [
        ("run", "Run the entire pipeline"),
        ("status", "Show pipeline status"),
        ("logs", "Watch live logs; repeat to stop watching"),
        ("stop", "Stop this pipeline only"),
        ("indicators", "Run importer only"),
        ("analysis", "Run indicator analysis only"),
        ("relationships", "Run all relationships"),
        ("spread", "Run spread only"),
        ("correlation", "Run correlation only"),
        ("momentum", "Run momentum only"),
        ("spread-momentum", "Run spread momentum only"),
        ("correlation-momentum", "Run correlation momentum only"),
    ]
] + [
    {
        "command": "us_cot_import",
        "description": "Run the CFTC COT import",
    },
    {
        "command": "us_cot_status",
        "description": "Show COT import status",
    },
    {
        "command": "backup",
        "description": "Back up Fund Server safely to GitHub",
    },
    {
        "command": "pythonstopall",
        "description": "Stop ALL Python processes; includes this bot",
    },
    {"command": "help", "description": "List commands and shortcuts"},
]


def api(method, **payload):
    token = credential("TELEGRAM_BOT_TOKEN")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=40) as response:
        result = json.load(response)
    if result.get("ok") is not True:
        raise RuntimeError("Telegram request failed")
    return result["result"]


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def powershell(script):
    # Scripts are fixed by this module; Telegram text never becomes shell code.
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; " + script,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Could not access the Windows pipeline task")
    return result.stdout.strip()


def task_state():
    return powershell(f"(Get-ScheduledTask -TaskName '{TASK}').State.ToString()")


def cot_task_state():
    return powershell(f"(Get-ScheduledTask -TaskName '{COT_TASK}').State.ToString()")


def backup_task_state():
    return powershell(
        f"(Get-ScheduledTask -TaskName '{BACKUP_TASK}').State.ToString()"
    )


def backup_busy():
    return backup_task_state() in ("Running", "Queued")


def start_backup():
    """Start the same scheduled task used for the daily safe GitHub backup."""
    powershell(f"Start-ScheduledTask -TaskName '{BACKUP_TASK}'")


def pipeline_locked():
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "complete_pipeline.lock").open("a+") as handle:
        try:
            acquire_lock(handle)
        except OSError:
            return True
    return False


def busy():
    return (
        (_child is not None and _child.poll() is None)
        or task_state() in ("Running", "Queued")
        or pipeline_locked()
    )


def cot_locked():
    COT_WORK.mkdir(parents=True, exist_ok=True)
    with (COT_WORK / "cot_import.lock").open("a+") as handle:
        try:
            acquire_lock(handle)
        except OSError:
            return True
    return False


def cot_busy():
    return (
        (_cot_child is not None and _cot_child.poll() is None)
        or cot_task_state() in ("Running", "Queued")
        or cot_locked()
    )


def start_pipeline(stage="all"):
    global _child
    if stage not in STAGE_COMMANDS.values():
        raise ValueError("Unsupported pipeline stage")
    if not LAUNCHER.is_file():
        raise RuntimeError("The server pipeline launcher is missing")
    environment = dict(os.environ)
    for key in (
        "FRED_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        if not environment.get(key):
            value = credential(key)
            if value:
                environment[key] = value
    _child = subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(LAUNCHER), "--stage", stage],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def start_cot_import():
    """Launch the same runner used by the Saturday task."""
    global _cot_child
    if not COT_LAUNCHER.is_file():
        raise RuntimeError("The server COT launcher is missing")
    environment = dict(os.environ)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not environment.get(key):
            value = credential(key)
            if value:
                environment[key] = value
    _cot_child = subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(COT_LAUNCHER)],
        cwd=COT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def cot_status():
    state = cot_task_state()
    active = (
        (_cot_child is not None and _cot_child.poll() is None)
        or state in ("Running", "Queued")
        or cot_locked()
    )
    lines = [f"COT import: {'running' if active else 'idle'} (task: {state})."]
    try:
        with COT_LOG_PATH.open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 5000))
            recent = safe_log_text(log.read().decode("utf-8", errors="replace"))
        markers = [line for line in recent.splitlines() if line.startswith("====")]
        if markers:
            lines.append("Latest run: " + markers[-1].strip("= "))
        else:
            lines.append("No completed COT run is recorded in the log yet.")
    except OSError:
        lines.append("The COT log is not available yet.")
    lines.append("A running import will send its own completion report.")
    return "\n".join(lines)


def status():
    state = task_state()
    active = (
        (_child is not None and _child.poll() is None)
        or state in ("Running", "Queued")
        or pipeline_locked()
    )
    lines = [f"Pipeline: {'running' if active else 'idle'} (task: {state})."]
    summaries = []
    for candidate in sorted(WORK.glob("[0-9]*T*/summary.json"), reverse=True):
        try:
            candidate_report = json.loads(candidate.read_text(encoding="utf-8"))
            if (
                isinstance(candidate_report, dict)
                and "status" in candidate_report
                and "stages" in candidate_report
            ):
                summaries.append(candidate)
                break
        except (OSError, ValueError):
            continue
    if summaries:
        try:
            report = json.loads(summaries[0].read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("Unrecognised summary")
            lines += [
                f"Latest recorded run: {summaries[0].parent.name}",
                f"Recorded status: {report.get('status', 'unknown')}",
                f"Dataset: {report.get('dataset', 'unknown')}",
                f"Current/last stage: {report.get('current_stage', 'not recorded')}",
                f"Completed stages: {len(report.get('stages', []))}",
            ]
            for stage in report.get("stages", []):
                lines.append(f"{stage['stage']}: {stage.get('seconds', '?')}s")
            if "seconds" in report:
                lines.append(f"Recorded elapsed: {report['seconds']}s")
            if report.get("error"):
                lines.append("Error: " + str(report["error"]))
            lines.append(f"Log: {summaries[0].parent / 'pipeline.log'}")
            if not active and report.get("status") == "running":
                lines.append(
                    "No active run remains; the last run may have been interrupted."
                )
        except (OSError, ValueError, KeyError, TypeError):
            lines.append("Latest summary is unavailable; inspect the local log.")
    else:
        lines.append("No run summary recorded yet.")
    return "\n".join(lines)


def stop_pipeline():
    if not pipeline_locked():
        return "No active pipeline holds the run lock. If a run is starting, retry shortly."
    try:
        active = json.loads((WORK / "active-pipeline.json").read_text(encoding="utf-8"))
        run_id = active["run_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid run ID")
    except (OSError, ValueError, KeyError, TypeError):
        return "Active run predates remote stopping or cannot be identified. No processes were stopped."
    (WORK / f"stop-{run_id}.request").touch()
    return "Stop requested for this pipeline only. Its runner will terminate the current stage and child processes; setup may need to finish first. Partial writes are not rolled back. The daily schedule stays enabled."


def is_admin():
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def stop_all_python():
    # Separate PowerShell process survives the Python processes it terminates.
    # Snapshot the targets once; the supervisor can restart the listener afterward.
    script = (
        "Start-Sleep -Seconds 2; "
        "$targets = Get-Process | Where-Object { $_.ProcessName -match '^python(?:w|[0-9]+(?:[.][0-9]+)*)?$|^py$' }; "
        "$targets | Stop-Process -Force -ErrorAction Continue"
    )
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def safe_log_text(text):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FRED_API_KEY"):
        value = credential(key)
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(
        r"(?i)(api[_-]?key|token|authorization)([=:]\s*)[^\s&]+",
        r"\1\2[redacted]",
        text,
    )
    return text


def log_tail():
    try:
        with LOG_PATH.open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 6000))
            text = log.read().decode("utf-8", errors="replace")
        return safe_log_text(text)[-3000:] or "No log output yet."
    except OSError:
        return "The server log is not available yet."


def parse_allowed_user_ids(raw_users):
    """Require explicit, positive Telegram user IDs for group control."""
    users = [value.strip() for value in raw_users.split(",")]
    if not users or any(
        not re.fullmatch(r"[0-9]+", value)
        or len(value.lstrip("0")) > 19
        or not 0 < int(value) <= 2**63 - 1
        for value in users
    ):
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive numeric IDs separated by commas")
    return frozenset(str(int(value)) for value in users)


class Controller:
    def __init__(self, chat_id, user_ids, username, offset_path=None):
        self.chat_id = str(chat_id)
        if not re.fullmatch(r"-[0-9]+", self.chat_id) or int(self.chat_id) >= 0:
            raise ValueError("TELEGRAM_CHAT_ID must be a negative group ID")
        self.user_ids = parse_allowed_user_ids(
            user_ids if isinstance(user_ids, str) else ",".join(str(user) for user in user_ids)
        )
        self.username = username.lower()
        self.offset_path = offset_path or WORK / "telegram-offset.json"
        self.offset = None
        if self.offset_path.exists():
            self.offset = int(json.loads(self.offset_path.read_text())["offset"])
        self.last_launch = 0.0
        self.last_cot_launch = 0.0
        self.last_backup_launch = 0.0
        self.log_watch = None

    def checkpoint(self, offset):
        save_json(self.offset_path, {"offset": offset})
        self.offset = offset

    def handle(self, update):
        update_id = int(update["update_id"])
        if self.offset is not None and update_id < self.offset:
            return
        # Persist before executing, so a reconnect/restart cannot replay /run.
        self.checkpoint(update_id + 1)
        message = update.get("message", {})
        if (
            str(message.get("chat", {}).get("id")) != self.chat_id
            or message.get("chat", {}).get("type") not in ("group", "supergroup")
            or str(message.get("from", {}).get("id")) not in self.user_ids
            or message.get("from", {}).get("is_bot")
            or message.get("forward_origin")
        ):
            return
        text = message.get("text", "").strip()
        parts = text.split()
        if not parts:
            return
        command, _, mention = parts[0].partition("@")
        if mention and mention.lower() != self.username:
            return
        command = command.lstrip("/").replace("_", "-").lower()
        if command.startswith("us-macroanalysis-"):
            command = command[len("us-macroanalysis-") :]
        elif command.startswith("us-macro-"):
            command = command[len("us-macro-") :]
        if not text.startswith("/") and command not in (
            *STAGE_COMMANDS,
            "cot",
            "cot-import",
            "cot-status",
            "us-cot-import",
            "us-cot-status",
            "status",
            "logs",
            "stop",
            "help",
            "pythonstopall",
            "backup",
            "fund-backup",
            "fund-server-backup",
        ):
            return
        if time.time() - message.get("date", 0) > 120:
            send_telegram(
                "This command expired while the listener was offline. Send it again.",
                "Telegram control",
            )
            return
        if len(parts) != 1:
            send_telegram("Commands take no arguments.\n" + HELP, "Telegram control")
            return
        try:
            if command in ("help", "start"):
                reply = HELP
            elif command == "status":
                reply = status()
            elif command in STAGE_COMMANDS:
                if time.monotonic() - self.last_launch < 30 or busy():
                    reply = "Already running or recently requested.\n" + status()
                else:
                    start_pipeline(STAGE_COMMANDS[command])
                    self.last_launch = time.monotonic()
                    return  # The runner sends the single start notification.
            elif command in ("cot", "cot-import", "us-cot-import"):
                if time.monotonic() - self.last_cot_launch < 30 or cot_busy():
                    reply = "Already running or recently requested.\n" + cot_status()
                else:
                    start_cot_import()
                    self.last_cot_launch = time.monotonic()
                    return  # The COT runner sends the start and completion reports.
            elif command in ("cot-status", "us-cot-status"):
                reply = cot_status()
            elif command in ("backup", "fund-backup", "fund-server-backup"):
                if time.monotonic() - self.last_backup_launch < 30 or backup_busy():
                    reply = "Backup is already running or was requested recently. Completion or failure will be reported here."
                else:
                    start_backup()
                    self.last_backup_launch = time.monotonic()
                    return  # The backup script sends the completion/failure notice.
            elif command == "stop":
                reply = stop_pipeline()
            elif command == "logs":
                self.toggle_logs()
                return
            elif command == "pythonstopall":
                if not is_admin():
                    reply = "Stopping every Python process requires the administrator listener setup. Run C:\\Server\\Scripts\\install_telegram_startup.ps1 in administrator PowerShell, then restart the listener task. No processes were stopped."
                else:
                    if send_telegram(
                        "Stopping ALL Python processes on this server, including this bot. Partial writes are not rolled back. The supervisor will restart the bot; pipeline runs are not restarted.",
                        "Python stop all",
                    ):
                        stop_all_python()
                    return
            else:
                reply = "Unknown command.\n" + HELP
        except Exception:
            reply = "Could not complete this command. Check the server listener and Task Scheduler. No extra run was queued; use /status before retrying."
        send_telegram(reply, "Telegram control")

    def toggle_logs(self):
        if self.log_watch is not None:
            self.log_watch = None
            send_telegram("Live log watching stopped.", "Pipeline logs")
            return
        text = (
            "Live pipeline log — updates every 30 seconds with no time limit. Send /logs again to stop.\n\n"
            + log_tail()
        )
        sent = api("sendMessage", chat_id=self.chat_id, text=text)
        self.log_watch = {
            "message_id": sent["message_id"],
            "next": time.monotonic() + 30,
            "text": text,
        }

    def update_logs(self):
        watch = self.log_watch
        if watch is None or time.monotonic() < watch["next"]:
            return
        watch["next"] = time.monotonic() + 30
        header = "Live pipeline log — updates every 30 seconds with no time limit. Send /logs again to stop."
        text = header + "\n\n" + log_tail()
        if text != watch["text"]:
            api(
                "editMessageText",
                chat_id=self.chat_id,
                message_id=watch["message_id"],
                text=text,
            )
            watch["text"] = text


def main():
    if sys.platform != "win32":
        raise RuntimeError("This controller requires Windows Task Scheduler")
    chat = credential("TELEGRAM_CHAT_ID")
    raw_users = credential("TELEGRAM_ALLOWED_USER_IDS")
    if not credential("TELEGRAM_BOT_TOKEN") or not chat or not raw_users:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_IDS")
    users = parse_allowed_user_ids(raw_users)
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "telegram-listener.lock").open("a+") as lock:
        acquire_lock(lock)
        me = api("getMe")
        if api("getWebhookInfo").get("url"):
            raise RuntimeError(
                "This bot has a webhook; use a dedicated bot for the pipeline"
            )
        task_state()  # Fail before accepting commands if this account cannot read the task.
        backup_task_state()
        controller = Controller(chat, users, me["username"])
        if controller.offset is None:
            updates = api(
                "getUpdates", offset=-1, timeout=0, allowed_updates=["message"]
            )
            controller.checkpoint(updates[-1]["update_id"] + 1 if updates else 0)
        api(
            "setMyCommands",
            commands=MENU,
            scope={"type": "chat", "chat_id": chat},
        )
        print("Telegram controller ready; restricted commands enabled", flush=True)
        while True:
            try:
                updates = api(
                    "getUpdates",
                    offset=controller.offset,
                    timeout=5 if controller.log_watch else 25,
                    allowed_updates=["message"],
                )
                for update in updates:
                    controller.handle(update)
                controller.update_logs()
                save_json(
                    WORK / "telegram-health.json",
                    {"last_poll": time.time(), "pid": os.getpid()},
                )
            except Exception:
                # Never log raw HTTP exceptions: the request URL contains the token.
                print("Telegram poll failed; retrying in 10 seconds", flush=True)
                time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Telegram controller stopped; check configuration, task access and logs",
            file=sys.stderr,
        )
        raise SystemExit(1)
