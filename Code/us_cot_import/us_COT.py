"""Download annual CFTC futures-only Disaggregated/TFF data and audit each contract.

Default destinations: COT, COT2, COT_Audit, COT2_Audit. Existing CLI options and
raw data are preserved. The default year follows the UTC calendar automatically.
Use --dry-run to download/audit without Google credentials or spreadsheet writes.

Audit scope is the selected annual file, not a merged historical archive. Coverage
and exact missing dates apply only between a contract's first and last observations;
weeks after the last observation are reported separately as source lag. This avoids
calling a discontinued contract's later absence a data gap. A wholly absent contract
cannot be detected from one file. SOURCE_DELAYED means observation age exceeds
--stale-after-days (default 10), not a verified CFTC publication delay. Historical
--year runs deliberately still measure age against today.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import platform
import re
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CFTC_INDEX_URL = (
    "https://www.cftc.gov/MarketReports/CommitmentsofTraders/"
    "HistoricalCompressed/index.htm"
)
DEFAULT_SERVICE_ACCOUNT_PATH = r'C:\Fund_Server\Code\us_complete_pipeline\google_credentials.json'
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SPREADSHEET_ID = "10fs18m9tX_t_4i8aj__P-NaiH_hsbDeHkudSAJjNQfw"
DEFAULT_DISAGG_SHEET_NAME = "COT"
DEFAULT_TFF_SHEET_NAME = "COT2"
DISAGG_SECTION_LABEL = "Disaggregated Futures Only Reports"
TFF_SECTION_LABEL = "Traders in Financial Futures ; Futures Only Reports"
PIPELINE_ROOT = Path(__file__).resolve().parents[1] / "us_complete_pipeline"
COT_WORK = Path(__file__).resolve().parent / "work"


@dataclass
class ExcelLink:
    year: int
    url: str


def _http_session() -> requests.Session:
    """Retry transient GET failures; never treat permission/network errors as 404."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


HTTP = _http_session()


def _extract_year_candidates(text: str, href: str) -> List[int]:
    """Use digit boundaries: underscores in filenames are word characters."""
    return sorted({int(y) for y in re.findall(r"(?<!\d)([12]\d{3})(?!\d)", f"{text} {href}")})


def _report_stem(section_label: str) -> str:
    if "disaggregated" in section_label.lower():
        return "fut_disagg"
    if "traders in financial futures" in section_label.lower():
        return "fut_fin"
    raise ValueError(f"Unknown report section: {section_label}")


def _fallback_urls(section_label: str, target_year: int) -> List[str]:
    stem = _report_stem(section_label)
    return [f"https://www.cftc.gov/files/dea/history/{stem}_{kind}_{target_year}.zip"
            for kind in ("txt", "xls")]


def _url_exists(url: str) -> bool:
    """Probe with GET (some servers reject HEAD), checking the ZIP signature.

    Only 404/410 mean unavailable. Fail visibly on outages/access denial instead
    of silently selecting an older year. HTML error pages with HTTP 200 are errors.
    """
    with HTTP.get(url, timeout=(15, 45), stream=True) as response:
        if response.status_code in (404, 410):
            return False
        response.raise_for_status()
        prefix = next(response.iter_content(chunk_size=8), b"")
        if not prefix.startswith(b"PK\x03\x04"):
            raise RuntimeError(f"Expected a nonempty ZIP, received a different response: {url}")
        return True


def fetch_section_report_link(section_label: str, year: int | None = None) -> ExcelLink:
    """Check the requested/current year before the index, then newest older year.

    Index links must identify the exact futures-only family. Never substitute a
    combined/options/legacy report when section matching fails. Explicit --year
    never falls back to a different year. Canonical paths also work if the index
    is down. Current-year files are probed even when absent from the index.
    """
    current_year = pd.Timestamp.now(tz="UTC").year
    target_year = current_year if year is None else year
    if not 2006 <= target_year <= current_year:
        raise ValueError(f"Year must be between 2006 and {current_year}.")
    checked = set()

    def probe(y, urls):
        for url in urls:
            if url in checked:
                continue
            checked.add(url)
            if _url_exists(url):
                return ExcelLink(year=y, url=url)
        return None

    hit = probe(target_year, _fallback_urls(section_label, target_year))
    if hit:
        return hit

    indexed = {}
    try:
        with HTTP.get(CFTC_INDEX_URL, timeout=(15, 45)) as response:
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        stem = _report_stem(section_label)
        for anchor in soup.find_all("a", href=True):
            url = urljoin(CFTC_INDEX_URL, anchor["href"])
            parsed = urlparse(url)
            filename = Path(parsed.path).name.lower()
            match = re.fullmatch(rf"{stem}_(txt|xls)_([12]\d{{3}})\.zip", filename)
            if parsed.hostname not in {"www.cftc.gov", "cftc.gov"} or not match:
                continue
            indexed.setdefault(int(match[2]), []).append(url)
    except requests.RequestException as exc:
        print(f"WARNING: CFTC index unavailable ({exc}); using canonical annual paths.")

    years = [target_year] if year is not None else range(target_year, 2005, -1)
    for y in years:
        urls = indexed.get(y, []) + _fallback_urls(section_label, y)
        urls = sorted(set(urls), key=lambda u: ("_txt_" not in u.lower(), u))
        hit = probe(y, urls)
        if hit:
            if y != target_year:
                print(f"WARNING: {section_label}: {target_year} unavailable; using {y}.")
            return hit
    raise RuntimeError(f"No annual futures-only file available for {section_label}, year={year}.")


def _read_delimited_text(path: Path) -> pd.DataFrame:
    """Read UTF-8/Windows text without losing leading zeros in contract codes."""
    errors = []
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                sample = handle.read(16384)
        except UnicodeError as exc:
            errors.append(str(exc))
            continue
        try:
            first_sep = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            first_sep = ","
        for sep in dict.fromkeys([first_sep, ",", "\t", ";", "|"]):
            try:
                data = pd.read_csv(path, sep=sep, encoding=encoding, dtype=object,
                                   keep_default_na=False, low_memory=False)
                _audit_columns(data)
                return data
            except (ValueError, RuntimeError, UnicodeError, pd.errors.ParserError) as exc:
                errors.append(str(exc))
    raise RuntimeError(f"Unable to parse CFTC text file {path.name}: {errors[-1:]}")


def download_report_to_dataframe(report_url: str) -> pd.DataFrame:
    """Read a verified report, preferring text members over legacy Excel.

    Try all supported members (e.g. skip README.txt). Ambiguous ZIPs containing
    multiple valid tables of the preferred format fail rather than lose data.
    Members are copied to a fixed temporary filename, never extracted by path.
    """
    suffix = Path(urlparse(report_url).path).suffix.lower() or ".bin"
    with tempfile.TemporaryDirectory(prefix="cot_download_") as temp_dir:
        local = Path(temp_dir) / f"report{suffix}"
        with HTTP.get(report_url, timeout=(15, 90)) as response:
            response.raise_for_status()
            local.write_bytes(response.content)

        def read(path):
            if path.suffix.lower() in {".xls", ".xlsx", ".xlsm"}:
                frame = pd.read_excel(path, sheet_name=0, dtype=object)
                _audit_columns(frame)
                return frame
            return _read_delimited_text(path)

        if suffix == ".zip":
            errors = []
            with zipfile.ZipFile(local) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()
                           and not m.filename.startswith("__MACOSX/")]
                for extensions in ((".txt", ".csv"), (".xlsx", ".xls", ".xlsm")):
                    valid = []
                    for member in members:
                        ext = Path(member.filename).suffix.lower()
                        if ext not in extensions:
                            continue
                        extracted = Path(temp_dir) / f"member{ext}"
                        extracted.write_bytes(archive.read(member))
                        try:
                            frame = read(extracted)
                            if frame.empty:
                                raise RuntimeError("Empty report table")
                            valid.append(frame)
                        except (ValueError, RuntimeError, ImportError, UnicodeError) as exc:
                            errors.append(f"{member.filename}: {exc}")
                    if len(valid) > 1:
                        raise RuntimeError("ZIP contains multiple report tables; cannot safely select one.")
                    if valid:
                        return valid[0].fillna("")
            raise RuntimeError(f"ZIP contains no readable CFTC report: {errors}")
        data = read(local)
        if data.empty:
            raise RuntimeError("Downloaded report is empty.")
        return data.fillna("")


def _normalize_column_name(name: object) -> str:
    """Ignore case, whitespace, punctuation and underscores in field names."""
    return ''.join(ch.lower() for ch in str(name) if ch.isalnum())


def _find_column(df: pd.DataFrame, candidates: List[str]) -> str | None:
    for candidate in candidates:
        hits = [c for c in df.columns if _normalize_column_name(c) == _normalize_column_name(candidate)]
        if len(hits) > 1:
            raise RuntimeError(f'Ambiguous CFTC field: {candidate}')
        if hits:
            return hits[0]
    return None


def _audit_columns(df: pd.DataFrame):
    """Identify fields conservatively; ambiguous/unknown schemas fail clearly."""
    asset = _find_column(df, ['Market_and_Exchange_Names', 'Market_and_Exchange_Name', 'Market_Name'])
    code = _find_column(df, ['CFTC_Contract_Market_Code', 'Contract_Market_Code'])
    date_cols = []
    for c in df.columns:
        name = _normalize_column_name(c)
        if name.startswith(('reportdate', 'asofdate')):
            date_cols.append(c)
    date_cols.sort(key=lambda c: ('yymmdd' in _normalize_column_name(c), str(c)))
    if asset is None or not date_cols:
        raise RuntimeError(f'Cannot identify market/report-date fields. Columns: {list(df.columns)}')
    return asset, code, date_cols


def _parse_cot_dates(series: pd.Series) -> pd.Series:
    """Parse mixed CFTC dates explicitly, avoiding integer-as-nanosecond errors.

    Six-digit YYMMDD is interpreted as 20YYMMDD for these post-2006 datasets,
    avoiding Python's 1969 pivot. Full four-digit-year fields are preferred.
    """
    raw = series.fillna('').astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    result = pd.Series(pd.NaT, index=series.index, dtype='datetime64[ns]')
    compact = raw.str.fullmatch(r'\d{6}')
    result.loc[compact] = pd.to_datetime('20' + raw.loc[compact], format='%Y%m%d', errors='coerce')
    for fmt in ('%Y%m%d', '%Y-%m-%d', '%m/%d/%Y', '%Y/%m/%d', '%m-%d-%Y', '%Y-%m-%d %H:%M:%S'):
        mask = result.isna()
        result.loc[mask] = pd.to_datetime(raw.loc[mask], format=fmt, errors='coerce')
    serial = result.isna() & raw.str.fullmatch(r'\d{5}')
    if serial.any():
        numbers = pd.to_numeric(raw.loc[serial], errors='coerce')
        numbers = numbers.where(numbers.between(38718, 100000))
        result.loc[serial] = pd.to_datetime(numbers, unit='D', origin='1899-12-30', errors='coerce')
    return result.dt.normalize()


def _report_dates(df: pd.DataFrame) -> pd.Series:
    _, _, columns = _audit_columns(df)
    result = pd.Series(pd.NaT, index=df.index, dtype='datetime64[ns]')
    for col in columns:
        result = result.fillna(_parse_cot_dates(df[col]))
    return result


def _find_long_gaps(dates, threshold_days: int = 10) -> List[str]:
    """Report observed intervals strictly greater than the threshold, not guessed dates."""
    ordered = sorted(set(dates))
    return [f'{a.date()} -> {b.date()} ({(b-a).days} days)'
            for a, b in zip(ordered, ordered[1:]) if (b-a).days > threshold_days]


def build_data_quality_audit(df: pd.DataFrame, stale_after_days: int = 10,
                             inactive_after_reports: int = 3,
                             today=None) -> tuple[pd.DataFrame, dict]:
    """Audit without modifying raw rows; retain invalid rows in counts and flags.

    Stable contract codes group renamed markets together; missing codes fall back
    to market names. Duplicate_Date_Rows counts excess valid rows per date (N-1).
    Coverage uses unique valid dates / source dates between the contract's first
    and last observations. Later source reports are measured as lag instead of
    being mislabeled as missing dates. A lag of ``inactive_after_reports`` or more
    is marked POSSIBLY_INACTIVE_OR_DISCONTINUED because annual CFTC files retain
    markets that may legitimately cease reporting. Source_Status independently
    exposes stale source data even when a contract also lags. Negative ages are
    avoided by excluding and flagging future observations. A contract with no valid
    dates remains visible as NO_VALID_DATES with unknown coverage/lag.
    """
    if df.empty:
        raise RuntimeError('Cannot audit an empty dataset.')
    if stale_after_days < 0:
        raise ValueError('stale_after_days must be nonnegative')
    if inactive_after_reports < 1:
        raise ValueError('inactive_after_reports must be at least 1')
    now = pd.Timestamp.now(tz='UTC') if today is None else pd.Timestamp(today)
    if now.tzinfo is not None:
        now = now.tz_convert('UTC').tz_localize(None)
    now = now.normalize()
    asset_col, code_col, _ = _audit_columns(df)
    work = pd.DataFrame(index=range(len(df)))
    work['asset'] = df[asset_col].fillna('').astype(str).str.strip().to_numpy()
    work['code'] = (df[code_col].fillna('').astype(str).str.strip().str.strip('"').str.replace(r'\.0$', '', regex=True).to_numpy()
                    if code_col is not None else '')
    work['date'] = _report_dates(df).to_numpy()
    work['future'] = work['date'] > now
    work['invalid'] = work['date'].isna()
    work.loc[work['future'], 'date'] = pd.NaT
    work['key'] = ['code:' + c if c else 'name:' + a for c, a in zip(work['code'], work['asset'])]
    source_dates = sorted(work['date'].dropna().unique())
    source_dates = [pd.Timestamp(d) for d in source_dates]
    if not source_dates:
        raise RuntimeError('No valid non-future report dates in source; refusing upload.')
    source_latest = source_dates[-1]
    source_age = (now - source_latest).days
    source_status = 'SOURCE_DELAYED' if source_age > stale_after_days else 'CURRENT'
    source_gaps = _find_long_gaps(source_dates)
    rows = []
    iso = lambda d: pd.Timestamp(d).date().isoformat() if pd.notna(d) else ''
    for _, group in work.groupby('key', sort=True, dropna=False):
        ordered = group.sort_values('date', na_position='first')
        names = ordered.loc[ordered['asset'].ne(''), 'asset']
        asset = names.iloc[-1] if len(names) else '(missing market name)'
        code = group['code'].iloc[0]
        dates = sorted(pd.Timestamp(d) for d in group['date'].dropna().unique())
        earliest = dates[0] if dates else pd.NaT
        latest = dates[-1] if dates else pd.NaT
        expected = [d for d in source_dates if earliest <= d <= latest] if dates else []
        missing = sorted(set(expected) - set(dates))
        lag = (source_latest - latest).days if dates else ''
        behind = sum(d > latest for d in source_dates) if dates else ''
        gaps = _find_long_gaps(dates)
        duplicate_dates = group['date'].dropna().value_counts()
        duplicate_dates = duplicate_dates[duplicate_dates > 1].sort_index()
        duplicates = int((duplicate_dates - 1).sum())
        invalid = int(group['invalid'].sum())
        future = int(group['future'].sum())
        freshness = ('ASSET_LAGGING' if lag > 0 else source_status) if dates else 'NO_VALID_DATES'
        if not dates:
            reporting_status = 'UNKNOWN_NO_VALID_DATES'
        elif behind == 0:
            reporting_status = 'REPORTED_IN_LATEST_SOURCE'
        elif behind < inactive_after_reports:
            reporting_status = 'RECENTLY_NOT_REPORTED'
        else:
            reporting_status = 'POSSIBLY_INACTIVE_OR_DISCONTINUED'
        flags = []
        for condition, flag in [(bool(missing), 'MISSING_SOURCE_DATES'), (bool(gaps), 'LONG_CALENDAR_GAPS'),
                                (duplicates > 0, 'DUPLICATE_DATES'), (invalid > 0, 'INVALID_DATES'),
                                (future > 0, 'FUTURE_DATES'),
                                (group['asset'].eq('').any(), 'MISSING_ASSET_NAME'),
                                (group['code'].eq('').any(), 'MISSING_CONTRACT_CODE')]:
            if condition:
                flags.append(flag)
        status_parts = [freshness]
        if reporting_status not in {'REPORTED_IN_LATEST_SOURCE', 'UNKNOWN_NO_VALID_DATES'}:
            status_parts.append(reporting_status)
        status_parts.extend(flags)
        rows.append({
            'Asset': asset, 'CFTC_Contract_Market_Code': code,
            'Status': ' | '.join(status_parts), 'Freshness': freshness,
            'Reporting_Status': reporting_status, 'Flags': ' | '.join(flags),
            'Source_Status': source_status,
            'Audit_Date_UTC': iso(now), 'Stale_After_Days': stale_after_days,
            'Earliest_Report_Date': iso(earliest), 'Latest_Report_Date': iso(latest),
            'Source_Earliest_Report_Date': iso(source_dates[0]), 'Source_Latest_Report_Date': iso(source_latest),
            'Age_Days_vs_Today': (now-latest).days if dates else '',
            'Source_Age_Days': source_age, 'Lag_Days_vs_Source': lag, 'Reports_Behind_Source': behind,
            'Rows': len(group), 'Valid_Date_Rows': int(group['date'].notna().sum()),
            'Invalid_Date_Rows': invalid, 'Future_Date_Rows': future,
            'Unique_Report_Dates': len(dates), 'Expected_Source_Dates': len(expected) if dates else '',
            'Coverage_Pct': round(100 * len(dates) / len(expected), 2) if expected else '',
            'Missing_Source_Date_Count': len(missing) if dates else '',
            'Missing_Source_Dates': ', '.join(iso(d) for d in missing),
            'Long_Calendar_Gap_Count': len(gaps), 'Long_Calendar_Gaps': ' | '.join(gaps),
            'Duplicate_Date_Rows': duplicates,
            'Duplicate_Dates': ' | '.join(f'{iso(d)} ({count} rows)' for d, count in duplicate_dates.items()),
            'Source_Report_Date_Count': len(source_dates),
            'Source_Long_Calendar_Gap_Count': len(source_gaps), 'Source_Long_Calendar_Gaps': ' | '.join(source_gaps),
            'Observed_Market_Names': ' | '.join(sorted(set(group['asset']) - {''})),
            'Coverage_Basis': 'Observed source dates between first and last contract observations; selected annual file only',
        })
    audit = pd.DataFrame(rows)
    numeric_issues = audit[['Invalid_Date_Rows', 'Future_Date_Rows',
                            'Duplicate_Date_Rows', 'Missing_Source_Date_Count',
                            'Long_Calendar_Gap_Count']].apply(pd.to_numeric, errors='coerce').fillna(0)
    serious_problem = numeric_issues[['Invalid_Date_Rows', 'Future_Date_Rows',
                                      'Duplicate_Date_Rows']].gt(0).any(axis=1)
    gap_problem = numeric_issues[['Missing_Source_Date_Count',
                                  'Long_Calendar_Gap_Count']].gt(0).any(axis=1)
    inactive = audit['Reporting_Status'].eq('POSSIBLY_INACTIVE_OR_DISCONTINUED')
    recent = audit['Reporting_Status'].eq('RECENTLY_NOT_REPORTED')
    priority = pd.Series(5, index=audit.index)
    priority.loc[inactive] = 4
    priority.loc[recent] = 2
    priority.loc[gap_problem & ~inactive] = 1
    priority.loc[serious_problem] = 0
    audit['_priority'] = priority
    audit['_lag_sort'] = pd.to_numeric(audit['Lag_Days_vs_Source'], errors='coerce').fillna(-1)
    audit = audit.sort_values(['_priority', '_lag_sort', 'Asset'], ascending=[True, False, True],
                              kind='stable').drop(columns=['_priority', '_lag_sort']).reset_index(drop=True)
    summary = {
        'source_earliest': iso(source_dates[0]), 'source_latest': iso(source_latest),
        'source_age_days': source_age, 'source_status': source_status,
        'source_report_dates': len(source_dates), 'source_calendar_gaps': source_gaps,
        'asset_count': len(audit), 'row_count': len(df),
        'current_count': int(audit['Freshness'].eq('CURRENT').sum()),
        'source_delayed_count': int(audit['Freshness'].eq('SOURCE_DELAYED').sum()),
        'lagging_count': int(audit['Freshness'].eq('ASSET_LAGGING').sum()),
        'no_valid_dates_count': int(audit['Freshness'].eq('NO_VALID_DATES').sum()),
        'recently_not_reported_count': int(audit['Reporting_Status'].eq('RECENTLY_NOT_REPORTED').sum()),
        'possibly_inactive_count': int(audit['Reporting_Status'].eq('POSSIBLY_INACTIVE_OR_DISCONTINUED').sum()),
        'reported_latest_count': int(audit['Reporting_Status'].eq('REPORTED_IN_LATEST_SOURCE').sum()),
        'clean_count': int(audit['Status'].eq('CURRENT').sum()),
        'assets_with_integrity_flags': int(audit['Flags'].ne('').sum()),
        'assets_with_missing_dates': int(audit['Flags'].str.contains('MISSING_SOURCE_DATES').sum()),
        'assets_with_long_gaps': int(audit['Long_Calendar_Gap_Count'].gt(0).sum()),
        'assets_with_duplicates': int(audit['Duplicate_Date_Rows'].gt(0).sum()),
        'invalid_date_rows': int(work['invalid'].sum()), 'future_date_rows': int(work['future'].sum()),
    }
    return audit, summary


def print_audit_summary(sheet_name: str, audit_df: pd.DataFrame, summary: dict,
                        inactive_after_reports: int = 3) -> None:
    """Print compact totals and at most ten problem contracts; full detail is in the tab."""
    print(f"\n{sheet_name} audit: {summary['asset_count']} contracts, {summary['row_count']} rows")
    print(f"Source: {summary['source_earliest']} -> {summary['source_latest']}; "
          f"{summary['source_report_dates']} dates; age {summary['source_age_days']} days; {summary['source_status']}")
    print(f"Freshness: current {summary['current_count']} | Source delayed {summary['source_delayed_count']} | "
          f"Lagging {summary['lagging_count']} | No valid dates {summary['no_valid_dates_count']}")
    print(f"Reporting presence: latest source {summary['reported_latest_count']} | recent absence {summary['recently_not_reported_count']} | "
          f"possibly inactive/discontinued {summary['possibly_inactive_count']}")
    print(f"Data quality: fully clean {summary['clean_count']} | flagged {summary['assets_with_integrity_flags']} | "
          f"internal missing dates {summary['assets_with_missing_dates']} | "
          f"Long gaps {summary['assets_with_long_gaps']} | Duplicates {summary['assets_with_duplicates']} | "
          f"Invalid/future rows {summary['invalid_date_rows']}/{summary['future_date_rows']}")
    print(f"Source calendar gaps >10 days: {len(summary['source_calendar_gaps'])}")
    for gap in summary['source_calendar_gaps'][:5]:
        print('  ' + gap)
    problems = audit_df.loc[audit_df['Status'].ne('CURRENT')]
    if not problems.empty:
        print(problems[['Asset', 'Status', 'Latest_Report_Date', 'Reports_Behind_Source']].head(10).to_string(index=False))
        print(f"{len(problems)} contracts flagged; full details in {sheet_name}_Audit.")
    print('Coverage/gaps: between each contract\'s first and last observations in the annual file.')
    print(f'Later source reports are lag; {inactive_after_reports}+ missed reports may indicate an inactive/discontinued market.')


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return (f"{hours}h {minutes:02d}m {seconds:02d}s" if hours
            else f"{minutes}m {seconds:02d}s")


def _telegram_sender():
    """Reuse the main pipeline's sender, credentials and destination chat."""
    pipeline_path = str(PIPELINE_ROOT)
    if pipeline_path not in sys.path:
        sys.path.insert(0, pipeline_path)
    from notifications.telegram import send_telegram
    return send_telegram


def _acquire_run_lock(handle) -> None:
    pipeline_path = str(PIPELINE_ROOT)
    if pipeline_path not in sys.path:
        sys.path.insert(0, pipeline_path)
    from run_lock import acquire_lock
    acquire_lock(handle)


def _notification_footer(run_id: str) -> List[str]:
    device = "Mac" if platform.system() == "Darwin" else platform.system()
    return ["", f"{device} ({platform.node()}) | Run {run_id}"]


def notify_cot_started(run_id: str, started_at: str, year: int | None) -> bool:
    lines = [
        "Scope: CFTC Disaggregated and TFF futures-only reports",
        f"Year: {year if year is not None else 'Latest available'}",
        "Destination: COT, COT_Audit, COT2 and COT2_Audit",
        "Started: " + started_at,
        "You will receive one completion report when this run ends.",
        *_notification_footer(run_id),
    ]
    return _telegram_sender()("\n".join(lines), "COT import started")


def notify_cot_finished(run_id: str, started: float, results: list[dict]) -> bool:
    lines = [
        "Scope: CFTC Disaggregated and TFF futures-only reports",
        "Duration: " + _duration(time.monotonic() - started),
        "Finished: " + datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        f"Reports completed: {len(results)}/2",
        "",
        "Completed:",
    ]
    for result in results:
        summary = result["summary"]
        lines.extend([
            (f"{result['sheet']} ({result['year']}) — {result['rows']:,} raw rows; "
             f"{summary['asset_count']:,} contracts; source through {summary['source_latest']}"),
            (f"  Audit: {summary['assets_with_integrity_flags']} integrity flags; "
             f"{summary['lagging_count']} lagging; "
             f"{summary['possibly_inactive_count']} possibly inactive/discontinued"),
        ])
    lines.extend([
        "",
        "Google Sheets: COT, COT_Audit, COT2 and COT2_Audit updated",
        "Details: C:\\Fund_Server\\Logs\\us_COT.log",
        *_notification_footer(run_id),
    ])
    return _telegram_sender()("\n".join(lines), "COT import finished")


def notify_cot_failed(run_id: str, started: float, error: BaseException) -> bool:
    detail = f"{type(error).__name__}: {error or 'Interrupted'}"
    lines = [
        "Scope: CFTC Disaggregated and TFF futures-only reports",
        "Duration: " + _duration(time.monotonic() - started),
        "Finished: " + datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "Reports completed: 0/2 or partial",
        "",
        "Issue: " + detail[-700:],
        "Some writes may be incomplete. Check the local log before retrying.",
        "",
        "Details: C:\\Fund_Server\\Logs\\us_COT.log",
        *_notification_footer(run_id),
    ]
    return _telegram_sender()("\n".join(lines), "COT import failed")


def to_sheet_values(df: pd.DataFrame) -> List[List[str]]:
    """Convert DataFrame into Google Sheets row matrix (header + data rows)."""
    header = [str(col) if col is not None else "" for col in df.columns.tolist()]
    rows: List[List[str]] = [header]
    for row in df.itertuples(index=False, name=None):
        rows.append(["" if val is None else str(val) for val in row])
    return rows


def chunk_rows(rows: List[List[str]], chunk_size: int = 1000) -> Iterable[List[List[str]]]:
    """Yield row chunks to keep API payload sizes manageable."""
    for i in range(0, len(rows), chunk_size):
        yield rows[i : i + chunk_size]


def _build_google_sheets_service(service_account_path: str):
    """Authenticate once so one run can reuse the same Sheets API connection."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(service_account_path, scopes=SHEETS_SCOPE)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def write_to_google_sheet(
    spreadsheet_id: str,
    sheet_name: str,
    values: List[List[str]],
    service_account_path: str,
    service=None,
) -> None:
    """Clear destination tab and write all values from row 1/col A."""
    if service is None:
        service = _build_google_sheets_service(service_account_path)
    safe_sheet_name = sheet_name.replace("'", "''")
    required_rows = max(len(values), 1)
    required_cols = max((len(r) for r in values), default=1)
    required_cols = max(required_cols, 26)

    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = metadata.get("sheets", [])
    by_title = {s["properties"]["title"]: s["properties"] for s in sheets}

    if sheet_name not in by_title:
        add_resp = (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": sheet_name,
                                    "gridProperties": {
                                        "rowCount": max(required_rows, 1000),
                                        "columnCount": max(required_cols, 26),
                                    },
                                }
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    else:
        props = by_title[sheet_name]
        sheet_id = props["sheetId"]
        current_rows = props.get("gridProperties", {}).get("rowCount", 0)
        current_cols = props.get("gridProperties", {}).get("columnCount", 0)
        if current_rows < required_rows or current_cols < required_cols:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {
                                        "rowCount": max(current_rows, required_rows),
                                        "columnCount": max(current_cols, required_cols),
                                    },
                                },
                                "fields": "gridProperties(rowCount,columnCount)",
                            }
                        }
                    ]
                },
            ).execute()

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{safe_sheet_name}'",
        body={},
    ).execute()

    start_row = 1
    for chunk in chunk_rows(values, chunk_size=1000):
        end_row = start_row + len(chunk) - 1
        cell_range = f"'{safe_sheet_name}'!A{start_row}"
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": chunk},
        ).execute(num_retries=3)
        start_row = end_row + 1


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and audit annual CFTC Disaggregated and TFF futures-only reports."
        )
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Year to fetch for both sections (default: latest available year per section)",
    )
    parser.add_argument(
        "--spreadsheet-id",
        default=DEFAULT_SPREADSHEET_ID,
        help="Target Google Spreadsheet ID",
    )
    parser.add_argument("--disagg-sheet-name", default=DEFAULT_DISAGG_SHEET_NAME, help="Disaggregated destination tab (default: COT)")
    parser.add_argument("--tff-sheet-name", default=DEFAULT_TFF_SHEET_NAME, help="TFF destination tab (default: COT2)")
    parser.add_argument(
        "--service-account-path",
        default=DEFAULT_SERVICE_ACCOUNT_PATH,
        help="Path to Google service account JSON",
    )
    parser.add_argument("--dry-run", action="store_true", help="Download/audit without writing Google Sheets")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Do not send Telegram start/end notifications")
    parser.add_argument("--stale-after-days", type=int, default=10,
                        help="Flag source observation age above this many days (default: 10)")
    parser.add_argument("--inactive-after-reports", type=int, default=3,
                        help="Mark lagging markets as possibly inactive after this many missed reports (default: 3)")
    args = parser.parse_args(argv)
    if args.stale_after_days < 0:
        parser.error("--stale-after-days must be nonnegative")
    if args.inactive_after_reports < 1:
        parser.error("--inactive-after-reports must be at least 1")
    names = [args.disagg_sheet_name, args.tff_sheet_name,
             f"{args.disagg_sheet_name}_Audit", f"{args.tff_sheet_name}_Audit"]
    if len(set(n.casefold() for n in names)) != 4:
        parser.error("Raw and audit sheet names must all be distinct")
    return args


def _run(args: argparse.Namespace) -> list[dict]:

    service_account_path = Path(args.service_account_path)
    if not args.dry_run and not service_account_path.is_file():
        raise FileNotFoundError(
            f"Service account JSON not found at: {service_account_path}. "
            "Pass --service-account-path with the correct file path."
        )

    jobs = [
        (DISAGG_SECTION_LABEL, args.disagg_sheet_name),
        (TFF_SECTION_LABEL, args.tff_sheet_name),
    ]

    prepared = []
    results = []
    for section_label, target_sheet in jobs:
        report_link = fetch_section_report_link(section_label, args.year)
        print(f"[{section_label}] Found {report_link.year} report link: {report_link.url}")
        df = download_report_to_dataframe(report_link.url)
        dates = _report_dates(df)
        valid = dates.dropna()
        if valid.empty or not valid.dt.year.eq(report_link.year).all():
            raise RuntimeError(f"Report dates do not match selected year {report_link.year}.")
        normalized = {_normalize_column_name(c) for c in df.columns}
        marker = "prodmercpositionslongall" if "disaggregated" in section_label.lower() else "dealerpositionslongall"
        if marker not in normalized:
            raise RuntimeError(f"Unexpected dataset schema for {section_label}; refusing to upload.")
        audit_df, summary = build_data_quality_audit(
            df,
            stale_after_days=args.stale_after_days,
            inactive_after_reports=args.inactive_after_reports,
        )
        audit_df["Source_Year"] = report_link.year
        audit_df["Source_URL"] = report_link.url
        print_audit_summary(target_sheet, audit_df, summary, args.inactive_after_reports)
        prepared.append((target_sheet, df, audit_df))
        results.append({
            "sheet": target_sheet,
            "year": report_link.year,
            "rows": len(df),
            "summary": summary,
        })

    if args.dry_run:
        print("Dry run complete: no Google Sheet changes made.")
        return results
    service = _build_google_sheets_service(str(service_account_path))
    for target_sheet, df, audit_df in prepared:
        for sheet_name, frame in ((target_sheet, df), (f"{target_sheet}_Audit", audit_df)):
            write_to_google_sheet(
                spreadsheet_id=args.spreadsheet_id, sheet_name=sheet_name,
                values=to_sheet_values(frame), service_account_path=str(service_account_path),
                service=service,
            )
            print(f"Uploaded {len(frame)} rows (+header) to '{sheet_name}'.")
    return results


def main(argv=None) -> None:
    args = parse_args(argv)
    notify = not args.dry_run and not args.no_telegram
    COT_WORK.mkdir(parents=True, exist_ok=True)
    with (COT_WORK / "cot_import.lock").open("a+") as lock:
        try:
            _acquire_run_lock(lock)
        except OSError:
            print("COT import skipped: another run is already active.")
            if notify:
                _telegram_sender()(
                    "Another COT import is active. This request was skipped.",
                    "COT import already running",
                )
            return
        run_id = uuid4().hex[:12]
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        if notify:
            notify_cot_started(run_id, started_at, args.year)
        try:
            results = _run(args)
        except (Exception, KeyboardInterrupt) as exc:
            if notify:
                notify_cot_failed(run_id, started, exc)
            raise
        if notify:
            notify_cot_finished(run_id, started, results)


if __name__ == "__main__":
    main()
