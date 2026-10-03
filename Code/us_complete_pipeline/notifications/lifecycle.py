"""Tailored alerts around executable entry points, on Windows, macOS and Colab."""

from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4
from functools import wraps
import os
import platform
import sys
import time

from notifications.telegram import send_telegram
from notifications import changes

STAGE_NAMES = {
    "all": "Pipeline",
    "importer": "Indicators",
    "analysis": "Indicator analysis",
    "relationships": "Relationships",
    "spread": "Spread",
    "correlation": "Correlation",
    "momentum": "Momentum",
    "spread-momentum": "Spread momentum",
    "correlation-momentum": "Correlation momentum",
}
_active = ContextVar("notification_run", default=None)


def record_failure(error, *, secrets=()):
    """Retain errors from entry points that deliberately return an exit code."""
    state = _active.get()
    if state is not None:
        state["error"] = str(error)
        state["secrets"] = state.get("secrets", ()) + tuple(secrets)


def option(argv, name, default=None):
    for index, value in enumerate(argv):
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
    return default


def relationship_name(argv):
    return next(
        (
            STAGE_NAMES[key]
            for key in (
                "correlation",
                "spread",
                "spread-momentum",
                "correlation-momentum",
                "momentum",
            )
            if "--" + key in argv
        ),
        "Relationships",
    )


def set_run_details(*, log_dir=None, stages=(), secrets=()):
    """Attach verified run metadata without emitting an extra alert."""
    state = _active.get()
    if state is not None:
        state["log_dir"] = str(log_dir) if log_dir else None
        state["stages"] = list(stages)
        state["journal"] = str(log_dir) + "/changes.jsonl" if log_dir else None
        state["secrets"] += tuple(secrets)


def duration(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return (
        f"{hours}h {minutes:02d}m {seconds:02d}s"
        if hours
        else f"{minutes}m {seconds:02d}s"
    )


def emit_event(title, detail=""):
    state = _active.get()
    if state is None:
        return
    starting = title.endswith(" started")
    scope = (
        "Entire US pipeline"
        if state["name"] == "Pipeline"
        else {
            "Indicators": "Source data import",
            "Relationships": "Relationship calculations",
        }.get(state["name"], state["name"])
        + " only"
    )
    lines = [f"Scope: {scope}"]
    if state["dataset"] != "default":
        lines.append("Dataset: " + state["dataset"].capitalize())
    if starting:
        lines.append("Started: " + state["started_at"])
        lines.append("You will receive one completion report when this run ends.")
    else:
        lines.append("Duration: " + duration(time.monotonic() - state["started"]))
        lines.append(
            "Finished: " + datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        )
        if state.get("stages"):
            lines.append(
                f"Stages completed: {len(state['completed'])}/{len(state['stages'])}"
            )
        lines.append("")
        try:
            lines.extend(changes.describe(changes.events(state.get("journal"))))
        except Exception:
            lines.append("Change summary unavailable; see the run log.")
        if state["completed"]:
            lines.extend(["", "Completed:", *state["completed"]])
        if detail and not title.endswith(" finished"):
            lines.extend(
                [
                    "",
                    "Issue: " + str(detail)[-700:],
                    "Some writes may be incomplete. Check /status or /logs before retrying.",
                ]
            )
        lines.extend(["", "Details: /status or /logs"])
    device = "Mac" if platform.system() == "Darwin" else platform.system()
    lines.extend(["", f"{device} ({platform.node()}) | Run {state['run_id']}"])
    send_telegram("\n".join(lines), title, secrets=state["secrets"])


def stage_event(index, total, name, outcome, *, seconds=None):
    state = _active.get()
    if state is None:
        return
    state["current_stage"] = f"{index}/{total} {name}"
    detail = name
    if seconds is not None:
        detail += f" — {duration(seconds)}"
    if outcome == "finished":
        state["completed"].append(detail)


def run_notified(name, action, *, argv=(), enabled=True):
    if (
        not enabled
        or os.getenv("US_PIPELINE_NOTIFICATIONS_MANAGED") == "1"
        or _active.get() is not None
    ):
        return action()
    state = {
        "name": name,
        "run_id": uuid4().hex[:12],
        "started": time.monotonic(),
        "started_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "dataset": option(argv, "--dataset", "default"),
        "secrets": (option(argv, "--api-key"),),
        "completed": [],
    }
    change_token = changes.begin()
    token = _active.set(state)
    started = state["started"]

    def notify(outcome, detail=""):
        emit_event(f"{name} {outcome}", detail)

    try:
        notify("started")
        try:
            result = action()
        except SystemExit as exc:
            if exc.code in (None, 0):
                notify("finished", f"Completed in {time.monotonic()-started:.1f}s.")
            else:
                notify("failed", f"SystemExit: {exc.code}")
            raise
        except (Exception, KeyboardInterrupt) as exc:
            notify(
                "stopped" if isinstance(exc, KeyboardInterrupt) else "failed",
                f'{type(exc).__name__}: {exc or "Interrupted"}',
            )
            raise
        if result in (None, 0):
            notify("finished", f"Completed in {time.monotonic()-started:.1f}s.")
        else:
            notify(
                "stopped" if result == 130 else "failed",
                state.get(
                    "error", f"Exited with status {result}; inspect the terminal/logs."
                ),
            )
        return result
    finally:
        _active.reset(token)
        changes.end(change_token)


def notify_entrypoint(name):
    """Wrap a CLI main while preserving its return value and exceptions."""

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            argv = kwargs.get("argv", args[0] if args else None)
            argv = list(sys.argv[1:] if argv is None else argv)
            enabled = not any(flag in argv for flag in ("--self-test", "--help", "-h"))
            label = name(argv) if callable(name) else name
            return run_notified(
                label, lambda: function(*args, **kwargs), argv=argv, enabled=enabled
            )

        return wrapped

    return decorate
