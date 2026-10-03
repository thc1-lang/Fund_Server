#!/usr/bin/env python3
"""Run all US data and scores, or select one independently runnable stage."""

from __future__ import annotations
import argparse
from collections import deque
from datetime import datetime, timezone
import getpass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4
from run_lock import acquire_lock
from notifications.lifecycle import (
    run_notified,
    record_failure,
    STAGE_NAMES,
    set_run_details,
    stage_event,
)

ROOT = Path(__file__).resolve().parent
STOP_REQUEST = None


class PipelineStopped(KeyboardInterrupt):
    """An authorised stop request, handled by the pipeline's own process."""


def check_stop():
    if STOP_REQUEST is not None and STOP_REQUEST.exists():
        raise PipelineStopped(
            "Stopped from Telegram; partial writes are not rolled back"
        )


STAGES = (
    "importer",
    "analysis",
    "relationships",
    "spread",
    "correlation",
    "momentum",
    "spread-momentum",
    "correlation-momentum",
)
PACKAGES = {
    "pandas": "pandas>=2,<4",
    "numpy": "numpy>=1.26,<3",
    "requests": "requests>=2.31,<3",
    "bs4": "beautifulsoup4>=4.12,<5",
    "xlrd": "xlrd>=2,<3",
    "openpyxl": "openpyxl>=3.1,<4",
    "google.auth": "google-auth>=2.30,<3",
    "googleapiclient": "google-api-python-client>=2.140,<3",
    "gspread": "gspread>=6,<7",
}


def dependencies():
    missing = []
    for module, requirement in PACKAGES.items():
        try:
            found = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            found = None
        if found is None:
            missing.append(requirement)
    if missing:
        print("SETUP | Installing missing dependencies", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", *missing], check=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", choices=("all", *STAGES), default="all")
    p.add_argument(
        "--dataset",
        choices=("all", "coincident", "leading"),
        default="all",
        help="Relationship dataset; macro import/analysis still refresh the shared upstream workbook",
    )
    p.add_argument(
        "--credentials", type=Path, default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    p.add_argument("--api-key", default=os.getenv("FRED_API_KEY"))
    p.add_argument("--months", type=int)
    p.add_argument(
        "--spreadsheet-id",
        default=os.getenv(
            "GOOGLE_SPREADSHEET_ID", "19E_Za0DOHMY_9AFSPatK8Cp2vCQypI81QnB3Hz4ve9c"
        ),
    )
    p.add_argument(
        "--wide-spreadsheet-id",
        default=os.getenv(
            "GOOGLE_WIDE_SPREADSHEET_ID", "1qIyTo-8esI23kaobPgnQT65Q6ykbjCXtPyNXC9Xo3KM"
        ),
    )
    p.add_argument(
        "--wide-sheet-name", default=os.getenv("GOOGLE_WIDE_SHEET_NAME", "All data")
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run offline regression tests; no source downloads or spreadsheet writes",
    )
    return p.parse_args(argv)


def build_commands(args):
    python = sys.executable
    importer = [
        python,
        str(ROOT / "monthly_indicators.py"),
        "--spreadsheet-id",
        args.spreadsheet_id,
        "--wide-spreadsheet-id",
        args.wide_spreadsheet_id,
        "--wide-sheet-name",
        args.wide_sheet_name,
        "--recent-only",
        "--months",
        str(args.months if args.months is not None else 12),
    ]
    analysis = [
        python,
        str(ROOT / "us_indicator_analysis.py"),
        "--all",
        "--spreadsheet-id",
        args.spreadsheet_id,
        "--verify",
    ]
    relationships = [
        python,
        str(ROOT / "run_relationships.py"),
        "--dataset",
        args.dataset,
    ]
    if args.stage not in ("all", "importer", "analysis", "relationships"):
        relationships += ["--" + args.stage]
    if args.stage == "all":
        return [
            ("Macro importer", importer),
            ("Macro analysis", analysis),
            (f"Spread, correlation and momentum ({args.dataset})", relationships),
        ]
    return [
        (
            args.stage,
            (
                importer
                if args.stage == "importer"
                else analysis if args.stage == "analysis" else relationships
            ),
        )
    ]


def run_stage(index, total, name, command, env, log):
    check_stop()
    started = time.monotonic()
    stop = threading.Event()
    print(f"\n[{index}/{total}] {name}\n" + "â”€" * 58, flush=True)

    child = None
    cancelled = threading.Event()

    def heartbeat():
        next_progress = time.monotonic() + 30
        while not stop.wait(1):
            if STOP_REQUEST is not None and STOP_REQUEST.exists() and child is not None:
                cancelled.set()
                if child.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=15,
                        )
                    else:
                        child.terminate()
                return
            if time.monotonic() >= next_progress:
                print(
                    f"  RUNNING | {name} | {time.monotonic()-started:.0f}s elapsed",
                    flush=True,
                )
                next_progress = time.monotonic() + 30

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    tail = deque(maxlen=8)
    try:
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in child.stdout:
            tail.append(line.rstrip()[-1000:])
            print("  " + line.rstrip(), flush=True)
            log.write(line)
            log.flush()
        code = child.wait()
        if cancelled.is_set():
            raise PipelineStopped(
                "Stopped from Telegram; partial writes are not rolled back"
            )
        check_stop()
        if code:
            raise RuntimeError(
                f"{name} exited with status {code}; later stages were not run\n"
                + "\n".join(tail)[-750:]
            )
    except BaseException:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        stop.set()
        thread.join()
    elapsed = time.monotonic() - started
    print(f"  DONE | {name} | {elapsed:.1f}s", flush=True)
    return {"stage": name, "status": "completed", "seconds": round(elapsed, 2)}


def main(argv=None):
    global STOP_REQUEST
    args = parse_args(argv)
    if args.self_test:
        return _run(args)
    (ROOT / "work").mkdir(parents=True, exist_ok=True)
    with (ROOT / "work" / "complete_pipeline.lock").open("a+") as lock:
        try:
            acquire_lock(lock)
        except OSError:
            from notifications.telegram import send_telegram

            send_telegram(
                "Another pipeline run is active. This request was skipped.",
                "Pipeline already running",
            )
            return 0
        run_id = uuid4().hex
        STOP_REQUEST = ROOT / "work" / f"stop-{run_id}.request"
        active = ROOT / "work" / "active-pipeline.json"
        active.write_text(
            json.dumps({"pid": os.getpid(), "run_id": run_id, "stage": args.stage}),
            encoding="utf-8",
        )
        try:
            return run_notified(
                STAGE_NAMES[args.stage],
                lambda: _run(args),
                argv=["--dataset", args.dataset, "--api-key", args.api_key or ""],
            )
        finally:
            active.unlink(missing_ok=True)
            STOP_REQUEST.unlink(missing_ok=True)
            STOP_REQUEST = None


def _run(args):
    if args.months is not None and args.months < 1:
        raise ValueError("--months must be positive")
    if args.spreadsheet_id == args.wide_spreadsheet_id:
        raise ValueError("Macro and wide workbook IDs must differ")
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 or later is required")
    check_stop()
    dependencies()
    check_stop()
    if args.self_test:
        for script in ("monthly_indicators.py", "us_indicator_analysis.py"):
            subprocess.run(
                [sys.executable, str(ROOT / script), "--self-test"],
                cwd=ROOT,
                check=True,
            )
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pytest>=8,<10"], check=True
        )
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT
        ).returncode
    from config import credentials_path

    try:
        credential = credentials_path(
            str(args.credentials) if args.credentials else None
        )
    except ValueError:
        if not sys.stdin.isatty():
            raise
        credential = credentials_path(
            input("Google service-account JSON path: ").strip()
        )
    if args.stage in ("all", "importer") and not args.api_key:
        if not sys.stdin.isatty():
            raise ValueError("Set FRED_API_KEY or pass --api-key")
        args.api_key = getpass.getpass(
            "FRED API key (paste, then press Enter; input is hidden): "
        )
        if not args.api_key:
            raise ValueError("FRED API key is required")
    env = {
        **os.environ,
        "GOOGLE_APPLICATION_CREDENTIALS": str(credential.resolve()),
        "GOOGLE_SPREADSHEET_ID": args.spreadsheet_id,
        "PYTHONUNBUFFERED": "1",
        "US_PIPELINE_COMPACT_OUTPUT": "1",
        "US_PIPELINE_FORCE_DIRECT_GOOGLE_API": "1",
    }
    if args.api_key:
        env["FRED_API_KEY"] = args.api_key
    env["US_PIPELINE_NOTIFICATIONS_MANAGED"] = "1"
    run_dir = ROOT / "work" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir.mkdir(parents=True)
    env["US_PIPELINE_CHANGE_REPORT"] = str(run_dir / "changes.jsonl")
    report = {"status": "running", "dataset": args.dataset, "stages": []}
    started = time.monotonic()

    def save_report():
        from notifications.changes import events

        report["changes"] = events(run_dir / "changes.jsonl")
        report["seconds"] = round(time.monotonic() - started, 2)
        temporary = run_dir / "summary.tmp"
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(run_dir / "summary.json")

    try:
        commands = build_commands(args)
        set_run_details(
            log_dir=run_dir,
            stages=[name for name, _ in commands],
            secrets=(args.api_key,),
        )
        print(
            f"US PIPELINE | {len(commands)} stage(s) | live updates\nLogs: {run_dir}",
            flush=True,
        )
        with (run_dir / "pipeline.log").open("w") as log:
            for index, (name, command) in enumerate(commands, 1):
                report["current_stage"] = f"{index}/{len(commands)} {name}"
                save_report()
                stage_event(index, len(commands), name, "started")
                stage_started = time.monotonic()
                report["stages"].append(
                    run_stage(index, len(commands), name, command, env, log)
                )
                save_report()
                stage_event(
                    index,
                    len(commands),
                    name,
                    "finished",
                    seconds=time.monotonic() - stage_started,
                )
        report["status"] = "completed"
    except (Exception, KeyboardInterrupt) as exc:
        report["status"] = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        )
        report["error"] = str(exc)
        if args.api_key:
            report["error"] = report["error"].replace(args.api_key, "[redacted]")
        print(
            f'\nSTOPPED | {exc or "Interrupted"}\nInspect logs: {run_dir}',
            file=sys.stderr,
        )
        record_failure(
            f'{type(exc).__name__}: {exc or "Interrupted"}', secrets=(args.api_key,)
        )
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        save_report()
    print(
        f'\nCOMPLETE | All selected stages verified | {report["seconds"]:.1f}s',
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR | {exc}", file=sys.stderr)
        raise SystemExit(1)
