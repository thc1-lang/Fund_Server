"""Strict date parsing, exact mappings, and auditable source validation."""

from collections import Counter
from datetime import datetime
from numbers import Real
import numpy as np
import pandas as pd


class ValidationError(ValueError):
    pass


def parse_date(value):
    if value is None or value == "":
        return pd.NaT
    if isinstance(value, bool):
        raise ValidationError(f"Malformed date: {value!r}")
    try:
        if isinstance(value, Real):
            if not np.isfinite(value) or float(value) != int(value):
                raise ValueError()
            result = pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(value))
        elif isinstance(value, (datetime, pd.Timestamp)):
            result = pd.Timestamp(value)
        else:
            result = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
                try:
                    result = pd.Timestamp(datetime.strptime(str(value), fmt))
                    break
                except ValueError:
                    pass
            if result is None:
                raise ValueError()
        if result.tzinfo is not None or result != result.normalize():
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise ValidationError(f"Malformed date: {value!r}") from None


def numeric(value):
    # Sheets ISNUMBER semantics: numeric-looking text and booleans are not observations.
    return (
        float(value)
        if isinstance(value, Real)
        and not isinstance(value, bool)
        and np.isfinite(value)
        else np.nan
    )


def source_frame(rows):
    if not rows or not rows[0] or rows[0][0] != "Date":
        raise ValidationError(
            "Source must have Date in A1 and resolved indicator headers; an import may be loading or broken"
        )
    headers = rows[0][1:]
    counts = Counter(headers)
    invalid = {h for h in headers if not isinstance(h, str) or not h or counts[h] != 1}
    issues = [f"Duplicate/invalid source header: {h!r}" for h in invalid]
    indices = [j for j, h in enumerate(headers, 1) if h not in invalid]
    dates, values = [], []
    for rownum, row in enumerate(rows[1:], 2):
        if not row or row[0] == "":
            if any(x != "" for x in row[1:]):
                raise ValidationError(
                    f"Source row {rownum} has observations without a date"
                )
            continue
        try:
            date = parse_date(row[0])
        except ValidationError as e:
            raise ValidationError(f"Source row {rownum}: {e}") from e
        dates.append(date)
        values.append([numeric(row[j]) if j < len(row) else np.nan for j in indices])
        for j in indices:
            if j < len(row) and row[j] != "" and not np.isfinite(numeric(row[j])):
                issues.append(
                    f"Source row {rownum}, indicator {headers[j-1]!r}: nonnumeric observation {row[j]!r}"
                )
    if not dates or len(set(dates)) != len(dates):
        raise ValidationError("Source dates are empty or duplicated")
    frame = pd.DataFrame(
        values, index=pd.DatetimeIndex(dates), columns=[headers[j - 1] for j in indices]
    ).sort_index()
    if not frame.notna().any().any():
        raise ValidationError("Source contains no finite indicator observations")
    return frame, issues


def pair_definitions(rows, frame):
    if len(rows) < 2:
        raise ValidationError("Output is missing the two pair-definition rows")
    width = max(len(rows[0]), len(rows[1]))
    if width < 2:
        raise ValidationError("No pair definitions found; refusing to clear output")
    pairs, issues = [], []
    for j in range(1, width):
        x = rows[0][j] if j < len(rows[0]) else ""
        y = rows[1][j] if j < len(rows[1]) else ""
        error = None
        if not x and not y:
            error = "completely empty pair column"
        elif not x or not y:
            error = "missing pair definition"
        elif x not in frame.columns or y not in frame.columns:
            error = "missing or duplicated indicator header"
        if error:
            issues.append(f"Column {j+1}, pair {x!r} minus/vs {y!r}: {error}")
        pairs.append((x, y, error))
    return pairs, issues


def output_dates(rows, source_dates):
    # Rows 1:2 and column A are immutable, including Spread Score's date in A2.
    dates = []
    last = max((i for i, r in enumerate(rows[2:], 2) if r and r[0] != ""), default=1)
    for i in range(2, last + 1):
        try:
            dates.append(parse_date(rows[i][0] if rows[i] else ""))
        except ValidationError as e:
            raise ValidationError(f"Output A{i+1}: {e}") from e
    valid = [d for d in dates if not pd.isna(d)]
    if valid != sorted(set(valid)):
        raise ValidationError(
            "Output dates must be unique and chronological; column A is preserved"
        )
    # A source date held in a header row cannot receive a result, but is still used as history.
    header_dates = set()
    for r in rows[:2]:
        try:
            d = parse_date(r[0] if r else "")
            if not pd.isna(d):
                header_dates.add(d)
        except ValidationError:
            pass
    missing = set(source_dates) - set(valid) - header_dates
    if missing:
        raise ValidationError(
            f"{len(missing)} source dates missing from preserved output column A (latest {max(missing).date()}); extend its date structure before publishing"
        )
    return pd.DatetimeIndex(dates)
