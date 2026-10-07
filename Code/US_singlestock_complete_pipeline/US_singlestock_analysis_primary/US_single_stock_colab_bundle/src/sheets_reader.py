from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import os
import pandas as pd

from .sheets_writer import _retry_google_request


@dataclass
class WorkbookMetadata:
    spreadsheet_id: str
    title: str
    tabs: list[str]
    source_row_count: int
    source_column_count: int
    modified_time: str | None = None


def read_local_export(path: str | Path, sheet_name: str = "Dataset") -> tuple[pd.DataFrame, WorkbookMetadata]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Local workbook not found: {p}")
    book = pd.ExcelFile(p)
    if sheet_name not in book.sheet_names:
        raise ValueError(f"Source tab {sheet_name!r} missing; tabs={book.sheet_names}")
    df = pd.read_excel(p, sheet_name=sheet_name, dtype=object)
    meta = WorkbookMetadata("LOCAL_EXPORT", p.stem, book.sheet_names, len(df) + 1, len(df.columns), str(p.stat().st_mtime))
    return df, meta


def _control_rows(values: list[list[object]]) -> list[list[object]]:
    rows: list[list[object]] = []
    for i, row in enumerate(values):
        normalized = [str(x).strip() for x in row]
        if "Parameter" in normalized and "Value" in normalized:
            for candidate in values[i + 1:]:
                if candidate and str(candidate[0]).strip().startswith("STRATEGY WEIGHTING CONTROLS"):
                    break
                if len(candidate) >= 3 and str(candidate[1]).strip():
                    rows.append(list(candidate))
            break
    for i, row in enumerate(values):
        normalized = [str(x).strip() for x in row]
        if normalized[:4] == [
            "Metric / Score", "Safe Weight", "High Growth Potential Weight", "Turnaround Story Weight"
        ]:
            for candidate in values[i + 1:]:
                if not candidate or not str(candidate[0]).strip() or str(candidate[0]).strip() == "Total Weight":
                    break
                family = str(candidate[0]).strip()
                for strategy, column in (("SAFE", 1), ("HIGH_GROWTH_POTENTIAL", 2), ("TURNAROUND_STORY", 3)):
                    raw = candidate[column] if len(candidate) > column else ""
                    rows.append(["Strategy weight", f"STRATEGY_WEIGHT_{strategy}_{family}", raw, "Control Panel matrix"])
            break
    return rows


def read_local_control_panel(path: str | Path, sheet_name: str = "Control Panel") -> list[list[object]]:
    book = pd.ExcelFile(path)
    if sheet_name not in book.sheet_names:
        return []
    values = pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=object).fillna("").values.tolist()
    return _control_rows(values)


def find_default_google_credentials() -> str | None:
    """Use the same shared credential discovery as the other Codex pipelines."""
    configured = os.getenv("GOOGLE_CREDENTIALS_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured:
        return configured
    script_path = Path(__file__).resolve()
    for parent in (script_path.parent, *script_path.parents):
        for filename in ("google_credentials.json", "service_account.json"):
            candidate = parent / filename
            if candidate.is_file():
                return str(candidate)
    return None


def _client(credentials_file: str | None = None):
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError("Install optional Google dependencies from requirements.txt") from exc
    resolved = credentials_file or find_default_google_credentials()
    if not resolved:
        raise RuntimeError(
            "Could not find google_credentials.json or service_account.json in the project "
            "or any parent folder."
        )
    credential_path = Path(resolved).expanduser()
    if not credential_path.is_file():
        raise RuntimeError(f"Google credentials file not found: {credential_path}")
    client = gspread.service_account(filename=str(credential_path))
    client.set_timeout(60)
    return client


def read_google_inputs(
    spreadsheet_id: str,
    sheet_name: str,
    credentials_file: str | None = None,
    control_sheet_name: str = "Control Panel",
) -> tuple[pd.DataFrame, WorkbookMetadata, list[list[object]]]:
    """Read the source data and Control Panel from one authenticated workbook session."""
    book = _client(credentials_file).open_by_key(spreadsheet_id)
    worksheets = {worksheet.title: worksheet for worksheet in _retry_google_request(book.worksheets)}
    if sheet_name not in worksheets:
        raise ValueError(f"Source tab {sheet_name!r} missing; tabs={list(worksheets)}")
    source = worksheets[sheet_name]
    values = _retry_google_request(source.get_all_values)
    if not values:
        raise ValueError("Source sheet is empty")
    df = pd.DataFrame(values[1:], columns=values[0])
    meta = WorkbookMetadata(
        spreadsheet_id, book.title, list(worksheets), source.row_count, source.col_count
    )
    control_rows = _control_rows(_retry_google_request(worksheets[control_sheet_name].get_all_values)) if control_sheet_name in worksheets else []
    return df, meta, control_rows


def read_google_sheet(spreadsheet_id: str, sheet_name: str, credentials_file: str | None = None) -> tuple[pd.DataFrame, WorkbookMetadata]:
    gc = _client(credentials_file)
    book = gc.open_by_key(spreadsheet_id)
    tabs = [ws.title for ws in _retry_google_request(book.worksheets)]
    if sheet_name not in tabs:
        raise ValueError(f"Source tab {sheet_name!r} missing; tabs={tabs}")
    ws = book.worksheet(sheet_name)
    values = _retry_google_request(ws.get_all_values)
    if not values:
        raise ValueError("Source sheet is empty")
    df = pd.DataFrame(values[1:], columns=values[0])
    meta = WorkbookMetadata(spreadsheet_id, book.title, tabs, ws.row_count, ws.col_count)
    return df, meta


def read_google_control_panel(spreadsheet_id: str, credentials_file: str | None = None, sheet_name: str = "Control Panel") -> list[list[object]]:
    book = _client(credentials_file).open_by_key(spreadsheet_id)
    if sheet_name not in [ws.title for ws in _retry_google_request(book.worksheets)]:
        return []
    return _control_rows(_retry_google_request(book.worksheet(sheet_name).get_all_values))


def metadata_json(meta: WorkbookMetadata) -> str:
    return json.dumps(meta.__dict__, indent=2)
