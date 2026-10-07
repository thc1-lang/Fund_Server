#!/usr/bin/env python3
"""Windows-safe runner for the live US single-stock pipeline.

The copied package originally shipped a Bash launcher.  This runner is the
server entry point: it keeps the three existing projects independent, runs
only fixed commands, writes an auditable run report, and compares published
shortlists without exposing credentials in Telegram notifications.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import queue
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
OUTPUTS = ROOT / "outputs"
MACRO_ROOT = ROOT.parent / "us_complete_pipeline"
sys.path.insert(0, str(MACRO_ROOT))

from notifications.telegram import credential, send_telegram  # noqa: E402
from run_lock import acquire_lock  # noqa: E402

STAGES = ("primary", "secondary", "summary")
STOP_REQUEST: Path | None = None


class PipelineStopped(KeyboardInterrupt):
    """A stop request that belongs to this runner only."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("all", *STAGES), default="all")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--zacks-market-cap-min", type=int, default=3000)
    parser.add_argument("--zacks-headed", action="store_true")
    parser.add_argument("--sec-user-agent")
    parser.add_argument("--sec-cache-dir", type=Path)
    parser.add_argument("--skip-human-sheet", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="Run the copied projects' offline tests only")
    return parser.parse_args(argv)


def resolve_credentials(explicit: Path | None) -> Path:
    candidates = [
        explicit,
        Path(os.environ["GOOGLE_CREDENTIALS_FILE"]) if os.getenv("GOOGLE_CREDENTIALS_FILE") else None,
        Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]) if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else None,
        ROOT / "google_credentials.json",
        ROOT / "service_account.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise ValueError("Google credentials are missing; set GOOGLE_CREDENTIALS_FILE or use --credentials-file")


def stage_commands(args, run_dir: Path, credentials_file: Path):
    common_env = {
        **os.environ,
        "GOOGLE_CREDENTIALS_FILE": str(credentials_file),
        "GOOGLE_APPLICATION_CREDENTIALS": str(credentials_file),
        "ALLOW_CREATE_INDUSTRY_TABS": "true",
        "PYTHONUNBUFFERED": "1",
    }
    primary = [
        sys.executable, "main.py", "--live", "--import-zacks",
        "--zacks-market-cap-min", str(args.zacks_market_cap_min),
        "--output-dir", str(run_dir / "primary"),
    ]
    if args.zacks_headed:
        primary.append("--zacks-headed")
    secondary = [
        sys.executable, "main.py", "--live", "--import-sec-data",
        "--credentials-file", str(credentials_file),
        "--output-dir", str(run_dir / "secondary"),
    ]
    if args.sec_user_agent:
        secondary += ["--sec-user-agent", args.sec_user_agent]
    if args.sec_cache_dir:
        secondary += ["--sec-cache-dir", str(args.sec_cache_dir)]
    summary = [
        sys.executable, "main.py", "--credentials-file", str(credentials_file),
        "--output-root", str(run_dir / "summary"),
    ]
    if args.skip_human_sheet:
        summary.append("--skip-human-sheet")
    mapping = {
        "primary": (ROOT / "US_singlestock_analysis_primary", primary),
        "secondary": (ROOT / "US_singlestock_analysis_secondary", secondary),
        "summary": (ROOT / "US_singlestock_analysis_summary", summary),
    }
    selected = STAGES if args.stage == "all" else (args.stage,)
    return [(stage, *mapping[stage], common_env) for stage in selected]


def stop_requested():
    return STOP_REQUEST is not None and STOP_REQUEST.exists()


def run_stage(index, total, stage, cwd, command, env, log):
    if stop_requested():
        raise PipelineStopped("Stopped from Telegram; partial workbook writes are not rolled back")
    started = time.monotonic()
    print(f"[{index}/{total}] {stage.title()} started", flush=True)
    child = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines = queue.Queue()

    def collect_output():
        if child.stdout is None:
            return
        for line in child.stdout:
            lines.put(line)

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()

    def write_line(line):
        print("  " + line.rstrip(), flush=True)
        log.write(line)
        log.flush()

    try:
        while child.poll() is None:
            try:
                write_line(lines.get(timeout=1))
            except queue.Empty:
                pass
            if stop_requested():
                subprocess.run(
                    ["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ) if os.name == "nt" else child.terminate()
                raise PipelineStopped("Stopped from Telegram; partial workbook writes are not rolled back")
        while True:
            try:
                write_line(lines.get_nowait())
            except queue.Empty:
                break
        if child.returncode:
            raise RuntimeError(f"{stage.title()} exited with status {child.returncode}")
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        reader.join(timeout=2)
    seconds = round(time.monotonic() - started, 2)
    print(f"[{index}/{total}] {stage.title()} complete in {seconds}s", flush=True)
    return {"stage": stage, "seconds": seconds, "status": "completed"}


def shortlist_from_summary(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = set()
    for record in data.get("records", []):
        ticker = str(record.get("ticker", "")).upper()
        for category in record.get("categories", []):
            if ticker and category:
                entries.add((str(category), ticker))
    return entries


def shortlist_from_secondary(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = set()
    category_by_file = {
        "safe_secondary_summary.csv": "Safe",
        "high_growth_potential_secondary_summary.csv": "High Growth Potential",
        "turnaround_story_secondary_summary.csv": "Turnaround Story",
        "short_summary.csv": "Short",
    }
    for filename, category in category_by_file.items():
        candidate = path.parent / filename
        if not candidate.is_file():
            continue
        with candidate.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                ticker = str(row.get("Ticker", "")).strip().upper()
                if ticker:
                    entries.add((category, ticker))
    # The JSON record remains a useful fallback if a failed older run did not
    # produce a CSV. Current runs always compare the complete CSV shortlists.
    if not entries:
        for category, value in data.get("categories", {}).items():
            for ticker in value.get("top_tickers", []):
                entries.add((category, str(ticker).upper()))
        for ticker in data.get("shorts", {}).get("top_tickers", []):
            entries.add(("Short", str(ticker).upper()))
    return entries


def state_path():
    return WORK / "shortlist-state.json"


def saved_shortlist():
    try:
        return {tuple(item) for item in json.loads(state_path().read_text(encoding="utf-8"))["entries"]}
    except (OSError, ValueError, KeyError, TypeError):
        for candidate in sorted(OUTPUTS.glob("*/summary/AI_master_index.json"), reverse=True):
            try:
                return shortlist_from_summary(candidate)
            except (OSError, ValueError, TypeError):
                continue
    return None


def save_shortlist(entries):
    state_path().write_text(
        json.dumps({"entries": sorted(entries), "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def describe_shortlist_change(before, after):
    added, removed = sorted(after - before), sorted(before - after)
    if not added and not removed:
        return "Shortlists unchanged."
    lines = ["Shortlist changes:"]
    for label, values in (("Added", added), ("Removed", removed)):
        grouped = {}
        for category, ticker in values:
            grouped.setdefault(category, []).append(ticker)
        for category, tickers in grouped.items():
            lines.append(f"{label} — {category}: {', '.join(tickers)}")
    return "\n".join(lines)


def notify(title, message):
    send_telegram(message, title)


def self_test():
    # The copied projects intentionally have overlapping module names such as
    # ``main`` and ``test_system``.  Run their suites in separate processes so
    # pytest cannot reuse an import from a different project.
    for project in STAGES:
        directory = ROOT / f"US_singlestock_analysis_{project}"
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=directory)
        if result.returncode:
            return result.returncode
    return 0


def main(argv=None):
    global STOP_REQUEST
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    if args.zacks_market_cap_min < 1:
        raise ValueError("--zacks-market-cap-min must be positive")
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "single_stock_pipeline.lock").open("a+") as lock:
        try:
            acquire_lock(lock)
        except OSError:
            notify("Single-stock pipeline already running", "Another single-stock run is active; this request was skipped.")
            return 0
        run_id = uuid4().hex
        STOP_REQUEST = WORK / f"stop-{run_id}.request"
        run_dir = OUTPUTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        run_dir.mkdir(parents=True)
        active = WORK / "active-single-stock-pipeline.json"
        active.write_text(json.dumps({"run_id": run_id, "pid": os.getpid(), "stage": args.stage, "run_dir": str(run_dir)}), encoding="utf-8")
        report = {"status": "running", "stage": args.stage, "run_dir": str(run_dir), "stages": []}
        started = time.monotonic()
        prior_shortlist = saved_shortlist()
        notify("Single-stock pipeline started", f"Scope: {args.stage}\nStarted: {datetime.now(timezone.utc):%d %b %Y, %H:%M UTC}\nCompletion is reported here; shortlist detail is sent only when it changes.")
        try:
            credentials_file = resolve_credentials(args.credentials_file)
            commands = stage_commands(args, run_dir, credentials_file)
            with (run_dir / "pipeline.log").open("w", encoding="utf-8") as log:
                for index, command in enumerate(commands, 1):
                    report["current_stage"] = command[0]
                    report["stages"].append(run_stage(index, len(commands), *command, log))
            report["status"] = "completed"
            latest = None
            summary_path = run_dir / "summary" / "AI_master_index.json"
            secondary_path = run_dir / "secondary" / "run_summary.json"
            if summary_path.is_file():
                latest = shortlist_from_summary(summary_path)
            elif secondary_path.is_file():
                latest = shortlist_from_secondary(secondary_path)
            if latest is not None:
                change_note = describe_shortlist_change(prior_shortlist, latest) if prior_shortlist is not None else "Shortlist baseline recorded."
                save_shortlist(latest)
            else:
                change_note = "No final shortlist was produced by this selected stage."
            notify("Single-stock pipeline finished", f"Scope: {args.stage}\nDuration: {int(time.monotonic() - started)}s\n{change_note}\n\nDetails: /us-stock-status or /us-stock-logs")
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            report["status"] = "stopped" if isinstance(exc, KeyboardInterrupt) else "failed"
            report["error"] = str(exc)
            notify("Single-stock pipeline stopped" if isinstance(exc, KeyboardInterrupt) else "Single-stock pipeline failed", f"Scope: {args.stage}\nIssue: {str(exc)[-700:]}\nPartial writes are not rolled back. Check /us-stock-status or /us-stock-logs before retrying.")
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
        finally:
            report["seconds"] = round(time.monotonic() - started, 2)
            (run_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            active.unlink(missing_ok=True)
            STOP_REQUEST.unlink(missing_ok=True)
            STOP_REQUEST = None


if __name__ == "__main__":
    raise SystemExit(main())
