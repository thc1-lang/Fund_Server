"""Small, best-effort value-change journal shared by notified subprocesses."""

from contextvars import ContextVar
import json
import math
import os
from pathlib import Path

_events = ContextVar("notification_changes", default=None)


def begin():
    return _events.set([])


def end(token):
    _events.reset(token)


def events(path=None):
    result = list(_events.get() or [])
    if path and Path(path).exists():
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return result + [
                {"category": "Run", "name": "change journal", "unknown": True}
            ]
        for line in lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    result.append(item)
            except ValueError:
                continue
    return result


def enabled():
    return _events.get() is not None or bool(os.getenv("US_PIPELINE_CHANGE_REPORT"))


def record(category, name, *, added=0, updated=0, removed=0, unknown=False, note=""):
    item = dict(
        category=category,
        name=str(name),
        added=int(added),
        updated=int(updated),
        removed=int(removed),
        unknown=bool(unknown),
        note=note,
    )
    try:
        path = os.getenv("US_PIPELINE_CHANGE_REPORT")
        if path:
            with Path(path).open("a", encoding="utf-8") as journal:
                journal.write(json.dumps(item) + "\n")
        elif _events.get() is not None:
            _events.get().append(item)
    except OSError:
        # Notification instrumentation must not interrupt spreadsheet work.
        pass


def compare(old, new):
    counts = {"added": 0, "updated": 0, "removed": 0}
    for i in range(max(len(old), len(new))):
        before = old[i] if i < len(old) else []
        after = new[i] if i < len(new) else []
        for j in range(max(len(before), len(after))):
            a = before[j] if j < len(before) else ""
            b = after[j] if j < len(after) else ""
            blank_a, blank_b = a in (None, ""), b in (None, "")
            if blank_a and blank_b:
                continue
            if blank_a:
                counts["added"] += 1
            elif blank_b:
                counts["removed"] += 1
            else:
                try:
                    same = math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
                except (TypeError, ValueError):
                    same = str(a).strip() == str(b).strip()
                if not same:
                    counts["updated"] += 1
    return counts


def capture(reader, new):
    if not enabled():
        return None
    try:
        return compare(reader(), new)
    except Exception:
        return {"unknown": True}


def describe(items):
    if not items:
        return ["Changes: not measured for this run."]
    lines = []
    for category in dict.fromkeys(item["category"] for item in items):
        group = [item for item in items if item["category"] == category]
        totals = {
            key: sum(item.get(key, 0) for item in group)
            for key in ("added", "updated", "removed")
        }
        unknown = sum(bool(item.get("unknown")) for item in group)
        changes = sum(totals.values())
        if changes:
            details = ", ".join(
                f"{value:,} {key}" for key, value in totals.items() if value
            )
            lines.append(f"{category}: {details} values.")
            affected = list(
                dict.fromkeys(
                    item["name"]
                    for item in group
                    if any(item.get(key) for key in totals)
                )
            )
            names = ", ".join(affected[:4])
            if len(affected) > 4:
                names += f" and {len(affected) - 4} more"
            lines.append("  Affected: " + names)
        elif not unknown:
            lines.append(
                f"{category}: no value changes detected in tracked ranges ({len(group)} outputs checked)."
            )
        if unknown:
            lines.append(
                f"{category}: {unknown} outputs without a reliable change comparison."
            )
    return lines
