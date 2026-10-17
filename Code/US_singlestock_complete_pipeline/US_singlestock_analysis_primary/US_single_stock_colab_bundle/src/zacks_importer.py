"""Optional Zacks CSV importer for the primary single-stock workbook.

The browser interaction is intentionally isolated from the scoring code.  It
never attempts to defeat login, CAPTCHA, or bot checks; use ``--zacks-headed``
when an access step must be completed by a person.
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gspread.utils import rowcol_to_a1

from .schema import validate_headers
from .sheets_writer import _retry_google_request

SCREENER_URL = (
    "https://www.zacks.com/screening/stock-screener"
    "?icid=screening-screening-nav_tracking-zcom-main_menu_wrapper-stock_screener"
)
CATEGORIES = (
    "Popular Criteria", "Company Descriptors", "Size & Share Volume",
    "Price & Price Changes", "Zacks Rank & Style Scores",
    "Broker Rating & Changes", "EPS Surprises & Actuals",
    "EPS Estimate Revisions", "EPS Estimates", "EPS Growth",
    "Sales, Growth & Estimates", "Valuations", "Return on Investment",
    "Income Statement & Growth", "Dividends", "Margins & Turnover",
    "Balance Sheet", "Liquidity & Coverage",
)
DEFAULT_TIMEOUT_MS = 30_000
LONG_TIMEOUT_MS = 90_000
UPLOAD_CHUNK_ROWS = 500


@dataclass(frozen=True)
class ZacksImportResult:
    csv_path: str
    backup_path: str
    rows: int
    columns: int
    market_cap_min_mil: int
    imported_at_utc: str


def _playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Zacks import needs Playwright. Run `python -m pip install -r requirements.txt` "
            "and then `python -m playwright install chromium`."
        ) from exc
    return sync_playwright


def _first_visible(locator: Any) -> Any | None:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    return None


def _wait_for_loader(frame: Any, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    for index in range(frame.locator(".page-loader").count()):
        loader = frame.locator(".page-loader").nth(index)
        if loader.is_visible():
            try:
                loader.wait_for(state="hidden", timeout=timeout_ms)
            except Exception:  # A non-blocking advert loader may remain in the DOM.
                pass


def _manual_gate(scope: Any) -> bool:
    try:
        text = scope.locator("body").inner_text(timeout=5_000).casefold()
    except Exception:
        return False
    return any(phrase in text for phrase in (
        "pardon our interruption", "verify you are human", "checking your browser",
        "complete the security check", "access denied", "unusual traffic",
        "i'm not a robot", "select all images",
    ))


def _screener_frame(page: Any) -> Any:
    deadline = time.monotonic() + LONG_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if "screener-api.zacks.com" in frame.url or frame.locator("#val_12010").count():
                    frame.locator("#val_12010").wait_for(state="visible", timeout=2_000)
                    return frame
            except Exception:
                continue
        time.sleep(.25)
    raise RuntimeError("Zacks did not load its screener frame.")


def _require_usable_screener(page: Any, headed: bool) -> Any:
    deadline = time.monotonic() + (600 if headed else LONG_TIMEOUT_MS / 1000)
    while time.monotonic() < deadline:
        if _manual_gate(page):
            if not headed:
                raise RuntimeError("Zacks requires a manual access step. Rerun with --zacks-headed.")
            time.sleep(1)
            continue
        try:
            frame = _screener_frame(page)
            if not _manual_gate(frame):
                return frame
        except RuntimeError:
            pass
        time.sleep(1)
    raise RuntimeError("Zacks screener was unavailable after waiting for manual access.")


def _add_market_cap(frame: Any, value: int) -> None:
    field = _first_visible(frame.locator("#val_12010"))
    if field is None:
        label = _first_visible(frame.get_by_text(re.compile(r"^\s*Market Cap \(mil\)\s*$", re.I)))
        field = _first_visible(label.locator("xpath=ancestor::tr[1]").locator("input[type='text']")) if label else None
    if field is None:
        raise RuntimeError("Could not locate the Zacks Market Cap (mil) filter.")
    row = field.locator("xpath=ancestor::tr[1]")
    field.fill(str(value))
    field.press("Tab")
    add = _first_visible(row.get_by_text("Add", exact=True))
    if add is None:
        raise RuntimeError("Could not add the Zacks Market Cap criterion.")
    add.click(timeout=DEFAULT_TIMEOUT_MS)
    _wait_for_loader(frame)


def _select_edit_fields(frame: Any) -> None:
    edit_tab = _first_visible(frame.locator("#edit-view-tab a"))
    if edit_tab is None:
        edit_tab = _first_visible(frame.get_by_text("Edit View", exact=True))
    if edit_tab is None:
        raise RuntimeError("Could not open Zacks Edit View.")
    edit_tab.click(timeout=DEFAULT_TIMEOUT_MS)
    _wait_for_loader(frame)
    panel = frame.locator("#edit_view")
    panel.wait_for(state="visible", timeout=LONG_TIMEOUT_MS)
    for category in CATEGORIES:
        category_control = _first_visible(frame.get_by_text(category, exact=True))
        if category_control is None:
            continue
        category_control.click(timeout=DEFAULT_TIMEOUT_MS)
        _wait_for_loader(frame)
        select_all = _first_visible(panel.get_by_text(re.compile(r"^(select|check|add|move) all$", re.I)))
        if select_all is not None and select_all.is_enabled():
            select_all.click(timeout=DEFAULT_TIMEOUT_MS)
            _wait_for_loader(frame)
        for index in range(panel.locator("input[type='checkbox']").count()):
            checkbox = panel.locator("input[type='checkbox']").nth(index)
            if checkbox.is_visible() and checkbox.is_enabled() and not checkbox.is_checked():
                checkbox.check(timeout=8_000)
                time.sleep(.08)


def _download_csv(page: Any, frame: Any, export_dir: Path) -> Path:
    run = _first_visible(frame.locator("#run_screen_result"))
    if run is None:
        run = _first_visible(frame.get_by_text("Run Screen", exact=True))
    if run is None:
        raise RuntimeError("Could not run the Zacks screen.")
    run.click(timeout=DEFAULT_TIMEOUT_MS)
    _wait_for_loader(frame, LONG_TIMEOUT_MS)
    csv_control = None
    deadline = time.monotonic() + LONG_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        csv_control = _first_visible(frame.locator("a.buttons-csv, button.buttons-csv, [class*='buttons-csv']"))
        if csv_control is not None:
            break
        time.sleep(.5)
    if csv_control is None:
        raise RuntimeError("Zacks did not expose a CSV download for the screen results.")
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"zacks_stock_screen_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    with page.expect_download(timeout=LONG_TIMEOUT_MS) as download_info:
        csv_control.click(timeout=DEFAULT_TIMEOUT_MS)
    download_info.value.save_as(target)
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("Zacks returned an empty CSV download.")
    return target


def download_zacks_csv(export_dir: Path, market_cap_min_mil: int, headed: bool) -> Path:
    """Download the selected Zacks screen without bypassing access controls."""
    if market_cap_min_mil < 1:
        raise ValueError("Zacks market-cap minimum must be a positive whole number.")
    profile_dir = export_dir / ".playwright_zacks_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    sync_playwright = _playwright()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=not headed, accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(SCREENER_URL, wait_until="domcontentloaded", timeout=LONG_TIMEOUT_MS)
                frame = _require_usable_screener(page, headed)
                _add_market_cap(frame, market_cap_min_mil)
                _select_edit_fields(frame)
                return _download_csv(page, frame, export_dir)
            except Exception:
                try:
                    page.screenshot(path=export_dir / "zacks_import_failure.png", full_page=True)
                except Exception:
                    pass
                raise
        finally:
            context.close()


def read_and_validate_csv(csv_path: Path) -> list[list[str]]:
    """Parse and validate a Zacks export before any worksheet is changed."""
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [list(row) for row in csv.reader(handle, strict=True)]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError(f"Could not parse Zacks CSV: {csv_path}") from exc
    if len(rows) < 2 or not rows[0]:
        raise RuntimeError("Zacks CSV must contain a header and at least one data row.")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise RuntimeError("Zacks CSV contains ragged rows and was not imported.")
    validate_headers(rows[0])
    return rows


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def replace_dataset_from_csv(book: Any, worksheet_name: str, csv_path: Path, backup_dir: Path) -> tuple[int, int, Path]:
    """Replace one validated worksheet and restore its previous values on failure."""
    rows = read_and_validate_csv(csv_path)
    worksheet = book.worksheet(worksheet_name)
    old_rows = worksheet.get_all_values()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{worksheet_name}_before_zacks_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    _write_csv(backup_path, old_rows)
    changed = False
    try:
        _retry_google_request(worksheet.clear)
        changed = True
        if worksheet.row_count < len(rows) or worksheet.col_count < len(rows[0]):
            _retry_google_request(lambda: worksheet.resize(rows=max(worksheet.row_count, len(rows)), cols=max(worksheet.col_count, len(rows[0]))))
        for start in range(0, len(rows), UPLOAD_CHUNK_ROWS):
            chunk = rows[start:start + UPLOAD_CHUNK_ROWS]
            end_row = start + len(chunk)
            end_cell = rowcol_to_a1(end_row, len(rows[0]))
            _retry_google_request(lambda chunk=chunk, start=start, end_cell=end_cell: worksheet.update(
                values=chunk, range_name=f"A{start + 1}:{end_cell}", value_input_option="RAW"))
        readback = worksheet.get_all_values()
        if readback != rows:
            raise RuntimeError("Dataset read-back did not match the validated Zacks CSV.")
    except Exception as exc:
        if changed:
            try:
                _retry_google_request(worksheet.clear)
                if old_rows:
                    end_cell = rowcol_to_a1(len(old_rows), max(map(len, old_rows)))
                    _retry_google_request(lambda: worksheet.update(values=old_rows, range_name=f"A1:{end_cell}", value_input_option="RAW"))
            except Exception as restore_exc:
                raise RuntimeError(f"Dataset import failed and restore failed; backup is {backup_path}") from restore_exc
        raise RuntimeError(f"Dataset import failed; previous values were restored. Backup: {backup_path}") from exc
    return len(rows), len(rows[0]), backup_path


def import_zacks_to_google_sheet(book: Any, worksheet_name: str, export_dir: Path, market_cap_min_mil: int, headed: bool) -> ZacksImportResult:
    csv_path = download_zacks_csv(export_dir, market_cap_min_mil, headed)
    rows, columns, backup_path = replace_dataset_from_csv(book, worksheet_name, csv_path, export_dir / "backups")
    return ZacksImportResult(str(csv_path), str(backup_path), rows, columns, market_cap_min_mil, datetime.now(timezone.utc).isoformat())
