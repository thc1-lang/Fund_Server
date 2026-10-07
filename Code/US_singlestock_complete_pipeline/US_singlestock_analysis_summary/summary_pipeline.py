"""Read-only evidence packaging for the existing Primary and Secondary models.

This module deliberately does not recalculate, rank, filter, or otherwise alter
either upstream model.  Its only ranking source is the four published Secondary
Summary tabs.  All derived diagnostics are labelled as such and never feed back
into selection.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import math
import os
import shutil
import subprocess
import sys
import uuid
from io import StringIO
from urllib.parse import urlencode
from urllib.request import urlopen
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


SCHEMA_VERSION = "2.0"
PRIMARY_WORKBOOK_ID = "1T2jn-zW7TIDMM5FK0Ih_WEP5puv1nkRHaSKHB3TK_Ms"
SECONDARY_WORKBOOK_ID = "1vVb8EfJnsbPufiVH3ckznzu2TAOL3xW1WTdfceLALeA"
HUMAN_WORKBOOK_ID = "1QLnitAgeWbZn-Uv6ndnzLdhpyPm9W8DlxpZikMo3S8M"
HUMAN_SHEET = "Sheet1"

SHEET_GIDS = {
    PRIMARY_WORKBOOK_ID: {"Dataset": 2037771434, "Control Panel": 1021055518, "Safe Summary": 597195503, "High Growth Potential Summary": 384591708, "Turnaround Story Summary": 319811044, "Short": 672565321},
    SECONDARY_WORKBOOK_ID: {"Control Panel": 35948956, "Safe Secondary Summary": 204165413, "High Growth Potential Secondary Summary": 654578434, "Turnaround Story Secondary Summary": 1809489047, "Short Secondary Summary": 784005448, "Safe Data": 1509264103, "High Growth Potential Data": 789743435, "Turnaround Story Data": 2143053105, "Short Data": 11124071, "Safe Analysis": 1672711200, "High Growth Potential Analysis": 1861429357, "Turnaround Story Analysis": 667236871, "Short Analysis": 1984015290},
}

CATEGORY_CONFIG = {
    "Safe": {
        "primary_summary": "Safe Summary", "secondary_summary": "Safe Secondary Summary",
        "secondary_analysis": "Safe Analysis", "secondary_data": "Safe Data",
        "score_column": "Final Score", "primary_score_column": "Final Strategy Score",
        "control_cap": "TOP_N_SAFE", "control_floor": "MIN_SELECTION_SCORE_SAFE",
    },
    "High Growth Potential": {
        "primary_summary": "High Growth Potential Summary", "secondary_summary": "High Growth Potential Secondary Summary",
        "secondary_analysis": "High Growth Potential Analysis", "secondary_data": "High Growth Potential Data",
        "score_column": "Final Score", "primary_score_column": "Final Strategy Score",
        "control_cap": "TOP_N_HIGH_GROWTH_POTENTIAL", "control_floor": "MIN_SELECTION_SCORE_HIGH_GROWTH_POTENTIAL",
    },
    "Turnaround Story": {
        "primary_summary": "Turnaround Story Summary", "secondary_summary": "Turnaround Story Secondary Summary",
        "secondary_analysis": "Turnaround Story Analysis", "secondary_data": "Turnaround Story Data",
        "score_column": "Final Score", "primary_score_column": "Final Strategy Score",
        "control_cap": "TOP_N_TURNAROUND_STORY", "control_floor": "MIN_SELECTION_SCORE_TURNAROUND_STORY",
    },
    "Short": {
        "primary_summary": "Short", "secondary_summary": "Short Secondary Summary",
        "secondary_analysis": "Short Analysis", "secondary_data": "Short Data",
        "score_column": "Short Score", "primary_score_column": "Short Score",
        "control_cap": "SHORT_TOP_N", "control_floor": "SHORT_MIN_SCORE",
    },
}

STATEMENT_LINE_ITEMS = {
    "income_statement": (
        "Revenue", "Gross Profit", "Operating Income", "EBIT", "EBITDA",
        "Interest Expense", "Income Tax Expense", "Net Income", "EPS Diluted",
    ),
    "cash_flow_statement": (
        "Operating Cash Flow", "Capital Expenditures", "Free Cash Flow",
        "Investing Cash Flow", "Financing Cash Flow", "Dividends Paid",
    ),
    "balance_sheet": (
        "Cash", "Current Assets", "Total Assets", "Current Liabilities",
        "Total Liabilities", "Total Debt", "Shareholders Equity",
        "Shares Outstanding", "Inventory", "Accounts Receivable", "Accounts Payable",
    ),
}

MANAGED_TICKER_FILE_SUFFIXES = (
    "_AI.json",
    "_income_statement.json",
    "_cash_flow_statement.json",
    "_balance_sheet.json",
    "_detailed_summary.json",
)

STATEMENT_PROVENANCE_KEYS = {
    "Revenue": ("revenue",),
    "Gross Profit": ("gross_profit",),
    "Operating Income": ("operating_income",),
    "EBIT": ("ebit",),
    "EBITDA": ("ebitda",),
    "Interest Expense": ("interest",),
    "Income Tax Expense": ("income_tax",),
    "Net Income": ("net_income",),
    "EPS Diluted": ("eps_diluted",),
    "Operating Cash Flow": ("ocf",),
    "Capital Expenditures": ("capex",),
    "Free Cash Flow": ("ocf", "capex"),
    "Investing Cash Flow": ("investing_cash_flow",),
    "Financing Cash Flow": ("financing_cash_flow",),
    "Dividends Paid": ("dividends_paid",),
    "Cash": ("cash",),
    "Current Assets": ("current_assets",),
    "Total Assets": ("assets",),
    "Current Liabilities": ("current_liabilities",),
    "Total Liabilities": ("liabilities",),
    "Total Debt": ("debt",),
    "Shareholders Equity": ("equity",),
    "Shares Outstanding": ("shares",),
    "Inventory": ("inventory",),
    "Accounts Receivable": ("receivables",),
    "Accounts Payable": ("payables",),
}

LOG = logging.getLogger("equity_summary")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"NO DATA", "N/A", "NA", "NONE", "NULL", "-"}:
        return None
    percent = text.endswith("%")
    try:
        result = float(text.rstrip("%"))
    except ValueError:
        return None
    if not math.isfinite(result):
        return None
    return result / 100 if percent else result


def json_safe(value: Any) -> Any:
    """Convert pandas/numpy values while preserving unavailable numerics as null."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value) if not isinstance(value, str) else value


def normalise_ticker(value: Any) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    return text.replace("/", "-")


def safe_folder(ticker: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ticker).strip("._") or "UNKNOWN"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def cell_value(value: str) -> Any:
    """Use numbers where Sheets supplies a simple numerical literal; keep text intact."""
    text = str(value).strip()
    if text == "":
        return None
    if text.upper() in {"TRUE", "FALSE"}:
        return text.upper() == "TRUE"
    number = finite(text)
    # Only coerce unambiguous, non-percent literals to avoid changing labels/dates.
    if number is not None and re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text.replace(",", "")):
        return number
    return text


def rows_from_sheet(values: list[list[str]], sheet: str) -> list[dict[str, Any]]:
    """Find the actual header rather than assuming a title/header row position."""
    header_index = next(
        (i for i, row in enumerate(values[:30]) if {"Ticker", "Secondary Ticker"} & {str(v).strip() for v in row}), None
    )
    if header_index is None:
        raise ValueError(f"{sheet}: could not find a header containing a ticker field in the first 30 rows")
    headers = [str(v).strip() for v in values[header_index]]
    records: list[dict[str, Any]] = []
    for physical_row, row in enumerate(values[header_index + 1 :], start=header_index + 2):
        padded = row + [""] * max(0, len(headers) - len(row))
        record = {header: cell_value(padded[i]) for i, header in enumerate(headers) if header}
        if not record.get("Ticker") and record.get("Secondary Ticker"):
            record["Ticker"] = record["Secondary Ticker"]
        if normalise_ticker(record.get("Ticker")):
            record["_source_row"] = physical_row
            record["_source_sheet"] = sheet
            records.append(record)
    return records


def lookup_by_ticker(records: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    matches: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for record in records:
        ticker = normalise_ticker(record.get("Ticker"))
        if not ticker:
            continue
        if ticker in matches:
            duplicates.add(ticker)
            continue
        matches[ticker] = record
    return matches, duplicates


def read_control_values(records: list[dict[str, Any]]) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    for record in records:
        key = record.get("Parameter") or record.get("Setting") or record.get("Control")
        value = record.get("Value")
        if key:
            controls[str(key).strip()] = value
    return controls


def source_ref(workbook_id: str, record: dict[str, Any] | None, analysis: str) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "workbook_id": workbook_id, "worksheet": record.get("_source_sheet"),
        "row": record.get("_source_row"), "ticker": record.get("Ticker"), "analysis_type": analysis,
    }


def metric_attribution(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not record:
        return []
    metrics: list[dict[str, Any]] = []
    for key, raw_score in record.items():
        if not key.endswith("__score") or key.startswith("_"):
            continue
        name = key.removesuffix("__score")
        if name.isupper():  # family score; family contributions are retained separately.
            continue
        audit = {k.removeprefix(name + "__"): json_safe(v) for k, v in record.items() if k.startswith(name + "__")}
        metric = {
            "metric": name,
            "raw_value": json_safe(record.get(name)),
            "metric_score": json_safe(raw_score),
            "weight": audit.get("weight"),
            "weighted_contribution": audit.get("final_contribution_points") or audit.get("family_contribution_points"),
            "peer_benchmark": audit.get("blended_peer_value") or audit.get("peer_median"),
            "percentile": audit.get("percentile"),
            "interpretation": audit.get("direction"),
            "audit": audit,
        }
        metrics.append(metric)
    return metrics


def contribution_items(record: dict[str, Any] | None, marker: str = "Contribution") -> list[dict[str, Any]]:
    if not record:
        return []
    result: list[dict[str, Any]] = []
    for key, value in record.items():
        if marker.lower() not in key.lower() or key.startswith("_"):
            continue
        number = finite(value)
        if number is not None:
            result.append({"component": key, "contribution": number})
    return sorted(result, key=lambda item: abs(item["contribution"]), reverse=True)


def top_components(record: dict[str, Any] | None, positive: bool) -> list[dict[str, Any]]:
    values = contribution_items(record)
    selected = [v for v in values if (v["contribution"] >= 0 if positive else v["contribution"] < 0)]
    return selected[:5]


def concentration(record: dict[str, Any] | None) -> dict[str, Any]:
    values = contribution_items(record)
    positives = sorted((item["contribution"] for item in values if item["contribution"] > 0), reverse=True)
    absolute = sorted((abs(item["contribution"]) for item in values if item["contribution"] != 0), reverse=True)
    total_positive, total_absolute = sum(positives), sum(absolute)
    return {
        "diagnostic_only": True,
        "positive_contributor_count": len(positives),
        "negative_contributor_count": sum(item["contribution"] < 0 for item in values),
        "top_1_positive_share": positives[0] / total_positive if total_positive else None,
        "top_3_positive_share": sum(positives[:3]) / total_positive if total_positive else None,
        "top_5_positive_share": sum(positives[:5]) / total_positive if total_positive else None,
        "top_1_absolute_share": absolute[0] / total_absolute if total_absolute else None,
    }


def primary_score(record: dict[str, Any] | None, category: str) -> float | None:
    if not record:
        return None
    return finite(record.get(CATEGORY_CONFIG[category]["primary_score_column"]))


def secondary_score(record: dict[str, Any] | None, category: str) -> float | None:
    if not record:
        return None
    for key in ("final_score", "short_score", "Final Short Score", CATEGORY_CONFIG[category]["score_column"]):
        if key in record:
            value = finite(record.get(key))
            if value is not None:
                return value
    return None


def alignment(category: str, primary: dict[str, Any] | None, secondary: dict[str, Any] | None) -> dict[str, Any]:
    """A transparent descriptive rule, never an input to a model or rank."""
    if not primary or not secondary:
        return {"classification": "insufficient evidence", "rule": "one model record unavailable", "contradictions": []}
    if category == "Short":
        breadth = finite(secondary.get("short_adverse_trend_breadth") or secondary.get("Four-Year Adverse Breadth"))
        if breadth is None:
            label = "insufficient evidence"
        else:
            label = "strong confirmation" if breadth >= 0.75 else "partial confirmation" if breadth >= 0.5 else "contradiction"
        topic = "Adverse trend breadth"
        secondary_value = breadth
    else:
        breadth = finite(secondary.get("trend_direction_breadth"))
        if breadth is None:
            label = "insufficient evidence"
        else:
            label = "strong confirmation" if breadth >= 0.75 else "partial confirmation" if breadth >= 0.5 else "contradiction"
        topic = "Favourable trend breadth"
        secondary_value = breadth
    conflict = []
    if label in {"contradiction", "partial confirmation"}:
        conflict.append({
            "topic": topic,
            "primary_evidence": {"score": primary_score(primary, category)},
            "secondary_evidence": {"breadth": secondary_value},
            "interpretation": "Secondary breadth does not fully support the current-state Primary case.",
            "materiality": "high" if label == "contradiction" else "medium",
        })
    return {
        "classification": label,
        "rule": f"descriptive threshold on the published {topic.lower()}; never used for selection",
        "contradictions": conflict,
    }


def threshold_diagnostic(category: str, record: dict[str, Any] | None, controls: dict[str, Any]) -> dict[str, Any]:
    if not record:
        return {"diagnostic_only": True, "status": "unavailable"}
    cfg = CATEGORY_CONFIG[category]
    rank = finite(record.get("Rank") if "Rank" in record else record.get("rank") or record.get("short_rank") or record.get("Secondary Rank"))
    score = secondary_score(record, category)
    cap = finite(controls.get(cfg["control_cap"]))
    floor = finite(controls.get(cfg["control_floor"]))
    return {
        "diagnostic_only": True, "rank": rank, "configured_display_cap": cap,
        "rank_distance_to_final_slot": cap - rank if cap is not None and rank is not None else None,
        "score": score, "configured_selection_score_floor": floor,
        "score_distance_to_floor": score - floor if score is not None and floor is not None else None,
    }


def data_quality(primary_dataset: dict[str, Any] | None, secondary: dict[str, Any] | None, duplicate_flags: list[str]) -> dict[str, Any]:
    warnings = list(duplicate_flags)
    if not primary_dataset:
        warnings.append("primary_dataset_match_missing")
    if not secondary:
        warnings.append("secondary_analysis_match_missing")
    missing_primary = [] if not primary_dataset else [k for k, v in primary_dataset.items() if not k.startswith("_") and v is None]
    missing_secondary = [] if not secondary else [k for k, v in secondary.items() if not k.startswith("_") and v is None]
    if secondary and finite(secondary.get("source_error_count")) not in (None, 0):
        warnings.append("secondary_source_error_count_nonzero")
    status = "warning" if warnings else "complete"
    return {
        "status": status, "warnings": warnings,
        "primary_missing_field_count": len(missing_primary),
        "secondary_missing_field_count": len(missing_secondary),
        "missing_primary_fields": missing_primary[:100],
        "missing_secondary_fields": missing_secondary[:100],
        "no_zero_substitution": True,
    }


def compose_thesis(category: str, primary: dict[str, Any] | None, secondary: dict[str, Any] | None, align: dict[str, Any], quality: dict[str, Any]) -> str:
    p_score, s_score = primary_score(primary, category), secondary_score(secondary, category)
    p = f"Primary {category} score {p_score:.2f}" if p_score is not None else "Primary score unavailable"
    s = f"Secondary score {s_score:.2f}" if s_score is not None else "Secondary score unavailable"
    strongest = top_components(secondary, True)
    weakness = top_components(secondary, False)
    drivers = ", ".join(item["component"] for item in strongest[:2]) or "no contribution detail available"
    risks = ", ".join(item["component"] for item in weakness[:2]) or "no negative contribution detail available"
    return f"{p}; {s}. Secondary-to-Primary alignment is {align['classification']}. Largest published contribution drivers: {drivers}. Largest published negative drivers: {risks}. Data quality status: {quality['status']}. This is a descriptive evidence summary, not a recommendation."


def secondary_reconciliation_error(category: str, record: dict[str, Any] | None) -> float | None:
    """Reconcile only where all published operands are exposed; do not infer missing terms."""
    if not record:
        return None
    def number(*keys: str) -> float | None:
        for key in keys:
            if key in record and record[key] is not None:
                return finite(record[key])
        return None
    final = secondary_score(record, category)
    if category == "Short":
        raw = number("short_raw_score", "Raw Short Score")
        reliability = number("short_trend_reliability", "Trend Reliability")
        expected = 50 + (raw - 50) * reliability if raw is not None and reliability is not None else None
    else:
        raw = number("raw_final_score", "Raw Final Score")
        penalty = number("TREND_DATA_PENALTY__contribution_points", "Trend Data Penalty")
        expected = raw + penalty if raw is not None and penalty is not None else None
    return abs(final - expected) if final is not None and expected is not None else None


def as_of(record: dict[str, Any] | None) -> Any:
    if not record:
        return None
    for field in ("Analysis As Of", "analysis_as_of", "Latest Fiscal Year", "latest_fiscal_year"):
        if record.get(field) is not None:
            return record[field]
    return None


def output_root(argument: str | None) -> Path:
    return Path(argument).expanduser() if argument else Path.home() / "Downloads" / "Equities Quantitative Analysis Summary - code interface"


def parsed_fact_provenance(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("Fact Provenance JSON")
    if isinstance(raw, dict):
        return json_safe(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"unparsed_source_value": str(raw)}
    return json_safe(parsed) if isinstance(parsed, dict) else {"unparsed_source_value": json_safe(parsed)}


def statement_fact_provenance(row: dict[str, Any], line_items: Iterable[str]) -> dict[str, Any]:
    """Keep only provenance relevant to this statement instead of repeating the full blob."""
    full = parsed_fact_provenance(row)
    keys = {key for line_item in line_items for key in STATEMENT_PROVENANCE_KEYS.get(line_item, ())}
    return {key: full[key] for key in sorted(keys) if key in full}


def consolidated_history(memberships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain every distinct published historical row once, with all source locations."""
    records: dict[str, dict[str, Any]] = {}
    for membership in memberships:
        category = str(membership.get("category"))
        for raw_row in membership.get("history") or []:
            row = {key: json_safe(value) for key, value in raw_row.items() if key not in {"_source_sheet", "_source_row", "Fact Provenance JSON"}}
            provenance = parsed_fact_provenance(raw_row)
            if provenance:
                row["Fact Provenance"] = provenance
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
            entry = records.setdefault(fingerprint, {"record": row, "source_categories": set(), "source_locations": []})
            entry["source_categories"].add(category)
            location = {"worksheet": raw_row.get("_source_sheet"), "row": raw_row.get("_source_row")}
            if location not in entry["source_locations"]:
                entry["source_locations"].append(location)
    result = [
        {"record": entry["record"], "source_categories": sorted(entry["source_categories"]), "source_locations": entry["source_locations"]}
        for entry in records.values()
    ]
    return sorted(result, key=lambda entry: (
        finite(entry["record"].get("Fiscal Year")) is None,
        finite(entry["record"].get("Fiscal Year")) or 0,
        str(entry["record"].get("Fiscal Period End") or ""),
    ))


def statement_payload(
    identity: dict[str, Any], memberships: list[dict[str, Any]], statement_type: str, generated_at: str
) -> dict[str, Any]:
    """Package only published annual statement line items for an AI analyst.

    Missing line items remain null and are named explicitly.  Records repeated
    across category-specific source tabs are collapsed by fiscal period and
    accession without changing any published value.
    """
    line_items = STATEMENT_LINE_ITEMS[statement_type]
    observations: dict[tuple[Any, ...], dict[str, Any]] = {}
    categories_by_observation: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for membership in memberships:
        for row in membership.get("history") or []:
            key = (
                row.get("Fiscal Year"), row.get("Fiscal Period End"), row.get("Form"),
                row.get("Accession"), tuple(json.dumps(json_safe(row.get(field)), sort_keys=True) for field in line_items),
            )
            observations.setdefault(key, row)
            categories_by_observation[key].add(str(membership.get("category")))

    periods = []
    for key, row in sorted(
        observations.items(),
        key=lambda item: (
            finite(item[1].get("Fiscal Year")) is None,
            finite(item[1].get("Fiscal Year")) or 0,
            str(item[1].get("Fiscal Period End") or ""),
        ),
    ):
        values = {field: json_safe(row.get(field)) for field in line_items}
        periods.append({
            "fiscal_year": json_safe(row.get("Fiscal Year")),
            "fiscal_period_end": json_safe(row.get("Fiscal Period End")),
            "form": json_safe(row.get("Form")),
            "accepted_at": json_safe(row.get("Accepted At")),
            "line_items": values,
            "unavailable_line_items": [field for field, value in values.items() if value is None],
            "source": {
                "categories": sorted(categories_by_observation[key]),
                "worksheet": json_safe(row.get("_source_sheet")),
                "row": json_safe(row.get("_source_row")),
                "accession": json_safe(row.get("Accession")),
                "data_source": json_safe(row.get("Data Source")),
                "line_item_provenance": statement_fact_provenance(row, line_items),
                "missing_data_notes": json_safe(row.get("Missing Data Notes")),
                "audit_notes": json_safe(row.get("Audit Notes")),
                "source_error": json_safe(row.get("Error")),
            },
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": statement_type,
        "identity": json_safe(identity),
        "basis": "Published annual observations from the Secondary source data tabs; no values are inferred or zero-filled.",
        "units_note": "Monetary values retain source units and currency metadata. Review provenance before combining issuers or periods.",
        "generated_at": generated_at,
        "periods": periods,
    }


def managed_ticker_files(folder: Path) -> list[Path]:
    """Return only module-owned per-ticker files, never unrelated user files."""
    return sorted(
        path for path in folder.iterdir()
        if path.is_file() and any(path.name == f"{folder.name}{suffix}" for suffix in MANAGED_TICKER_FILE_SUFFIXES)
    )


def cleanup_output_bloat(root: Path) -> list[str]:
    """Remove only known disposable filesystem metadata from the managed output tree."""
    removed = []
    if not root.exists():
        return removed
    for path in root.rglob(".DS_Store"):
        if path.is_file():
            path.unlink()
            removed.append(str(path.relative_to(root)))
    return sorted(removed)


def archive_departed_ticker_folders(root: Path, active_ticker_keys: set[str], generated_at: str) -> list[str]:
    """Retain, rather than delete, prior canonical files for names no longer shortlisted.

    Only a direct child containing one of the module's expected ticker files is
    managed.  This avoids touching a user-created directory placed in
    the output root.  A renamed ticker is conservatively a new security key:
    the new key is written and the old one is archived; no fuzzy company match
    can cause evidence to be assigned to the wrong issuer.
    """
    retired: list[str] = []
    archive_root = root / "retired" / generated_at.replace(":", "-")
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name == "retired":
            continue
        if folder.name in active_ticker_keys or not managed_ticker_files(folder):
            continue
        archive_root.mkdir(parents=True, exist_ok=True)
        destination = archive_root / folder.name
        if destination.exists():
            raise RuntimeError(f"Refusing to overwrite an existing retired ticker folder: {destination}")
        shutil.move(str(folder), str(destination))
        retired.append(folder.name)
    return sorted(retired)


class GoogleReader:
    def __init__(self, credentials_file: str | None):
        try:
            import gspread
        except ImportError as exc:  # pragma: no cover - dependency error
            raise RuntimeError("Install requirements.txt before running the pipeline.") from exc
        resolved = credentials_file or os.getenv("GOOGLE_CREDENTIALS_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not resolved or not Path(resolved).expanduser().is_file():
            raise RuntimeError("Supply --credentials-file or GOOGLE_APPLICATION_CREDENTIALS; credentials are never stored by this project.")
        self.client = gspread.service_account(filename=str(Path(resolved).expanduser()))

    def records(self, workbook_id: str, sheet: str) -> list[dict[str, Any]]:
        LOG.info("Reading %s / %s", workbook_id, sheet)
        values = self.client.open_by_key(workbook_id).worksheet(sheet).get_all_values()
        return rows_from_sheet(values, sheet)

    def values(self, workbook_id: str, sheet: str) -> list[list[str]]:
        return self.client.open_by_key(workbook_id).worksheet(sheet).get_all_values()

    def write_human_sheet(self, matrix: list[list[Any]]) -> None:
        book = self.client.open_by_key(HUMAN_WORKBOOK_ID)
        worksheet = book.worksheet(HUMAN_SHEET)
        # Replacing the managed output area is the idempotence policy: no stale or duplicate rows survive.
        worksheet.clear()
        worksheet.update("A1", [["" if x is None else x for x in row] for row in matrix], value_input_option="RAW")
        column_count = len(matrix[2])
        section_rows = [index for index, row in enumerate(matrix) if index >= 3 and str(row[0]).upper() in {name.upper() for name in CATEGORY_CONFIG}]
        section_colours = {
            "SAFE": {"red": 0.85, "green": 0.94, "blue": 0.88},
            "HIGH GROWTH POTENTIAL": {"red": 0.90, "green": 0.88, "blue": 0.97},
            "TURNAROUND STORY": {"red": 1.0, "green": 0.94, "blue": 0.82},
            "SHORT": {"red": 0.98, "green": 0.88, "blue": 0.88},
        }
        requests = [
            {"unmergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": column_count}}},
            {"unmergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": column_count}}},
            {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
            {"mergeCells": {"range": {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
            {"updateSheetProperties": {"properties": {"sheetId": worksheet.id, "gridProperties": {"frozenRowCount": 3, "frozenColumnCount": 2}}, "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 80}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 170}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 11}, "properties": {"pixelSize": 105}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 11, "endIndex": 15}, "properties": {"pixelSize": 240}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 15, "endIndex": column_count}, "properties": {"pixelSize": 190, "hiddenByUser": False}, "fields": "pixelSize,hiddenByUser"}},
            {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": column_count}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.12, "green": 0.28, "blue": 0.45}, "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(textFormat,backgroundColor,wrapStrategy)"}},
            {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": column_count}, "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "foregroundColor": {"red": 0.25, "green": 0.25, "blue": 0.25}}, "backgroundColor": {"red": 0.95, "green": 0.97, "blue": 0.99}, "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(textFormat,backgroundColor,wrapStrategy)"}},
            {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": column_count}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "wrapStrategy": "WRAP", "backgroundColor": {"red": 0.20, "green": 0.40, "blue": 0.60}}}, "fields": "userEnteredFormat(textFormat,wrapStrategy,backgroundColor)"}},
            {"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": len(matrix), "startColumnIndex": 0, "endColumnIndex": column_count}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 52}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 74}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 42}, "fields": "pixelSize"}},
        ]
        for row_index in section_rows:
            requests.append({"repeatCell": {"range": {"sheetId": worksheet.id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 0, "endColumnIndex": column_count}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "foregroundColor": {"red": 0.12, "green": 0.12, "blue": 0.12}}, "backgroundColor": section_colours[str(matrix[row_index][0]).upper()]}}, "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
            requests.append({"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": row_index, "endIndex": row_index + 1}, "properties": {"pixelSize": 56}, "fields": "pixelSize"}})
        book.batch_update({"requests": requests})
        readback = worksheet.get("A1:B3")
        if not readback or readback[0][0] != "Equities Quantitative Analysis Summary":
            raise RuntimeError("Human-sheet read-back failed")


class PublicGoogleReader:
    """Read public Sheets exports only; intentionally has no write capability."""
    def records(self, workbook_id: str, sheet: str) -> list[dict[str, Any]]:
        return rows_from_sheet(self.values(workbook_id, sheet), sheet)

    def values(self, workbook_id: str, sheet: str) -> list[list[str]]:
        try:
            gid = SHEET_GIDS[workbook_id][sheet]
        except KeyError as exc:
            raise ValueError(f"No public-export gid is configured for {workbook_id}/{sheet}") from exc
        url = f"https://docs.google.com/spreadsheets/d/{workbook_id}/export?" + urlencode({"format": "csv", "gid": gid})
        LOG.info("Reading public export %s / %s", workbook_id, sheet)
        with urlopen(url, timeout=90) as response:  # nosec B310 - fixed Google Sheets URL
            payload = response.read().decode("utf-8-sig")
        return list(csv.reader(StringIO(payload)))


def git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def build_human_matrix(memberships: list[dict[str, Any]], generated_at: str) -> list[list[Any]]:
    headers = [
        "Ticker", "Company", "Rank", "Primary Score", "Secondary Score", "Model Agreement", "Market Cap", "Revenue Growth", "Net Margin", "ROA", "Trend Reliability",
        "Key Strengths", "Key Risks / Caveats", "Data Quality", "Plain-English View",
        "Sector", "Industry", "Primary Universe Rank", "Secondary Rank", "Trend & Evidence", "Score Concentration", "Cut-off Detail", "Selection Evidence", "Full Quantitative Thesis", "AI Evidence File", "Fiscal Data Span",
    ]
    matrix: list[list[Any]] = [["Equities Quantitative Analysis Summary"], [f"Updated {generated_at}\nCore view A–O; supporting evidence P–Z is available by scrolling right.\nSelection methodology unchanged."], headers]
    for category in CATEGORY_CONFIG:
        rows = [m for m in memberships if m["category"] == category]
        matrix.append([category.upper()])
        for item in sorted(rows, key=lambda x: (x.get("category_rank") is None, x.get("category_rank") or 10**9, x["ticker"])):
            pweak = human_components(item["primary_negative"], limit=2, empty="")
            sweak = human_components(item["secondary_negative"], limit=2, empty="")
            diag = item["threshold"]
            concentration = item["concentration"]
            concentration_text = concentration_detail(concentration)
            threshold = cut_off_position(diag, item.get("secondary_score"))
            conflict = "; ".join(c["interpretation"] for c in item["alignment"]["contradictions"])
            risk = risk_detail(item, pweak, sweak, conflict)
            source_text = str(item.get("why_selected") or "")
            matrix.append([
                item["ticker"], company_display(item), rank_display(item.get("category_rank"), diag.get("configured_display_cap")), score_display(item.get("primary_score")), score_display(item.get("secondary_score")), alignment_display(item["alignment"]),
                market_cap_display(item.get("market_cap_mil")), published_stat(source_text, "latest revenue growth"), published_stat(source_text, "net margin"), published_stat(source_text, "ROA"), trend_reliability(source_text),
                key_strengths(item), risk, quality_detail(item["data_quality"]), plain_english_view(item, risk),
                item.get("sector"), item.get("industry"), rank_display(item.get("primary_rank")), rank_display(item.get("secondary_rank"), diag.get("configured_display_cap")), trend_data_signal(item.get("why_selected")),
                concentration_text, threshold, selection_snapshot(item.get("why_selected")), item["thesis"], f"{Path(item['ai_file']).name}\nGenerated {generated_at}", fiscal_span(source_text),
            ])
    return matrix


def human_components(components: list[dict[str, Any]], limit: int = 3, empty: str = "No published component detail") -> str:
    """Turn model field names into short, readable labels without altering any scores."""
    labels = []
    for component in components[:limit]:
        label = str(component.get("component") or "").replace("__", " ").replace("_", " ")
        for suffix in (" Family Contribution Points", " Contribution Points", " Contribution", " Final Points", " Raw Score"):
            label = label.replace(suffix, "")
        labels.append(" ".join(label.split()).title())
    return "; ".join(labels) or empty


def human_component_details(components: list[dict[str, Any]], limit: int = 3) -> str:
    details = []
    for component in components[:limit]:
        label = human_components([component], limit=1, empty="")
        contribution = finite(component.get("contribution"))
        details.append(f"{label} {contribution:+.1f} pts" if contribution is not None else label)
    return "; ".join(details) or "No published component detail"


def key_strengths(item: dict[str, Any]) -> str:
    primary = human_component_details(item.get("primary_positive") or [], limit=2)
    secondary = human_component_details(item.get("secondary_positive") or [], limit=2)
    return f"Primary: {primary}\nSecondary: {secondary}"


def plain_english_view(item: dict[str, Any], risk: str) -> str:
    rank = rank_display(item.get("category_rank"), (item.get("threshold") or {}).get("configured_display_cap"))
    agreement = str((item.get("alignment") or {}).get("classification") or "not published")
    primary_strength = human_components(item.get("primary_positive") or [], limit=1)
    secondary_strength = human_components(item.get("secondary_positive") or [], limit=1)
    main_caveat = risk.split("\n", 1)[0]
    return (
        f"{item.get('category')} {rank}. Primary {score_display(item.get('primary_score'))}; "
        f"Secondary {score_display(item.get('secondary_score'))}. Models show {agreement}. "
        f"Main strengths: {primary_strength} and {secondary_strength}. {main_caveat}. "
        "Descriptive evidence only—not a recommendation."
    )


def score_display(value: Any) -> str:
    score = finite(value)
    return f"{score:.2f} / 100" if score is not None else "Not published"


def rank_display(rank_value: Any, cap_value: Any = None) -> str:
    rank = finite(rank_value)
    cap = finite(cap_value)
    if rank is None:
        return "Not published"
    return f"#{rank:.0f} of top {cap:.0f}" if cap is not None else f"#{rank:.0f}"


def company_display(item: dict[str, Any]) -> str:
    company = str(item.get("company") or "")
    exchange = str(item.get("exchange") or "").strip()
    return f"{company}\n{exchange}" if company and exchange else company or "Not published"


def alignment_display(alignment_record: dict[str, Any]) -> str:
    classification = str(alignment_record.get("classification") or "Not published")
    conflict_count = len(alignment_record.get("contradictions") or [])
    return f"{classification.title()}\n{conflict_count} published conflict(s)"


def concentration_detail(concentration: dict[str, Any]) -> str:
    shares = []
    for label, key in (("Top 1", "top_1_positive_share"), ("Top 3", "top_3_positive_share"), ("Top 5", "top_5_positive_share")):
        value = finite(concentration.get(key))
        if value is not None:
            shares.append(f"{label}: {value:.0%}")
    counts = []
    for label, key in (("positive", "positive_contributor_count"), ("negative", "negative_contributor_count")):
        value = finite(concentration.get(key))
        if value is not None:
            counts.append(f"{value:.0f} {label}")
    return f"{' | '.join(shares)}; {', '.join(counts)} contributors" if shares else "Contribution detail unavailable"


def quality_detail(quality: dict[str, Any]) -> str:
    status = str(quality.get("status") or "not published")
    primary_missing = finite(quality.get("primary_missing_field_count"))
    secondary_missing = finite(quality.get("secondary_missing_field_count"))
    counts = []
    if primary_missing is not None:
        counts.append(f"{primary_missing:.0f} primary fields unavailable")
    if secondary_missing is not None:
        counts.append(f"{secondary_missing:.0f} secondary fields unavailable")
    warnings = "; ".join(quality.get("warnings") or []) or "no matching warnings"
    return f"{status.title()}\n{' • '.join(counts)}\n{warnings}" if counts else f"{status.title()}\n{warnings}"


def published_stat(text: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}\s+(-?\d+(?:\.\d+)?%?)", text, flags=re.IGNORECASE)
    return match.group(1) if match else "Not published"


def fiscal_span(text: str) -> str:
    match = re.search(r"(\d+)\s+(?:unique\s+)?fiscal years through (FY\d+)", text, flags=re.IGNORECASE)
    return f"{match.group(1)} years to {match.group(2).upper()}" if match else "Not published"


def trend_reliability(text: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)%\s+(?:effective\s+)?trend reliability", text, flags=re.IGNORECASE)
    return f"{float(match.group(1)):.0f}%" if match else "Not published"


def market_cap_display(value: Any) -> str:
    market_cap = finite(value)
    return f"${market_cap:,.0f}m" if market_cap is not None else "Not published"


def selection_snapshot(why_selected: Any) -> str:
    text = str(why_selected or "").strip()
    sentences = re.split(r"(?<=\.)\s+(?=[A-Z])", text)
    return sentences[0] if text else "Published shortlist membership."


def trend_data_signal(why_selected: Any) -> str:
    text = str(why_selected or "")
    labels = ("Trajectory:", "Deterioration evidence:", "Evidence quality:")
    fragments = []
    for label in labels:
        if label in text:
            fragment = re.split(r"(?<=\.)\s+(?=[A-Z])", text.split(label, 1)[1].strip())[0]
            if fragment:
                fragments.append(f"{label[:-1]}: {fragment}")
    return "\n".join(fragments) or "No published trend/data signal detail."


def published_segment(text: str, label: str, following_labels: tuple[str, ...]) -> str:
    """Read a labelled, published fact segment without deriving a new signal."""
    if label not in text:
        return ""
    segment = text.split(label, 1)[1]
    end_positions = [segment.find(next_label) for next_label in following_labels if segment.find(next_label) >= 0]
    if end_positions:
        segment = segment[:min(end_positions)]
    return segment.strip().rstrip(".")


def risk_detail(item: dict[str, Any], primary_negative: str, secondary_negative: str, conflict: str) -> str:
    """Surface published risk context and caveats; this never changes selection or scores."""
    source = str(item.get("why_selected") or "")
    parts = []
    negative = "; ".join(part for part in [primary_negative, secondary_negative, conflict] if part)
    if negative:
        parts.append(f"Negative score contributor(s): {negative}")

    reliability = published_segment(source, "Evidence quality:", ())
    if reliability:
        parts.append(f"Reliability / coverage: {reliability}")

    if item.get("category") == "Short":
        counter_signals = published_segment(source, "Primary signals:", ("Evidence quality:",))
        if counter_signals:
            parts.append(f"Short counter-signals to monitor: {counter_signals}")
        deterioration = published_segment(source, "Deterioration evidence:", ("Fiscal fundamentals:", "Primary signals:", "Evidence quality:"))
        if deterioration:
            parts.append(f"Published deterioration breadth: {deterioration}")
    else:
        fundamentals = published_segment(source, "Latest fundamentals and growth:", ("Evidence quality:",))
        negative_metrics = re.findall(r"(?:revenue CAGR|latest revenue growth|ROA|net margin|FCF yield)\s+-\d+(?:\.\d+)?%?", fundamentals, flags=re.IGNORECASE)
        if negative_metrics:
            parts.append("Negative fundamental readings: " + "; ".join(negative_metrics))

    return "\n".join(parts) or "No published cross-model conflict detected by the descriptive rule."


def cut_off_position(diagnostic: dict[str, Any], score: Any = None) -> str:
    cap = finite(diagnostic.get("configured_display_cap"))
    rank = finite(diagnostic.get("rank"))
    distance = finite(diagnostic.get("rank_distance_to_final_slot"))
    if cap is not None and rank is not None:
        inside = f" ({distance:.0f} places inside the cap)" if distance is not None else ""
        score_text = f" | secondary score {finite(score):.2f}" if finite(score) is not None else ""
        return f"Rank {rank:.0f} of top {cap:.0f}{inside}{score_text}"
    return "Published cut-off diagnostic unavailable"


def load_sources(reader: GoogleReader | PublicGoogleReader) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return only read-only source data; no upstream API writes occur in this module."""
    primary: dict[str, Any] = {"summaries": {}, "dataset": []}
    secondary: dict[str, Any] = {"summaries": {}, "analysis": {}, "data": {}}
    for category, cfg in CATEGORY_CONFIG.items():
        primary["summaries"][category] = reader.records(PRIMARY_WORKBOOK_ID, cfg["primary_summary"])
        secondary["summaries"][category] = reader.records(SECONDARY_WORKBOOK_ID, cfg["secondary_summary"])
        secondary["analysis"][category] = reader.records(SECONDARY_WORKBOOK_ID, cfg["secondary_analysis"])
        secondary["data"][category] = reader.records(SECONDARY_WORKBOOK_ID, cfg["secondary_data"])
    primary["dataset"] = reader.records(PRIMARY_WORKBOOK_ID, "Dataset")
    # Secondary controls have non-standard headers, so read them defensively.
    values = reader.values(SECONDARY_WORKBOOK_ID, "Control Panel")
    header = next((i for i, row in enumerate(values[:12]) if any(str(v).strip() in {"Parameter", "Setting", "Control"} for v in row)), None)
    if header is None:
        secondary["controls"] = {}
    else:
        headers = [str(v).strip() for v in values[header]]
        controls = []
        for row in values[header + 1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            controls.append({headers[i]: cell_value(padded[i]) for i in range(len(headers)) if headers[i]})
        secondary["controls"] = read_control_values(controls)
    return primary, secondary


def process(primary: dict[str, Any], secondary: dict[str, Any], root: Path, generated_at: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    primary_dataset, primary_dataset_dupes = lookup_by_ticker(primary["dataset"])
    p_summary = {category: lookup_by_ticker(records) for category, records in primary["summaries"].items()}
    s_analysis = {category: lookup_by_ticker(records) for category, records in secondary["analysis"].items()}
    s_data: dict[str, dict[str, list[dict[str, Any]]]] = {}
    data_duplicates: dict[str, set[str]] = {}
    for category, records in secondary["data"].items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in records:
            grouped[normalise_ticker(item.get("Ticker"))].append(item)
        s_data[category] = grouped
        data_duplicates[category] = {ticker for ticker, rows in grouped.items() if len({r.get("Fiscal Year") for r in rows}) != len(rows)}

    selected: list[tuple[str, dict[str, Any]]] = []
    category_duplicates: list[str] = []
    for category, records in secondary["summaries"].items():
        seen: set[str] = set()
        for record in records:
            ticker = normalise_ticker(record.get("Ticker"))
            if ticker in seen:
                category_duplicates.append(f"duplicate_final_shortlist:{category}:{ticker}")
            else:
                selected.append((category, record))
                seen.add(ticker)

    ticker_memberships: dict[str, list[dict[str, Any]]] = defaultdict(list)
    validation = {"shortlist_entries_expected": len(selected), "unique_tickers_expected": 0, "fully_processed": 0, "partially_processed": 0, "failed": 0, "primary_match_failures": 0, "secondary_match_failures": 0, "score_reconciliations_checked": 0, "score_reconciliation_failures": 0, "data_quality_warnings": 0, "statement_files_written": 0, "detailed_summary_files_written": 0, "file_contract_errors": 0, "critical_errors": [], "issues": category_duplicates}
    for category, summary in selected:
        ticker = normalise_ticker(summary.get("Ticker"))
        primary_summary = p_summary[category][0].get(ticker)
        secondary_analysis = s_analysis[category][0].get(ticker)
        primary_record = primary_dataset.get(ticker)
        duplicate_flags = []
        if ticker in primary_dataset_dupes:
            duplicate_flags.append("duplicate_primary_dataset_ticker")
        if ticker in p_summary[category][1]:
            duplicate_flags.append("duplicate_primary_summary_ticker")
        if ticker in s_analysis[category][1]:
            duplicate_flags.append("duplicate_secondary_analysis_ticker")
        if ticker in data_duplicates[category]:
            duplicate_flags.append("duplicate_secondary_fiscal_year")
        if not primary_summary:
            validation["primary_match_failures"] += 1
        if not secondary_analysis:
            validation["secondary_match_failures"] += 1
        reconciliation_error = secondary_reconciliation_error(category, secondary_analysis)
        if reconciliation_error is not None:
            validation["score_reconciliations_checked"] += 1
            # Public/exported summary cells are rounded to two decimals. The tolerance is
            # evidence of that display precision, not a relaxation of model arithmetic.
            if reconciliation_error > 0.02:
                validation["score_reconciliation_failures"] += 1
                validation["issues"].append(f"secondary_score_reconciliation:{category}:{ticker}:{reconciliation_error:.8f}")
        align = alignment(category, primary_summary, secondary_analysis)
        quality = data_quality(primary_record, secondary_analysis, duplicate_flags)
        if quality["warnings"]:
            validation["data_quality_warnings"] += 1
        sec_score = secondary_score(secondary_analysis or summary, category)
        p_score = primary_score(primary_summary, category)
        # The Primary universe rank is explicitly carried into Secondary analysis where present.
        p_rank = finite((secondary_analysis or {}).get("Primary Universe Rank") or (secondary_analysis or {}).get("Primary Short Rank"))
        cat_rank = finite(summary.get("Rank") or (secondary_analysis or {}).get("rank") or (secondary_analysis or {}).get("short_rank"))
        diagnostic = threshold_diagnostic(category, secondary_analysis or summary, secondary["controls"])
        thesis = compose_thesis(category, primary_summary, secondary_analysis, align, quality)
        ticker_memberships[ticker].append({
            "source_ticker": str(summary.get("Ticker") or ticker).strip().upper(),
            "category": category, "category_rank": cat_rank, "primary_score": p_score, "primary_rank": p_rank,
            "secondary_score": sec_score, "secondary_rank": finite((secondary_analysis or {}).get("rank") or (secondary_analysis or {}).get("short_rank") or (secondary_analysis or {}).get("Secondary Rank")),
            "primary_positive": top_components(primary_summary, True), "primary_negative": top_components(primary_summary, False),
            "secondary_positive": top_components(secondary_analysis, True), "secondary_negative": top_components(secondary_analysis, False),
            "alignment": align, "concentration": concentration(secondary_analysis), "threshold": diagnostic,
            "data_quality": quality, "why_selected": summary.get("Quantitative Reason") or summary.get("Shortlist Reason"), "thesis": thesis,
            "company": summary.get("Company Name") or (primary_record or {}).get("Company Name"), "sector": summary.get("Sector") or (primary_record or {}).get("Sector"), "industry": summary.get("Industry") or (primary_record or {}).get("Industry"),
            "exchange": summary.get("Exchange") or (primary_record or {}).get("Exchange"), "market_cap_mil": (primary_record or {}).get("Market Cap (mil)"),
            "summary": summary, "primary_summary": primary_summary, "secondary_analysis": secondary_analysis, "primary_record": primary_record,
            "history": s_data[category].get(ticker, []),
        })

    root.mkdir(parents=True, exist_ok=True)
    validation["bloat_files_removed"] = cleanup_output_bloat(root)
    master_records: list[dict[str, Any]] = []
    human_memberships: list[dict[str, Any]] = []
    for ticker, memberships in sorted(ticker_memberships.items()):
        anchor = memberships[0]
        ticker_key = safe_folder(ticker)
        ticker_folder = root / ticker_key
        ticker_folder.mkdir(parents=True, exist_ok=True)
        summary_path = ticker_folder / f"{ticker_key}_detailed_summary.json"
        statement_paths = {
            statement_type: ticker_folder / f"{ticker_key}_{statement_type}.json"
            for statement_type in STATEMENT_LINE_ITEMS
        }
        identity = {"ticker": anchor["source_ticker"], "matching_key": ticker, "company_name": anchor["company"], "sector": anchor["sector"], "industry": anchor["industry"], "market_cap_mil": (anchor["primary_record"] or {}).get("Market Cap (mil)")}
        combined_history = consolidated_history(memberships)
        record = {
            "schema_version": SCHEMA_VERSION,
            "document_type": "detailed_ai_analyst_summary",
            "identity": identity,
            "financial_statement_files": {name: path.name for name, path in statement_paths.items()},
            "selection": {"final_shortlist": True, "memberships": [{k: json_safe(v) for k, v in m.items() if k in {"category", "category_rank", "primary_score", "primary_rank", "secondary_score", "secondary_rank", "why_selected", "threshold"}} for m in memberships], "selection_source": "published Secondary Summary tabs"},
            "analyst_summary": {
                "quantitative_thesis": " ".join(m["thesis"] for m in memberships),
                "quantitative_strengths": {m["category"]: m["secondary_positive"] for m in memberships},
                "quantitative_weaknesses": {m["category"]: m["secondary_negative"] for m in memberships},
                "risks_and_contradictions": {m["category"]: m["alignment"]["contradictions"] for m in memberships},
                "data_quality": {m["category"]: m["data_quality"] for m in memberships},
            },
            "primary_analysis": {
                "model": "primary_snapshot_v7",
                "point_in_time_note": "Current snapshot evidence; historical PIT validity is not demonstrated by the upstream model.",
                "dataset_record": json_safe(anchor["primary_record"]),
                "category_summary_records": json_safe({m["category"]: m["primary_summary"] for m in memberships}),
                "score_attribution": json_safe({m["category"]: contribution_items(m["primary_summary"]) for m in memberships}),
            },
            "secondary_analysis": {
                "model": "secondary_trend_first_v22",
                "control_values": json_safe(secondary["controls"]),
                "category_summary_records": json_safe({m["category"]: m["summary"] for m in memberships}),
                "category_analysis_records": json_safe({m["category"]: m["secondary_analysis"] for m in memberships}),
                "historical_observations": combined_history,
                "metric_attribution": json_safe({m["category"]: metric_attribution(m["secondary_analysis"]) for m in memberships}),
                "score_attribution": json_safe({m["category"]: contribution_items(m["secondary_analysis"]) for m in memberships}),
            },
            "cross_analysis": {m["category"]: m["alignment"] for m in memberships},
            "model_diagnostics": {m["category"]: {"score_concentration": m["concentration"], "threshold_proximity": m["threshold"]} for m in memberships},
            "source_result_inventory": {
                "primary_dataset_records": 1 if anchor["primary_record"] else 0,
                "primary_category_summary_records": sum(m["primary_summary"] is not None for m in memberships),
                "secondary_category_summary_records": len(memberships),
                "secondary_category_analysis_records": sum(m["secondary_analysis"] is not None for m in memberships),
                "distinct_secondary_historical_records": len(combined_history),
                "secondary_control_values": len(secondary["controls"]),
            },
            "provenance": {"primary_workbook_id": PRIMARY_WORKBOOK_ID, "secondary_workbook_id": SECONDARY_WORKBOOK_ID, "primary_records": [source_ref(PRIMARY_WORKBOOK_ID, m["primary_summary"], "primary_summary") for m in memberships], "secondary_records": [source_ref(SECONDARY_WORKBOOK_ID, m["secondary_analysis"], "secondary_analysis") for m in memberships], "generated_at": generated_at},
            "human_interface_reference": {"workbook_id": HUMAN_WORKBOOK_ID, "sheet": HUMAN_SHEET, "ticker": ticker, "categories": [m["category"] for m in memberships]},
        }
        for statement_type, statement_path in statement_paths.items():
            payload = statement_payload(identity, memberships, statement_type, generated_at)
            statement_path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            validation["statement_files_written"] += 1
        summary_path.write_text(json.dumps(json_safe(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        validation["detailed_summary_files_written"] += 1
        legacy_path = ticker_folder / f"{ticker_key}_AI.json"
        if legacy_path.exists():
            legacy_path.unlink()
        expected_files = {summary_path, *statement_paths.values()}
        if set(managed_ticker_files(ticker_folder)) != expected_files:
            validation["file_contract_errors"] += 1
            validation["issues"].append(f"ticker_file_contract:{ticker}")
        validation["fully_processed"] += 1 if all(m["primary_summary"] and m["secondary_analysis"] for m in memberships) else 0
        validation["partially_processed"] += 1 if not all(m["primary_summary"] and m["secondary_analysis"] for m in memberships) else 0
        master_records.append({"ticker": anchor["source_ticker"], "matching_key": ticker, "company": anchor["company"], "categories": [m["category"] for m in memberships], "category_ranks": {m["category"]: m["category_rank"] for m in memberships}, "primary_scores": {m["category"]: m["primary_score"] for m in memberships}, "secondary_scores": {m["category"]: m["secondary_score"] for m in memberships}, "alignment": {m["category"]: m["alignment"]["classification"] for m in memberships}, "main_quantitative_strength": anchor["secondary_positive"][0] if anchor["secondary_positive"] else None, "main_quantitative_risk": anchor["secondary_negative"][0] if anchor["secondary_negative"] else None, "data_quality_status": anchor["data_quality"]["status"], "detailed_summary_path": str(summary_path), "financial_statement_paths": {name: str(path) for name, path in statement_paths.items()}})
        for membership in memberships:
            human_memberships.append({**membership, "ticker": membership["source_ticker"], "ai_file": str(summary_path)})
    validation["unique_tickers_expected"] = len(ticker_memberships)
    validation["retired_ticker_folders"] = archive_departed_ticker_folders(root, {safe_folder(ticker) for ticker in ticker_memberships}, generated_at)
    validation["status"] = "complete" if all(validation[key] == 0 for key in ("failed", "partially_processed", "primary_match_failures", "secondary_match_failures", "score_reconciliation_failures", "file_contract_errors")) else "partial"
    return human_memberships, {"master_records": master_records, "validation": validation}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a read-only-upstream quantitative evidence package.")
    parser.add_argument("--credentials-file", help="Existing Google service-account JSON; never copied into this project.")
    parser.add_argument("--output-root", help="Defaults to ~/Downloads/Equities Quantitative Analysis Summary - code interface")
    parser.add_argument("--skip-human-sheet", action="store_true", help="Build local JSON evidence only; do not update the human spreadsheet.")
    parser.add_argument("--public-read", action="store_true", help="Read public source-sheet CSV exports without credentials; requires --skip-human-sheet because this mode is read-only.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    generated_at, root = now_utc(), output_root(args.output_root)
    run_id = str(uuid.uuid4())
    if args.public_read and not args.skip_human_sheet:
        parser.error("--public-read is read-only; add --skip-human-sheet or provide credentials for the normal route.")
    reader: GoogleReader | PublicGoogleReader = PublicGoogleReader() if args.public_read else GoogleReader(args.credentials_file)
    try:
        primary, secondary = load_sources(reader)
        memberships, generated = process(primary, secondary, root, generated_at)
        matrix = build_human_matrix(memberships, generated_at)
        if not args.skip_human_sheet:
            LOG.info("Replacing managed human-interface sheet %s", HUMAN_SHEET)
            reader.write_human_sheet(matrix)
        manifest = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "run_timestamp": generated_at, "summary_generated_at": generated_at, "code_version": git_commit(), "source_workbook_ids": {"primary": PRIMARY_WORKBOOK_ID, "secondary": SECONDARY_WORKBOOK_ID}, "source_worksheets_accessed": {"primary": ["Dataset"] + [x["primary_summary"] for x in CATEGORY_CONFIG.values()], "secondary": ["Control Panel"] + [v[k] for v in CATEGORY_CONFIG.values() for k in ("secondary_summary", "secondary_analysis", "secondary_data")]}, "shortlist_entries_expected": generated["validation"]["shortlist_entries_expected"], "unique_tickers": generated["validation"]["unique_tickers_expected"], "number_successfully_processed": generated["validation"]["fully_processed"], "number_partially_processed": generated["validation"]["partially_processed"], "number_failed": generated["validation"]["failed"], "output_root": str(root), "human_sheet_updated": not args.skip_human_sheet, "upstream_sources_written": False}
        (root / "AI_master_index.json").write_text(json.dumps(json_safe({"schema_version": SCHEMA_VERSION, "generated_at": generated_at, "records": generated["master_records"]}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (root / "validation_report.json").write_text(json.dumps(json_safe(generated["validation"]), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (root / "run_manifest.json").write_text(json.dumps(json_safe(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        LOG.info("Completed %s: %s unique ticker files", generated["validation"]["status"], generated["validation"]["unique_tickers_expected"])
        return 0 if generated["validation"]["status"] == "complete" else 2
    except Exception as exc:
        root.mkdir(parents=True, exist_ok=True)
        failure = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "run_timestamp": generated_at, "status": "critical", "error": str(exc)}
        (root / "validation_report.json").write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        LOG.exception("Evidence package failed")
        return 1
