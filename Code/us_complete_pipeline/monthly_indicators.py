#!/usr/bin/env python3
"""
Export monthly economic indicators to Google Sheets.

Example:
    python3 monthly_indicators.py --api-key YOUR_FRED_KEY

You must pass the FRED API key in the run command with --api-key.
ISM-only exports do not require a FRED API key.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
from time import sleep
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse
from urllib.request import urlopen

BOOTSTRAP_PACKAGES = {
    "pandas": "pandas>=2.0",
    "requests": "requests>=2.31",
    "bs4": "beautifulsoup4>=4.12",
    "urllib3": "urllib3>=2.0",
    "xlrd": "xlrd>=2.0.1",
    "openpyxl": "openpyxl>=3.1",
    "googleapiclient": "google-api-python-client>=2.140",
}


def ensure_dependencies() -> None:
    """Install missing runtime packages so the script can be run with one command."""
    missing = [
        package
        for module, package in BOOTSTRAP_PACKAGES.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return

    print("Installing missing Python packages: " + ", ".join(missing))
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


ensure_dependencies()

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

FRED_API_BASE = "https://api.stlouisfed.org/fred"
IMF_DATAMAPPER_API_BASE = "https://www.imf.org/external/datamapper/api/v2"
DEFAULT_IMF_COUNTRY = "USA"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
GOOGLE_REQUEST_CONNECT_TIMEOUT_SECONDS = 15
GOOGLE_REQUEST_READ_TIMEOUT_SECONDS = 90
GOOGLE_REQUEST_MAX_ATTEMPTS = 5
DEFAULT_GOOGLE_SPREADSHEET_ID = "19E_Za0DOHMY_9AFSPatK8Cp2vCQypI81QnB3Hz4ve9c"
DEFAULT_WIDE_SPREADSHEET_ID = "1qIyTo-8esI23kaobPgnQT65Q6ykbjCXtPyNXC9Xo3KM"
COMPACT_TERMINAL = os.getenv("US_PIPELINE_COMPACT_OUTPUT") == "1"
ANALYSIS_DECIMAL_PLACES = 5
ANALYSIS_VALUE_QUANTUM = Decimal("0.00001")


def find_default_google_credentials() -> str:
    configured = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if configured:
        return configured
    script_path = Path(__file__).resolve()
    for parent in (script_path.parent, *script_path.parents):
        candidate = parent / "google_credentials.json"
        if candidate.is_file():
            return str(candidate)
    return str(script_path.parent / "google_credentials.json")


DEFAULT_GOOGLE_CREDENTIALS = find_default_google_credentials()
DEFAULT_GOOGLE_SHEET_NAME = "Monthly Indicators"
DEFAULT_WIDE_SHEET_NAME = "All data"
DEFAULT_GOOGLE_STATUS_SHEET_NAME = "Source Status"
DEFAULT_GOOGLE_METADATA_SHEET_NAME = "Indicator Metadata"
DEFAULT_GOOGLE_VALIDATION_SHEET_NAME = "Validation Report"
DEFAULT_GOOGLE_DIAGNOSTIC_SHEET_NAME = "Selection Diagnostics"
LEGACY_SHEET_NAMES = {"FRED Indicators", "ISM Indicators"}
CENTRAL_BANK_LIQUIDITY_SHEET = "Central bank liquidity growth"
(Path(__file__).parent / "work").mkdir(exist_ok=True)
ERROR_LOG_PATH = Path(__file__).parent / "work" / "monthly_indicators_errors.log"
SOURCE_ARCHIVE_ROOT = Path(__file__).parent / "work" / "source_archive"
OUTPUT_ROOT = Path(__file__).parent / "work" / "importer_outputs"
GOOGLE_SHEET_AUDIT_CSV = OUTPUT_ROOT / "google_sheet_audit.csv"
ISM_REPORT_INDEX_URL = (
    "https://www.ismworld.org/supply-management-news-and-reports/"
    "reports/ism-pmi-reports/"
)
NYFED_SCE_DATA_URL = (
    "https://www.newyorkfed.org/medialibrary/interactives/sce/sce/"
    "downloads/data/frbny-sce-data.xlsx?sc_lang=en"
)
NYFED_SCE_PAGE_URL = "https://www.newyorkfed.org/microeconomics/sce"
MICHIGAN_CONSUMER_EXPECTATIONS_URL = "https://www.sca.isr.umich.edu/files/chicer.xls"
MICHIGAN_CONSUMER_SENTIMENT_URL = "https://www.sca.isr.umich.edu/files/chicsr.xls"
MICHIGAN_CHARTS_PAGE_URL = "https://www.sca.isr.umich.edu/charts.html"
OFFICIAL_SOURCE_HOSTS = {
    "www.ismworld.org",
    "ismworld.org",
    "www.newyorkfed.org",
    "newyorkfed.org",
    "www.sca.isr.umich.edu",
    "sca.isr.umich.edu",
    "www.imf.org",
    "imf.org",
}
MIN_HTML_BYTES = 1_000
MIN_WORKBOOK_BYTES = 5_000


class OfficialSourceRedirectError(RuntimeError):
    """Raised when an official source redirects outside its approved data hosts."""

    def __init__(self, source_name: str, url: str) -> None:
        super().__init__(f"{source_name} returned a non-official URL: {url}")
        self.url = url


@dataclass
class SourceStatus:
    source: str
    status: str
    latest_data_month: str
    retrieved_at: str
    source_url: str
    sha256: str
    byte_size: int
    content_type: str
    schema_fingerprint: str
    archive_file: str
    revision: str = ""


MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
MONTH_NUMBERS = {name: idx + 1 for idx, name in enumerate(MONTHS)}
MONTH_ALIASES = {
    "jan": "january",
    "feb": "february",
    "mar": "march",
    "apr": "april",
    "jun": "june",
    "jul": "july",
    "aug": "august",
    "sep": "september",
    "sept": "september",
    "oct": "october",
    "nov": "november",
    "dec": "december",
}


@dataclass(frozen=True)
class Indicator:
    name: str
    series_id: str | None = None
    units: str = "lin"
    numerator_series_id: str | None = None
    denominator_series_id: str | None = None
    calculation: str | None = None
    source: str = "fred"
    source_column: str | None = None
    release_lag_months: int = 0
    monthly_aggregation: str = "latest"
    monthly_method: str = (
        "Most recent source observation assigned to the row month; otherwise blank"
    )
    unit_label: str = ""
    release_rule: str = ""


@dataclass
class IsmReportData:
    report_date: date
    source_url: str
    values: dict[str, float | None]
    publication_date: date | None = None


SOURCE_STATUSES: dict[str, SourceStatus] = {}
VALIDATION_MESSAGES: list[dict[str, str]] = []
LAST_SELECTIONS: dict[tuple[str, date], dict | None] = {}
DEFAULT_OUTPUT_MONTHS = 12
CURRENT_OUTPUT_MONTHS = DEFAULT_OUTPUT_MONTHS
FULL_HISTORY_MODE = False


def set_source_status(status: SourceStatus) -> None:
    """Keep the newest period when one source archives several historical files."""
    existing = SOURCE_STATUSES.get(status.source)
    if existing is None or status.latest_data_month >= existing.latest_data_month:
        SOURCE_STATUSES[status.source] = status


def is_central_bank_component(indicator_name: str) -> bool:
    """Return true for source series that live in the shared liquidity tab."""
    return indicator_name in CENTRAL_BANK_LIQUIDITY_COMPONENTS


def is_imf_support_indicator(indicator_name: str) -> bool:
    """Return true for IMF inputs that feed the published coverage-ratio tab."""
    return indicator_name in IMF_SUPPORT_INDICATORS


def is_imf_coverage_indicator(indicator_name: str) -> bool:
    """Return true for source/derived indicators belonging to the shared IMF tab."""
    return (
        indicator_name in IMF_SUPPORT_INDICATORS or indicator_name == IMF_COVERAGE_SHEET
    )


def has_dedicated_indicator_tab(indicator_name: str) -> bool:
    """Return true when an indicator should have its own Google Sheet tab."""
    return not is_central_bank_component(
        indicator_name
    ) and not is_imf_coverage_indicator(indicator_name)


INDICATORS = [
    Indicator(
        "Manufacturing PMI",
        source="ism",
        source_column="manufacturing_pmi",
        monthly_method="Most recent official report observation assigned to the row month; otherwise blank",
    ),
    Indicator(
        "Manufacturing new orders",
        source="ism",
        source_column="manufacturing_new_orders",
        monthly_method="Most recent official report observation assigned to the row month; otherwise blank",
    ),
    Indicator(
        "Services PMI",
        source="ism",
        source_column="services_pmi",
        monthly_method="Most recent official report observation assigned to the row month; otherwise blank",
    ),
    Indicator(
        "Services new orders",
        source="ism",
        source_column="services_new_orders",
        monthly_method="Most recent official report observation assigned to the row month; otherwise blank",
    ),
    Indicator(
        "Services business activity",
        source="ism",
        source_column="services_business_activity",
        monthly_method="Most recent official report observation assigned to the row month; otherwise blank",
    ),
    Indicator(
        "Industrial production growth",
        "INDPRO",
        units="lin",
        unit_label="Index 2017=100",
    ),
    Indicator(
        "Real consumer spending growth",
        "PCEC96",
        units="lin",
        unit_label="Billions of chained 2017 dollars",
    ),
    Indicator(
        "Consumer sentiment",
        source="michigan_sentiment",
        source_column="ICS_M",
        unit_label="Index",
        release_rule="Michigan final monthly release; preliminary only if final is unavailable",
    ),
    Indicator(
        "Consumer expectations",
        source="michigan_sca",
        source_column="ICE_M",
        unit_label="Index",
        release_rule="Michigan final monthly release; preliminary only if final is unavailable",
    ),
    Indicator(
        "Initial unemployment claims",
        "ICSA",
        monthly_aggregation="latest",
        monthly_method="Latest weekly observation in the row month",
        unit_label="Number",
    ),
    Indicator(
        "Employment growth",
        "PAYEMS",
        units="lin",
        unit_label="Thousands of persons",
    ),
    Indicator("Average hours worked", "AWHAETP"),
    Indicator("Building permits", "PERMIT"),
    Indicator(
        "Core consumption inflation",
        "PCEPILFE",
        units="lin",
        unit_label="Index 2017=100",
    ),
    Indicator(
        "Core CPI inflation",
        "CPILFESL",
        units="lin",
        unit_label="Index 1982-1984=100",
    ),
    Indicator(
        "Headline CPI inflation",
        "CPIAUCSL",
        units="lin",
        unit_label="Index 1982-1984=100",
    ),
    Indicator(
        "Core producer price inflation",
        "PPIFES",
        units="lin",
        unit_label="Index Apr 2010=100",
    ),
    Indicator(
        "Headline producer price inflation",
        "PPIFIS",
        units="lin",
        unit_label="Index Nov 2009=100",
    ),
    Indicator(
        "Wage growth",
        "ECIWAG",
        units="lin",
        unit_label="Index Dec 2005=100",
    ),
    Indicator(
        "Consumer inflation expectations",
        source="nyfed_sce",
        source_column="Median one-year ahead expected inflation rate",
        release_lag_months=1,
        unit_label="Percent",
        release_rule="NY Fed SCE publication date; conservative 20th-of-next-month fallback",
        monthly_method="Most recent NY Fed SCE survey observation assigned to the row month; otherwise blank",
    ),
    Indicator("10Y real yield", "DFII10"),
    Indicator("2Y government bond yield", "DGS2"),
    Indicator("10Y government bond yield", "DGS10"),
    Indicator("10Y-3M yield curve spread", "T10Y3M"),
    Indicator("10Y term premium", "THREEFYTP10"),
    Indicator(
        "Overnight interbank rate",
        "IRSTCI01USM156N",
        unit_label="Percent",
    ),
    Indicator(
        "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level",
        "WALCL",
        units="lin",
        unit_label="Millions of U.S. dollars",
    ),
    Indicator(
        "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level",
        "WDTGAL",
        units="lin",
        unit_label="Millions of U.S. dollars",
    ),
    Indicator(
        "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations",
        "RRPONTSYD",
        units="lin",
        unit_label="Billions of U.S. dollars",
    ),
    Indicator(
        "Banking system reserves",
        "WRESBAL",
        unit_label="Millions of U.S. dollars",
    ),
    Indicator(
        "Broad money growth",
        "M2SL",
        units="lin",
        unit_label="Billions of dollars",
    ),
    Indicator("Fiscal balance % GDP", "FYFSGDA188S"),
    Indicator(
        "Real government spending growth",
        "GCEC1",
        units="lin",
        unit_label="Billions of chained 2017 dollars, SAAR",
    ),
    Indicator(
        "Government revenue growth",
        "MTSR133FMS",
        units="lin",
        unit_label="Millions of dollars, not seasonally adjusted",
    ),
    Indicator("Public debt % GDP", "GFDEGDQ188S"),
    Indicator(
        "Government revenue % GDP",
        "rev",
        source="imf_datamapper",
        source_column=DEFAULT_IMF_COUNTRY,
        unit_label="Percent of GDP",
        monthly_method="Annual IMF FPP observation matched to its December year-end month",
        release_rule="Latest available IMF DataMapper FPP release metadata",
    ),
    Indicator(
        "Interest paid on public debt % GDP",
        "ie",
        source="imf_datamapper",
        source_column=DEFAULT_IMF_COUNTRY,
        unit_label="Percent of GDP",
        monthly_method="Annual IMF FPP observation matched to its December year-end month",
        release_rule="Latest available IMF DataMapper FPP release metadata",
    ),
    Indicator(
        "Government interest coverage ratio",
        "rev_over_ie",
        source="imf_datamapper",
        source_column=DEFAULT_IMF_COUNTRY,
        calculation="rev / ie",
        unit_label="Ratio (x)",
        monthly_method="Annual IMF FPP revenue divided by interest paid, matched to December year-end month",
        release_rule="Latest available IMF DataMapper FPP release metadata",
    ),
    Indicator("Financial conditions index", "NFCI"),
    Indicator(
        "Bank credit growth",
        "TOTLL",
        units="lin",
        unit_label="Billions of U.S. dollars",
    ),
    Indicator("Corporate credit spread", "BAA10Y"),
]
EXPECTED_HEADERS = [
    "Manufacturing PMI",
    "Manufacturing new orders",
    "Services PMI",
    "Services new orders",
    "Services business activity",
    "Industrial production growth",
    "Real consumer spending growth",
    "Consumer sentiment",
    "Consumer expectations",
    "Initial unemployment claims",
    "Employment growth",
    "Average hours worked",
    "Building permits",
    "Core consumption inflation",
    "Core CPI inflation",
    "Headline CPI inflation",
    "Core producer price inflation",
    "Headline producer price inflation",
    "Wage growth",
    "Consumer inflation expectations",
    "10Y real yield",
    "2Y government bond yield",
    "10Y government bond yield",
    "10Y-3M yield curve spread",
    "10Y term premium",
    "Overnight interbank rate",
    "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level",
    "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level",
    "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations",
    "Banking system reserves",
    "Broad money growth",
    "Fiscal balance % GDP",
    "Real government spending growth",
    "Government revenue growth",
    "Public debt % GDP",
    "Government revenue % GDP",
    "Interest paid on public debt % GDP",
    "Government interest coverage ratio",
    "Financial conditions index",
    "Bank credit growth",
    "Corporate credit spread",
]

CENTRAL_BANK_LIQUIDITY_COMPONENTS = {
    "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level": {
        "column": 1,
        "header": "Central Bank Total Assets",
    },
    "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level": {
        "column": 2,
        "header": "Cental Bank General Account",
    },
    "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations": {
        "column": 3,
        "header": "Central Bank RRP",
    },
}
IMF_SUPPORT_INDICATORS = {
    "Government revenue % GDP",
    "Interest paid on public debt % GDP",
}
IMF_COVERAGE_SHEET = "Government interest coverage ratio"
IMF_COVERAGE_INPUT_COLUMNS = {
    "Government revenue % GDP": 1,
    "Interest paid on public debt % GDP": 2,
}
ANALYSIS_CONTROL_PANEL_SHEET = "Control Panel"
EXPECTED_ANALYSIS_SHEET_ORDER = tuple(
    dict.fromkeys(
        (
            CENTRAL_BANK_LIQUIDITY_SHEET
            if name in CENTRAL_BANK_LIQUIDITY_COMPONENTS
            else (
                IMF_COVERAGE_SHEET
                if name in IMF_SUPPORT_INDICATORS or name == IMF_COVERAGE_SHEET
                else name
            )
        )
        for name in EXPECTED_HEADERS
    )
)
ANALYSIS_CONTROL_PANEL_RANGE = (
    f"'{ANALYSIS_CONTROL_PANEL_SHEET}'!" f"A3:A{len(EXPECTED_ANALYSIS_SHEET_ORDER) + 2}"
)
QUARTER_END_MONTHS = {3, 6, 9, 12}
ANNUAL_OUTPUT_MONTH = 12
SPARSE_FRED_FREQUENCY_PREFIXES = ("Quarterly", "Annual")
GOOGLE_SHEETS_WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_SHEETS_READONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly"
]

_ISM_CACHE: dict[int, pd.DataFrame] = {}
_EXPLICIT_BLANK_OUTPUT_MONTHS: dict[str, set[date]] = {}

# Preserve existing values when a current source no longer exposes older
# history. Entries belong here only after the individual workbook cells have
# been independently verified as erroneous, never merely because a response
# omits their dates.
_VERIFIED_INVALID_EXISTING_MONTHS: dict[str, set[date]] = {
    "Core CPI inflation": {date(2025, 10, 1)},
    "Headline CPI inflation": {date(2025, 10, 1)},
    "Consumer inflation expectations": {date(2013, 6, 1)},
}
_NYFED_SCE_CACHE: pd.DataFrame | None = None
_MICHIGAN_CONSUMER_EXPECTATIONS_CACHE: pd.DataFrame | None = None
_MICHIGAN_CONSUMER_SENTIMENT_CACHE: pd.DataFrame | None = None
_FRED_METADATA_CACHE: dict[str, dict] = {}
_IMF_FPP_CACHE: dict[str, dict] = {}
LAST_FRED_REQUEST_AT = 0.0
FRED_MIN_REQUEST_INTERVAL_SECONDS = 0.65
FRED_EXPECTED_METADATA = {
    "CPIAUCSL": {
        "title_contains": "Consumer Price Index for All Urban Consumers: All Items",
        "units_contains": "Index 1982-1984=100",
        "frequency": "Monthly",
    },
    "DFII10": {
        "title_contains": "10-Year Constant Maturity",
        "units_contains": "Percent",
        "frequency": "Daily",
    },
    "DGS2": {
        "title_contains": "2-Year Constant Maturity",
        "units_contains": "Percent",
        "frequency": "Daily",
    },
    "DGS10": {
        "title_contains": "10-Year Constant Maturity",
        "units_contains": "Percent",
        "frequency": "Daily",
    },
    "T10Y3M": {
        "title_contains": "10-Year Treasury Constant Maturity Minus 3-Month",
        "units_contains": "Percent",
        "frequency": "Daily",
    },
    "THREEFYTP10": {
        "title_contains": "Term Premium on a 10 Year Zero Coupon Bond",
        "units_contains": "Percent",
        "frequency": "Daily",
    },
    "IRSTCI01USM156N": {
        "title_contains": "Call Money/Interbank Rate: Total for United States",
        "units_contains": "Percent",
        "frequency": "Monthly",
    },
    "INDPRO": {
        "title_contains": "Industrial Production",
        "units_contains": "Index 2017=100",
        "frequency": "Monthly",
    },
    "PCEC96": {
        "title_contains": "Real Personal Consumption Expenditures",
        "units_contains": "Chained 2017 Dollars",
        "frequency": "Monthly",
    },
    "ECIWAG": {
        "title_contains": "Employment Cost Index: Wages and Salaries",
        "units_contains": "Index Dec 2005=100",
        "frequency": "Quarterly",
    },
    "MTSR133FMS": {
        "title_contains": "Total Federal Receipts",
        "units_contains": "Millions of Dollars",
        "frequency": "Monthly",
    },
    "WALCL": {
        "title_contains": "Assets",
        "units_contains": "Millions of U.S. Dollars",
        "frequency": "Weekly, As of Wednesday",
    },
    "WDTGAL": {
        "title_contains": "U.S. Treasury, General Account",
        "units_contains": "Millions of U.S. Dollars",
        "frequency": "Weekly, As of Wednesday",
    },
    "RRPONTSYD": {
        "title_contains": "Overnight Reverse Repurchase Agreements",
        "units_contains": "Billions of U.S. Dollars",
        "frequency": "Daily",
    },
    "WRESBAL": {
        "title_contains": "Reserve Balances with Federal Reserve Banks",
        "units_contains": "Millions of U.S. Dollars",
        "frequency": "Weekly, Ending Wednesday",
    },
    "NFCI": {
        "title_contains": "Chicago Fed National Financial Conditions Index",
        "units_contains": "Index",
        "frequency": "Weekly, Ending Friday",
    },
    "TOTLL": {
        "title_contains": "Loans and Leases in Bank Credit",
        "units_contains": "Billions of U.S. Dollars",
        "frequency": "Weekly, Ending Wednesday",
    },
    "BAA10Y": {
        "title_contains": "Baa Corporate Bond Yield Relative",
        "units_contains": "Percent",
        "frequency": "Daily",
    },
}
ISM_VALUE_COLUMNS = [
    "manufacturing_pmi",
    "manufacturing_new_orders",
    "services_pmi",
    "services_new_orders",
    "services_business_activity",
]
ISM_VALUE_LABELS = {
    "manufacturing_pmi": "Manufacturing PMI",
    "manufacturing_new_orders": "Manufacturing new orders",
    "services_pmi": "Services PMI",
    "services_new_orders": "Services new orders",
    "services_business_activity": "Services business activity",
}


def subtract_months(input_date: date, months: int) -> date:
    month_index = input_date.month - 1 - months
    year = input_date.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    day = min(input_date.day, days_in_month[month - 1])
    return date(year, month, day)


def month_start(input_date: date) -> date:
    return date(input_date.year, input_date.month, 1)


def add_months(input_date: date, months: int) -> date:
    month_index = input_date.month - 1 + months
    year = input_date.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def month_end(input_date: date) -> date:
    return add_months(input_date, 1) - date.resolution


def nth_weekday_of_month(input_month: date, occurrence: int) -> date:
    """Return the nth Monday-Friday date in a month."""
    current = month_start(input_month)
    found = 0
    while True:
        if current.weekday() < 5:
            found += 1
            if found == occurrence:
                return current
        current += date.resolution


def last_weekday_of_month(input_month: date, weekday: int) -> date:
    current = month_end(input_month)
    while current.weekday() != weekday:
        current -= date.resolution
    return current


def recent_month_starts(months: int) -> list[date]:
    current_month = month_start(date.today())
    first_month = add_months(current_month, -(months - 1))
    return [add_months(first_month, offset) for offset in range(months)]


def ism_release_month(report_month: date) -> date:
    """Place ISM data in the month when ISM publishes it."""
    return add_months(report_month, 1)


def make_requests_session() -> requests.Session:
    """Create a browser-like HTTP session with retries for official source pages."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


ISM_PRESS_RELEASES = {
    (
        "pmi",
        "2026-06-01",
    ): "https://www.prnewswire.com/news-releases/manufacturing-pmi-at-53-3-june-2026-ism-manufacturing-pmi-report-302814991.html",
    (
        "services",
        "2026-06-01",
    ): "https://www.prnewswire.com/news-releases/services-pmi-at-54-june-2026-ism-services-pmi-report-302817275.html",
}


def validate_official_url(url: str, source_name: str) -> None:
    """Reject redirects or discovered links outside the official source domains."""
    parsed = urlparse(url)
    if url in ISM_PRESS_RELEASES.values():
        return  # Exact ISM-issued releases, not general third-party news pages.
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_SOURCE_HOSTS:
        raise OfficialSourceRedirectError(source_name, url)


def is_ism_login_redirect(exc: BaseException) -> bool:
    """Recognize ISM's current SSO redirect without trusting it as report data."""
    if not isinstance(exc, OfficialSourceRedirectError):
        return False
    parsed = urlparse(exc.url)
    return parsed.hostname == "ecommerce.ismworld.org" and parsed.path.lower().endswith(
        "/sso/login.aspx"
    )


def fetch_url(
    session: requests.Session,
    url: str,
    pause: float = 0.3,
    minimum_bytes: int = 1,
    request_headers: dict[str, str] | None = None,
) -> requests.Response:
    """Fetch one official URL and raise a clear error for failed responses."""
    validate_official_url(url, "Official source")
    response = session.get(url, timeout=30, headers=request_headers)
    response.raise_for_status()
    validate_official_url(response.url, "Official source redirect")
    if len(response.content) < minimum_bytes:
        raise RuntimeError(
            f"Official source response was unexpectedly small "
            f"({len(response.content)} bytes): {response.url}"
        )
    if pause:
        sleep(pause)
    return response


def fresh_ism_url(url: str) -> str:
    """Bypass stale ISM CDN pages while preserving the official URL path."""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["_ism_refresh"] = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return parsed._replace(query=urlencode(query)).geturl()


ISM_FRESH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; EconomicDataBot/1.0; " "+https://www.ismworld.org/)"
    ),
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}


def discover_download_url(
    session: requests.Session,
    page_url: str,
    href_pattern: str,
    link_text_pattern: str | None = None,
) -> str | None:
    """Find a download URL from an official source page."""
    response = fetch_url(session, page_url, pause=0, minimum_bytes=MIN_HTML_BYTES)
    return discover_download_url_from_html(
        response.text,
        page_url,
        href_pattern,
        link_text_pattern,
    )


def discover_download_url_from_html(
    html: str,
    page_url: str,
    href_pattern: str,
    link_text_pattern: str | None = None,
) -> str | None:
    """Find and validate a download URL in already-fetched official HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = " ".join(link.get_text(" ", strip=True).split())
        href_matches = re.search(href_pattern, href, flags=re.IGNORECASE)
        text_matches = link_text_pattern is None or re.search(
            link_text_pattern, text, flags=re.IGNORECASE
        )
        if href_matches and text_matches:
            discovered = urljoin(page_url, href)
            validate_official_url(discovered, "Discovered download")
            return discovered
    return None


def discover_current_ism_urls(
    session: requests.Session,
) -> dict[str, str]:
    """Discover current report links so minor ISM URL changes do not break the scraper."""
    response = fetch_url(
        session,
        fresh_ism_url(ISM_REPORT_INDEX_URL),
        pause=0,
        minimum_bytes=MIN_HTML_BYTES,
        request_headers=ISM_FRESH_HEADERS,
    )
    soup = BeautifulSoup(response.text, "html.parser")
    discovered: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        href = urljoin(ISM_REPORT_INDEX_URL, link["href"])
        text = " ".join(link.get_text(" ", strip=True).split()).lower()
        if "past month" in text:
            continue
        heading = link.find_previous(["h2", "h3", "h4"])
        heading_text = heading.get_text(" ", strip=True).lower() if heading else ""
        if "manufacturing" in heading_text or re.search(
            r"/ism-pmi-reports/pmi/", href, flags=re.IGNORECASE
        ):
            discovered.setdefault("pmi", href)
        elif "services" in heading_text or re.search(
            r"/ism-pmi-reports/services/", href, flags=re.IGNORECASE
        ):
            discovered.setdefault("services", href)
    return discovered


def fetch_first_available(
    session: requests.Session,
    urls: Iterable[str],
    description: str,
) -> requests.Response:
    """Try multiple official URLs before reporting a source failure."""
    errors = []
    for url in urls:
        try:
            return fetch_url(
                session,
                url,
                pause=0,
                minimum_bytes=MIN_WORKBOOK_BYTES,
            )
        except OfficialSourceRedirectError:
            raise
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(f"Could not download {description}. Tried: {' | '.join(errors)}")


def safe_filename(value: str) -> str:
    """Convert a source or period label into a stable filesystem name."""
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write an archive file atomically so interrupted runs cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_bytes(
        path,
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
    )


def normalize_archive_values(values: dict[str, object]) -> dict[str, object]:
    """Convert parsed values to deterministic JSON-compatible metadata."""
    normalized = {}
    for key, value in sorted(values.items()):
        if isinstance(value, (date, datetime)):
            normalized[key] = value.isoformat()
        elif pd.isna(value):
            normalized[key] = None
        elif isinstance(value, (int, float, str, bool)) or value is None:
            normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


def archive_validated_source(
    source: str,
    period: str,
    response: requests.Response,
    extension: str,
    schema: Iterable[str],
    parsed_values: dict[str, object],
) -> SourceStatus:
    """Archive one validated official response and record immutable metadata."""
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = response.content
    digest = hashlib.sha256(content).hexdigest()
    schema_items = sorted(str(item) for item in schema)
    schema_fingerprint = hashlib.sha256(
        "\n".join(schema_items).encode("utf-8")
    ).hexdigest()
    source_dir = SOURCE_ARCHIVE_ROOT / safe_filename(source)
    period_name = safe_filename(period)
    raw_path = source_dir / f"{period_name}-{digest[:16]}.{extension.lstrip('.')}"
    metadata_path = raw_path.with_name(
        f"{raw_path.name}.{schema_fingerprint[:12]}.json"
    )
    period_pointer = source_dir / f"{period_name}-latest.json"
    source_pointer = source_dir / "latest.json"
    normalized_values = normalize_archive_values(parsed_values)

    revision_fields: set[str] = set()
    if period_pointer.exists():
        try:
            previous = json.loads(period_pointer.read_text(encoding="utf-8"))
            previous_values = previous.get("parsed_values", {})
            if previous_values != normalized_values:
                revision_fields.update(
                    key
                    for key in set(previous_values) | set(normalized_values)
                    if previous_values.get(key) != normalized_values.get(key)
                )
        except (OSError, ValueError, TypeError):
            revision_fields.add("unreadable previous period metadata")

    previous_latest = None
    if source_pointer.exists():
        try:
            previous_latest = json.loads(source_pointer.read_text(encoding="utf-8"))
            previous_values = previous_latest.get("parsed_values", {})
            common_keys = {
                key
                for key in set(previous_values) & set(normalized_values)
                if re.match(r"^\d{4}-\d{2}", key)
            }
            revision_fields.update(
                key
                for key in common_keys
                if previous_values.get(key) != normalized_values.get(key)
            )
        except (OSError, ValueError, TypeError):
            revision_fields.add("unreadable previous source metadata")

    revision = (
        "Changed fields: " + ", ".join(sorted(revision_fields))
        if revision_fields
        else ""
    )
    if revision:
        logging.warning(
            "%s revised previously archived data for %s: %s",
            source,
            period,
            ", ".join(sorted(revision_fields)),
        )

    metadata = {
        "source": source,
        "period": period,
        "retrieved_at": retrieved_at,
        "requested_url": response.request.url if response.request else response.url,
        "final_url": response.url,
        "content_type": response.headers.get("Content-Type", ""),
        "byte_size": len(content),
        "sha256": digest,
        "schema": schema_items,
        "schema_fingerprint": schema_fingerprint,
        "parsed_values": normalized_values,
        "archive_file": str(raw_path),
        "revision": revision,
    }
    if not raw_path.exists():
        atomic_write_bytes(raw_path, content)
    if not metadata_path.exists():
        atomic_write_json(metadata_path, metadata)
    atomic_write_json(period_pointer, metadata)
    previous_latest_period = (
        str(previous_latest.get("period", ""))
        if isinstance(previous_latest, dict)
        else ""
    )
    if not previous_latest_period or period >= previous_latest_period:
        atomic_write_json(source_pointer, metadata)

    status = SourceStatus(
        source=source,
        status="OK",
        latest_data_month=period,
        retrieved_at=retrieved_at,
        source_url=response.url,
        sha256=digest,
        byte_size=len(content),
        content_type=response.headers.get("Content-Type", ""),
        schema_fingerprint=schema_fingerprint,
        archive_file=str(raw_path),
        revision=revision,
    )
    set_source_status(status)
    return status


def load_archived_source(source: str, period: str) -> tuple[bytes, dict] | None:
    """Load the latest previously validated official response for one period."""
    pointer = (
        SOURCE_ARCHIVE_ROOT
        / safe_filename(source)
        / f"{safe_filename(period)}-latest.json"
    )
    if not pointer.exists():
        return None
    try:
        metadata = json.loads(pointer.read_text(encoding="utf-8"))
        raw_path = Path(metadata["archive_file"])
        if not raw_path.is_file():
            # Archive metadata can be copied with the project. Resolve the
            # immutable payload beside its pointer when the original absolute
            # workstation path no longer exists.
            raw_path = pointer.parent / raw_path.name
        content = raw_path.read_bytes()
    except (OSError, KeyError, ValueError, TypeError):
        return None
    if hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
        raise RuntimeError(
            f"Archived {source} data for {period} failed checksum validation."
        )
    return content, metadata


def load_latest_archived_source(source: str) -> tuple[bytes, dict] | None:
    """Load the latest checksum-verified archive for one official source."""
    pointer = SOURCE_ARCHIVE_ROOT / safe_filename(source) / "latest.json"
    if not pointer.exists():
        return None
    try:
        metadata = json.loads(pointer.read_text(encoding="utf-8"))
        period = metadata["period"]
    except (OSError, KeyError, ValueError, TypeError):
        return None
    return load_archived_source(source, period)


def response_from_archive(content: bytes, metadata: dict) -> requests.Response:
    """Recreate a minimal response object from a checksum-verified archive."""
    response = requests.Response()
    response.status_code = 200
    response._content = content
    response.url = metadata.get("final_url", "")
    response.headers["Content-Type"] = metadata.get("content_type", "")
    prepared = requests.Request(
        "GET",
        metadata.get("requested_url") or response.url,
    ).prepare()
    response.request = prepared
    return response


def source_status_values() -> list[list[object]]:
    """Build the Google Sheets source-status table."""
    headers = [
        "Source",
        "Status",
        "Latest data month",
        "Retrieved at UTC",
        "Source URL",
        "SHA-256",
        "Bytes",
        "Schema fingerprint",
        "Archive file",
        "Revision",
    ]
    rows = [
        [
            status.source,
            status.status,
            status.latest_data_month,
            status.retrieved_at,
            status.source_url,
            status.sha256,
            status.byte_size,
            status.schema_fingerprint,
            status.archive_file,
            status.revision,
        ]
        for status in sorted(SOURCE_STATUSES.values(), key=lambda item: item.source)
    ]
    return [headers, *rows]


def indicator_metadata_values(
    api_key: str,
    indicators: list[Indicator],
) -> list[list[object]]:
    """Explain the source, dates, transformation, and monthly method for every column."""
    headers = [
        "Indicator",
        "Source",
        "Official source URL",
        "Series / file",
        "Native frequency",
        "Native units",
        "Output units",
        "Transformation",
        "Observation date meaning",
        "Release date rule",
        "Monthly selection",
        "Final / preliminary policy",
        "Point-in-time selections (row | observation | release)",
    ]
    rows = []
    for indicator in indicators:
        if indicator.source == "fred":
            series_ids = [
                item
                for item in [
                    indicator.series_id,
                    indicator.numerator_series_id,
                    indicator.denominator_series_id,
                ]
                if item
            ]
            metadata = (
                get_fred_series_metadata(api_key, series_ids[0]) if series_ids else {}
            )
            source = "FRED / ALFRED real-time API"
            source_url = (
                f"https://fred.stlouisfed.org/series/{series_ids[0]}"
                if series_ids
                else "https://fred.stlouisfed.org/"
            )
            code = " / ".join(series_ids)
            frequency = metadata.get("frequency", "")
            native_units = metadata.get("units", "")
            output_units = indicator.unit_label or (
                "Percent change from year ago"
                if indicator.units == "pc1"
                else native_units
            )
            transformation = (
                "Percent change from year ago, calculated from current FRED levels"
                if indicator.units == "pc1"
                else indicator.calculation or "None"
            )
            observation_meaning = "FRED observation date"
            release_rule = "ALFRED realtime_start (initial public vintage date)"
            final_policy = (
                "Current FRED value matched to its observation date; "
                "initial ALFRED publication date retained as metadata"
            )
        elif indicator.source == "ism":
            source = "Institute for Supply Management"
            source_url = ISM_REPORT_INDEX_URL
            code = indicator.source_column or ""
            frequency = "Monthly"
            native_units = "Diffusion index"
            output_units = "Index"
            transformation = "None"
            observation_meaning = "Survey reference month"
            release_rule = (
                "Official page publication metadata; conservative 7th/10th "
                "of following month fallback"
            )
            final_policy = "Official monthly report"
        elif indicator.source == "nyfed_sce":
            source = "Federal Reserve Bank of New York SCE"
            source_url = NYFED_SCE_PAGE_URL
            code = "frbny-sce-data.xlsx / one-year median"
            frequency = "Monthly"
            native_units = "Percent"
            output_units = "Percent"
            transformation = "None; median one-year-ahead expectation"
            observation_meaning = "Survey month"
            release_rule = indicator.release_rule
            final_policy = "Official monthly SCE workbook"
        elif indicator.source == "imf_datamapper":
            source = "IMF DataMapper"
            source_url = IMF_DATAMAPPER_API_BASE
            code = f"{indicator.series_id or ''} / {indicator.source_column or DEFAULT_IMF_COUNTRY}"
            frequency = "Annual"
            native_units = "% of GDP"
            output_units = indicator.unit_label or "% of GDP"
            transformation = "None; raw IMF FPP annual value"
            observation_meaning = "Annual fiscal year-end observation"
            release_rule = indicator.release_rule
            final_policy = "Latest available IMF DataMapper FPP observations"
        else:
            source = "University of Michigan Surveys of Consumers"
            source_url = MICHIGAN_CHARTS_PAGE_URL
            code = (
                "chicsr.xls / ICS_M"
                if indicator.source == "michigan_sentiment"
                else "chicer.xls / ICE_M"
            )
            frequency = "Monthly"
            native_units = "Index"
            output_units = "Index"
            transformation = "None"
            observation_meaning = "Survey month"
            release_rule = indicator.release_rule
            final_policy = indicator.release_rule

        rows.append(
            [
                indicator.name,
                source,
                source_url,
                code,
                frequency,
                native_units,
                output_units,
                transformation,
                observation_meaning,
                release_rule,
                indicator.monthly_method,
                final_policy,
                "; ".join(
                    (
                        (
                            f"{row_date.isoformat()} | "
                            f"{selection['observation_date'].isoformat()} | "
                            f"{selection['release_date'].isoformat()}"
                        )
                        if selection
                        else f"{row_date.isoformat()} | missing"
                    )
                    for row_date in [
                        month_end(item)
                        for item in recent_month_starts(CURRENT_OUTPUT_MONTHS)
                    ]
                    for selection in [LAST_SELECTIONS.get((indicator.name, row_date))]
                ),
            ]
        )
    return [headers, *rows]


def decimal_places(value: float) -> int:
    token = format(float(value), ".12f").rstrip("0").rstrip(".")
    return len(token.split(".", 1)[1]) if "." in token else 0


def build_validation_report(
    indicators: list[Indicator],
    workbook_data: list[tuple[str, list[dict]]],
    months: int,
) -> list[list[object]]:
    """Validate source records and observation-period month-end selections."""
    messages = list(VALIDATION_MESSAGES)
    data_by_name = dict(workbook_data)
    row_dates = [month_end(item) for item in recent_month_starts(months)]
    ranges = {
        "Manufacturing PMI": (0, 100),
        "Manufacturing new orders": (0, 100),
        "Services PMI": (0, 100),
        "Services new orders": (0, 100),
        "Services business activity": (0, 100),
        "Consumer sentiment": (0, 200),
        "Consumer expectations": (0, 200),
        "Consumer inflation expectations": (-5, 25),
        "Industrial production growth": (50, 150),
        "Real consumer spending growth": (10_000, 30_000),
        "Employment growth": (100_000, 200_000),
        "Core consumption inflation": (50, 250),
        "Core CPI inflation": (50, 500),
        "Headline CPI inflation": (50, 500),
        "Core producer price inflation": (50, 300),
        "Headline producer price inflation": (50, 300),
        "Wage growth": (50, 300),
        "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level": (
            1_000_000,
            15_000_000,
        ),
        "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level": (
            0,
            3_000_000,
        ),
        "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations": (
            0,
            5_000,
        ),
        "Banking system reserves": (1_000_000, 6_000_000),
        "Broad money growth": (10_000, 30_000),
        "Real government spending growth": (1_000, 10_000),
        "Government revenue growth": (0, 2_000_000),
        "Government revenue % GDP": (0, 100),
        "Interest paid on public debt % GDP": (0, 50),
        "Government interest coverage ratio": (0, 100),
        "Bank credit growth": (10_000, 30_000),
        "Corporate credit spread": (0, 20),
    }
    intentional_level_columns = {
        "Industrial production growth",
        "Real consumer spending growth",
        "Employment growth",
        "Wage growth",
        "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level",
        "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level",
        "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations",
        "Broad money growth",
        "Real government spending growth",
        "Government revenue growth",
        "Bank credit growth",
    }
    intentional_index_columns = {
        "Core consumption inflation",
        "Core CPI inflation",
        "Headline CPI inflation",
        "Core producer price inflation",
        "Headline producer price inflation",
    }
    expected_sources = {
        "fred": "FRED / ALFRED",
        "ism": "Institute for Supply Management",
        "nyfed_sce": "Federal Reserve Bank of New York SCE",
        "michigan_sca": "University of Michigan Surveys of Consumers",
        "michigan_sentiment": "University of Michigan Surveys of Consumers",
        "imf_datamapper": "IMF DataMapper FPP",
    }

    for indicator in indicators:
        records = data_by_name.get(indicator.name, [])
        seen = set()
        for record in records:
            if not isinstance(record.get("observation_date"), date):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": "Candidate record is missing a valid observation date.",
                    }
                )
            if not isinstance(record.get("release_date"), date):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": "Candidate record is missing a valid release date.",
                    }
                )
            if record.get("source") != expected_sources.get(indicator.source):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            f"Unexpected source identity {record.get('source')!r}; "
                            f"expected {expected_sources.get(indicator.source)!r}."
                        ),
                    }
                )
            if indicator.source == "fred" and not isinstance(
                record.get("realtime_start"),
                date,
            ):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": "FRED record is missing ALFRED realtime_start.",
                    }
                )
            elif indicator.source == "fred" and record.get(
                "realtime_start"
            ) != record.get("release_date"):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            "FRED release_date does not match its initial "
                            "ALFRED realtime_start."
                        ),
                    }
                )
            if indicator.source == "fred" and not str(
                record.get("value_vintage", "")
            ).startswith("Current FRED"):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            "FRED value is not marked as the current API value "
                            "matched to its observation date."
                        ),
                    }
                )
            key = (
                record.get("observation_date"),
                record.get("release_date"),
                record.get("selection_date"),
            )
            if key in seen:
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": f"Duplicate observation/release record: {key}",
                    }
                )
            seen.add(key)

        if (
            "growth" in indicator.name.lower()
            and indicator.units != "pc1"
            and indicator.name not in intentional_level_columns
        ):
            messages.append(
                {
                    "severity": "ERROR",
                    "indicator": indicator.name,
                    "message": "Column is labelled growth but transformation is not pc1.",
                }
            )
        if indicator.name in intentional_level_columns:
            messages.append(
                {
                    "severity": "WARNING",
                    "indicator": indicator.name,
                    "message": (
                        "Legacy column name is intentionally preserved, but values "
                        "are raw FRED levels rather than growth rates."
                    ),
                }
            )
        if indicator.name in intentional_index_columns:
            messages.append(
                {
                    "severity": "WARNING",
                    "indicator": indicator.name,
                    "message": (
                        "Legacy inflation column name is intentionally preserved, "
                        "but the value is the raw FRED price index."
                    ),
                }
            )

        for row_date in row_dates:
            selected = latest_available_record(records, row_date)
            if selected is None:
                messages.append(
                    {
                        "severity": "WARNING",
                        "indicator": indicator.name,
                        "message": (
                            f"No source observation belongs to {row_date:%B %Y}; "
                            "output is blank."
                        ),
                    }
                )
                continue
            if selected["observation_date"] > row_date:
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            f"Observation date {selected['observation_date']} is after "
                            f"row date {row_date}."
                        ),
                    }
                )
            if (
                selected["release_date"] > row_date
                and selected.get("period_aligned") is not True
            ):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            f"Release date {selected['release_date']} is after "
                            f"row date {row_date}."
                        ),
                    }
                )
            if (
                indicator.source == "fred"
                and selected["realtime_start"] > row_date
                and selected.get("period_aligned") is not True
            ):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            f"ALFRED realtime_start {selected['realtime_start']} is "
                            f"after row date {row_date}."
                        ),
                    }
                )
            elif selected["release_date"] > row_date:
                messages.append(
                    {
                        "severity": "WARNING",
                        "indicator": indicator.name,
                        "message": (
                            f"Observation-period dating places this value at "
                            f"{row_date}; it was published on "
                            f"{selected['release_date']}."
                        ),
                    }
                )
            lower_upper = ranges.get(indicator.name)
            if (
                lower_upper
                and not lower_upper[0] <= selected["value"] <= lower_upper[1]
            ):
                messages.append(
                    {
                        "severity": "ERROR",
                        "indicator": indicator.name,
                        "message": (
                            f"Impossible value {selected['value']} at {row_date}; "
                            f"expected {lower_upper}."
                        ),
                    }
                )
            if decimal_places(selected["value"]) > 6:
                messages.append(
                    {
                        "severity": "WARNING",
                        "indicator": indicator.name,
                        "message": (
                            f"Suspicious precision ({decimal_places(selected['value'])} "
                            f"decimals) at {row_date}."
                        ),
                    }
                )

        latest_row_date = row_dates[-1]
        latest_selected = latest_available_record(records, latest_row_date)
        if latest_selected is not None:
            metadata = _FRED_METADATA_CACHE.get(indicator.series_id or "", {})
            frequency = metadata.get("frequency", "Monthly")
            stale_days = (
                45
                if frequency.startswith(("Daily", "Weekly"))
                else 100 if frequency.startswith(("Monthly", "Quarterly")) else 400
            )
            age_days = (latest_row_date - latest_selected["release_date"]).days
            if age_days > stale_days:
                messages.append(
                    {
                        "severity": "WARNING",
                        "indicator": indicator.name,
                        "message": (
                            f"Selected source data is stale by {age_days} days "
                            f"for frequency {frequency}."
                        ),
                    }
                )

    headers = ["Severity", "Indicator", "Message"]
    if not messages:
        return [headers, ["OK", "All indicators", "No validation warnings or errors."]]
    rows = [[item["severity"], item["indicator"], item["message"]] for item in messages]
    return [headers, *rows]


def selection_diagnostic_values(
    indicators: list[Indicator],
    workbook_data: list[tuple[str, list[dict]]],
    months: int,
) -> list[list[object]]:
    """Explain every observation-period selection for the latest three row dates."""
    headers = [
        "Row date",
        "Indicator",
        "Observation date",
        "Release date",
        "Realtime start",
        "Selected value",
        "Source",
        "Source series / field",
        "Reason selected",
    ]
    data_by_name = dict(workbook_data)
    row_dates = [month_end(item) for item in recent_month_starts(months)][-3:]
    rows = []
    for row_date in row_dates:
        for indicator in indicators:
            selected = latest_available_record(
                data_by_name.get(indicator.name, []),
                row_date,
            )
            if selected is None:
                rows.append(
                    [
                        format_output_month(row_date),
                        indicator.name,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "Blank: no observation belongs to the row month.",
                    ]
                )
                continue
            rows.append(
                [
                    format_output_month(row_date),
                    indicator.name,
                    selected["observation_date"].isoformat(),
                    selected["release_date"].isoformat(),
                    (
                        selected["realtime_start"].isoformat()
                        if isinstance(selected.get("realtime_start"), date)
                        else ""
                    ),
                    selected["value"],
                    selected.get("source", ""),
                    selected.get("source_series_id", ""),
                    (
                        f"Selected as the average of {selected['monthly_average_count']} "
                        "source observations assigned to the row month."
                        if selected.get("monthly_average_count")
                        else "Selected as the most recent source observation "
                        "assigned to the row month."
                    ),
                ]
            )
    return [headers, *rows]


def save_clean_csv_outputs(
    workbook_data: list[tuple[str, list[dict]]],
    metadata_values: list[list[object]],
    validation_values: list[list[object]],
    diagnostic_values: list[list[object]],
) -> None:
    """Write the same three clean tables locally for inspection and downstream use."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    table_values = sheet_values_for_google(workbook_data)
    pd.DataFrame(table_values[1:], columns=table_values[0]).to_csv(
        OUTPUT_ROOT / "monthly_indicators.csv",
        index=False,
    )
    pd.DataFrame(metadata_values[1:], columns=metadata_values[0]).to_csv(
        OUTPUT_ROOT / "indicator_metadata.csv",
        index=False,
    )
    pd.DataFrame(validation_values[1:], columns=validation_values[0]).to_csv(
        OUTPUT_ROOT / "validation_report.csv",
        index=False,
    )
    pd.DataFrame(diagnostic_values[1:], columns=diagnostic_values[0]).to_csv(
        OUTPUT_ROOT / "selection_diagnostics.csv",
        index=False,
    )


def print_changed_cells(
    workbook_data: list[tuple[str, list[dict]]],
) -> None:
    """Print every cell that differs from the preceding local export."""
    output_path = OUTPUT_ROOT / "monthly_indicators.csv"
    if not output_path.exists():
        return
    try:
        previous = pd.read_csv(output_path)
    except Exception:
        logging.warning("Could not read the preceding CSV for before/after comparison.")
        return

    if "Date" not in previous.columns:
        return

    current_values = sheet_values_for_google(workbook_data)
    current = pd.DataFrame(current_values[1:], columns=current_values[0])
    common_columns = [
        column
        for column in current.columns
        if column != "Date" and column in previous.columns
    ]
    comparison = previous[["Date", *common_columns]].merge(
        current[["Date", *common_columns]],
        on="Date",
        how="inner",
        suffixes=(" before", " after"),
    )
    if comparison.empty:
        return
    changes = []
    for _, row in comparison.iterrows():
        for column in common_columns:
            before = row[f"{column} before"]
            after = row[f"{column} after"]
            both_missing = pd.isna(before) and pd.isna(after)
            one_missing = pd.isna(before) != pd.isna(after)
            if both_missing or (
                not one_missing and google_values_equivalent(before, after)
            ):
                continue
            changes.append(
                {
                    "Date": row["Date"],
                    "Indicator": column,
                    "Before": "" if pd.isna(before) else before,
                    "After": "" if pd.isna(after) else after,
                }
            )
    if COMPACT_TERMINAL:
        print(f"Changes detected: {len(changes)} cells.")
    elif changes:
        print_console_table(
            "Changed cells:",
            ["Date", "Indicator", "Before", "After"],
            (
                [item["Date"], item["Indicator"], item["Before"], item["After"]]
                for item in changes
            ),
        )
    else:
        print("\nChanged cells: none.")


def console_cell(value: object) -> str:
    """Return a consistent display value for command-line tables."""
    return "" if value is None else str(value)


def print_console_table(
    title: str | None,
    headers: list[str],
    rows: Iterable[Iterable[object]],
) -> None:
    """Print an aligned text table without changing the underlying CSV output."""
    display_rows = [
        [console_cell(value) for value in row][: len(headers)] for row in rows
    ]
    display_rows = [row + [""] * (len(headers) - len(row)) for row in display_rows]
    widths = [
        (
            max(len(header), *(len(row[index]) for row in display_rows))
            if display_rows
            else len(header)
        )
        for index, header in enumerate(headers)
    ]
    if title:
        print(f"\n{title}")
    print(
        " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    )
    print("-+-".join("-" * width for width in widths))
    for row in display_rows:
        print(
            " | ".join(row[index].ljust(widths[index]) for index in range(len(headers)))
        )


def validate_excel_signature(
    content: bytes, source_name: str, legacy_xls: bool = False
) -> None:
    """Use file metadata bytes to reject HTML error pages disguised as spreadsheets."""
    expected = b"\xd0\xcf\x11\xe0" if legacy_xls else b"PK"
    if not content.startswith(expected):
        file_type = "XLS" if legacy_xls else "XLSX"
        raise RuntimeError(
            f"{source_name} download is no longer a valid {file_type} workbook; "
            "the source URL or website format may have changed."
        )


def validate_response_content_type(
    response: requests.Response,
    source_name: str,
    allowed_fragments: Iterable[str],
) -> None:
    """Reject explicit content types that do not match the expected source format."""
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and not any(
        fragment in content_type for fragment in allowed_fragments
    ):
        raise RuntimeError(
            f"{source_name} returned unexpected Content-Type '{content_type}'."
        )


def text_from_html(html: str) -> str:
    """Convert HTML into normalized text while preserving report line breaks."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def parse_html_publication_date(html: str) -> date | None:
    """Read common HTML metadata and JSON-LD publication-date fields."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for tag in soup.find_all("meta"):
        key = (
            tag.get("property") or tag.get("name") or tag.get("itemprop") or ""
        ).lower()
        if key in {
            "article:published_time",
            "date",
            "datepublished",
            "publish_date",
            "publication_date",
        }:
            candidates.append(tag.get("content"))
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                candidates.extend(
                    item.get(key)
                    for key in ("datePublished", "dateCreated")
                    if item.get(key)
                )
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    for candidate in candidates:
        parsed = pd.to_datetime(candidate, errors="coerce", utc=True)
        if not pd.isna(parsed):
            return parsed.date()
    return None


def canonical_month(raw: str) -> str:
    """Normalize full or abbreviated month names to lowercase full names."""
    token = raw.strip().lower().rstrip(".")
    return MONTH_ALIASES.get(token[:4], MONTH_ALIASES.get(token[:3], token))


def parse_ism_report_date(text: str, expected_kind: str) -> date | None:
    """Extract the month and year from an ISM report body."""
    month_pattern = (
        r"January|February|March|April|May|June|July|August|September|"
        r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )
    patterns = [
        rf"\b({month_pattern})\s+(20\d{{2}})\s+ISM\S*\s+{expected_kind}\s+PMI",
        rf"\b({month_pattern})\s+(20\d{{2}})\s+{expected_kind}\s+Index",
        rf"\b{expected_kind}\s+PMI\S*\s+Report.*?\b({month_pattern})\s+(20\d{{2}})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        month_name = canonical_month(match.group(1))
        if month_name in MONTH_NUMBERS:
            return date(int(match.group(2)), MONTH_NUMBERS[month_name], 1)
    return None


def parse_ism_report_date_from_html(html: str, expected_kind: str) -> date | None:
    """Prefer the visible report heading over stale metadata or comparison text."""
    soup = BeautifulSoup(html, "html.parser")
    heading_text = "\n".join(
        heading.get_text(" ", strip=True)
        for heading in soup.find_all(["h1", "h2", "h3"])
    )
    return parse_ism_report_date(heading_text, expected_kind) or parse_ism_report_date(
        text_from_html(html),
        expected_kind,
    )


def first_number(patterns: Iterable[str], text: str) -> float | None:
    """Return the first numeric value matched by any regex pattern."""
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if match:
            return float(match.group(1))
    return None


def checked_value(
    source_name: str,
    field_name: str,
    primary: float | None,
    corroborating: float | None,
    tolerance: float = 0.11,
) -> float | None:
    """Use one value while rejecting disagreements between official page sections."""
    if (
        primary is not None
        and corroborating is not None
        and abs(primary - corroborating) > tolerance
    ):
        raise RuntimeError(
            f"{source_name} contains conflicting {field_name} values: "
            f"{primary} versus {corroborating}."
        )
    return primary if primary is not None else corroborating


def first_table_value(labels: Iterable[str], text: str) -> float | None:
    """Read the first numeric value after an ISM table label."""
    for label in labels:
        escaped = re.escape(label)
        value = first_number(
            [
                rf"^{escaped}\s*(?:®)?\s+(\d{{1,3}}(?:\.\d+)?)\b",
                rf"\n{escaped}\s*(?:®)?\s+(\d{{1,3}}(?:\.\d+)?)\b",
            ],
            text,
        )
        if value is not None:
            return value
    return None


def html_table_row_numbers(html: str, labels: Iterable[str]) -> list[float] | None:
    """Read numeric cells from a real HTML table row identified by its first cell."""
    soup = BeautifulSoup(html, "html.parser")
    normalized_labels = {re.sub(r"\s+", " ", label).strip().lower() for label in labels}
    for row in soup.find_all("tr"):
        cells = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            for cell in row.find_all(["th", "td"])
        ]
        if not cells:
            continue
        row_label = cells[0].replace("®", "").strip().lower()
        if not any(
            row_label == label or row_label.startswith(label)
            for label in normalized_labels
        ):
            continue
        numbers = []
        for cell in cells[1:]:
            match = re.fullmatch(r"\s*([+-]?\d{1,3}(?:\.\d+)?)\s*%?\s*", cell)
            if match:
                numbers.append(float(match.group(1)))
        if numbers:
            return numbers
    return None


def current_html_table_value(html: str, labels: Iterable[str]) -> float | None:
    """Return the first numeric value from a matching HTML table row."""
    numbers = html_table_row_numbers(html, labels)
    return numbers[0] if numbers else None


def manufacturing_value_from_services_comparison(
    label: str,
    text: str,
    html: str | None = None,
) -> float | None:
    """Read Manufacturing's current value from the Services comparison table."""
    if html:
        numbers = html_table_row_numbers(html, [label])
        # Services comparison rows end with Manufacturing current, prior, change.
        if numbers and len(numbers) >= 6:
            return numbers[-3]

    escaped = re.escape(label)
    match = re.search(
        rf"^{escaped}\s*(?:®)?\s+([^\n]+)$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return None
    numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", match.group(1))
    # The final three values are Manufacturing current, previous, and change.
    if len(numbers) < 3:
        return None
    return float(numbers[-3])


def parse_ism_manufacturing_report(html: str, source_url: str) -> IsmReportData | None:
    """Parse Manufacturing PMI and New Orders from an official ISM page."""
    text = text_from_html(html)
    if "content you are looking for is no longer available" in text.lower():
        return None

    report_date = parse_ism_report_date_from_html(html, "Manufacturing")
    if not report_date:
        raise RuntimeError("ISM Manufacturing report date could not be identified.")

    manufacturing_pmi = checked_value(
        "ISM Manufacturing report",
        "Manufacturing PMI",
        current_html_table_value(html, ["Manufacturing PMI"])
        or first_table_value(["Manufacturing PMI"], text),
        first_number(
            [
                r"Manufacturing PMI\S*\s+at\s+(\d{1,3}(?:\.\d+)?)\s*%",
                r"Manufacturing PMI\S*\s+registered\s+(\d{1,3}(?:\.\d+)?)\s*percent",
                r"Manufacturing PMI\S*[^.\n]{0,160}?\breading\s+(?:of\s+)?"
                r"\(?(\d{1,3}(?:\.\d+)?)\s*percent",
            ],
            text,
        ),
    )

    manufacturing_new_orders = checked_value(
        "ISM Manufacturing report",
        "Manufacturing New Orders",
        current_html_table_value(html, ["New Orders"])
        or first_table_value(["New Orders"], text),
        first_number(
            [
                r"New Orders Index[^.\n]{0,160}?\bregister(?:ed|ing)\s+"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
                r"New Orders Index[^.\n]{0,160}?\breading\s+(?:of\s+)?"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
            ],
            text,
        ),
    )

    return IsmReportData(
        report_date,
        source_url,
        {
            "manufacturing_pmi": manufacturing_pmi,
            "manufacturing_new_orders": manufacturing_new_orders,
        },
        parse_html_publication_date(html),
    )


def parse_ism_services_report(html: str, source_url: str) -> IsmReportData | None:
    """Parse Services PMI, New Orders, and Business Activity from ISM."""
    text = text_from_html(html)
    if "content you are looking for is no longer available" in text.lower():
        return None

    report_date = parse_ism_report_date_from_html(html, "Services")
    if not report_date:
        raise RuntimeError("ISM Services report date could not be identified.")

    services_pmi = checked_value(
        "ISM Services report",
        "Services PMI",
        current_html_table_value(html, ["Services PMI"])
        or first_table_value(["Services PMI"], text),
        first_number(
            [
                r"Services PMI\S*\s+at\s+(\d{1,3}(?:\.\d+)?)\s*%",
                r"Services PMI\S*\s+registered\s+(\d{1,3}(?:\.\d+)?)\s*percent",
                r"Services PMI\S*[^.\n]{0,160}?\breading\s+(?:of\s+)?"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
            ],
            text,
        ),
    )

    services_new_orders = checked_value(
        "ISM Services report",
        "Services New Orders",
        current_html_table_value(html, ["New Orders"])
        or first_table_value(["New Orders"], text),
        first_number(
            [
                r"New Orders Index\s+at\s+(\d{1,3}(?:\.\d+)?)\s*%",
                r"New Orders Index[^.\n]{0,160}?\bregister(?:ed|ing)\s+"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
                r"New Orders Index[^.\n]{0,160}?\breading\s+(?:of\s+)?"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
            ],
            text,
        ),
    )

    services_business_activity = checked_value(
        "ISM Services report",
        "Services Business Activity",
        current_html_table_value(
            html,
            ["Business Activity/Production", "Business Activity"],
        )
        or first_table_value(["Business Activity/Production"], text),
        first_number(
            [
                r"Business Activity Index\s+at\s+(\d{1,3}(?:\.\d+)?)\s*%",
                r"Business Activity Index[^.\n]{0,200}?\bto\s+"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
                r"Business Activity Index[^.\n]{0,160}?\bregistered\s+"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
                r"Business Activity Index[^.\n]{0,160}?\breading\s+(?:of\s+)?"
                r"(\d{1,3}(?:\.\d+)?)\s*percent",
            ],
            text,
        ),
    )

    manufacturing_pmi = manufacturing_value_from_services_comparison(
        "Services PMI",
        text,
        html,
    )
    manufacturing_new_orders = manufacturing_value_from_services_comparison(
        "New Orders",
        text,
        html,
    )

    return IsmReportData(
        report_date,
        source_url,
        {
            "services_pmi": services_pmi,
            "services_new_orders": services_new_orders,
            "services_business_activity": services_business_activity,
            "manufacturing_pmi": manufacturing_pmi,
            "manufacturing_new_orders": manufacturing_new_orders,
        },
        parse_html_publication_date(html),
    )


def ism_report_url(kind: str, month_slug: str) -> str:
    return urljoin(ISM_REPORT_INDEX_URL, f"{kind}/{month_slug}/")


def month_sequence_ending_at(end_month: date, count: int) -> list[date]:
    months = []
    year = end_month.year
    month = end_month.month
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


def latest_contiguous_ism_rows(frame: pd.DataFrame, months: int) -> pd.DataFrame:
    """Return the latest contiguous block of ISM release months."""
    if frame.empty:
        return frame
    sorted_frame = frame.sort_values("date").reset_index(drop=True)
    release_months = [
        date(item.year, item.month, 1)
        for item in pd.to_datetime(sorted_frame["date"], errors="coerce")
    ]
    for end_index in range(len(sorted_frame) - 1, months - 2, -1):
        start_index = end_index - months + 1
        block_months = release_months[start_index : end_index + 1]
        expected = list(reversed(month_sequence_ending_at(block_months[-1], months)))
        if block_months == expected:
            return sorted_frame.iloc[start_index : end_index + 1].reset_index(drop=True)
    return sorted_frame.tail(months).reset_index(drop=True)


def candidate_months_from_today(lookback: int = 18) -> list[date]:
    return month_sequence_ending_at(date.today().replace(day=1), lookback)


def load_ism_backup_response(session, kind, target_month, parser):
    """Use a verified local archive or an explicitly registered ISM release."""
    source_name = "ISM Manufacturing" if kind == "pmi" else "ISM Services"
    period = target_month.isoformat()
    archived = load_archived_source(source_name, period)
    if archived is not None:
        content, metadata = archived
        return response_from_archive(content, metadata), True
    url = ISM_PRESS_RELEASES.get((kind, period))
    if not url:
        return None, False
    print(
        f"  ISM {kind}: checking original press release for {target_month:%B %Y}...",
        flush=True,
    )
    try:
        response = fetch_url(session, url, minimum_bytes=MIN_HTML_BYTES)
    except requests.RequestException:
        logging.warning(
            "ISM original press release unavailable for %s %s; preserving existing cells.",
            kind,
            period,
        )
        return None, False
    validate_response_content_type(
        response, source_name, ("text/html", "application/xhtml")
    )
    parsed = parser(response.text, response.url)
    if not parsed or parsed.report_date != target_month:
        raise RuntimeError(f"ISM press release does not match {kind} {period}.")
    if not parsed.publication_date or parsed.publication_date.replace(
        day=1
    ) != ism_release_month(target_month):
        raise RuntimeError(
            f"ISM press release has an unexpected publication date for {period}."
        )
    return response, False


def scrape_ism_month(
    session: requests.Session,
    target_month: date,
    scraped_at: str,
    logger: logging.Logger,
    url_overrides: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """Scrape one month from ISM, tolerating missing Manufacturing or Services pages."""
    month_slug = MONTHS[target_month.month - 1]
    row: dict[str, object] = {
        "date": ism_release_month(target_month).isoformat(),
        "report_month": target_month.isoformat(),
        "scraped_at": scraped_at,
    }
    for kind, parser in [
        ("pmi", parse_ism_manufacturing_report),
        ("services", parse_ism_services_report),
    ]:
        source_name = "ISM Manufacturing" if kind == "pmi" else "ISM Services"
        period = target_month.isoformat()
        url = fresh_ism_url(
            (url_overrides or {}).get(kind) or ism_report_url(kind, month_slug)
        )
        response = None
        used_archive = False
        try:
            response = fetch_url(
                session,
                url,
                minimum_bytes=MIN_HTML_BYTES,
                request_headers=ISM_FRESH_HEADERS,
            )
        except OfficialSourceRedirectError as exc:
            if not is_ism_login_redirect(exc):
                raise
            logger.warning(
                "ISM %s report for %s redirected to the login page; treating it as unavailable.",
                kind,
                target_month,
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in {404, 410}:
                raise
            logger.warning(
                "Could not fetch ISM %s report for %s: %s", kind, target_month, exc
            )
        except requests.RequestException as exc:
            logger.warning(
                "Could not fetch ISM %s report for %s: %s", kind, target_month, exc
            )

        if response is None:
            response, used_archive = load_ism_backup_response(
                session, kind, target_month, parser
            )
            if response is None:
                continue

        validate_response_content_type(
            response,
            f"ISM {kind} report",
            ("text/html", "application/xhtml"),
        )
        parsed = parser(response.text, response.url)

        if not parsed or parsed.report_date != target_month:
            # ISM's CDN can briefly serve the prior month's body at the new
            # month's canonical URL. Retry with a new cache token before
            # concluding that the requested report is unavailable.
            if not used_archive:
                try:
                    refreshed_response = fetch_url(
                        session,
                        fresh_ism_url(url),
                        minimum_bytes=MIN_HTML_BYTES,
                        request_headers=ISM_FRESH_HEADERS,
                    )
                    refreshed_parsed = parser(
                        refreshed_response.text,
                        refreshed_response.url,
                    )
                    if (
                        refreshed_parsed
                        and refreshed_parsed.report_date == target_month
                    ):
                        response = refreshed_response
                        parsed = refreshed_parsed
                except (OfficialSourceRedirectError, requests.RequestException):
                    pass

        if not parsed or parsed.report_date != target_month:
            # ISM sometimes returns a valid generic/current report page with
            # HTTP 200 for an older month slug. That is not the requested
            # release, so use the checksum-verified period archive if present.
            if used_archive:
                continue
            response, used_archive = load_ism_backup_response(
                session, kind, target_month, parser
            )
            if response is None:
                continue
            parsed = parser(response.text, response.url)
            if not parsed or parsed.report_date != target_month:
                continue
        if not used_archive:
            archive_validated_source(
                source_name,
                period,
                response,
                "html",
                parsed.values.keys(),
                parsed.values,
            )
        else:
            archived_metadata = load_archived_source(source_name, period)
            if archived_metadata is not None:
                _, metadata = archived_metadata
                set_source_status(
                    SourceStatus(
                        source=source_name,
                        status="OK (archive)",
                        latest_data_month=period,
                        retrieved_at=metadata.get("retrieved_at", ""),
                        source_url=metadata.get("final_url", ""),
                        sha256=metadata.get("sha256", ""),
                        byte_size=int(metadata.get("byte_size", 0)),
                        content_type=metadata.get("content_type", ""),
                        schema_fingerprint=metadata.get("schema_fingerprint", ""),
                        archive_file=metadata.get("archive_file", ""),
                        revision=metadata.get("revision", ""),
                    )
                )
        row[f"{kind}_source_url"] = parsed.source_url
        row[f"{kind}_publication_date"] = (
            parsed.publication_date.isoformat() if parsed.publication_date else None
        )
        for column, value in parsed.values.items():
            if value is None:
                continue
            existing = row.get(column)
            if existing is not None and abs(float(existing) - value) > 0.11:
                raise RuntimeError(
                    f"ISM reports disagree for {target_month:%B %Y} {column}: "
                    f"{existing} versus {value}."
                )
            if existing is None:
                row[column] = value

    has_value = any(row.get(column) is not None for column in ISM_VALUE_COLUMNS)
    return row if has_value else None


def find_latest_ism_month(session: requests.Session, logger: logging.Logger) -> date:
    """Find the newest recent month with at least one official ISM report."""
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        current_urls = discover_current_ism_urls(session)
    except Exception as exc:
        if is_ism_login_redirect(exc):
            logger.info(
                "ISM report index redirected to its login page; "
                "trying the known public monthly report URLs."
            )
        else:
            logger.warning(
                "ISM report index is unreachable; trying known URLs and "
                "checksum-verified archives: %s",
                exc,
            )
        current_urls = {}

    if current_urls:
        for candidate in candidate_months_from_today():
            if scrape_ism_month(
                session,
                candidate,
                scraped_at,
                logger,
                url_overrides=current_urls,
            ):
                return candidate

    logger.info(
        "ISM current-report links were not recognized; trying known monthly URLs."
    )
    for candidate in candidate_months_from_today():
        if scrape_ism_month(session, candidate, scraped_at, logger):
            return candidate
    raise RuntimeError("No parseable recent ISM report pages were found.")


def scrape_ism_pmi(months: int) -> pd.DataFrame:
    """Scrape official ISM PMI values into a DataFrame."""
    logger = logging.getLogger("economic_indicator_export")
    session = make_requests_session()
    latest_month = find_latest_ism_month(session, logger)
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        current_urls = discover_current_ism_urls(session)
    except Exception:
        current_urls = {}
    rows = []
    for target_month in month_sequence_ending_at(latest_month, months):
        row = scrape_ism_month(
            session,
            target_month,
            scraped_at,
            logger,
            url_overrides=current_urls if target_month == latest_month else None,
        )
        if row:
            rows.append(row)
    if not rows:
        raise RuntimeError("ISM returned no parseable PMI rows.")
    frame = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    validate_ism_frame(frame, expected_months=months)
    return frame


def validate_numeric_column(
    frame: pd.DataFrame,
    column: str,
    source_name: str,
    minimum: float,
    maximum: float,
) -> None:
    """Reject missing, nonnumeric, or implausible official-source values."""
    if column not in frame.columns:
        raise RuntimeError(f"{source_name} format changed; missing '{column}'.")
    numeric = pd.to_numeric(frame[column], errors="coerce")
    if numeric.dropna().empty:
        raise RuntimeError(f"{source_name} has no numeric values for '{column}'.")
    invalid = numeric.dropna()[~numeric.dropna().between(minimum, maximum)]
    if not invalid.empty:
        raise RuntimeError(
            f"{source_name} returned an implausible '{column}' value: {invalid.iloc[0]}."
        )


def validate_release_freshness(
    latest_release_month: date,
    source_name: str,
    current_month_deadline_day: int,
) -> None:
    """Require the current release after its conservative monthly deadline."""
    current_month = month_start(date.today())
    oldest_allowed = (
        current_month
        if date.today().day >= current_month_deadline_day
        else add_months(current_month, -1)
    )
    if latest_release_month < oldest_allowed:
        raise RuntimeError(
            f"{source_name} appears stale. Latest release month is "
            f"{latest_release_month:%B %Y}; expected {oldest_allowed:%B %Y} or later."
        )
    if latest_release_month > current_month:
        raise RuntimeError(
            f"{source_name} returned a future release month: "
            f"{latest_release_month:%B %Y}."
        )


def validate_monthly_dates(
    frame: pd.DataFrame,
    source_name: str,
    date_column: str = "date",
    continuity_months: int = 24,
) -> None:
    """Reject invalid, duplicate, future, or recently gapped monthly dates."""
    if date_column not in frame.columns:
        raise RuntimeError(f"{source_name} format changed; missing '{date_column}'.")
    parsed = pd.to_datetime(frame[date_column], errors="coerce")
    if parsed.isna().any():
        raise RuntimeError(f"{source_name} contains invalid monthly dates.")
    normalized = [date(item.year, item.month, 1) for item in parsed]
    if len(normalized) != len(set(normalized)):
        raise RuntimeError(f"{source_name} contains duplicate monthly dates.")
    if normalized != sorted(normalized):
        raise RuntimeError(f"{source_name} monthly dates are not sorted.")
    if normalized[-1] > month_start(date.today()):
        raise RuntimeError(
            f"{source_name} contains a future month: {normalized[-1]:%B %Y}."
        )
    tail = normalized[-min(continuity_months, len(normalized)) :]
    expected = list(reversed(month_sequence_ending_at(tail[-1], len(tail))))
    if tail != expected:
        raise RuntimeError(
            f"{source_name} contains a gap in its recent monthly history."
        )


def validate_observation_tail(
    observations: list[dict],
    source_name: str,
    expected_months: int,
) -> None:
    """Require every requested recent month exactly once with a finite value."""
    if len(observations) != expected_months:
        raise RuntimeError(
            f"{source_name} returned {len(observations)} monthly values; "
            f"{expected_months} were requested."
        )
    dates = [item["date"] for item in observations]
    if len(dates) != len(set(dates)):
        raise RuntimeError(f"{source_name} returned duplicate output months.")
    expected_dates = list(reversed(month_sequence_ending_at(dates[-1], len(dates))))
    if dates != expected_dates:
        raise RuntimeError(f"{source_name} output months contain a gap.")
    for item in observations:
        value = item.get("value")
        if value is None or not isinstance(value, (int, float)) or not pd.notna(value):
            raise RuntimeError(f"{source_name} returned a missing or nonnumeric value.")


def validate_ism_frame(frame: pd.DataFrame, expected_months: int | None = None) -> None:
    """Validate known ISM values; missing releases never clear existing history."""
    frame = frame.sort_values("date").copy()
    parsed_dates = pd.to_datetime(frame["date"], errors="coerce")
    if parsed_dates.isna().any():
        raise RuntimeError("ISM returned an invalid release date.")
    if parsed_dates.duplicated().any():
        raise RuntimeError("ISM returned duplicate release months.")
    release_dates = [date(item.year, item.month, 1) for item in parsed_dates]
    if not release_dates:
        raise RuntimeError("ISM returned no verified release months.")
    latest_release = release_dates[-1]
    expected_dates = list(
        reversed(
            month_sequence_ending_at(
                latest_release, expected_months or len(release_dates)
            )
        )
    )
    missing_dates = sorted(set(expected_dates) - set(release_dates))
    if missing_dates:
        add_validation_warning(
            "ISM",
            "Unavailable release month(s): "
            + ", ".join(item.isoformat() for item in missing_dates)
            + "; existing spreadsheet cells will be preserved.",
        )

    if "report_month" not in frame.columns:
        raise RuntimeError("ISM format changed; missing 'report_month'.")
    parsed_report_months = pd.to_datetime(frame["report_month"], errors="coerce")
    if parsed_report_months.isna().any():
        raise RuntimeError("ISM returned an invalid report month.")

    latest = frame.iloc[-1]
    for column in ISM_VALUE_COLUMNS:
        values = (
            frame[column]
            if column in frame
            else pd.Series(float("nan"), index=frame.index)
        )
        numeric = pd.to_numeric(values, errors="coerce")
        if (values.notna() & numeric.isna()).any():
            raise RuntimeError(f"ISM returned a nonnumeric '{column}' value.")
        if not numeric.dropna().between(0, 100).all():
            raise RuntimeError(f"ISM returned an implausible '{column}' value.")
        missing = frame.loc[numeric.isna(), "report_month"].astype(str).str[:7].tolist()
        if missing:
            add_validation_warning(
                ISM_VALUE_LABELS[column],
                f"Official ISM {ISM_VALUE_LABELS[column]} unavailable for "
                + ", ".join(missing)
                + "; existing spreadsheet cells will be preserved.",
            )

    if all(pd.isna(latest.get(column)) for column in ISM_VALUE_COLUMNS):
        raise RuntimeError(
            "ISM newest report month contains no official indicator values."
        )

    for report_timestamp, release_timestamp in zip(parsed_report_months, parsed_dates):
        report_month = report_timestamp.date()
        release_month = release_timestamp.date()
        if ism_release_month(report_month) != release_month:
            raise RuntimeError("ISM report month and release month are inconsistent.")
    validate_release_freshness(
        latest_release,
        "ISM",
        current_month_deadline_day=10,
    )
    logging.info(
        "Validated ISM: %s available release months through %s; updating verified values only.",
        len(frame),
        latest_release,
    )


def fred_get(endpoint: str, params: dict[str, str]) -> dict:
    global LAST_FRED_REQUEST_AT
    query = urlencode({**params, "file_type": "json"})
    url = f"{FRED_API_BASE}/{endpoint}?{query}"

    last_error = None
    for attempt in range(1, 4):
        retry_seconds = 2 * attempt
        elapsed = time.monotonic() - LAST_FRED_REQUEST_AT
        if elapsed < FRED_MIN_REQUEST_INTERVAL_SECONDS:
            sleep(FRED_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        try:
            LAST_FRED_REQUEST_AT = time.monotonic()
            with urlopen(url, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"FRED returned an error ({exc.code}): {details}")
            if exc.code == 429:
                retry_seconds = 20 * attempt
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise last_error from exc
        except (URLError, TimeoutError) as exc:
            last_error = RuntimeError(
                f"Could not reach FRED: {getattr(exc, 'reason', exc)}"
            )
            if attempt == 3:
                raise last_error from exc

        sleep(retry_seconds)

    raise last_error or RuntimeError("FRED request failed.")


def imf_datamapper_get(path: str, params: dict[str, str] | None = None) -> dict:
    """Fetch JSON from the official IMF DataMapper API with simple retries."""
    query = f"?{urlencode(params)}" if params else ""
    url = f"{IMF_DATAMAPPER_API_BASE}/{path.strip('/')}{query}"

    last_error = None
    for attempt in range(1, 4):
        try:
            with urlopen(url, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"IMF DataMapper returned an error ({exc.code}): {details}"
            )
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise last_error from exc
        except URLError as exc:
            last_error = RuntimeError(f"Could not reach IMF DataMapper: {exc.reason}")
            if attempt == 3:
                raise last_error from exc

        sleep(2 * attempt)

    raise last_error or RuntimeError("IMF DataMapper request failed.")


def parse_optional_datetime(value: object) -> datetime | None:
    """Parse an optional source timestamp without assuming one exact format."""
    if not value:
        return None
    parsed = pd.to_datetime(str(value), errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def parse_imf_year_values(data: dict, series_id: str, country: str) -> dict[int, float]:
    """Extract {year: value} from IMF DataMapper's nested values payload."""
    values = data.get("values", {})
    candidates = [
        values.get(series_id, {}).get(country),
        values.get(country, {}).get(series_id),
        values.get(series_id.upper(), {}).get(country),
    ]
    raw_series = next((item for item in candidates if isinstance(item, dict)), None)
    if raw_series is None:
        raise RuntimeError(
            f"IMF DataMapper returned no {series_id} values for {country}."
        )

    parsed: dict[int, float] = {}
    for raw_year, raw_value in raw_series.items():
        try:
            year = int(raw_year)
            value = float(Decimal(str(raw_value)))
        except (TypeError, ValueError, InvalidOperation):
            continue
        parsed[year] = value
    if not parsed:
        raise RuntimeError(
            f"IMF DataMapper returned no numeric {series_id} values for {country}."
        )
    return parsed


def get_imf_indicator_metadata(series_ids: Iterable[str]) -> dict[str, dict]:
    """Fetch metadata for the IMF indicators used by this calculation."""
    data = imf_datamapper_get("indicators")
    indicators = data.get("indicators", {})
    metadata = {}
    for series_id in series_ids:
        item = indicators.get(series_id)
        if not isinstance(item, dict):
            raise RuntimeError(
                f"IMF DataMapper indicators metadata is missing {series_id}."
            )
        metadata[series_id] = item
    return metadata


def get_imf_fpp_country_data(country: str) -> dict:
    """Fetch and cache the IMF FPP revenue and interest series for one country."""
    cached = _IMF_FPP_CACHE.get(country)
    if cached is not None:
        return cached

    metadata = get_imf_indicator_metadata(["rev", "ie"])
    revenue_data = imf_datamapper_get(f"rev/{country}")
    interest_data = imf_datamapper_get(f"ie/{country}")
    revenue_by_year = parse_imf_year_values(revenue_data, "rev", country)
    interest_by_year = parse_imf_year_values(interest_data, "ie", country)

    latest_modified = max(
        (
            parse_optional_datetime(metadata_item.get("last-modified"))
            for metadata_item in metadata.values()
        ),
        default=None,
    )
    retrieved_at = datetime.now(timezone.utc)
    release_date = latest_modified.date() if latest_modified else retrieved_at.date()
    latest_year = max(set(revenue_by_year) | set(interest_by_year))
    source_url = f"{IMF_DATAMAPPER_API_BASE}/rev/{country} | {IMF_DATAMAPPER_API_BASE}/ie/{country}"
    payload_hash = hashlib.sha256(
        json.dumps(
            {
                "rev": revenue_by_year,
                "ie": interest_by_year,
                "metadata": metadata,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    set_source_status(
        SourceStatus(
            source="IMF DataMapper FPP",
            status="OK",
            latest_data_month=f"{latest_year}-12-01",
            retrieved_at=retrieved_at.isoformat(),
            source_url=source_url,
            sha256=payload_hash,
            byte_size=len(json.dumps({"rev": revenue_data, "ie": interest_data})),
            content_type="application/json",
            schema_fingerprint="values.rev.country.year + values.ie.country.year",
            archive_file="",
            revision=(
                f"rev last-modified={metadata['rev'].get('last-modified', '')}; "
                f"ie last-modified={metadata['ie'].get('last-modified', '')}"
            ),
        )
    )
    cached = {
        "metadata": metadata,
        "release_date": release_date,
        "rev": revenue_by_year,
        "ie": interest_by_year,
    }
    _IMF_FPP_CACHE[country] = cached
    return cached


def get_imf_public_finance_observations(
    indicator: Indicator, months: int
) -> list[dict]:
    """Return annual IMF FPP records matched to their year-end month."""
    country = indicator.source_column or DEFAULT_IMF_COUNTRY
    series_id = indicator.series_id
    if series_id not in {"rev", "ie", "rev_over_ie"}:
        raise RuntimeError(
            f"Unsupported IMF FPP series for {indicator.name}: {series_id}"
        )

    data = get_imf_fpp_country_data(country)
    if series_id == "rev_over_ie":
        series_by_year = {}
        for year in sorted(set(data["rev"]) | set(data["ie"])):
            revenue = data["rev"].get(year)
            interest = data["ie"].get(year)
            if revenue is None or interest is None:
                add_validation_warning(
                    indicator.name,
                    f"IMF rev or ie is missing for {country} {year}; coverage ratio left blank.",
                )
                continue
            if interest == 0:
                add_validation_warning(
                    indicator.name,
                    f"IMF ie is zero for {country} {year}; coverage ratio left blank.",
                )
                continue
            series_by_year[year] = revenue / interest
    else:
        series_by_year = data[series_id]
    release_date = data["release_date"]

    observations = []
    for year, value in sorted(series_by_year.items()):
        observation_date = date(year, 12, 31)
        observations.append(
            {
                "country": country,
                "year": year,
                "observation_date": observation_date,
                "release_date": release_date,
                "realtime_start": None,
                "source": "IMF DataMapper FPP",
                "source_series_id": (
                    "rev/ie" if series_id == "rev_over_ie" else series_id
                ),
                "selection_date": date(year, 12, 1),
                "period_aligned": True,
                "history_complete": FULL_HISTORY_MODE,
                "value_vintage": "Latest IMF DataMapper FPP values",
                "value": value,
            }
        )

    validate_release_records(observations, indicator.name)
    return observations


def print_imf_public_finance_audit(
    country: str = DEFAULT_IMF_COUNTRY,
    limit: int = 10,
) -> None:
    """Print raw IMF revenue and interest inputs for manual verification."""
    data = get_imf_fpp_country_data(country)
    rows = []
    for year in sorted(set(data["rev"]) & set(data["ie"]))[-limit:]:
        revenue = data["rev"][year]
        interest = data["ie"][year]
        coverage_ratio = "" if interest == 0 else revenue / interest
        rows.append([country, year, revenue, interest, coverage_ratio])
    if COMPACT_TERMINAL:
        print(
            f"IMF public-finance history: {len(rows)} audit years "
            f"through {rows[-1][1] if rows else 'n/a'}."
        )
        return
    print_console_table(
        "IMF Public Finance audit:",
        [
            "Country",
            "Year",
            "Government revenue (% GDP)",
            "Interest paid on public debt (% GDP)",
            "Coverage ratio",
        ],
        rows,
    )


def normalize_fred_metadata_text(value: str) -> str:
    """Normalize small FRED wording changes without hiding real series changes."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower().replace("u.s.", "us")).strip()


def add_validation_warning(indicator: str, message: str) -> None:
    """Record a non-blocking validation issue for the report sheets."""
    VALIDATION_MESSAGES.append(
        {
            "severity": "WARNING",
            "indicator": indicator,
            "message": message,
        }
    )


def get_fred_series_metadata(api_key: str, series_id: str) -> dict:
    """Fetch and validate FRED's series identity metadata."""
    if series_id in _FRED_METADATA_CACHE:
        return _FRED_METADATA_CACHE[series_id]

    data = fred_get(
        "series",
        {
            "api_key": api_key,
            "series_id": series_id,
        },
    )
    series = data.get("seriess", [])
    if not series:
        raise RuntimeError(f"FRED did not return a series for {series_id}.")
    metadata = series[0]
    if metadata.get("id") != series_id:
        raise RuntimeError(
            f"FRED metadata identity mismatch: requested {series_id}, "
            f"received {metadata.get('id')!r}."
        )
    required_fields = [
        "title",
        "units",
        "frequency",
        "observation_start",
        "last_updated",
    ]
    missing = [field for field in required_fields if not metadata.get(field)]
    if missing:
        raise RuntimeError(
            f"FRED metadata changed for {series_id}; missing: {', '.join(missing)}."
        )

    expected = FRED_EXPECTED_METADATA.get(series_id, {})
    title_contains = expected.get("title_contains")
    units_contains = expected.get("units_contains")
    expected_frequency = expected.get("frequency")
    if title_contains and normalize_fred_metadata_text(
        title_contains
    ) not in normalize_fred_metadata_text(metadata["title"]):
        add_validation_warning(
            f"FRED {series_id}",
            "FRED title wording differs from the expected metadata guard: "
            f"{metadata['title']}.",
        )
    if units_contains and normalize_fred_metadata_text(
        units_contains
    ) not in normalize_fred_metadata_text(metadata["units"]):
        add_validation_warning(
            f"FRED {series_id}",
            "FRED units wording differs from the expected metadata guard: "
            f"{metadata['units']}.",
        )
    if expected_frequency and metadata["frequency"] != expected_frequency:
        add_validation_warning(
            f"FRED {series_id}",
            "FRED frequency wording differs from the expected metadata guard: "
            f"{metadata['frequency']}.",
        )

    _FRED_METADATA_CACHE[series_id] = metadata
    return metadata


def get_series_title(api_key: str, series_id: str) -> str:
    return get_fred_series_metadata(api_key, series_id)["title"]


def get_fred_observations(
    api_key: str,
    series_id: str,
    start_date: date,
    end_date: date,
    units: str = "lin",
    frequency: str = "",
) -> list[dict]:
    # Fetch the current published FRED values for the exact observation dates.
    # A second ALFRED request supplies each observation's initial publication
    # date without replacing the current value with its unrevised first print.
    current_data = fred_get(
        "series/observations",
        {
            "api_key": api_key,
            "series_id": series_id,
            "observation_start": start_date.isoformat(),
            "observation_end": end_date.isoformat(),
            "sort_order": "asc",
            "units": "lin",
        },
    )
    current_items = current_data.get("observations", [])
    try:
        reported_count = int(current_data.get("count", len(current_items)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"FRED {series_id} returned an invalid observation count."
        ) from exc
    if len(current_items) < reported_count:
        raise RuntimeError(
            f"FRED {series_id} returned only {len(current_items)} of "
            f"{reported_count} observations; refusing a truncated history."
        )
    # Current FRED values above are always fetched for the complete history.
    # Exact ALFRED release-date metadata is limited to the latest four years;
    # older observations use the documented metadata-only fallback below.
    # This avoids hundreds of vintage calls without reducing value history.
    initial_release_observations = []
    window_months = 60
    window_start = max(start_date, subtract_months(end_date, 48))
    alfred_history_start = date(1991, 1, 1)
    while window_start <= end_date:
        window_end = min(
            end_date,
            add_months(window_start, window_months) - timedelta(days=1),
        )
        if window_end < alfred_history_start:
            window_start = window_end + timedelta(days=1)
            continue
        realtime_start = max(window_start, alfred_history_start)
        try:
            initial_release_data = fred_get(
                "series/observations",
                {
                    "api_key": api_key,
                    "series_id": series_id,
                    "observation_start": window_start.isoformat(),
                    "observation_end": window_end.isoformat(),
                    "realtime_start": realtime_start.isoformat(),
                    "realtime_end": min(
                        # FRED's real-time calendar can still be on the prior
                        # US date during an evening run in Europe. This field
                        # is release-date metadata only; current values come
                        # from the separate non-vintage request above.
                        end_date - timedelta(days=1),
                        add_months(window_end, 3),
                    ).isoformat(),
                    "sort_order": "asc",
                    "units": "lin",
                    "output_type": "4",
                },
            )
        except RuntimeError as exc:
            if "does not exist in ALFRED" not in str(exc):
                raise
            initial_release_data = {"observations": []}
        initial_release_observations.extend(
            initial_release_data.get("observations", [])
        )
        window_start = window_end + timedelta(days=1)
    initial_release_dates: dict[str, date] = {}
    for item in initial_release_observations:
        if not item.get("realtime_start"):
            continue
        released = datetime.strptime(item["realtime_start"], "%Y-%m-%d").date()
        observed_key = item["date"]
        previous = initial_release_dates.get(observed_key)
        if previous is None or released < previous:
            initial_release_dates[observed_key] = released

    observations = []
    missing_initial_release_dates = 0
    for item in current_items:
        raw_value = item.get("value")
        if raw_value in (None, "."):
            continue

        try:
            value = float(Decimal(raw_value))
        except (InvalidOperation, ValueError):
            continue

        observation_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
        if frequency.startswith("Quarterly"):
            selection_date = add_months(
                month_start(observation_date),
                2,
            )
        elif frequency.startswith("Annual"):
            selection_date = date(observation_date.year, 12, 1)
        else:
            selection_date = month_start(observation_date)
        release_date = initial_release_dates.get(item["date"])
        if release_date is None:
            # ALFRED real-time vintages do not cover every observation in
            # century-long histories. The value and output month remain exact;
            # this fallback is metadata-only because FRED records are explicitly
            # period-aligned below.
            release_date = observation_date
            missing_initial_release_dates += 1

        observations.append(
            {
                "observation_date": observation_date,
                "release_date": release_date,
                "realtime_start": release_date,
                "source": "FRED / ALFRED",
                "source_series_id": series_id,
                "native_frequency": frequency,
                "selection_date": selection_date,
                "period_aligned": True,
                "value_vintage": "Current FRED value",
                "value": value,
            }
        )

    if missing_initial_release_dates:
        add_validation_warning(
            f"FRED {series_id}",
            f"ALFRED had no initial vintage date for {missing_initial_release_dates} "
            "historical observations; their observation dates were retained as "
            "metadata-only release-date fallbacks.",
        )

    if units == "lin":
        return observations
    if units != "pc1":
        raise RuntimeError(
            f"Unsupported point-in-time FRED transformation for {series_id}: {units}"
        )
    return transform_percent_change_from_year_ago(
        observations,
        frequency,
        series_id,
    )


def prior_year_target(input_date: date) -> date:
    """Return the same calendar date one year earlier, including leap-day safety."""
    try:
        return input_date.replace(year=input_date.year - 1)
    except ValueError:
        return input_date.replace(year=input_date.year - 1, day=28)


def transform_percent_change_from_year_ago(
    records: list[dict],
    frequency: str,
    series_id: str,
) -> list[dict]:
    """Reproduce FRED pc1 from current levels without losing release dates."""
    transformed = []
    ordered = sorted(records, key=lambda item: item["observation_date"])
    for record in ordered:
        target = prior_year_target(record["observation_date"])
        if frequency.startswith("Weekly"):
            candidates = [
                item
                for item in ordered
                if item["observation_date"] <= target
                and (target - item["observation_date"]).days <= 8
            ]
            prior = max(
                candidates,
                key=lambda item: item["observation_date"],
                default=None,
            )
        else:
            prior = next(
                (item for item in ordered if item["observation_date"] == target),
                None,
            )
        if prior is None or prior["value"] == 0:
            continue
        transformed.append(
            {
                "observation_date": record["observation_date"],
                "release_date": max(
                    record["release_date"],
                    prior["release_date"],
                ),
                "realtime_start": max(
                    record["realtime_start"],
                    prior["realtime_start"],
                ),
                "source": "FRED / ALFRED",
                "source_series_id": series_id,
                "native_frequency": frequency,
                "selection_date": record["selection_date"],
                "period_aligned": True,
                "value_vintage": "Current FRED value",
                "value": ((record["value"] / prior["value"]) - 1.0) * 100.0,
            }
        )
    if not transformed:
        raise RuntimeError(
            f"Could not calculate year-over-year growth for FRED series {series_id}."
        )
    return transformed


def validate_release_records(records: list[dict], series_name: str) -> None:
    """Validate normalized observation/release/value records."""
    seen = set()
    for record in records:
        observation_date = record.get("observation_date")
        release_date = record.get("release_date")
        value = record.get("value")
        if not isinstance(observation_date, date) or not isinstance(release_date, date):
            raise RuntimeError(
                f"{series_name} lost observation or release date metadata."
            )
        if release_date < observation_date and "Daily" not in series_name:
            VALIDATION_MESSAGES.append(
                {
                    "severity": "WARNING",
                    "indicator": series_name,
                    "message": (
                        f"Release date {release_date} precedes observation date "
                        f"{observation_date}; verify source dating convention."
                    ),
                }
            )
        if value is None or not pd.notna(value):
            raise RuntimeError(f"{series_name} contains a missing value record.")
        key = (
            observation_date,
            release_date,
            record.get("selection_date"),
        )
        if key in seen:
            raise RuntimeError(
                f"{series_name} contains duplicate observation/release dates."
            )
        seen.add(key)


def latest_available_record(records: list[dict], row_date: date) -> dict | None:
    """Return the requested month-end aggregation; never carry values forward."""
    row_month = month_start(row_date)
    eligible = [
        record
        for record in records
        if isinstance(record.get("release_date"), date)
        and isinstance(record.get("observation_date"), date)
        and record["observation_date"] <= row_date
        and month_start(record.get("selection_date", record["release_date"]))
        == row_month
        and (record.get("period_aligned") is True or record["release_date"] <= row_date)
    ]
    if not eligible:
        return None
    latest = max(
        eligible,
        key=lambda record: (record["observation_date"], record["release_date"]),
    )
    numeric_values = [
        record["value"] for record in eligible if pd.notna(record.get("value"))
    ]
    if len(numeric_values) != len(eligible):
        raise RuntimeError("Monthly aggregation received a missing source value.")
    aggregation_methods = {
        str(record.get("monthly_aggregation", "latest")) for record in eligible
    }
    if len(aggregation_methods) != 1:
        raise RuntimeError("Monthly aggregation received conflicting methods.")
    aggregation_method = next(iter(aggregation_methods))
    if aggregation_method == "latest" or len(eligible) == 1:
        return latest
    if aggregation_method != "mean":
        raise RuntimeError(f"Unsupported monthly aggregation: {aggregation_method}")

    averaged = dict(latest)
    averaged["value"] = sum(numeric_values) / len(numeric_values)
    averaged["observation_date"] = max(
        record["observation_date"] for record in eligible
    )
    averaged["release_date"] = max(record["release_date"] for record in eligible)
    realtime_dates = [
        record.get("realtime_start")
        for record in eligible
        if isinstance(record.get("realtime_start"), date)
    ]
    averaged["realtime_start"] = max(realtime_dates) if realtime_dates else None
    averaged["monthly_average_count"] = len(eligible)
    averaged["value_vintage"] = (
        f"Monthly average of {len(eligible)} source observations"
    )
    return averaged


def shift_monthly_observations(
    raw_observations: list[dict],
    months: int,
    release_lag_months: int,
) -> list[dict]:
    """Shift monthly observations into their publication month without duplicating values."""
    wanted_months = set(recent_month_starts(months))
    shifted = []
    for observation in raw_observations:
        release_month = add_months(month_start(observation["date"]), release_lag_months)
        if release_month in wanted_months:
            shifted.append({"date": release_month, "value": observation["value"]})
    return shifted


def get_monthly_observations(
    api_key: str,
    series_id: str,
    months: int,
    units: str = "lin",
) -> list[dict]:
    metadata = get_fred_series_metadata(api_key, series_id)
    today = date.today()
    if FULL_HISTORY_MODE:
        lookback_start = datetime.strptime(
            metadata["observation_start"], "%Y-%m-%d"
        ).date()
    else:
        # Four years covers sparse annual/quarterly releases and pc1 base
        # values. Full-history mode uses bounded ALFRED windows instead.
        lookback_start = subtract_months(today, max(months + 25, 48))
    records = get_fred_observations(
        api_key,
        series_id,
        lookback_start,
        today,
        units,
        metadata["frequency"],
    )
    validate_release_records(records, series_id)
    # This evidence flag, rather than the provider name, authorises removal of
    # stale workbook rows during a full authoritative reconciliation.
    for record in records:
        record["history_complete"] = FULL_HISTORY_MODE
    return records


def get_indicator_observations(
    api_key: str, indicator: Indicator, months: int
) -> list[dict]:
    if indicator.source == "ism":
        return get_ism_indicator_observations(indicator, months)
    if indicator.source == "nyfed_sce":
        return get_nyfed_sce_indicator_observations(indicator, months)
    if indicator.source == "michigan_sca":
        return get_michigan_consumer_expectations_observations(indicator, months)
    if indicator.source == "michigan_sentiment":
        return get_michigan_consumer_sentiment_observations(indicator, months)
    if indicator.source == "imf_datamapper":
        return get_imf_public_finance_observations(indicator, months)
    if indicator.series_id:
        return get_monthly_observations(
            api_key,
            indicator.series_id,
            months,
            indicator.units,
        )
    raise RuntimeError(
        f"Unsupported indicator source for {indicator.name}: {indicator.source}"
    )


def get_indicator_observations_safely(
    api_key: str,
    indicator: Indicator,
    months: int,
) -> list[dict]:
    """Fetch one indicator and reject missing data before it reaches Google Sheets."""
    try:
        observations = get_indicator_observations(api_key, indicator, months)
        for observation in observations:
            observation["monthly_aggregation"] = indicator.monthly_aggregation
        if not observations and indicator.source not in {"fred", "ism"}:
            raise RuntimeError("the source returned no requested monthly values")
        if not observations and indicator.source == "fred":
            if not indicator.name.startswith("Internal "):
                logging.info(
                    "%s has no actual FRED observation in the requested months; "
                    "leaving those cells blank.",
                    indicator.name,
                )
        return observations
    except Exception as exc:
        raise RuntimeError(f"{indicator.name} failed validation: {exc}") from exc


def get_ism_indicator_observations(indicator: Indicator, months: int) -> list[dict]:
    if not indicator.source_column:
        raise RuntimeError(f"{indicator.name} needs an ISM column name.")

    if months not in _ISM_CACHE:
        _ISM_CACHE[months] = scrape_ism_pmi(months=months)

    frame = _ISM_CACHE[months]
    if frame.empty or indicator.source_column not in frame.columns:
        return []

    observations = []
    for _, row in frame.iterrows():
        value = row[indicator.source_column]
        if value is None or value != value:
            continue
        observation_date = datetime.strptime(
            str(row["report_month"])[:10],
            "%Y-%m-%d",
        ).date()
        is_services = indicator.source_column.startswith("services_")
        publication_column = (
            "services_publication_date" if is_services else "pmi_publication_date"
        )
        publication_value = row.get(publication_column)
        if publication_value is not None and publication_value == publication_value:
            release_date = datetime.strptime(
                str(publication_value)[:10],
                "%Y-%m-%d",
            ).date()
        else:
            publication_month = add_months(observation_date, 1)
            # Calendar fallback is intentionally conservative to avoid early use.
            release_date = publication_month.replace(day=10 if is_services else 7)
            VALIDATION_MESSAGES.append(
                {
                    "severity": "WARNING",
                    "indicator": indicator.name,
                    "message": (
                        f"ISM publication metadata was unavailable for "
                        f"{observation_date:%B %Y}; used conservative "
                        f"{release_date} fallback."
                    ),
                }
            )
        observations.append(
            {
                "observation_date": observation_date,
                "release_date": release_date,
                "realtime_start": None,
                "source": "Institute for Supply Management",
                "source_series_id": indicator.source_column,
                "selection_date": month_start(release_date),
                "period_aligned": True,
                "value": float(value),
            }
        )
    _EXPLICIT_BLANK_OUTPUT_MONTHS.pop(indicator.name, None)
    validate_release_records(observations, indicator.name)
    return observations


def parse_yyyymm(value: object) -> date | None:
    """Parse dates stored as YYYYMM numbers in official chart workbooks."""
    if value is None or value != value:
        return None
    token = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
    if not re.fullmatch(r"\d{6}", token):
        return None
    try:
        parsed = date(int(token[:4]), int(token[4:]), 1)
    except ValueError:
        return None
    if parsed.year < 2000 or parsed > month_start(date.today()):
        return None
    return parsed


def parse_nyfed_page_latest(html: str) -> tuple[date, float] | None:
    """Optionally corroborate the workbook against the NY Fed's current summary."""
    text = text_from_html(html)
    month_match = re.search(
        r"\b(" + "|".join(name.title() for name in MONTHS) + r")\s+Survey\b",
        text,
        flags=re.IGNORECASE,
    )
    value_match = re.search(
        r"inflation expectations.{0,300}?"
        r"(?:to|at)\s+(-?\d{1,2}(?:\.\d+)?)\s*percent"
        r".{0,100}?one-year-ahead",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not month_match or not value_match:
        return None
    month_name = canonical_month(month_match.group(1))
    month_number = MONTH_NUMBERS[month_name]
    current_year = date.today().year
    # A December survey can be the latest page during January.
    year = (
        current_year - 1
        if date.today().month == 1 and month_number == 12
        else current_year
    )
    return date(year, month_number, 1), float(value_match.group(1))


def get_nyfed_sce_frame() -> pd.DataFrame:
    """Download the official NY Fed SCE chart data workbook."""
    global _NYFED_SCE_CACHE
    if _NYFED_SCE_CACHE is not None:
        return _NYFED_SCE_CACHE

    session = make_requests_session()
    page_from_archive = False
    try:
        page_response = fetch_url(
            session,
            NYFED_SCE_PAGE_URL,
            pause=0,
            minimum_bytes=MIN_HTML_BYTES,
        )
    except requests.RequestException:
        archived_page = load_latest_archived_source("NY Fed SCE Page")
        if archived_page is None:
            raise
        page_response = response_from_archive(*archived_page)
        page_from_archive = True
        logging.info("Using checksum-verified archive for the NY Fed SCE page.")
    validate_response_content_type(
        page_response,
        "NY Fed SCE page",
        ("text/html", "application/xhtml"),
    )
    discovered_url = discover_download_url_from_html(
        page_response.text,
        NYFED_SCE_PAGE_URL,
        href_pattern=r"frbny-sce-data\.xlsx",
        link_text_pattern=r"Chart Data",
    )
    workbook_from_archive = False
    try:
        response = fetch_first_available(
            session,
            [url for url in [discovered_url, NYFED_SCE_DATA_URL] if url],
            "NY Fed SCE chart data workbook",
        )
    except RuntimeError:
        archived_workbook = load_latest_archived_source("NY Fed SCE Workbook")
        if archived_workbook is None:
            raise
        response = response_from_archive(*archived_workbook)
        workbook_from_archive = True
        logging.info("Using checksum-verified archive for the NY Fed SCE workbook.")
    validate_response_content_type(
        response,
        "NY Fed SCE workbook",
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/octet-stream",
            "application/zip",
            "application/x-zip",
        ),
    )
    validate_excel_signature(response.content, "NY Fed SCE")
    try:
        workbook = pd.ExcelFile(BytesIO(response.content))
        if "Inflation expectations" not in workbook.sheet_names:
            raise RuntimeError(
                "NY Fed SCE workbook format changed; "
                "missing 'Inflation expectations' worksheet."
            )
        frame = workbook.parse("Inflation expectations", header=3)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            "Could not read the NY Fed SCE inflation expectations workbook."
        ) from exc

    if frame.shape[1] < 3:
        raise RuntimeError("NY Fed SCE workbook has too few columns.")
    frame = frame.rename(columns={frame.columns[0]: "date"})
    frame["date"] = frame["date"].apply(parse_yyyymm)
    frame = frame.dropna(subset=["date"])
    required_columns = {"Median one-year ahead expected inflation rate"}
    missing_columns = required_columns - set(frame.columns)
    if missing_columns:
        raise RuntimeError(
            "NY Fed SCE workbook format changed; missing columns: "
            + ", ".join(sorted(missing_columns))
        )
    if frame.empty:
        raise RuntimeError(
            "NY Fed SCE workbook did not contain parseable monthly rows."
        )
    frame = frame.sort_values("date").reset_index(drop=True)
    validate_monthly_dates(frame, "NY Fed SCE")
    for column in required_columns:
        validate_numeric_column(frame, column, "NY Fed SCE", -20, 50)

    page_latest = parse_nyfed_page_latest(page_response.text)
    if page_latest is not None:
        page_month, page_value = page_latest
        workbook_latest = frame.iloc[-1]
        workbook_month = workbook_latest["date"]
        workbook_value = float(
            workbook_latest["Median one-year ahead expected inflation rate"]
        )
        if page_month != workbook_month or abs(page_value - workbook_value) > 0.051:
            raise RuntimeError(
                "NY Fed SCE webpage and workbook disagree on the latest "
                f"one-year inflation expectation: page={page_month} {page_value}, "
                f"workbook={workbook_month} {workbook_value}."
            )

    latest_period = frame.iloc[-1]["date"].isoformat()
    latest_values = {
        column: float(frame.iloc[-1][column]) for column in required_columns
    }
    revision_values = {
        f"{row['date'].isoformat()}:{column}": float(row[column])
        for _, row in frame.tail(24).iterrows()
        for column in required_columns
    }
    if not page_from_archive:
        archive_validated_source(
            "NY Fed SCE Page",
            latest_period,
            page_response,
            "html",
            ("survey_month", "one_year_inflation_expectation"),
            {
                "survey_month": page_latest[0] if page_latest else latest_period,
                "one_year_inflation_expectation": (
                    page_latest[1]
                    if page_latest
                    else latest_values["Median one-year ahead expected inflation rate"]
                ),
            },
        )
    if not workbook_from_archive:
        archive_validated_source(
            "NY Fed SCE Workbook",
            latest_period,
            response,
            "xlsx",
            frame.columns,
            revision_values,
        )
    else:
        archived_workbook = load_latest_archived_source("NY Fed SCE Workbook")
        if archived_workbook is not None:
            _, metadata = archived_workbook
            set_source_status(
                SourceStatus(
                    source="NY Fed SCE Workbook",
                    status="OK (archive)",
                    latest_data_month=metadata.get("period", ""),
                    retrieved_at=metadata.get("retrieved_at", ""),
                    source_url=metadata.get("final_url", ""),
                    sha256=metadata.get("sha256", ""),
                    byte_size=int(metadata.get("byte_size", 0)),
                    content_type=metadata.get("content_type", ""),
                    schema_fingerprint=metadata.get("schema_fingerprint", ""),
                    archive_file=metadata.get("archive_file", ""),
                    revision=metadata.get("revision", ""),
                )
            )

    logging.info(
        "Validated NY Fed SCE workbook through %s from %s.",
        frame.iloc[-1]["date"],
        response.url,
    )
    frame.attrs["latest_publication_date"] = parse_html_publication_date(
        page_response.text
    )
    _NYFED_SCE_CACHE = frame
    return frame


def get_nyfed_sce_indicator_observations(
    indicator: Indicator, months: int
) -> list[dict]:
    """Return monthly NY Fed SCE inflation expectation observations."""
    if not indicator.source_column:
        raise RuntimeError(f"{indicator.name} needs a NY Fed SCE column name.")

    frame = get_nyfed_sce_frame()
    if indicator.source_column not in frame.columns:
        return []

    ordered = frame.sort_values("date")
    recent = (
        ordered
        if FULL_HISTORY_MODE
        else ordered.tail(months + indicator.release_lag_months)
    )
    observations = []
    latest_observation_date = frame.iloc[-1]["date"]
    latest_publication_date = frame.attrs.get("latest_publication_date")
    for _, row in recent.iterrows():
        value = row[indicator.source_column]
        if value is None or value != value:
            continue
        release_date = add_months(row["date"], 1).replace(day=20)
        if (
            row["date"] == latest_observation_date
            and isinstance(latest_publication_date, date)
            and latest_publication_date >= row["date"]
        ):
            release_date = latest_publication_date
        observations.append(
            {
                "observation_date": row["date"],
                "release_date": release_date,
                "realtime_start": None,
                "source": "Federal Reserve Bank of New York SCE",
                "source_series_id": indicator.source_column,
                "selection_date": add_months(
                    month_start(row["date"]),
                    indicator.release_lag_months,
                ),
                "period_aligned": True,
                "history_complete": FULL_HISTORY_MODE,
                "value": float(value),
            }
        )
    validate_release_records(observations, "NY Fed SCE")
    validate_release_freshness(
        month_start(observations[-1]["release_date"]),
        "NY Fed SCE",
        current_month_deadline_day=20,
    )
    return observations


def get_michigan_consumer_expectations_frame() -> pd.DataFrame:
    """Download the official Michigan SCA Index of Consumer Expectations workbook."""
    global _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE
    if _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE is not None:
        return _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE

    session = make_requests_session()
    page_from_archive = False
    try:
        page_response = fetch_url(
            session,
            MICHIGAN_CHARTS_PAGE_URL,
            pause=0,
            minimum_bytes=MIN_HTML_BYTES,
        )
    except requests.RequestException:
        archived_page = load_latest_archived_source("Michigan SCA Page")
        if archived_page is None:
            raise
        page_response = response_from_archive(*archived_page)
        page_from_archive = True
        logging.info("Using checksum-verified archive for the Michigan SCA page.")
    validate_response_content_type(
        page_response,
        "University of Michigan SCA page",
        ("text/html", "application/xhtml"),
    )
    discovered_url = discover_download_url_from_html(
        page_response.text,
        MICHIGAN_CHARTS_PAGE_URL,
        href_pattern=r"chicer\.xls",
    )
    workbook_from_archive = False
    try:
        response = fetch_first_available(
            session,
            [
                url
                for url in [discovered_url, MICHIGAN_CONSUMER_EXPECTATIONS_URL]
                if url
            ],
            "University of Michigan consumer expectations workbook",
        )
    except RuntimeError:
        archived_workbook = load_latest_archived_source("Michigan SCA Workbook")
        if archived_workbook is None:
            raise
        response = response_from_archive(*archived_workbook)
        workbook_from_archive = True
        logging.info("Using checksum-verified archive for the Michigan SCA workbook.")
    validate_response_content_type(
        response,
        "University of Michigan SCA workbook",
        (
            "application/vnd.ms-excel",
            "application/octet-stream",
            "application/xls",
            "application/x-msexcel",
            "application/x-ole-storage",
            "application/vnd.ms-office",
        ),
    )
    validate_excel_signature(
        response.content,
        "University of Michigan SCA",
        legacy_xls=True,
    )
    try:
        workbook = pd.ExcelFile(BytesIO(response.content), engine="xlrd")
        if "Data" not in workbook.sheet_names:
            raise RuntimeError(
                "Michigan workbook format changed; missing 'Data' worksheet."
            )
        raw = workbook.parse("Data", header=None)
    except ImportError as exc:
        raise RuntimeError(
            "Missing Michigan Excel dependency. Re-run the script so it can install xlrd automatically."
        ) from exc
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            "Could not read the Michigan consumer expectations workbook."
        ) from exc

    if raw.shape[1] < 2:
        raise RuntimeError("Michigan workbook has too few columns.")
    rows = []
    for _, row in raw.iterrows():
        item_date = pd.to_datetime(row.iloc[0], errors="coerce")
        value = pd.to_numeric(row.iloc[1], errors="coerce")
        if pd.isna(item_date) or pd.isna(value):
            continue
        if item_date.year < 1950 or item_date.year > date.today().year:
            continue
        rows.append(
            {
                "date": date(item_date.year, item_date.month, 1),
                "ICE_M": float(value),
            }
        )

    if not rows:
        raise RuntimeError(
            "Michigan workbook did not contain parseable consumer expectations rows."
        )

    _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE = (
        pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    )
    validate_monthly_dates(
        _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE,
        "University of Michigan SCA",
    )
    validate_numeric_column(
        _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE,
        "ICE_M",
        "University of Michigan SCA",
        0,
        200,
    )
    latest_period = _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE.iloc[-1]["date"].isoformat()
    revision_values = {
        row["date"].isoformat(): float(row["ICE_M"])
        for _, row in _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE.tail(24).iterrows()
    }
    if not page_from_archive:
        archive_validated_source(
            "Michigan SCA Page",
            latest_period,
            page_response,
            "html",
            ("Index of Consumer Expectations",),
            {"latest_data_month": latest_period},
        )
    if not workbook_from_archive:
        archive_validated_source(
            "Michigan SCA Workbook",
            latest_period,
            response,
            "xls",
            ("date", "ICE_M"),
            revision_values,
        )
    else:
        archived_workbook = load_latest_archived_source("Michigan SCA Workbook")
        if archived_workbook is not None:
            _, metadata = archived_workbook
            set_source_status(
                SourceStatus(
                    source="Michigan SCA Workbook",
                    status="OK (archive)",
                    latest_data_month=metadata.get("period", ""),
                    retrieved_at=metadata.get("retrieved_at", ""),
                    source_url=metadata.get("final_url", ""),
                    sha256=metadata.get("sha256", ""),
                    byte_size=int(metadata.get("byte_size", 0)),
                    content_type=metadata.get("content_type", ""),
                    schema_fingerprint=metadata.get("schema_fingerprint", ""),
                    archive_file=metadata.get("archive_file", ""),
                    revision=metadata.get("revision", ""),
                )
            )
    logging.info(
        "Validated University of Michigan SCA workbook through %s from %s.",
        _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE.iloc[-1]["date"],
        response.url,
    )
    return _MICHIGAN_CONSUMER_EXPECTATIONS_CACHE


def get_michigan_consumer_expectations_observations(
    indicator: Indicator,
    months: int,
) -> list[dict]:
    """Return monthly University of Michigan consumer expectations values."""
    column = indicator.source_column or "ICE_M"
    frame = get_michigan_consumer_expectations_frame()
    if frame.empty or column not in frame.columns:
        return []

    ordered = frame.sort_values("date")
    recent = ordered if FULL_HISTORY_MODE else ordered.tail(months)
    observations = []
    for _, row in recent.iterrows():
        value = row[column]
        if value is None or value != value:
            continue
        observation_date = row["date"]
        final_release = last_weekday_of_month(observation_date, 4)
        release_date = (
            min(date.today(), final_release)
            if month_start(observation_date) == month_start(date.today())
            else final_release
        )
        observations.append(
            {
                "observation_date": observation_date,
                "release_date": release_date,
                "realtime_start": None,
                "source": "University of Michigan Surveys of Consumers",
                "source_series_id": column,
                "value": float(value),
            }
        )
    validate_release_records(observations, "University of Michigan SCA")
    validate_release_freshness(
        month_start(observations[-1]["release_date"]),
        "University of Michigan SCA",
        current_month_deadline_day=20,
    )
    return observations


def get_michigan_consumer_sentiment_frame() -> pd.DataFrame:
    """Download the official Michigan Consumer Sentiment Index workbook."""
    global _MICHIGAN_CONSUMER_SENTIMENT_CACHE
    if _MICHIGAN_CONSUMER_SENTIMENT_CACHE is not None:
        return _MICHIGAN_CONSUMER_SENTIMENT_CACHE

    session = make_requests_session()
    page_from_archive = False
    try:
        page_response = fetch_url(
            session,
            MICHIGAN_CHARTS_PAGE_URL,
            pause=0,
            minimum_bytes=MIN_HTML_BYTES,
        )
    except requests.RequestException:
        archived_page = load_latest_archived_source("Michigan SCA Page")
        if archived_page is None:
            raise
        page_response = response_from_archive(*archived_page)
        page_from_archive = True
        logging.info("Using checksum-verified archive for the Michigan SCA page.")
    validate_response_content_type(
        page_response,
        "University of Michigan SCA page",
        ("text/html", "application/xhtml"),
    )
    discovered_url = discover_download_url_from_html(
        page_response.text,
        MICHIGAN_CHARTS_PAGE_URL,
        href_pattern=r"chicsr\.xls",
    )
    workbook_from_archive = False
    try:
        response = fetch_first_available(
            session,
            [url for url in [discovered_url, MICHIGAN_CONSUMER_SENTIMENT_URL] if url],
            "University of Michigan consumer sentiment workbook",
        )
    except RuntimeError:
        archived_workbook = load_latest_archived_source(
            "Michigan Consumer Sentiment Workbook"
        )
        if archived_workbook is None:
            raise
        response = response_from_archive(*archived_workbook)
        workbook_from_archive = True
        logging.info(
            "Using checksum-verified archive for the Michigan sentiment workbook."
        )
    validate_response_content_type(
        response,
        "University of Michigan sentiment workbook",
        (
            "application/vnd.ms-excel",
            "application/octet-stream",
            "application/xls",
            "application/x-msexcel",
            "application/x-ole-storage",
            "application/vnd.ms-office",
        ),
    )
    validate_excel_signature(
        response.content,
        "University of Michigan Consumer Sentiment",
        legacy_xls=True,
    )
    try:
        workbook = pd.ExcelFile(BytesIO(response.content), engine="xlrd")
        if "Data" not in workbook.sheet_names:
            raise RuntimeError("Michigan sentiment workbook is missing 'Data'.")
        raw = workbook.parse("Data", header=None)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("Could not read the Michigan sentiment workbook.") from exc

    rows = []
    for _, row in raw.iterrows():
        item_date = pd.to_datetime(row.iloc[0], errors="coerce")
        value = pd.to_numeric(row.iloc[1], errors="coerce")
        if pd.isna(item_date) or pd.isna(value):
            continue
        rows.append(
            {
                "date": date(item_date.year, item_date.month, 1),
                "ICS_M": float(value),
            }
        )
    frame = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    if frame.empty:
        raise RuntimeError("Michigan sentiment workbook contained no monthly rows.")
    validate_monthly_dates(frame, "University of Michigan Consumer Sentiment")
    validate_numeric_column(frame, "ICS_M", "Michigan Consumer Sentiment", 0, 200)
    latest_period = frame.iloc[-1]["date"].isoformat()
    if not page_from_archive:
        archive_validated_source(
            "Michigan SCA Page",
            latest_period,
            page_response,
            "html",
            ("Consumer Sentiment Index",),
            {"latest_sentiment_data_month": latest_period},
        )
    if not workbook_from_archive:
        archive_validated_source(
            "Michigan Consumer Sentiment Workbook",
            latest_period,
            response,
            "xls",
            ("date", "ICS_M"),
            {
                row["date"].isoformat(): float(row["ICS_M"])
                for _, row in frame.tail(24).iterrows()
            },
        )
    else:
        archived_workbook = load_latest_archived_source(
            "Michigan Consumer Sentiment Workbook"
        )
        if archived_workbook is not None:
            _, metadata = archived_workbook
            set_source_status(
                SourceStatus(
                    source="Michigan Consumer Sentiment Workbook",
                    status="OK (archive)",
                    latest_data_month=metadata.get("period", ""),
                    retrieved_at=metadata.get("retrieved_at", ""),
                    source_url=metadata.get("final_url", ""),
                    sha256=metadata.get("sha256", ""),
                    byte_size=int(metadata.get("byte_size", 0)),
                    content_type=metadata.get("content_type", ""),
                    schema_fingerprint=metadata.get("schema_fingerprint", ""),
                    archive_file=metadata.get("archive_file", ""),
                    revision=metadata.get("revision", ""),
                )
            )
    _MICHIGAN_CONSUMER_SENTIMENT_CACHE = frame
    return frame


def get_michigan_consumer_sentiment_observations(
    indicator: Indicator,
    months: int,
) -> list[dict]:
    frame = get_michigan_consumer_sentiment_frame()
    recent = frame if FULL_HISTORY_MODE else frame.tail(months)
    records = []
    for _, row in recent.iterrows():
        observation_date = row["date"]
        final_release = last_weekday_of_month(observation_date, 4)
        release_date = (
            min(date.today(), final_release)
            if month_start(observation_date) == month_start(date.today())
            else final_release
        )
        records.append(
            {
                "observation_date": observation_date,
                "release_date": release_date,
                "realtime_start": None,
                "source": "University of Michigan Surveys of Consumers",
                "source_series_id": indicator.source_column or "ICS_M",
                "value": float(row[indicator.source_column or "ICS_M"]),
            }
        )
    validate_release_records(records, "Michigan Consumer Sentiment")
    return records


def check_non_api_sources(months: int) -> None:
    """Smoke test the official scraped/downloaded sources."""

    def load_and_cache_ism() -> pd.DataFrame:
        warning_start = len(VALIDATION_MESSAGES)
        frame = scrape_ism_pmi(months)
        _ISM_CACHE[months] = frame
        for warning in VALIDATION_MESSAGES[warning_start:]:
            print(f"  ISM: {warning['message']}", flush=True)
        return frame

    nyfed_indicator = next(item for item in INDICATORS if item.source == "nyfed_sce")
    michigan_indicator = next(
        item for item in INDICATORS if item.source == "michigan_sca"
    )
    michigan_sentiment_indicator = next(
        item for item in INDICATORS if item.source == "michigan_sentiment"
    )
    checks = [
        ("ISM PMI", load_and_cache_ism),
        (
            "NY Fed SCE",
            lambda: pd.DataFrame(
                get_nyfed_sce_indicator_observations(nyfed_indicator, months)
            ),
        ),
        (
            "Michigan SCA",
            lambda: pd.DataFrame(
                get_michigan_consumer_expectations_observations(
                    michigan_indicator,
                    months,
                )
            ),
        ),
        (
            "Michigan Sentiment",
            lambda: pd.DataFrame(
                get_michigan_consumer_sentiment_observations(
                    michigan_sentiment_indicator,
                    months,
                )
            ),
        ),
    ]
    failures = []
    for name, loader in checks:
        try:
            frame = loader()
            if getattr(frame, "empty", False):
                raise RuntimeError("no rows parsed")
            print(f"{name}: OK ({len(frame)} rows)")
        except Exception as exc:
            print(f"{name}: FAILED ({exc})")
            failures.append(f"{name}: {exc}")
    if failures:
        raise RuntimeError("Non-API source check failed: " + " | ".join(failures))


def build_table(
    indicators: list[tuple[str, list[dict]]],
) -> tuple[list[str], list[list[object]]]:
    month_dates = [
        month_end(item) for item in recent_month_starts(CURRENT_OUTPUT_MONTHS)
    ]
    headers = ["Date"] + [title for title, _ in indicators]
    rows = []
    LAST_SELECTIONS.clear()

    for month_date in month_dates:
        row = [month_date]
        for title, observations in indicators:
            selected = latest_available_record(observations, month_date)
            LAST_SELECTIONS[(title, month_date)] = selected
            row.append(analysis_value(selected["value"]) if selected else None)
        rows.append(row)

    return headers, rows


def format_output_month(value: date) -> str:
    """Display release months as calendar month-end dates in DD/MM/YYYY format."""
    return month_end(value).strftime("%d/%m/%Y")


def sheet_values_for_google(
    indicators: list[tuple[str, list[dict]]],
) -> list[list[object]]:
    headers, rows = build_table(indicators)
    values = [headers]
    for row in rows:
        values.append(
            [
                format_output_month(value) if isinstance(value, date) else value
                for value in row
            ]
        )
    return values


def validate_non_fred_output_alignment(
    indicators: list[Indicator],
    workbook_data: list[tuple[str, list[dict]]],
    months: int,
) -> None:
    """Validate present non-FRED values while allowing months with no release."""
    observations_by_name = dict(workbook_data)
    expected_dates = [month_end(item) for item in recent_month_starts(months)]
    for indicator in indicators:
        if indicator.source == "fred":
            continue
        observations = observations_by_name.get(indicator.name, [])
        for row_date in expected_dates:
            selected = latest_available_record(observations, row_date)
            if selected is None:
                continue
            if (
                selected["release_date"] > row_date
                and selected.get("period_aligned") is not True
            ):
                raise RuntimeError(
                    f"{indicator.name} selected a release after {row_date}."
                )


def mutable_output_months() -> set[date]:
    """Months whose latest daily/weekly point legitimately changes during a rerun."""
    return {month_start(date.today())}


def verify_google_sheet_values(
    expected: list[list[object]],
    actual: list[list[object]],
) -> None:
    """Verify every nonblank value after Google Sheets reports a successful write."""
    if not actual or actual[0] != expected[0]:
        raise RuntimeError("Google Sheets read-back header verification failed.")
    if len(actual) < len(expected):
        raise RuntimeError(
            f"Google Sheets read-back returned {len(actual)} rows; "
            f"{len(expected)} were written."
        )
    for row_index, expected_row in enumerate(expected):
        actual_row = actual[row_index] if row_index < len(actual) else []
        for column_index, expected_value in enumerate(expected_row):
            if expected_value is None:
                continue
            if column_index >= len(actual_row):
                if expected_value == "":
                    continue
                raise RuntimeError(
                    f"Google Sheets read-back is missing row {row_index + 1}, "
                    f"column {column_index + 1}."
                )
            actual_value = actual_row[column_index]
            if google_values_equivalent(expected_value, actual_value):
                continue
            if column_index == 0:
                expected_date = normalise_observation_date(expected_value)
                actual_date = normalise_observation_date(actual_value)
                if expected_date is not None and expected_date == actual_date:
                    continue
            header = (
                expected[0][column_index]
                if column_index < len(expected[0])
                else "unknown"
            )
            raise RuntimeError(
                "Google Sheets read-back verification failed at "
                f"row {row_index + 1}, column '{header}': "
                f"wrote {expected_value!r}, read {actual_value!r}."
            )


def google_sheet_table_is_current(
    expected: list[list[object]],
    actual: list[list[object]],
) -> bool:
    """Compare complete value tables while tolerating Google date serialization."""
    if not expected or not actual or len(expected) != len(actual):
        return False
    if [normalise_header_name(item) for item in expected[0]] != [
        normalise_header_name(item) for item in actual[0]
    ]:
        return False
    for row_index, expected_row in enumerate(expected[1:], start=1):
        actual_row = actual[row_index]
        for column_index, expected_value in enumerate(expected_row):
            actual_value = (
                actual_row[column_index] if column_index < len(actual_row) else ""
            )
            if column_index == 0:
                if normalise_observation_date(
                    expected_value
                ) != normalise_observation_date(actual_value):
                    return False
                continue
            if expected_value in (None, ""):
                if actual_value not in (None, ""):
                    return False
                continue
            if not google_values_equivalent(expected_value, actual_value):
                return False
        if any(value not in (None, "") for value in actual_row[len(expected_row) :]):
            return False
    return True


def _indexed_sheet_rows(
    values: list[list[object]],
    context: str,
    allow_undated_data_rows: bool = False,
) -> dict[date, list[object]]:
    """Index a date-led table while rejecting malformed or duplicate data rows."""
    indexed: dict[date, list[object]] = {}
    for row_number, row in enumerate(values[1:], start=2):
        raw_date = row[0] if row else None
        observed = normalise_observation_date(raw_date)
        if observed is None:
            if any(item not in (None, "") for item in row[1:]):
                if allow_undated_data_rows:
                    continue
                raise RuntimeError(
                    f"{context}: row {row_number} contains data but has no valid date ({raw_date!r})."
                )
            continue
        if observed in indexed:
            raise RuntimeError(f"{context}: duplicate month {observed.isoformat()}.")
        indexed[observed] = row
    return indexed


def indicator_input_table_is_current(
    existing: list[list[object]],
    incoming: list[list[object]],
) -> bool:
    """Compare an indicator A:B table without relying on Google date rendering."""
    if len(existing) != len(incoming) or not existing or not incoming:
        return False
    if [normalise_header_name(item) for item in existing[0]] != [
        normalise_header_name(item) for item in incoming[0]
    ]:
        return False
    for old_row, new_row in zip(existing[1:], incoming[1:]):
        old_date = normalise_observation_date(old_row[0] if old_row else None)
        new_date = normalise_observation_date(new_row[0] if new_row else None)
        if old_date != new_date:
            return False
        old_value = normalise_value(old_row[1] if len(old_row) > 1 else "")
        new_value = normalise_value(new_row[1] if len(new_row) > 1 else "")
        if old_value is None or new_value is None:
            if old_value is not None or new_value is not None:
                return False
        elif not google_values_equivalent(old_value, new_value):
            return False
    return True


def validate_historical_table_replacement(
    existing: list[list[object]],
    incoming: list[list[object]],
    context: str,
    *,
    allow_absent_existing_dates: bool = False,
    allow_undated_existing_rows: bool = False,
    allow_blank_replacements: bool = False,
    allow_value_revisions: bool = False,
    mutable_months: set[date] | None = None,
) -> None:
    """Protect history while allowing explicit cleanup and authoritative revisions."""
    date_headers = {
        "date",
        "observation date",
        "month",
        "month-end date",
        "month end date",
        "period",
    }
    if (
        not incoming
        or not incoming[0]
        or normalise_header_name(incoming[0][0]) not in date_headers
    ):
        raise RuntimeError(
            f"{context}: generated replacement has no valid Date header."
        )
    if not existing:
        return
    if [normalise_header_name(item) for item in existing[0]] != [
        normalise_header_name(item) for item in incoming[0]
    ]:
        raise RuntimeError(
            f"{context}: refusing overwrite because headers differ; "
            f"existing={existing[0]!r}, incoming={incoming[0]!r}."
        )
    old_rows = _indexed_sheet_rows(
        existing,
        f"{context} existing table",
        allow_undated_data_rows=allow_undated_existing_rows,
    )
    new_rows = _indexed_sheet_rows(incoming, f"{context} replacement table")
    mutable_months = mutable_months or set()
    mismatches: list[str] = []
    for column_index, header in enumerate(existing[0][1:], start=1):
        for observed, old_row in sorted(old_rows.items()):
            old_raw = old_row[column_index] if len(old_row) > column_index else ""
            old_value = normalise_value(old_raw)
            if old_value is None:
                continue
            if observed in mutable_months:
                continue
            if observed not in new_rows and allow_absent_existing_dates:
                continue
            new_row = new_rows.get(observed, [])
            new_raw = new_row[column_index] if len(new_row) > column_index else ""
            new_value = normalise_value(new_raw)
            if new_value is None and allow_blank_replacements:
                continue
            if new_value is not None and allow_value_revisions:
                continue
            if new_value is None or not google_values_equivalent(old_value, new_value):
                mismatches.append(
                    f"{header} {observed.isoformat()}: existing={old_raw!r}, incoming={new_raw!r}"
                )
    if mismatches:
        preview = "; ".join(mismatches[:5])
        suffix = f"; plus {len(mismatches) - 5} more" if len(mismatches) > 5 else ""
        raise RuntimeError(
            f"{context}: historical overlap does not match ({preview}{suffix}). "
            "Only genuinely new or previously blank cells may be added."
        )


def validate_overlapping_indicator_values(
    existing: dict[date, dict],
    incoming: dict[date, float | None],
    context: str,
    *,
    allow_value_revisions: bool = False,
    mutable_months: set[date] | None = None,
) -> None:
    """Reject unexplained revisions to populated closed months before any write."""
    mutable_months = mutable_months or set()
    mismatches = []
    for observed, new_value in sorted(incoming.items()):
        old = existing.get(observed)
        old_value = old.get("value") if old else None
        if old_value is None:
            continue
        if not FULL_HISTORY_MODE:
            # Incremental imports never revise or clear a populated cell. The
            # source's changed/blank value is intentionally ignored below.
            continue
        if observed in mutable_months:
            continue
        if new_value is not None and allow_value_revisions:
            continue
        if new_value is None or not google_values_equivalent(old_value, new_value):
            mismatches.append(
                f"{observed.isoformat()}: existing={old.get('raw_value')!r}, incoming={new_value!r}"
            )
    if mismatches:
        preview = "; ".join(mismatches[:5])
        suffix = f"; plus {len(mismatches) - 5} more" if len(mismatches) > 5 else ""
        raise RuntimeError(
            f"{context}: historical overlap does not match ({preview}{suffix}). "
            "Only new dates or blank historical cells may be written."
        )


def google_values_equivalent(expected: object, actual: object) -> bool:
    """Treat equivalent Google numeric representations as equal."""
    if str(expected) == str(actual):
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    try:
        expected_number = Decimal(str(expected).strip())
        actual_number = Decimal(str(actual).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return False
    difference = abs(expected_number - actual_number)
    if difference <= Decimal("0.000000001"):
        return True
    scale = max(abs(expected_number), abs(actual_number), Decimal("1"))
    return difference <= max(Decimal("0.0001"), scale * Decimal("0.00000001"))


def column_letter(column_index: int) -> str:
    """Convert a zero-based column index to a Google Sheets column letter."""
    column_number = column_index + 1
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def quote_sheet_name(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def plausible_sheet_year(year: int) -> bool:
    """Keep spreadsheet date detection from accepting malformed ancient/future years."""
    return 1900 <= year <= date.today().year + 2


def normalise_observation_date(value: object) -> date | None:
    """Normalise sheet/source dates to the first day of their observation month."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if plausible_sheet_year(value.year):
            return date(value.year, value.month, 1)
        return None
    if isinstance(value, date):
        if plausible_sheet_year(value.year):
            return date(value.year, value.month, 1)
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            serial = float(value)
            if not 1 <= serial <= 80000:
                return None
            parsed = date(1899, 12, 30) + timedelta(days=serial)
            if plausible_sheet_year(parsed.year):
                return date(parsed.year, parsed.month, 1)
            return None
        except (OverflowError, ValueError, TypeError):
            return None

    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "null", "none", "-"}:
        return None
    iso_date = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if iso_date:
        year, month, _day = map(int, iso_date.groups())
        if plausible_sheet_year(year) and 1 <= month <= 12:
            return date(year, month, 1)
        return None
    month_only = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if month_only:
        year, month = map(int, month_only.groups())
        if plausible_sheet_year(year) and 1 <= month <= 12:
            return date(year, month, 1)
        return None
    try:
        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
        if pd.isna(parsed):
            parsed = pd.to_datetime(text, errors="coerce", dayfirst=False)
        if pd.isna(parsed) or not plausible_sheet_year(parsed.year):
            return None
        return date(parsed.year, parsed.month, 1)
    except (OverflowError, ValueError, TypeError):
        return None


def normalise_value(value: object) -> float | None:
    """Normalise spreadsheet/source numeric values for comparison."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if pd.notna(value) else None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "null", "none", "-"}:
        return None
    text = text.replace(",", "").replace("%", "")
    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def analysis_value(value: object) -> float | None:
    """Return an analysis-tab value rounded deterministically to five decimals."""
    normalised = normalise_value(value)
    if normalised is None:
        return None
    return float(
        Decimal(str(normalised)).quantize(
            ANALYSIS_VALUE_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    )


def normalise_header_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def get_sheet_layout(
    values: list[list[object]],
    indicator_name: str | None = None,
) -> dict:
    """Validate the fixed indicator-sheet layout: column A date, column B value."""
    if not values:
        return {"date_col": 0, "value_col": 1, "header_row": 0}

    first_row = [normalise_header_name(item) for item in values[0]]
    if first_row and first_row[0] not in {
        "date",
        "observation date",
        "month",
        "month-end date",
        "month end date",
        "period",
    }:
        raise RuntimeError("Column A header is not a recognised date header.")
    if len(first_row) < 2 or not first_row[1]:
        raise RuntimeError("Column B header is missing.")
    return {"date_col": 0, "value_col": 1, "header_row": 0}


def read_existing_indicator_data(
    values: list[list[object]], layout: dict
) -> dict[date, dict]:
    """Read existing dates from column A and values from column B only."""
    existing: dict[date, dict] = {}
    for row_index, row in enumerate(values[1:], start=2):
        raw_date = row[0] if row else None
        observed = normalise_observation_date(raw_date)
        if observed is None:
            continue
        raw_value = row[1] if len(row) > 1 else None
        existing[observed] = {
            "row": row_index,
            "value": normalise_value(raw_value),
            "raw_value": raw_value,
        }
    return existing


def monthly_records_for_indicator(records: list[dict]) -> dict[date, float]:
    """Aggregate incoming records by source month using their declared method."""
    latest_by_day: dict[date, dict] = {}
    for record in records:
        observed = record.get("observation_date")
        value = normalise_value(record.get("value"))
        if not isinstance(observed, date) or value is None:
            continue
        day_key = observed
        current = latest_by_day.get(day_key)
        if current is None or (
            record.get("release_date", date.min),
            record.get("realtime_start", date.min) or date.min,
        ) >= (
            current.get("release_date", date.min),
            current.get("realtime_start", date.min) or date.min,
        ):
            latest_by_day[day_key] = {**record, "value": value}

    grouped: dict[date, list[dict]] = {}
    for record in latest_by_day.values():
        month_key = normalise_observation_date(record["observation_date"])
        if month_key is not None:
            grouped.setdefault(month_key, []).append(record)

    aggregated: dict[date, float] = {}
    for month_key, month_records in grouped.items():
        methods = {
            str(record.get("monthly_aggregation", "latest")) for record in month_records
        }
        if len(methods) != 1:
            raise RuntimeError("Monthly aggregation received conflicting methods.")
        method = next(iter(methods))
        if method == "mean":
            aggregated[month_key] = sum(
                float(record["value"]) for record in month_records
            ) / len(month_records)
        elif method == "latest":
            latest = max(
                month_records,
                key=lambda record: (
                    record["observation_date"],
                    record.get("release_date", date.min),
                ),
            )
            aggregated[month_key] = float(latest["value"])
        else:
            raise RuntimeError(f"Unsupported monthly aggregation: {method}")
    return aggregated


def uses_sparse_source_dates(records: list[dict]) -> bool:
    """Return true for sources whose valid output dates are not a rolling monthly window."""
    if not records:
        return False
    first = records[0]
    if first.get("source") == "IMF DataMapper FPP":
        return True
    if first.get("source") != "FRED / ALFRED":
        return False
    frequency = str(first.get("native_frequency", ""))
    return frequency.startswith(SPARSE_FRED_FREQUENCY_PREFIXES)


def has_complete_source_history(records: list[dict]) -> bool:
    """Return true only when the fetch path proved it supplied full history."""
    if not records:
        return False
    # A provider can return a syntactically valid but partial response. Never
    # infer destructively safe coverage from its name alone. Limited-history
    # sources such as ISM and Michigan are never authoritative for deletion.
    complete_sources = {
        "FRED / ALFRED",
        "IMF DataMapper FPP",
        "Federal Reserve Bank of New York SCE",
    }
    return records[0].get("source") in complete_sources and all(
        record.get("history_complete") is True for record in records
    )


def output_month_records_for_indicator(records: list[dict]) -> dict[date, float]:
    """Use the same month-end point-in-time values as the trusted combined output."""
    if uses_sparse_source_dates(records):
        rows: dict[date, float] = {}
        for record in records:
            selection_date = record.get("selection_date")
            value = analysis_value(record.get("value"))
            if isinstance(selection_date, date) and value is not None:
                rows[month_start(selection_date)] = value
        return rows

    rows: dict[date, float] = {}
    if FULL_HISTORY_MODE:
        row_months = sorted(
            {
                month_start(record.get("selection_date", record["observation_date"]))
                for record in records
                if isinstance(record.get("observation_date"), date)
                and isinstance(
                    record.get("selection_date", record.get("release_date")),
                    date,
                )
            }
        )
    else:
        row_months = recent_month_starts(CURRENT_OUTPUT_MONTHS)
    for row_month in row_months:
        row_date = month_end(row_month)
        selected = latest_available_record(records, row_date)
        if selected is None:
            continue
        value = analysis_value(selected.get("value"))
        if value is not None:
            rows[month_start(row_date)] = value
    return rows


def sheet_rows_for_indicator(
    indicator_name: str,
    records: list[dict],
) -> dict[date, float | None]:
    """Return source rows plus explicit blanks for unavailable official releases."""
    rows: dict[date, float | None] = dict(output_month_records_for_indicator(records))
    for output_month in _EXPLICIT_BLANK_OUTPUT_MONTHS.get(indicator_name, set()):
        rows[output_month] = None
    return rows


def analysis_source_columns() -> dict[str, tuple[str, int]]:
    """Map every standalone-table indicator to its raw analysis-tab column."""
    columns = {
        indicator.name: (indicator.name, 1)
        for indicator in INDICATORS
        if has_dedicated_indicator_tab(indicator.name)
    }
    for indicator_name, target in CENTRAL_BANK_LIQUIDITY_COMPONENTS.items():
        columns[indicator_name] = (CENTRAL_BANK_LIQUIDITY_SHEET, target["column"])
    coverage_sheet = IMF_COVERAGE_SHEET
    columns["Government revenue % GDP"] = (coverage_sheet, 1)
    columns["Interest paid on public debt % GDP"] = (coverage_sheet, 2)
    columns[coverage_sheet] = (coverage_sheet, 3)
    return columns


def wide_indicator_header(indicator_name: str) -> str:
    """Use readable standalone headers without changing analysis-tab names."""
    if indicator_name in CENTRAL_BANK_LIQUIDITY_COMPONENTS:
        header = CENTRAL_BANK_LIQUIDITY_COMPONENTS[indicator_name]["header"]
        return header.replace("Cental Bank", "Central Bank")
    return indicator_name


def analysis_input_ranges() -> list[str]:
    """Return the smallest analysis ranges needed to rebuild the wide table."""
    widest_by_sheet: dict[str, int] = {}
    for sheet_name, column_index in analysis_source_columns().values():
        widest_by_sheet[sheet_name] = max(
            widest_by_sheet.get(sheet_name, 0), column_index
        )
    return [
        f"{quote_sheet_name(sheet_name)}!A:{column_letter(column_index)}"
        for sheet_name, column_index in widest_by_sheet.items()
    ]


def analysis_number_format_requests(metadata: dict) -> list[dict]:
    """Format every numeric analysis input column to exactly five decimals."""
    sheet_ids = {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in metadata.get("sheets", [])
    }
    requests = []
    for sheet_name in EXPECTED_ANALYSIS_SHEET_ORDER:
        sheet_id = sheet_ids.get(sheet_name)
        if sheet_id is None:
            continue
        end_column_index = (
            4 if sheet_name in {CENTRAL_BANK_LIQUIDITY_SHEET, IMF_COVERAGE_SHEET} else 2
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": 1,
                        "endColumnIndex": end_column_index,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "NUMBER",
                                "pattern": "0.00000",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )
    return requests


def merge_analysis_values_into_wide_table(
    range_values: dict[str, list[list[object]]],
    fresh_data: list[tuple[str, list[dict]]] | None = None,
) -> list[list[object]]:
    """Build one continuous month-end table from analysis inputs plus fresh data."""
    by_indicator: dict[str, dict[date, float]] = {
        indicator.name: {} for indicator in INDICATORS
    }
    for indicator_name, (sheet_name, column_index) in analysis_source_columns().items():
        rows = range_values.get(sheet_name, [])
        for row in rows[1:]:
            observed = normalise_observation_date(row[0] if row else None)
            raw_value = row[column_index] if len(row) > column_index else None
            value = normalise_value(raw_value)
            if observed is not None and value is not None:
                by_indicator[indicator_name][observed] = value

    for indicator_name, records in fresh_data or []:
        if indicator_name in by_indicator:
            # Source omission is not evidence that an older workbook value is
            # wrong. Preserve inaccessible history and remove only cells that
            # were separately verified as invalid.
            for observed in _VERIFIED_INVALID_EXISTING_MONTHS.get(
                indicator_name, set()
            ):
                by_indicator[indicator_name].pop(observed, None)
        sparse_rule = sparse_date_rule_for_records(records)
        if sparse_rule is not None and indicator_name in by_indicator:
            by_indicator[indicator_name] = {
                observed: value
                for observed, value in by_indicator[indicator_name].items()
                if sparse_date_is_valid(observed, sparse_rule)
            }
        for observed, value in sheet_rows_for_indicator(
            indicator_name, records
        ).items():
            if indicator_name in by_indicator and value is None:
                by_indicator[indicator_name].pop(observed, None)
            elif indicator_name in by_indicator:
                by_indicator[indicator_name][observed] = value

    populated_dates = sorted(
        set().union(*(values.keys() for values in by_indicator.values()))
    )
    headers = [
        "Date",
        *[wide_indicator_header(indicator.name) for indicator in INDICATORS],
    ]
    if not populated_dates:
        return [headers]

    current = min(populated_dates)
    final = max(populated_dates)
    month_keys = []
    while current <= final:
        month_keys.append(current)
        current = add_months(current, 1)
    rows = [
        [
            (month_end(month_key) - date(1899, 12, 30)).days,
            *[
                by_indicator[indicator.name].get(month_key, "")
                for indicator in INDICATORS
            ],
        ]
        for month_key in month_keys
    ]
    return [headers, *rows]


def preserve_inaccessible_wide_history(
    table_values: list[list[object]],
    existing_values: list[list[object]],
    fresh_data: list[tuple[str, list[dict]]] | None,
) -> list[list[object]]:
    """Carry forward old wide cells unless a specific cleanup rule rejects them."""
    if not table_values or not existing_values:
        return table_values
    if [normalise_header_name(item) for item in table_values[0]] != [
        normalise_header_name(item) for item in existing_values[0]
    ]:
        return table_values

    fresh_by_indicator = dict(fresh_data or [])
    indicator_by_column = {
        wide_indicator_header(indicator.name): indicator.name
        for indicator in INDICATORS
    }
    rows_by_month: dict[date, list[object]] = {}
    for row in table_values[1:]:
        observed = normalise_observation_date(row[0] if row else None)
        if observed is not None:
            rows_by_month[observed] = list(row) + [""] * (
                len(table_values[0]) - len(row)
            )

    for old_row in existing_values[1:]:
        observed = normalise_observation_date(old_row[0] if old_row else None)
        if observed is None:
            continue
        row = rows_by_month.setdefault(
            observed,
            [
                (month_end(observed) - date(1899, 12, 30)).days,
                *([""] * (len(table_values[0]) - 1)),
            ],
        )
        for column_index, header in enumerate(table_values[0][1:], start=1):
            if column_index >= len(old_row):
                continue
            old_value = normalise_value(old_row[column_index])
            new_value = normalise_value(row[column_index])
            if old_value is None or new_value is not None:
                continue
            indicator_name = indicator_by_column.get(str(header))
            if indicator_name is None:
                continue
            if observed in _VERIFIED_INVALID_EXISTING_MONTHS.get(indicator_name, set()):
                continue
            if observed in _EXPLICIT_BLANK_OUTPUT_MONTHS.get(indicator_name, set()):
                continue
            records = fresh_by_indicator.get(indicator_name, [])
            sparse_rule = sparse_date_rule_for_records(records)
            if sparse_rule is not None and not sparse_date_is_valid(
                observed, sparse_rule
            ):
                continue
            row[column_index] = old_row[column_index]

    return [
        table_values[0],
        *[row for _observed, row in sorted(rows_by_month.items())],
    ]


def fetch_analysis_input_values_direct(
    access_token: str,
    analysis_spreadsheet_id: str,
) -> dict[str, list[list[object]]]:
    """Read complete raw-input columns from the existing analysis workbook."""
    base_url = f"{GOOGLE_SHEETS_API_BASE}/{analysis_spreadsheet_id}"
    ranges = analysis_input_ranges()
    query = urlencode(
        [("ranges", item) for item in ranges]
        + [("valueRenderOption", "UNFORMATTED_VALUE")]
    )
    response = google_sheets_request(
        "GET",
        f"{base_url}/values:batchGet?{query}",
        access_token,
    )
    result: dict[str, list[list[object]]] = {}
    for requested_range, value_range in zip(ranges, response.get("valueRanges", [])):
        sheet_name = requested_range.split("!", 1)[0][1:-1].replace("''", "'")
        result[sheet_name] = value_range.get("values", [])
    return result


def ensure_wide_sheet_direct(
    base_url: str,
    access_token: str,
    sheet_name: str,
) -> dict:
    """Resolve the wide tab, reusing a blank default Sheet1 when available."""
    metadata = google_sheets_request("GET", base_url, access_token)
    sheets = metadata.get("sheets", [])
    existing = next(
        (sheet for sheet in sheets if sheet["properties"]["title"] == sheet_name),
        None,
    )
    if existing is not None:
        return existing
    default_sheet = next(
        (sheet for sheet in sheets if sheet["properties"]["title"] == "Sheet1"),
        None,
    )
    if len(sheets) == 1 and default_sheet is not None:
        values = google_sheets_request(
            "GET",
            google_values_url(base_url, "'Sheet1'!A1:Z10"),
            access_token,
        ).get("values", [])
        if not values:
            google_sheets_request(
                "POST",
                f"{base_url}:batchUpdate",
                access_token,
                {
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": default_sheet["properties"]["sheetId"],
                                    "title": sheet_name,
                                },
                                "fields": "title",
                            }
                        }
                    ]
                },
            )
            default_sheet["properties"]["title"] = sheet_name
            return default_sheet
    google_sheets_request(
        "POST",
        f"{base_url}:batchUpdate",
        access_token,
        {"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    )
    metadata = google_sheets_request("GET", base_url, access_token)
    return next(
        sheet
        for sheet in metadata.get("sheets", [])
        if sheet["properties"]["title"] == sheet_name
    )


def sync_wide_indicator_workbook(
    analysis_spreadsheet_id: str,
    wide_spreadsheet_id: str,
    credentials_path: Path,
    sheet_name: str,
    fresh_data: list[tuple[str, list[dict]]] | None = None,
    validate_only: bool = False,
) -> dict[str, object]:
    """Rebuild the standalone wide table without changing analysis-tab structure."""
    if analysis_spreadsheet_id == wide_spreadsheet_id:
        raise RuntimeError("Wide and analysis spreadsheet IDs must be different.")
    access_token = get_service_account_access_token(credentials_path)
    range_values = fetch_analysis_input_values_direct(
        access_token,
        analysis_spreadsheet_id,
    )
    table_values = merge_analysis_values_into_wide_table(range_values, fresh_data)
    base_url = f"{GOOGLE_SHEETS_API_BASE}/{wide_spreadsheet_id}"
    metadata = google_sheets_request("GET", base_url, access_token)
    existing_sheet = next(
        (
            sheet
            for sheet in metadata.get("sheets", [])
            if sheet["properties"]["title"] == sheet_name
        ),
        None,
    )
    old_values: list[list[object]] = []
    if existing_sheet is not None:
        old_values = google_sheets_request(
            "GET",
            google_values_url(base_url, quote_sheet_name(sheet_name)),
            access_token,
        ).get("values", [])
        table_values = preserve_inaccessible_wide_history(
            table_values,
            old_values,
            fresh_data,
        )
    summary = {
        "spreadsheet_id": wide_spreadsheet_id,
        "sheet_name": sheet_name,
        "rows": max(0, len(table_values) - 1),
        "columns": len(table_values[0]),
        "first_date": "",
        "last_date": "",
    }
    if len(table_values) > 1:
        summary["first_date"] = format_output_month(
            date(1899, 12, 30) + timedelta(days=int(table_values[1][0]))
        )
        summary["last_date"] = format_output_month(
            date(1899, 12, 30) + timedelta(days=int(table_values[-1][0]))
        )

    if existing_sheet is not None and not FULL_HISTORY_MODE:
        fills, appends, merged_values = prepare_incremental_table_changes(
            old_values,
            table_values,
            f"Standalone sheet {sheet_name!r}",
        )
        summary["rows"] = max(0, len(merged_values) - 1)
        if len(merged_values) > 1:
            summary["first_date"] = format_output_month(
                date(1899, 12, 30)
                + timedelta(days=int(merged_values[1][0]))
            )
            summary["last_date"] = format_output_month(
                date(1899, 12, 30)
                + timedelta(days=int(merged_values[-1][0]))
            )
        if not fills and not appends:
            return summary
        if validate_only:
            return summary

        properties = existing_sheet["properties"]
        sheet_id = properties["sheetId"]
        grid = properties.get("gridProperties", {})
        required_rows = len(merged_values)
        required_columns = len(merged_values[0])
        if (
            required_rows > int(grid.get("rowCount", 0))
            or required_columns > int(grid.get("columnCount", 0))
        ):
            google_sheets_request(
                "POST",
                f"{base_url}:batchUpdate",
                access_token,
                {
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id,
                                    "gridProperties": {
                                        "rowCount": max(
                                            required_rows,
                                            int(grid.get("rowCount", 0)),
                                        ),
                                        "columnCount": max(
                                            required_columns,
                                            int(grid.get("columnCount", 0)),
                                        ),
                                    },
                                },
                                "fields": "gridProperties(rowCount,columnCount)",
                            }
                        }
                    ]
                },
            )

        read_url = google_values_url(base_url, quote_sheet_name(sheet_name))
        current_values = google_sheets_request("GET", read_url, access_token).get(
            "values", []
        )
        if current_values != old_values:
            raise RuntimeError(
                f"Standalone sheet {sheet_name!r} changed after preflight; refusing stale incremental write."
            )

        if old_values:
            data_ranges = incremental_table_value_ranges(
                sheet_name,
                fills,
                appends,
                len(old_values),
            )
            google_sheets_request(
                "POST",
                f"{base_url}/values:batchUpdate?valueInputOption=RAW",
                access_token,
                {"valueInputOption": "RAW", "data": data_ranges},
            )
        else:
            google_sheets_request(
                "PUT",
                f"{base_url}/values/{quote(f'{quote_sheet_name(sheet_name)}!A1', safe='')}?valueInputOption=RAW",
                access_token,
                {"values": merged_values},
            )

        if appends or not old_values:
            format_start = len(old_values) if old_values else 1
            format_end = len(merged_values)
            format_requests = [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": format_start,
                            "endRowIndex": format_end,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "DATE",
                                    "pattern": "dd/mm/yyyy",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": format_start,
                            "endRowIndex": format_end,
                            "startColumnIndex": 1,
                            "endColumnIndex": len(merged_values[0]),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": "0.00000",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
            ]
            if not old_values:
                format_requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(merged_values[0]),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                    "wrapStrategy": "WRAP",
                                    "verticalAlignment": "BOTTOM",
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,wrapStrategy,verticalAlignment)",
                        }
                    }
                )
            google_sheets_request(
                "POST",
                f"{base_url}:batchUpdate",
                access_token,
                {"requests": format_requests},
            )

        written = google_sheets_request("GET", read_url, access_token).get(
            "values", []
        )
        verify_google_sheet_values(merged_values, written)
        return summary

    if existing_sheet is not None:
        validate_historical_table_replacement(
            old_values,
            table_values,
            f"Standalone sheet {sheet_name!r}",
            allow_absent_existing_dates=fresh_data is not None,
            allow_undated_existing_rows=fresh_data is not None,
            allow_blank_replacements=fresh_data is not None,
            allow_value_revisions=fresh_data is not None and FULL_HISTORY_MODE,
            mutable_months=mutable_output_months(),
        )
        if google_sheet_table_is_current(table_values, old_values):
            return summary
    if validate_only:
        return summary

    target_sheet = ensure_wide_sheet_direct(base_url, access_token, sheet_name)
    properties = target_sheet["properties"]
    sheet_id = properties["sheetId"]
    grid = properties.get("gridProperties", {})
    row_count = max(int(grid.get("rowCount", 0)), len(table_values))
    column_count = max(int(grid.get("columnCount", 0)), len(table_values[0]))
    if row_count != grid.get("rowCount") or column_count != grid.get("columnCount"):
        google_sheets_request(
            "POST",
            f"{base_url}:batchUpdate",
            access_token,
            {
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {
                                    "rowCount": row_count,
                                    "columnCount": column_count,
                                },
                            },
                            "fields": "gridProperties(rowCount,columnCount)",
                        }
                    }
                ]
            },
        )

    quoted_sheet = quote_sheet_name(sheet_name)
    range_url = f"{base_url}/values/{quote(quoted_sheet, safe='')}"
    read_url = google_values_url(base_url, quoted_sheet)
    if not old_values:
        old_values = google_sheets_request("GET", read_url, access_token).get(
            "values", []
        )
    validate_historical_table_replacement(
        old_values,
        table_values,
        f"Standalone sheet {sheet_name!r}",
        allow_absent_existing_dates=fresh_data is not None,
        allow_undated_existing_rows=fresh_data is not None,
        allow_blank_replacements=fresh_data is not None,
        allow_value_revisions=fresh_data is not None and FULL_HISTORY_MODE,
        mutable_months=mutable_output_months(),
    )
    try:
        current_values = google_sheets_request("GET", read_url, access_token).get(
            "values", []
        )
        if current_values != old_values:
            raise RuntimeError(
                f"Standalone sheet {sheet_name!r} changed after preflight; refusing stale overwrite."
            )
        google_sheets_request("POST", f"{range_url}:clear", access_token, {})
        google_sheets_request(
            "PUT",
            f"{base_url}/values/{quote(f'{quoted_sheet}!A1', safe='')}?valueInputOption=RAW",
            access_token,
            {"values": table_values},
        )
        written = google_sheets_request("GET", read_url, access_token).get("values", [])
        verify_google_sheet_values(table_values, written)
        google_sheets_request(
            "POST",
            f"{base_url}:batchUpdate",
            access_token,
            {
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(table_values[0]),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                    "wrapStrategy": "WRAP",
                                    "verticalAlignment": "BOTTOM",
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,wrapStrategy,verticalAlignment)",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": len(table_values),
                                "startColumnIndex": 0,
                                "endColumnIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "DATE",
                                        "pattern": "dd/mm/yyyy",
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": len(table_values),
                                "startColumnIndex": 1,
                                "endColumnIndex": len(table_values[0]),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "NUMBER",
                                        "pattern": "0.00000",
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {
                                    "frozenRowCount": 1,
                                    "frozenColumnCount": 1,
                                },
                            },
                            "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
                        }
                    },
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": 0,
                                "endIndex": 1,
                            },
                            "properties": {"pixelSize": 48},
                            "fields": "pixelSize",
                        }
                    },
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": 1,
                            },
                            "properties": {"pixelSize": 105},
                            "fields": "pixelSize",
                        }
                    },
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 1,
                                "endIndex": len(table_values[0]),
                            },
                            "properties": {"pixelSize": 135},
                            "fields": "pixelSize",
                        }
                    },
                ]
            },
        )
    except Exception:
        logging.exception(
            "Wide Google Sheet update failed; restoring its previous contents."
        )
        google_sheets_request("POST", f"{range_url}:clear", access_token, {})
        if old_values:
            google_sheets_request(
                "PUT",
                f"{base_url}/values/{quote(f'{quoted_sheet}!A1', safe='')}?valueInputOption=RAW",
                access_token,
                {"values": old_values},
            )
        raise
    return summary


def prepare_incremental_table_changes(
    existing_values: list[list[object]],
    incoming_values: list[list[object]],
    context: str,
) -> tuple[
    list[tuple[int, int, object]], list[list[object]], list[list[object]]
]:
    """Plan blank-cell fills and newer-row appends without revising stored data."""
    if not incoming_values or not incoming_values[0]:
        raise RuntimeError(f"{context}: incoming table has no header row.")

    headers = list(incoming_values[0])
    width = len(headers)
    if existing_values and [normalise_header_name(value) for value in existing_values[0]] != [
        normalise_header_name(value) for value in headers
    ]:
        raise RuntimeError(f"{context}: existing headers do not match the source table.")

    def rectangular(row: list[object]) -> list[object]:
        return (list(row[:width]) + [""] * width)[:width]

    existing_rows = [rectangular(row) for row in existing_values[1:]]
    incoming_rows = [rectangular(row) for row in incoming_values[1:]]

    existing_by_date: dict[date, tuple[int, list[object]]] = {}
    previous_date: date | None = None
    for row_number, row in enumerate(existing_rows, start=2):
        observed = normalise_observation_date(row[0])
        if observed is None:
            raise RuntimeError(f"{context}: existing row {row_number} has no valid date.")
        if observed in existing_by_date:
            raise RuntimeError(f"{context}: duplicate existing date {observed.isoformat()}.")
        if previous_date is not None and observed <= previous_date:
            raise RuntimeError(f"{context}: existing dates are not strictly increasing.")
        existing_by_date[observed] = (row_number, row)
        previous_date = observed

    incoming_by_date: dict[date, list[object]] = {}
    previous_date = None
    for row_number, row in enumerate(incoming_rows, start=2):
        observed = normalise_observation_date(row[0])
        if observed is None:
            raise RuntimeError(f"{context}: incoming row {row_number} has no valid date.")
        if observed in incoming_by_date:
            raise RuntimeError(f"{context}: duplicate incoming date {observed.isoformat()}.")
        if previous_date is not None and observed <= previous_date:
            raise RuntimeError(f"{context}: incoming dates are not strictly increasing.")
        incoming_by_date[observed] = row
        previous_date = observed

    latest_existing = max(existing_by_date, default=None)
    fills: list[tuple[int, int, object]] = []
    appends: list[list[object]] = []
    merged_rows = [list(row) for row in existing_rows]

    for observed, incoming_row in incoming_by_date.items():
        current = existing_by_date.get(observed)
        if current is None:
            if latest_existing is not None and observed <= latest_existing:
                raise RuntimeError(
                    f"{context}: missing historical date {observed.isoformat()}; "
                    "incremental sync will not insert or reorder history."
                )
            appends.append(incoming_row)
            continue

        row_number, existing_row = current
        merged_row = merged_rows[row_number - 2]
        for column_index in range(1, width):
            new_value = incoming_row[column_index]
            if normalise_value(existing_row[column_index]) is not None:
                continue
            if normalise_value(new_value) is None:
                continue
            fills.append((row_number, column_index, new_value))
            merged_row[column_index] = new_value

    merged = [headers, *merged_rows, *appends]
    return fills, appends, merged


def incremental_table_value_ranges(
    sheet_name: str,
    fills: list[tuple[int, int, object]],
    appends: list[list[object]],
    existing_row_count: int,
) -> list[dict[str, object]]:
    """Turn incremental cell changes into bounded Google Sheets value ranges."""
    quoted_sheet = quote_sheet_name(sheet_name)
    ranges: list[dict[str, object]] = []
    by_row: dict[int, dict[int, object]] = {}
    for row_number, column_index, value in fills:
        by_row.setdefault(row_number, {})[column_index] = value

    for row_number, columns in sorted(by_row.items()):
        ordered = sorted(columns.items())
        start = 0
        while start < len(ordered):
            end = start + 1
            while end < len(ordered) and ordered[end][0] == ordered[end - 1][0] + 1:
                end += 1
            first_column = ordered[start][0]
            last_column = ordered[end - 1][0]
            ranges.append(
                {
                    "range": (
                        f"{quoted_sheet}!{column_letter(first_column)}{row_number}:"
                        f"{column_letter(last_column)}{row_number}"
                    ),
                    "values": [[value for _column, value in ordered[start:end]]],
                }
            )
            start = end

    if appends:
        width = max(len(row) for row in appends)
        padded = [list(row) + [""] * (width - len(row)) for row in appends]
        start_row = existing_row_count + 1
        end_row = start_row + len(padded) - 1
        ranges.append(
            {
                "range": (
                    f"{quoted_sheet}!A{start_row}:"
                    f"{column_letter(width - 1)}{end_row}"
                ),
                "values": padded,
            }
        )
    return ranges


def prepare_sheet_changes(
    existing: dict[date, dict],
    incoming: dict[date, float | None],
) -> tuple[list[tuple[int, date, float | None]], list[tuple[date, float]]]:
    """Fill blank cells and append newer dates; full-history mode may revise values."""
    updates: list[tuple[int, date, float | None]] = []
    appends: list[tuple[date, float]] = []
    latest_existing_date = max(existing, default=None)
    for observed, value in sorted(incoming.items()):
        existing_row = existing.get(observed)
        if value is None:
            if (
                FULL_HISTORY_MODE
                and existing_row is not None
                and existing_row.get("value") is not None
            ):
                updates.append((existing_row["row"], observed, value))
            continue
        if existing_row is not None:
            existing_value = existing_row.get("value")
            if existing_value is None or (
                FULL_HISTORY_MODE
                and not google_values_equivalent(value, existing_value)
            ):
                updates.append((existing_row["row"], observed, value))
            continue
        if latest_existing_date is None or observed > latest_existing_date:
            appends.append((observed, value))
    return updates, appends


def indicator_sheet_name(indicator_name: str, sheet_names: set[str]) -> str | None:
    """Match only the exact indicator tab name."""
    return indicator_name if indicator_name in sheet_names else None


def sync_result_row(
    indicator: str,
    sheet_name: str,
    latest_before: date | None,
    revised: int,
    appended: int,
    latest_after: date | None,
    warning: str = "",
) -> dict:
    return {
        "indicator": indicator,
        "sheet_name": sheet_name,
        "latest_before": latest_before.isoformat() if latest_before else "",
        "revised": revised,
        "appended": appended,
        "latest_after": latest_after.isoformat() if latest_after else "",
        "no_new_value": "yes" if appended == 0 else "no",
        "warning": warning,
    }


def validate_analysis_workbook_topology(
    sheet_names: set[str],
    control_values: list[list[object]],
) -> None:
    """Require the exact tabs and Control Panel order used by the analysis engine."""
    required_tabs = {
        ANALYSIS_CONTROL_PANEL_SHEET,
        *EXPECTED_ANALYSIS_SHEET_ORDER,
    }
    missing_tabs = sorted(required_tabs - sheet_names)
    if missing_tabs:
        raise RuntimeError(
            "Analysis workbook is missing required tabs: " + ", ".join(missing_tabs)
        )

    actual = [
        str(row[0]).strip() if row and row[0] not in (None, "") else ""
        for row in control_values
    ]
    actual.extend([""] * (len(EXPECTED_ANALYSIS_SHEET_ORDER) - len(actual)))
    actual = actual[: len(EXPECTED_ANALYSIS_SHEET_ORDER)]
    expected = list(EXPECTED_ANALYSIS_SHEET_ORDER)
    if actual == expected:
        return

    mismatches = [
        f"A{row_number}: expected={expected_name!r}, actual={actual_name!r}"
        for row_number, (expected_name, actual_name) in enumerate(
            zip(expected, actual), start=3
        )
        if expected_name != actual_name
    ]
    preview = "; ".join(mismatches[:5])
    suffix = f"; plus {len(mismatches) - 5} more" if len(mismatches) > 5 else ""
    raise RuntimeError(
        "Analysis Control Panel does not match the required 37-indicator order "
        f"({preview}{suffix})."
    )


def ensure_google_sheet_client(
    sheets,
    spreadsheet_id: str,
    sheet_name: str,
    allow_legacy_rename: bool = False,
) -> None:
    metadata = sheets.get(spreadsheetId=spreadsheet_id).execute()
    existing = metadata.get("sheets", [])
    if any(sheet["properties"]["title"] == sheet_name for sheet in existing):
        return
    if allow_legacy_rename:
        legacy_sheet = next(
            (
                sheet
                for sheet in existing
                if sheet["properties"]["title"] in LEGACY_SHEET_NAMES
            ),
            None,
        )
        if legacy_sheet is not None:
            sheets.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": legacy_sheet["properties"]["sheetId"],
                                    "title": sheet_name,
                                },
                                "fields": "title",
                            }
                        }
                    ]
                },
            ).execute()
            return
    sheets.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()


def update_google_sheet(
    spreadsheet_id: str,
    credentials_path: Path,
    indicators: list[tuple[str, list[dict]]],
    sheet_name: str,
    metadata_values: list[list[object]],
    validation_values: list[list[object]],
    diagnostic_values: list[list[object]],
) -> None:
    logging.getLogger("google.oauth2._client").setLevel(logging.ERROR)

    try:
        if os.getenv("US_PIPELINE_FORCE_DIRECT_GOOGLE_API") == "1":
            raise ImportError("Direct Google Sheets API selected by pipeline runner.")
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        logging.info(
            "Google client libraries are unavailable; using direct Sheets API fallback."
        )
        update_google_sheet_direct(
            spreadsheet_id,
            credentials_path,
            indicators,
            sheet_name,
            metadata_values,
            validation_values,
            diagnostic_values,
        )
        return

    if not credentials_path.exists():
        raise SystemExit(f"Google credentials file not found: {credentials_path}")

    credentials = Credentials.from_service_account_file(
        credentials_path,
        scopes=GOOGLE_SHEETS_WRITE_SCOPES,
    )
    service = build("sheets", "v4", credentials=credentials)
    sheets = service.spreadsheets()
    tables = {
        sheet_name: sheet_values_for_google(indicators),
        DEFAULT_GOOGLE_STATUS_SHEET_NAME: source_status_values(),
        DEFAULT_GOOGLE_METADATA_SHEET_NAME: metadata_values,
        DEFAULT_GOOGLE_VALIDATION_SHEET_NAME: validation_values,
        DEFAULT_GOOGLE_DIAGNOSTIC_SHEET_NAME: diagnostic_values,
    }
    for table_name in tables:
        ensure_google_sheet_client(
            sheets,
            spreadsheet_id,
            table_name,
            allow_legacy_rename=table_name == sheet_name,
        )

    old_tables = {
        table_name: (
            sheets.values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{table_name}'")
            .execute()
            .get("values", [])
        )
        for table_name in tables
    }
    try:
        for table_name, table_values in tables.items():
            sheets.values().clear(
                spreadsheetId=spreadsheet_id,
                range=f"'{table_name}'",
                body={},
            ).execute()
            sheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{table_name}'!A1",
                valueInputOption="RAW",
                body={"values": table_values},
            ).execute()
            written = (
                sheets.values()
                .get(spreadsheetId=spreadsheet_id, range=f"'{table_name}'")
                .execute()
                .get("values", [])
            )
            verify_google_sheet_values(table_values, written)
    except Exception:
        logging.exception(
            "Google Sheets update failed; restoring the previous sheet contents."
        )
        for table_name, old_values in old_tables.items():
            sheets.values().clear(
                spreadsheetId=spreadsheet_id,
                range=f"'{table_name}'",
                body={},
            ).execute()
            if old_values:
                sheets.values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{table_name}'!A1",
                    valueInputOption="RAW",
                    body={"values": old_values},
                ).execute()
        raise


def base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_with_openssl(private_key: str, signing_input: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
    signature = key.sign(
        signing_input.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def get_service_account_access_token(credentials_path: Path) -> str:
    if not credentials_path.exists():
        raise SystemExit(f"Google credentials file not found: {credentials_path}")

    credentials = json.loads(credentials_path.read_text())
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": credentials["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": GOOGLE_TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = ".".join(
        [
            base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            base64url_encode(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            ),
        ]
    )
    assertion = f"{signing_input}.{sign_with_openssl(credentials['private_key'], signing_input)}"
    response = urlopen(
        GOOGLE_TOKEN_URL,
        data=urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode("utf-8"),
        timeout=30,
    )
    token_response = json.loads(response.read().decode("utf-8"))
    return token_response["access_token"]


def google_sheets_request(
    method: str,
    url: str,
    access_token: str,
    body: dict | None = None,
) -> dict:
    import requests

    response = None
    for attempt in range(1, GOOGLE_REQUEST_MAX_ATTEMPTS + 1):
        try:
            response = requests.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=(
                    GOOGLE_REQUEST_CONNECT_TIMEOUT_SECONDS,
                    GOOGLE_REQUEST_READ_TIMEOUT_SECONDS,
                ),
            )
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= GOOGLE_REQUEST_MAX_ATTEMPTS:
                raise
            logging.warning(
                "Google Sheets request failed transiently; retrying (%s/%s).",
                attempt,
                GOOGLE_REQUEST_MAX_ATTEMPTS,
            )
            sleep(min(30, 2**attempt))
            continue
        if response.status_code < 400:
            return response.json() if response.text else {}
        retryable = response.status_code in {429, 500, 502, 503, 504} or (
            response.status_code == 404 and method == "GET"
        )
        if not retryable or attempt == GOOGLE_REQUEST_MAX_ATTEMPTS:
            break
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            delay = float(retry_after)
        elif response.status_code == 429:
            # Per-user Sheets read quotas reset on a minute boundary. Short
            # exponential retries only exhaust all attempts inside the same
            # quota window.
            delay = 60.0
        else:
            delay = min(30, 2**attempt)
        sleep(delay)
    assert response is not None
    raise RuntimeError(
        f"Google Sheets returned {response.status_code}: {response.text}"
    )


def google_values_url(base_url: str, range_name: str) -> str:
    """Build a Sheets values URL that reads actual stored values, not display text."""
    return (
        f"{base_url}/values/{quote(range_name, safe='')}"
        "?valueRenderOption=UNFORMATTED_VALUE"
    )


def ensure_google_sheet_direct(
    base_url: str,
    access_token: str,
    sheet_name: str,
    allow_legacy_rename: bool = False,
) -> None:
    metadata = google_sheets_request("GET", base_url, access_token)
    sheets = metadata.get("sheets", [])
    if any(sheet["properties"]["title"] == sheet_name for sheet in sheets):
        return
    if allow_legacy_rename:
        legacy_sheet = next(
            (
                sheet
                for sheet in sheets
                if sheet["properties"]["title"] in LEGACY_SHEET_NAMES
            ),
            None,
        )
        if legacy_sheet is not None:
            google_sheets_request(
                "POST",
                f"{base_url}:batchUpdate",
                access_token,
                {
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": legacy_sheet["properties"]["sheetId"],
                                    "title": sheet_name,
                                },
                                "fields": "title",
                            }
                        }
                    ]
                },
            )
            return
    google_sheets_request(
        "POST",
        f"{base_url}:batchUpdate",
        access_token,
        {"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    )


def update_google_sheet_direct(
    spreadsheet_id: str,
    credentials_path: Path,
    indicators: list[tuple[str, list[dict]]],
    sheet_name: str,
    metadata_values: list[list[object]],
    validation_values: list[list[object]],
    diagnostic_values: list[list[object]],
) -> None:
    access_token = get_service_account_access_token(credentials_path)
    base_url = f"{GOOGLE_SHEETS_API_BASE}/{spreadsheet_id}"
    tables = {
        sheet_name: sheet_values_for_google(indicators),
        DEFAULT_GOOGLE_STATUS_SHEET_NAME: source_status_values(),
        DEFAULT_GOOGLE_METADATA_SHEET_NAME: metadata_values,
        DEFAULT_GOOGLE_VALIDATION_SHEET_NAME: validation_values,
        DEFAULT_GOOGLE_DIAGNOSTIC_SHEET_NAME: diagnostic_values,
    }
    for table_name in tables:
        ensure_google_sheet_direct(
            base_url,
            access_token,
            table_name,
            allow_legacy_rename=table_name == sheet_name,
        )
    range_urls = {
        table_name: f"{base_url}/values/{quote(repr(table_name), safe='')}"
        for table_name in tables
    }
    old_tables = {
        table_name: google_sheets_request(
            "GET",
            range_urls[table_name],
            access_token,
        ).get("values", [])
        for table_name in tables
    }
    try:
        for table_name, table_values in tables.items():
            range_url = range_urls[table_name]
            google_sheets_request("POST", f"{range_url}:clear", access_token, {})
            google_sheets_request(
                "PUT",
                f"{base_url}/values/"
                f"{quote(f'{table_name!r}!A1', safe='')}?valueInputOption=RAW",
                access_token,
                {"values": table_values},
            )
            written = google_sheets_request(
                "GET",
                range_url,
                access_token,
            ).get("values", [])
            verify_google_sheet_values(table_values, written)
    except Exception:
        logging.exception(
            "Google Sheets update failed; restoring the previous sheet contents."
        )
        for table_name, old_values in old_tables.items():
            range_url = range_urls[table_name]
            google_sheets_request("POST", f"{range_url}:clear", access_token, {})
            if old_values:
                google_sheets_request(
                    "PUT",
                    f"{base_url}/values/"
                    f"{quote(f'{table_name!r}!A1', safe='')}?valueInputOption=RAW",
                    access_token,
                    {"values": old_values},
                )
        raise


def sync_indicator_sheet_with_service(
    sheets,
    spreadsheet_id: str,
    sheet_name: str,
    indicator_name: str,
    records: list[dict],
    validate_only: bool = False,
) -> dict:
    """Append newer observations to one indicator's A:B tab without rewriting history."""
    values = (
        sheets.values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:B",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
        .get("values", [])
    )
    layout = get_sheet_layout(values, indicator_name)
    existing = read_existing_indicator_data(values, layout)
    incoming = sheet_rows_for_indicator(indicator_name, records)
    if not incoming:
        return sync_result_row(
            indicator_name,
            sheet_name,
            max(existing, default=None),
            0,
            0,
            max(existing, default=None),
            "No valid source observation available; left unchanged.",
        )
    validate_overlapping_indicator_values(
        existing,
        incoming,
        f"Indicator sheet {sheet_name!r}",
        allow_value_revisions=FULL_HISTORY_MODE,
        mutable_months=mutable_output_months(),
    )

    if FULL_HISTORY_MODE:
        verified_invalid = _VERIFIED_INVALID_EXISTING_MONTHS.get(indicator_name, set())
        rebuild_rows = {
            observed: item["value"]
            for observed, item in existing.items()
            if item.get("value") is not None and observed not in verified_invalid
        }
        rebuild_rows.update(incoming)
        header = [values[0][0], values[0][1]]
        table_values = [
            header,
            *[
                [format_output_month(observed), value]
                for observed, value in sorted(rebuild_rows.items())
            ],
        ]
        validate_historical_table_replacement(
            values,
            table_values,
            f"Indicator sheet {sheet_name!r}",
            allow_absent_existing_dates=bool(verified_invalid),
            allow_value_revisions=True,
            mutable_months=mutable_output_months(),
        )
        if indicator_input_table_is_current(values, table_values):
            return sync_result_row(
                indicator_name,
                sheet_name,
                max(existing, default=None),
                0,
                0,
                max(rebuild_rows, default=None),
            )
        if validate_only:
            return sync_result_row(
                indicator_name,
                sheet_name,
                max(existing, default=None),
                len(rebuild_rows),
                0,
                max(rebuild_rows, default=None),
                "DRY RUN: would rebuild the reconciled A:B history.",
            )
        range_name = f"{quote_sheet_name(sheet_name)}!A:B"
        old_values = values
        try:
            current_values = (
                sheets.values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    valueRenderOption="UNFORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
            if current_values != old_values:
                raise RuntimeError(
                    f"Indicator sheet {sheet_name!r} changed after preflight; refusing stale overwrite."
                )
            sheets.values().clear(
                spreadsheetId=spreadsheet_id, range=range_name, body={}
            ).execute()
            sheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(sheet_name)}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": table_values},
            ).execute()
            written = (
                sheets.values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    valueRenderOption="UNFORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
            verify_google_sheet_values(table_values, written)
        except Exception:
            sheets.values().clear(
                spreadsheetId=spreadsheet_id, range=range_name, body={}
            ).execute()
            if old_values:
                sheets.values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{quote_sheet_name(sheet_name)}!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": old_values},
                ).execute()
            raise
        from notifications.changes import record, compare

        dates = sorted(set(existing) | set(rebuild_rows))
        before = [[existing.get(d, {}).get("value")] for d in dates]
        after = [[rebuild_rows.get(d)] for d in dates]
        record("Source data", indicator_name, **compare(before, after))
        return sync_result_row(
            indicator_name,
            sheet_name,
            max(existing, default=None),
            len(rebuild_rows),
            0,
            max(rebuild_rows, default=None),
            "Rebuilt reconciled A:B history.",
        )

    fills, appends = prepare_sheet_changes(existing, incoming)
    latest_before = max(existing, default=None)
    latest_after = max(set(existing) | set(incoming), default=None)
    if not fills and not appends:
        return sync_result_row(
            indicator_name,
            sheet_name,
            latest_before,
            0,
            0,
            latest_before,
            "No newer source observation available; left unchanged.",
        )

    if validate_only:
        preview = ", ".join(
            f"{format_output_month(observed)}={value}" for observed, value in appends
        )
        fill_preview = ", ".join(
            f"row {row} {format_output_month(observed)}={value}"
            for row, observed, value in fills
        )
        pieces = []
        if fill_preview:
            pieces.append(f"update cells {fill_preview}")
        if preview:
            pieces.append(f"append {preview}")
        return sync_result_row(
            indicator_name,
            sheet_name,
            latest_before,
            len(fills),
            len(appends),
            latest_after,
            "DRY RUN: would " + "; ".join(pieces) + ".",
        )

    if fills:
        value_col_letter = column_letter(1)
        sheets.values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {
                        "range": f"{quote_sheet_name(sheet_name)}!{value_col_letter}{row_number}",
                        "values": [[value]],
                    }
                    for row_number, _observed, value in fills
                ],
            },
        ).execute()

    rows = [[format_output_month(observed), value] for observed, value in appends]
    if rows:
        sheets.values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:B",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    return sync_result_row(
        indicator_name,
        sheet_name,
        latest_before,
        len(fills),
        len(appends),
        latest_after,
    )


def sync_indicator_sheet_direct(
    base_url: str,
    access_token: str,
    sheet_name: str,
    indicator_name: str,
    records: list[dict],
    validate_only: bool = False,
) -> dict:
    range_name = quote_sheet_name(sheet_name)
    values = google_sheets_request(
        "GET",
        google_values_url(base_url, f"{range_name}!A:B"),
        access_token,
    ).get("values", [])
    layout = get_sheet_layout(values, indicator_name)
    existing = read_existing_indicator_data(values, layout)
    incoming = sheet_rows_for_indicator(indicator_name, records)
    if not incoming:
        return sync_result_row(
            indicator_name,
            sheet_name,
            max(existing, default=None),
            0,
            0,
            max(existing, default=None),
            "No valid source observation available; left unchanged.",
        )
    validate_overlapping_indicator_values(
        existing,
        incoming,
        f"Indicator sheet {sheet_name!r}",
        allow_value_revisions=FULL_HISTORY_MODE,
        mutable_months=mutable_output_months(),
    )

    if FULL_HISTORY_MODE:
        verified_invalid = _VERIFIED_INVALID_EXISTING_MONTHS.get(indicator_name, set())
        rebuild_rows = {
            observed: item["value"]
            for observed, item in existing.items()
            if item.get("value") is not None and observed not in verified_invalid
        }
        rebuild_rows.update(incoming)
        header = [values[0][0], values[0][1]]
        table_values = [
            header,
            *[
                [format_output_month(observed), value]
                for observed, value in sorted(rebuild_rows.items())
            ],
        ]
        validate_historical_table_replacement(
            values,
            table_values,
            f"Indicator sheet {sheet_name!r}",
            allow_absent_existing_dates=bool(verified_invalid),
            allow_value_revisions=True,
            mutable_months=mutable_output_months(),
        )
        if indicator_input_table_is_current(values, table_values):
            return sync_result_row(
                indicator_name,
                sheet_name,
                max(existing, default=None),
                0,
                0,
                max(rebuild_rows, default=None),
            )
        if validate_only:
            return sync_result_row(
                indicator_name,
                sheet_name,
                max(existing, default=None),
                len(rebuild_rows),
                0,
                max(rebuild_rows, default=None),
                "DRY RUN: would rebuild the reconciled A:B history.",
            )
        clear_url = f"{base_url}/values/" f"{quote(f'{range_name}!A:B', safe='')}:clear"
        old_values = values
        try:
            current_values = google_sheets_request(
                "GET", google_values_url(base_url, f"{range_name}!A:B"), access_token
            ).get("values", [])
            if current_values != old_values:
                raise RuntimeError(
                    f"Indicator sheet {sheet_name!r} changed after preflight; refusing stale overwrite."
                )
            google_sheets_request("POST", clear_url, access_token, {})
            google_sheets_request(
                "PUT",
                f"{base_url}/values/{quote(f'{range_name}!A1', safe='')}?valueInputOption=USER_ENTERED",
                access_token,
                {"values": table_values},
            )
            written = google_sheets_request(
                "GET", google_values_url(base_url, f"{range_name}!A:B"), access_token
            ).get("values", [])
            verify_google_sheet_values(table_values, written)
        except Exception:
            google_sheets_request("POST", clear_url, access_token, {})
            if old_values:
                google_sheets_request(
                    "PUT",
                    f"{base_url}/values/{quote(f'{range_name}!A1', safe='')}?valueInputOption=USER_ENTERED",
                    access_token,
                    {"values": old_values},
                )
            raise
        from notifications.changes import record, compare

        dates = sorted(set(existing) | set(rebuild_rows))
        before = [[existing.get(d, {}).get("value")] for d in dates]
        after = [[rebuild_rows.get(d)] for d in dates]
        record("Source data", indicator_name, **compare(before, after))
        return sync_result_row(
            indicator_name,
            sheet_name,
            max(existing, default=None),
            len(rebuild_rows),
            0,
            max(rebuild_rows, default=None),
            "Rebuilt reconciled A:B history.",
        )

    fills, appends = prepare_sheet_changes(existing, incoming)
    latest_before = max(existing, default=None)
    latest_after = max(set(existing) | set(incoming), default=None)
    if not fills and not appends:
        return sync_result_row(
            indicator_name,
            sheet_name,
            latest_before,
            0,
            0,
            latest_before,
            "No newer source observation available; left unchanged.",
        )

    if validate_only:
        preview = ", ".join(
            f"{format_output_month(observed)}={value}" for observed, value in appends
        )
        fill_preview = ", ".join(
            f"row {row} {format_output_month(observed)}={value}"
            for row, observed, value in fills
        )
        pieces = []
        if fill_preview:
            pieces.append(f"update cells {fill_preview}")
        if preview:
            pieces.append(f"append {preview}")
        return sync_result_row(
            indicator_name,
            sheet_name,
            latest_before,
            len(fills),
            len(appends),
            latest_after,
            "DRY RUN: would " + "; ".join(pieces) + ".",
        )

    if fills:
        value_col_letter = column_letter(1)
        google_sheets_request(
            "POST",
            f"{base_url}/values:batchUpdate",
            access_token,
            {
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {
                        "range": f"{range_name}!{value_col_letter}{row_number}",
                        "values": [[value]],
                    }
                    for row_number, _observed, value in fills
                ],
            },
        )

    rows = [[format_output_month(observed), value] for observed, value in appends]
    if rows:
        google_sheets_request(
            "POST",
            f"{base_url}/values/{quote(f'{range_name}!A:B', safe='')}:append"
            "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
            access_token,
            {"values": rows},
        )

    return sync_result_row(
        indicator_name,
        sheet_name,
        latest_before,
        len(fills),
        len(appends),
        latest_after,
    )


def central_bank_append_rows(
    values: list[list[object]],
    component_data: dict[str, list[dict]],
) -> tuple[
    date | None, list[tuple[int, int, date, float]], list[list[object]], date | None
]:
    """Prepare blank fills and append-only rows for the shared central-bank tab."""
    if values:
        layout = get_sheet_layout(values, CENTRAL_BANK_LIQUIDITY_SHEET)
    else:
        layout = {"date_col": 0, "value_col": 1, "header_row": 0}
    existing = read_existing_indicator_data(values, layout)
    latest_before = max(existing, default=None)

    existing_rows: dict[date, dict] = {}
    for row_index, row in enumerate(values[1:], start=2):
        observed = normalise_observation_date(row[0] if row else None)
        if observed is not None:
            existing_rows[observed] = {"row": row_index, "values": row}

    fills: list[tuple[int, int, date, float]] = []
    by_month: dict[date, list[object]] = {}
    for indicator_name, records in component_data.items():
        target = CENTRAL_BANK_LIQUIDITY_COMPONENTS[indicator_name]
        for month_key, value in output_month_records_for_indicator(records).items():
            existing_row = existing_rows.get(month_key)
            if existing_row is not None:
                row_values = existing_row["values"]
                raw_value = (
                    row_values[target["column"]]
                    if len(row_values) > target["column"]
                    else None
                )
                existing_value = normalise_value(raw_value)
                if existing_value is not None and not google_values_equivalent(
                    value, existing_value
                ):
                    if FULL_HISTORY_MODE:
                        raise RuntimeError(
                            f"{CENTRAL_BANK_LIQUIDITY_SHEET}: historical overlap does not match for "
                            f"{indicator_name} {month_key.isoformat()}: existing={raw_value!r}, incoming={value!r}."
                        )
                    continue
                if existing_value is None:
                    fills.append(
                        (existing_row["row"], target["column"], month_key, value)
                    )
                continue
            if latest_before is not None and month_key <= latest_before:
                continue
            by_month.setdefault(month_key, [format_output_month(month_key), "", "", ""])
            by_month[month_key][target["column"]] = value

    rows = [row for _month, row in sorted(by_month.items()) if any(row[1:])]
    latest_after = max(
        [latest_before] + sorted(by_month) if latest_before else sorted(by_month),
        default=latest_before,
    )
    return latest_before, fills, rows, latest_after


def central_bank_full_table_values(
    values: list[list[object]],
    component_data: dict[str, list[dict]],
) -> list[list[object]]:
    """Build the complete shared central-bank A:D table from source histories."""
    header = (
        list(values[0][:4])
        if values
        else [
            "Month-End Date",
            *[
                target["header"]
                for target in CENTRAL_BANK_LIQUIDITY_COMPONENTS.values()
            ],
        ]
    )
    header.extend([""] * (4 - len(header)))
    by_month: dict[date, list[object]] = {}
    for row in values[1:]:
        observed = normalise_observation_date(row[0] if row else None)
        if observed is None:
            continue
        preserved = [format_output_month(observed), "", "", ""]
        for column_index in range(1, 4):
            if len(row) > column_index:
                preserved[column_index] = row[column_index]
        by_month[observed] = preserved
    for indicator_name, records in component_data.items():
        target = CENTRAL_BANK_LIQUIDITY_COMPONENTS[indicator_name]
        for month_key, value in output_month_records_for_indicator(records).items():
            row = by_month.setdefault(
                month_key,
                [format_output_month(month_key), "", "", ""],
            )
            row[target["column"]] = value
    return [header, *[row for _month, row in sorted(by_month.items())]]


def sync_central_bank_liquidity_with_service(
    sheets,
    spreadsheet_id: str,
    component_data: dict[str, list[dict]],
    sheet_names: set[str],
    validate_only: bool,
) -> dict:
    sheet_name = CENTRAL_BANK_LIQUIDITY_SHEET
    if sheet_name not in sheet_names:
        return sync_result_row(
            sheet_name,
            "",
            None,
            0,
            0,
            None,
            "Missing matching central-bank liquidity tab; skipped.",
        )
    values = (
        sheets.values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:D",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
        .get("values", [])
    )
    if FULL_HISTORY_MODE:
        complete_history = bool(component_data) and all(
            has_complete_source_history(records) for records in component_data.values()
        )
        table_values = central_bank_full_table_values(values, component_data)
        validate_historical_table_replacement(
            values,
            table_values,
            f"Shared sheet {sheet_name!r}",
            allow_absent_existing_dates=False,
            allow_undated_existing_rows=complete_history,
            allow_value_revisions=complete_history,
            mutable_months=mutable_output_months(),
        )
        latest_before = max(
            read_existing_indicator_data(values, get_sheet_layout(values, sheet_name)),
            default=None,
        )
        latest_after = max(
            (normalise_observation_date(row[0]) for row in table_values[1:]),
            default=None,
        )
        if google_sheet_table_is_current(table_values, values):
            return sync_result_row(
                sheet_name, sheet_name, latest_before, 0, 0, latest_after
            )
        if validate_only:
            return sync_result_row(
                sheet_name,
                sheet_name,
                latest_before,
                len(table_values) - 1,
                0,
                latest_after,
                "DRY RUN: would rebuild the complete shared A:D history.",
            )
        range_name = f"{quote_sheet_name(sheet_name)}!A:D"
        try:
            current_values = (
                sheets.values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    valueRenderOption="UNFORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
            if current_values != values:
                raise RuntimeError(
                    f"Shared sheet {sheet_name!r} changed after preflight; "
                    "refusing stale overwrite."
                )
            sheets.values().clear(
                spreadsheetId=spreadsheet_id, range=range_name, body={}
            ).execute()
            sheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(sheet_name)}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": table_values},
            ).execute()
            written = (
                sheets.values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    valueRenderOption="UNFORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
            verify_google_sheet_values(table_values, written)
        except Exception:
            sheets.values().clear(
                spreadsheetId=spreadsheet_id, range=range_name, body={}
            ).execute()
            if values:
                sheets.values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{quote_sheet_name(sheet_name)}!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": values},
                ).execute()
            raise
        return sync_result_row(
            sheet_name,
            sheet_name,
            latest_before,
            len(table_values) - 1,
            0,
            latest_after,
            "Rebuilt complete shared A:D history.",
        )
    latest_before, fills, rows, latest_after = central_bank_append_rows(
        values, component_data
    )
    if not fills and not rows:
        return sync_result_row(
            sheet_name,
            sheet_name,
            latest_before,
            0,
            0,
            latest_before,
            "No newer source observation available; left unchanged.",
        )
    if validate_only:
        preview = "; ".join(str(row) for row in rows)
        fill_preview = "; ".join(
            f"row {row} {column_letter(col)} {format_output_month(month)}={value}"
            for row, col, month, value in fills
        )
        pieces = []
        if fill_preview:
            pieces.append(f"update cells {fill_preview}")
        if preview:
            pieces.append(f"append {preview}")
        return sync_result_row(
            sheet_name,
            sheet_name,
            latest_before,
            len(fills),
            len(rows),
            latest_after,
            "DRY RUN: would " + "; ".join(pieces) + ".",
        )
    if fills:
        sheets.values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {
                        "range": f"{quote_sheet_name(sheet_name)}!{column_letter(col)}{row}",
                        "values": [[value]],
                    }
                    for row, col, _month, value in fills
                ],
            },
        ).execute()
    if rows:
        sheets.values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:D",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
    return sync_result_row(
        sheet_name,
        sheet_name,
        latest_before,
        len(fills),
        len(rows),
        latest_after,
    )


def sync_central_bank_liquidity_direct(
    base_url: str,
    access_token: str,
    component_data: dict[str, list[dict]],
    sheet_names: set[str],
    validate_only: bool,
) -> dict:
    sheet_name = CENTRAL_BANK_LIQUIDITY_SHEET
    if sheet_name not in sheet_names:
        return sync_result_row(
            sheet_name,
            "",
            None,
            0,
            0,
            None,
            "Missing matching central-bank liquidity tab; skipped.",
        )
    range_name = quote_sheet_name(sheet_name)
    values = google_sheets_request(
        "GET",
        google_values_url(base_url, f"{range_name}!A:D"),
        access_token,
    ).get("values", [])
    if FULL_HISTORY_MODE:
        complete_history = bool(component_data) and all(
            has_complete_source_history(records) for records in component_data.values()
        )
        table_values = central_bank_full_table_values(values, component_data)
        validate_historical_table_replacement(
            values,
            table_values,
            f"Shared sheet {sheet_name!r}",
            allow_absent_existing_dates=False,
            allow_undated_existing_rows=complete_history,
            allow_value_revisions=complete_history,
            mutable_months=mutable_output_months(),
        )
        latest_before = max(
            read_existing_indicator_data(values, get_sheet_layout(values, sheet_name)),
            default=None,
        )
        latest_after = max(
            (normalise_observation_date(row[0]) for row in table_values[1:]),
            default=None,
        )
        if google_sheet_table_is_current(table_values, values):
            return sync_result_row(
                sheet_name, sheet_name, latest_before, 0, 0, latest_after
            )
        if validate_only:
            return sync_result_row(
                sheet_name,
                sheet_name,
                latest_before,
                len(table_values) - 1,
                0,
                latest_after,
                "DRY RUN: would rebuild the complete shared A:D history.",
            )
        full_range = f"{range_name}!A:D"
        clear_url = f"{base_url}/values/{quote(full_range, safe='')}:clear"
        try:
            current_values = google_sheets_request(
                "GET", google_values_url(base_url, full_range), access_token
            ).get("values", [])
            if current_values != values:
                raise RuntimeError(
                    f"Shared sheet {sheet_name!r} changed after preflight; "
                    "refusing stale overwrite."
                )
            google_sheets_request("POST", clear_url, access_token, {})
            google_sheets_request(
                "PUT",
                f"{base_url}/values/{quote(f'{range_name}!A1', safe='')}?valueInputOption=USER_ENTERED",
                access_token,
                {"values": table_values},
            )
            written = google_sheets_request(
                "GET", google_values_url(base_url, full_range), access_token
            ).get("values", [])
            verify_google_sheet_values(table_values, written)
        except Exception:
            google_sheets_request("POST", clear_url, access_token, {})
            if values:
                google_sheets_request(
                    "PUT",
                    f"{base_url}/values/{quote(f'{range_name}!A1', safe='')}?valueInputOption=USER_ENTERED",
                    access_token,
                    {"values": values},
                )
            raise
        return sync_result_row(
            sheet_name,
            sheet_name,
            latest_before,
            len(table_values) - 1,
            0,
            latest_after,
            "Rebuilt complete shared A:D history.",
        )
    latest_before, fills, rows, latest_after = central_bank_append_rows(
        values, component_data
    )
    if not fills and not rows:
        return sync_result_row(
            sheet_name,
            sheet_name,
            latest_before,
            0,
            0,
            latest_before,
            "No newer source observation available; left unchanged.",
        )
    if validate_only:
        preview = "; ".join(str(row) for row in rows)
        fill_preview = "; ".join(
            f"row {row} {column_letter(col)} {format_output_month(month)}={value}"
            for row, col, month, value in fills
        )
        pieces = []
        if fill_preview:
            pieces.append(f"update cells {fill_preview}")
        if preview:
            pieces.append(f"append {preview}")
        return sync_result_row(
            sheet_name,
            sheet_name,
            latest_before,
            len(fills),
            len(rows),
            latest_after,
            "DRY RUN: would " + "; ".join(pieces) + ".",
        )
    if fills:
        google_sheets_request(
            "POST",
            f"{base_url}/values:batchUpdate",
            access_token,
            {
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {
                        "range": f"{range_name}!{column_letter(col)}{row}",
                        "values": [[value]],
                    }
                    for row, col, _month, value in fills
                ],
            },
        )
    if rows:
        google_sheets_request(
            "POST",
            f"{base_url}/values/{quote(f'{range_name}!A:D', safe='')}:append"
            "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
            access_token,
            {"values": rows},
        )
    return sync_result_row(
        sheet_name,
        sheet_name,
        latest_before,
        len(fills),
        len(rows),
        latest_after,
    )


def print_sync_summary(results: list[dict], *, validate_only: bool = False) -> None:
    """Print a concise per-indicator sync summary."""
    if not validate_only:
        from notifications.changes import record

        for item in results:
            warning = str(item.get("warning", ""))
            if warning.startswith("Rebuilt reconciled A:B history"):
                continue  # Exact before/after counts were recorded at the write.
            unknown = bool(
                warning
                and not warning.startswith(("No newer source", "No valid source"))
            )
            record(
                "Source data",
                item["indicator"],
                added=item.get("appended", 0),
                updated=0 if unknown else item.get("revised", 0),
                unknown=unknown,
            )

    if not results:
        print("No indicator sheets were synced.")
        return
    if COMPACT_TERMINAL:
        safe_warnings = (
            "DRY RUN:",
            "No newer source observation",
            "No valid source observation",
            "Rebuilt ",
        )
        problems = [
            item
            for item in results
            if item.get("warning")
            and not str(item["warning"]).startswith(safe_warnings)
        ]
        action = "checked" if validate_only else "synced"
        print(
            f"Indicator sheets: {len(results)} {action}, " f"{len(problems)} problems."
        )
        for item in problems:
            print(f"  WARNING {item['indicator']}: {item['warning']}")
        return
    print_console_table(
        "Indicator sheet sync summary:",
        [
            "Indicator",
            "Sheet",
            "Latest before",
            "Updated cells",
            "Appended",
            "Latest after",
            "No new value",
            "Warning",
        ],
        [
            [
                item["indicator"],
                item["sheet_name"],
                item["latest_before"],
                item["revised"],
                item["appended"],
                item["latest_after"],
                item["no_new_value"],
                item["warning"],
            ]
            for item in results
        ],
    )


def assert_sync_preflight_passed(results: list[dict]) -> None:
    """Turn per-tab safety failures into a pipeline-wide stop before any writes."""
    safe_warnings = (
        "DRY RUN:",
        "No newer source observation",
        "No valid source observation",
        "Rebuilt ",
    )
    failures = [
        f"{item['indicator']}: {item['warning']}"
        for item in results
        if item.get("warning") and not str(item["warning"]).startswith(safe_warnings)
    ]
    if failures:
        raise RuntimeError(
            "Indicator-sheet safety preflight failed; no writes were attempted: "
            + " | ".join(failures)
        )


def sparse_date_rule_for_records(records: list[dict]) -> str | None:
    """Return the expected date rule for sparse source records."""
    if not records:
        return None
    first = records[0]
    if first.get("source") == "IMF DataMapper FPP":
        return "annual"
    if first.get("source") != "FRED / ALFRED":
        return None
    frequency = str(first.get("native_frequency", ""))
    if frequency.startswith("Quarterly"):
        return "quarterly"
    if frequency.startswith("Annual"):
        return "annual"
    return None


def sparse_date_is_valid(observed: date, rule: str) -> bool:
    if rule == "quarterly":
        return observed.month in QUARTER_END_MONTHS
    if rule == "annual":
        return observed.month == ANNUAL_OUTPUT_MONTH
    return True


def sparse_replacement_date(observed: date, rule: str) -> date:
    """Return the canonical row month for an old sparse-source row."""
    if rule == "annual":
        return date(observed.year, ANNUAL_OUTPUT_MONTH, 1)
    if rule == "quarterly":
        quarter_end_month = ((observed.month - 1) // 3 + 1) * 3
        return date(observed.year, quarter_end_month, 1)
    return month_start(observed)


def sparse_cleanup_plan(
    values: list[list[object]],
    records: list[dict],
) -> list[int]:
    """Return sheet row numbers for old sparse rows that use the wrong month."""
    rule = sparse_date_rule_for_records(records)
    if rule is None:
        return []
    incoming_dates = set(output_month_records_for_indicator(records))
    if not incoming_dates:
        return []
    rows_to_delete: list[int] = []
    for row_index, row in enumerate(values[1:], start=2):
        observed = normalise_observation_date(row[0] if row else None)
        if observed is None or sparse_date_is_valid(observed, rule):
            continue
        same_period_replacement = sparse_replacement_date(observed, rule)
        if same_period_replacement in incoming_dates:
            rows_to_delete.append(row_index)
    return rows_to_delete


def cleanup_sparse_date_rows_with_service(
    sheets,
    spreadsheet_id: str,
    workbook_data: list[tuple[str, list[dict]]],
    validate_only: bool,
) -> list[dict]:
    """Delete old off-quarter/off-year-end rows created by earlier sparse-date logic."""
    metadata = sheets.get(spreadsheetId=spreadsheet_id).execute()
    sheet_by_name = {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in metadata.get("sheets", [])
    }
    results: list[dict] = []
    requests: list[dict] = []
    for indicator_name, records in workbook_data:
        if is_central_bank_component(indicator_name) or is_imf_coverage_indicator(
            indicator_name
        ):
            continue
        if sparse_date_rule_for_records(records) is None:
            continue
        sheet_name = indicator_sheet_name(indicator_name, set(sheet_by_name))
        if sheet_name is None:
            continue
        values = (
            sheets.values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(sheet_name)}!A:B",
            )
            .execute()
            .get("values", [])
        )
        rows_to_delete = sparse_cleanup_plan(values, records)
        if not rows_to_delete:
            continue
        for row_number in sorted(rows_to_delete, reverse=True):
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_by_name[sheet_name],
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        }
                    }
                }
            )
        warning = (
            "DRY RUN: would delete sparse misdated rows "
            if validate_only
            else "Deleted sparse misdated rows "
        ) + ", ".join(str(row) for row in rows_to_delete)
        results.append(
            sync_result_row(
                indicator_name,
                sheet_name,
                None,
                len(rows_to_delete),
                0,
                None,
                warning,
            )
        )
    if requests and not validate_only:
        sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    return results


def cleanup_sparse_date_rows_direct(
    base_url: str,
    access_token: str,
    workbook_data: list[tuple[str, list[dict]]],
    validate_only: bool,
) -> list[dict]:
    """Direct Sheets API fallback for sparse-date cleanup."""
    metadata = google_sheets_request("GET", base_url, access_token)
    sheet_by_name = {
        sheet["properties"]["title"]: sheet["properties"]["sheetId"]
        for sheet in metadata.get("sheets", [])
    }
    results: list[dict] = []
    requests: list[dict] = []
    for indicator_name, records in workbook_data:
        if is_central_bank_component(indicator_name) or is_imf_coverage_indicator(
            indicator_name
        ):
            continue
        if sparse_date_rule_for_records(records) is None:
            continue
        sheet_name = indicator_sheet_name(indicator_name, set(sheet_by_name))
        if sheet_name is None:
            continue
        range_name = quote_sheet_name(sheet_name)
        values = google_sheets_request(
            "GET",
            google_values_url(base_url, f"{range_name}!A:B"),
            access_token,
        ).get("values", [])
        rows_to_delete = sparse_cleanup_plan(values, records)
        if not rows_to_delete:
            continue
        for row_number in sorted(rows_to_delete, reverse=True):
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_by_name[sheet_name],
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        }
                    }
                }
            )
        warning = (
            "DRY RUN: would delete sparse misdated rows "
            if validate_only
            else "Deleted sparse misdated rows "
        ) + ", ".join(str(row) for row in rows_to_delete)
        results.append(
            sync_result_row(
                indicator_name,
                sheet_name,
                None,
                len(rows_to_delete),
                0,
                None,
                warning,
            )
        )
    if requests and not validate_only:
        google_sheets_request(
            "POST",
            f"{base_url}:batchUpdate",
            access_token,
            {"requests": requests},
        )
    return results


def cleanup_sparse_date_rows(
    spreadsheet_id: str,
    credentials_path: Path,
    workbook_data: list[tuple[str, list[dict]]],
    validate_only: bool,
) -> list[dict]:
    """Clean old sparse-date rows after the correct quarter/year-end rows exist."""
    if not credentials_path.exists():
        raise SystemExit(f"Google credentials file not found: {credentials_path}")
    try:
        if os.getenv("US_PIPELINE_FORCE_DIRECT_GOOGLE_API") == "1":
            raise ImportError("Direct Google Sheets API selected by pipeline runner.")
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        access_token = get_service_account_access_token(credentials_path)
        base_url = f"{GOOGLE_SHEETS_API_BASE}/{spreadsheet_id}"
        results = cleanup_sparse_date_rows_direct(
            base_url,
            access_token,
            workbook_data,
            validate_only,
        )
    else:
        credentials = Credentials.from_service_account_file(
            credentials_path,
            scopes=GOOGLE_SHEETS_WRITE_SCOPES,
        )
        service = build("sheets", "v4", credentials=credentials)
        results = cleanup_sparse_date_rows_with_service(
            service.spreadsheets(),
            spreadsheet_id,
            workbook_data,
            validate_only,
        )
    if results:
        print("\nSparse-date cleanup summary:")
        print_sync_summary(results, validate_only=validate_only)
    else:
        print("\nSparse-date cleanup summary: no misdated sparse rows found.")
    return results


def central_bank_component_data(
    workbook_data: list[tuple[str, list[dict]]],
) -> dict[str, list[dict]]:
    """Return the source records that belong in the shared liquidity tab."""
    return {
        indicator_name: records
        for indicator_name, records in workbook_data
        if is_central_bank_component(indicator_name)
    }


def dedicated_indicator_data(
    workbook_data: list[tuple[str, list[dict]]],
) -> list[tuple[str, list[dict]]]:
    """Return source records that should sync to one indicator tab each."""
    return [
        (indicator_name, records)
        for indicator_name, records in workbook_data
        if has_dedicated_indicator_tab(indicator_name)
    ]


def imf_coverage_component_data(
    workbook_data: list[tuple[str, list[dict]]],
) -> dict[str, list[dict]]:
    """Return the two official IMF inputs written to the shared coverage tab."""
    return {
        indicator_name: records
        for indicator_name, records in workbook_data
        if indicator_name in IMF_COVERAGE_INPUT_COLUMNS
    }


def imf_coverage_sheet_values(
    component_data: dict[str, list[dict]],
) -> list[list[object]]:
    """Build the complete, sorted A:C raw-input table for the IMF analysis tab."""
    by_indicator = {
        indicator_name: output_month_records_for_indicator(records)
        for indicator_name, records in component_data.items()
    }
    months = sorted(set().union(*(values.keys() for values in by_indicator.values())))
    headers = ["Date", "Government Revenue % GDP", "Government Interest % GDP"]
    return [
        headers,
        *[
            [
                (month_end(month_key) - date(1899, 12, 30)).days,
                by_indicator.get("Government revenue % GDP", {}).get(month_key, ""),
                by_indicator.get("Interest paid on public debt % GDP", {}).get(
                    month_key, ""
                ),
            ]
            for month_key in months
        ],
    ]


def merge_imf_coverage_history(
    existing: list[list[object]],
    incoming: list[list[object]],
) -> list[list[object]]:
    """Preserve legacy annual IMF values while canonicalising dates to year-end."""
    if not incoming:
        raise RuntimeError("Generated IMF coverage table is empty.")
    if existing and [normalise_header_name(item) for item in existing[0]] != [
        normalise_header_name(item) for item in incoming[0]
    ]:
        raise RuntimeError(
            f"Shared sheet {IMF_COVERAGE_SHEET!r}: refusing overwrite because headers differ; "
            f"existing={existing[0]!r}, incoming={incoming[0]!r}."
        )

    def values_by_year(
        table: list[list[object]], context: str
    ) -> dict[int, list[object]]:
        result: dict[int, list[object]] = {}
        for row_number, row in enumerate(table[1:], start=2):
            observed = normalise_observation_date(row[0] if row else None)
            raw_components = [
                row[column] if len(row) > column else "" for column in (1, 2)
            ]
            if observed is None:
                if any(normalise_value(value) is not None for value in raw_components):
                    raise RuntimeError(
                        f"{context}: row {row_number} has IMF values but no valid date."
                    )
                continue
            year_values = result.setdefault(observed.year, ["", ""])
            for index, raw_value in enumerate(raw_components):
                value = normalise_value(raw_value)
                if value is None:
                    continue
                prior_value = normalise_value(year_values[index])
                if prior_value is None:
                    year_values[index] = raw_value
                elif not google_values_equivalent(prior_value, value):
                    raise RuntimeError(
                        f"{context}: conflicting duplicate IMF values for "
                        f"{observed.year}, column {index + 2}: "
                        f"{year_values[index]!r} versus {raw_value!r}."
                    )
        return result

    old_by_year = values_by_year(existing, f"Shared sheet {IMF_COVERAGE_SHEET!r}")
    new_by_year = values_by_year(incoming, "Generated IMF coverage table")
    rows: list[list[object]] = [list(incoming[0])]
    for year in sorted(set(old_by_year) | set(new_by_year)):
        old_values = old_by_year.get(year, ["", ""])
        new_values = new_by_year.get(year, ["", ""])
        merged_values = [
            new_raw if normalise_value(new_raw) is not None else old_raw
            for old_raw, new_raw in zip(old_values, new_values)
        ]
        rows.append(
            [
                (date(year, 12, 31) - date(1899, 12, 30)).days,
                *merged_values,
            ]
        )
    return rows


def imf_coverage_sync_result(
    table_values: list[list[object]], warning: str = ""
) -> dict:
    incoming_dates = [normalise_observation_date(row[0]) for row in table_values[1:]]
    incoming_dates = [item for item in incoming_dates if item is not None]
    return sync_result_row(
        IMF_COVERAGE_SHEET,
        IMF_COVERAGE_SHEET,
        None,
        max(0, len(table_values) - 1) * 2,
        max(0, len(table_values) - 1),
        max(incoming_dates, default=None),
        warning,
    )


def sync_imf_coverage_with_service(
    sheets,
    spreadsheet_id: str,
    component_data: dict[str, list[dict]],
    sheet_names: set[str],
    validate_only: bool,
) -> dict:
    """Rebuild A:C of the shared IMF tab while preserving analysis columns D onward."""
    if IMF_COVERAGE_SHEET not in sheet_names:
        return sync_result_row(
            IMF_COVERAGE_SHEET,
            "",
            None,
            0,
            0,
            None,
            "Missing matching IMF coverage tab; skipped.",
        )
    table_values = imf_coverage_sheet_values(component_data)
    range_name = f"{quote_sheet_name(IMF_COVERAGE_SHEET)}!A:C"
    old_values = (
        sheets.values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
        .get("values", [])
    )
    if not FULL_HISTORY_MODE:
        fills, appends, merged_values = prepare_incremental_table_changes(
            old_values,
            table_values,
            f"Shared sheet {IMF_COVERAGE_SHEET!r}",
        )
        old_dates = [
            normalise_observation_date(row[0] if row else None)
            for row in old_values[1:]
        ]
        new_dates = [
            normalise_observation_date(row[0] if row else None)
            for row in merged_values[1:]
        ]
        latest_before = max((item for item in old_dates if item), default=None)
        latest_after = max((item for item in new_dates if item), default=None)
        result = sync_result_row(
            IMF_COVERAGE_SHEET,
            IMF_COVERAGE_SHEET,
            latest_before,
            len(fills),
            len(appends),
            latest_after,
        )
        if not fills and not appends:
            return result
        if validate_only:
            result["warning"] = (
                "DRY RUN: incremental preflight passed; populated historical values would be preserved."
            )
            return result

        current_values = (
            sheets.values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
            .get("values", [])
        )
        if current_values != old_values:
            raise RuntimeError(
                f"Shared sheet {IMF_COVERAGE_SHEET!r} changed after preflight; refusing stale incremental write."
            )
        if old_values:
            data_ranges = incremental_table_value_ranges(
                IMF_COVERAGE_SHEET,
                fills,
                appends,
                len(old_values),
            )
            sheets.values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data_ranges},
            ).execute()
        else:
            sheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(IMF_COVERAGE_SHEET)}!A1",
                valueInputOption="RAW",
                body={"values": merged_values},
            ).execute()
        written = (
            sheets.values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
            .get("values", [])
        )
        verify_google_sheet_values(merged_values, written)
        return result

    table_values = merge_imf_coverage_history(old_values, table_values)
    validate_historical_table_replacement(
        old_values,
        table_values,
        f"Shared sheet {IMF_COVERAGE_SHEET!r}",
        allow_absent_existing_dates=True,
        allow_value_revisions=True,
    )
    if google_sheet_table_is_current(table_values, old_values):
        return imf_coverage_sync_result(table_values)
    if validate_only:
        return imf_coverage_sync_result(
            table_values,
            "DRY RUN: historical preflight passed; would rebuild the complete IMF A:C input table.",
        )
    try:
        current_values = (
            sheets.values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
            .get("values", [])
        )
        if current_values != old_values:
            raise RuntimeError(
                f"Shared sheet {IMF_COVERAGE_SHEET!r} changed after preflight; refusing stale overwrite."
            )
        sheets.values().clear(
            spreadsheetId=spreadsheet_id, range=range_name, body={}
        ).execute()
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(IMF_COVERAGE_SHEET)}!A1",
            valueInputOption="RAW",
            body={"values": table_values},
        ).execute()
        written = (
            sheets.values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
            .get("values", [])
        )
        verify_google_sheet_values(table_values, written)
    except Exception:
        sheets.values().clear(
            spreadsheetId=spreadsheet_id, range=range_name, body={}
        ).execute()
        if old_values:
            sheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(IMF_COVERAGE_SHEET)}!A1",
                valueInputOption="RAW",
                body={"values": old_values},
            ).execute()
        raise
    return imf_coverage_sync_result(table_values)


def sync_imf_coverage_direct(
    base_url: str,
    access_token: str,
    component_data: dict[str, list[dict]],
    sheet_names: set[str],
    validate_only: bool,
) -> dict:
    """Direct-Sheets-API version of the shared IMF input-table rebuild."""
    if IMF_COVERAGE_SHEET not in sheet_names:
        return sync_result_row(
            IMF_COVERAGE_SHEET,
            "",
            None,
            0,
            0,
            None,
            "Missing matching IMF coverage tab; skipped.",
        )
    table_values = imf_coverage_sheet_values(component_data)
    quoted_sheet = quote_sheet_name(IMF_COVERAGE_SHEET)
    range_name = f"{quoted_sheet}!A:C"
    range_url = google_values_url(base_url, range_name)
    clear_url = f"{base_url}/values/{quote(range_name, safe='')}:clear"
    old_values = google_sheets_request("GET", range_url, access_token).get("values", [])
    if not FULL_HISTORY_MODE:
        fills, appends, merged_values = prepare_incremental_table_changes(
            old_values,
            table_values,
            f"Shared sheet {IMF_COVERAGE_SHEET!r}",
        )
        old_dates = [
            normalise_observation_date(row[0] if row else None)
            for row in old_values[1:]
        ]
        new_dates = [
            normalise_observation_date(row[0] if row else None)
            for row in merged_values[1:]
        ]
        latest_before = max((item for item in old_dates if item), default=None)
        latest_after = max((item for item in new_dates if item), default=None)
        result = sync_result_row(
            IMF_COVERAGE_SHEET,
            IMF_COVERAGE_SHEET,
            latest_before,
            len(fills),
            len(appends),
            latest_after,
        )
        if not fills and not appends:
            return result
        if validate_only:
            result["warning"] = (
                "DRY RUN: incremental preflight passed; populated historical values would be preserved."
            )
            return result

        current_values = google_sheets_request("GET", range_url, access_token).get(
            "values", []
        )
        if current_values != old_values:
            raise RuntimeError(
                f"Shared sheet {IMF_COVERAGE_SHEET!r} changed after preflight; refusing stale incremental write."
            )
        if old_values:
            data_ranges = incremental_table_value_ranges(
                IMF_COVERAGE_SHEET,
                fills,
                appends,
                len(old_values),
            )
            google_sheets_request(
                "POST",
                f"{base_url}/values:batchUpdate?valueInputOption=RAW",
                access_token,
                {"valueInputOption": "RAW", "data": data_ranges},
            )
        else:
            google_sheets_request(
                "PUT",
                f"{base_url}/values/{quote(f'{quote_sheet_name(IMF_COVERAGE_SHEET)}!A1', safe='')}?valueInputOption=RAW",
                access_token,
                {"values": merged_values},
            )
        written = google_sheets_request("GET", range_url, access_token).get(
            "values", []
        )
        verify_google_sheet_values(merged_values, written)
        return result

    table_values = merge_imf_coverage_history(old_values, table_values)
    validate_historical_table_replacement(
        old_values,
        table_values,
        f"Shared sheet {IMF_COVERAGE_SHEET!r}",
        allow_absent_existing_dates=True,
        allow_value_revisions=True,
    )
    if google_sheet_table_is_current(table_values, old_values):
        return imf_coverage_sync_result(table_values)
    if validate_only:
        return imf_coverage_sync_result(
            table_values,
            "DRY RUN: historical preflight passed; would rebuild the complete IMF A:C input table.",
        )
    try:
        current_values = google_sheets_request("GET", range_url, access_token).get(
            "values", []
        )
        if current_values != old_values:
            raise RuntimeError(
                f"Shared sheet {IMF_COVERAGE_SHEET!r} changed after preflight; refusing stale overwrite."
            )
        google_sheets_request("POST", clear_url, access_token, {})
        google_sheets_request(
            "PUT",
            f"{base_url}/values/{quote(f'{quoted_sheet}!A1', safe='')}?valueInputOption=RAW",
            access_token,
            {"values": table_values},
        )
        written = google_sheets_request("GET", range_url, access_token).get(
            "values", []
        )
        verify_google_sheet_values(table_values, written)
    except Exception:
        google_sheets_request("POST", clear_url, access_token, {})
        if old_values:
            google_sheets_request(
                "PUT",
                f"{base_url}/values/{quote(f'{quoted_sheet}!A1', safe='')}?valueInputOption=RAW",
                access_token,
                {"values": old_values},
            )
        raise
    return imf_coverage_sync_result(table_values)


def expected_indicator_tabs() -> list[str]:
    """Return the indicator tabs that should exist in the dedicated workbook."""
    return [
        indicator.name
        for indicator in INDICATORS
        if has_dedicated_indicator_tab(indicator.name)
    ] + [CENTRAL_BANK_LIQUIDITY_SHEET, IMF_COVERAGE_SHEET]


def sheet_date_stats(values: list[list[object]]) -> dict:
    """Inspect column A without mutating the sheet."""
    dates: list[date] = []
    duplicate_dates: set[date] = set()
    seen_dates: set[date] = set()
    blank_values = 0
    non_numeric_values = 0
    for row in values[1:]:
        observed = normalise_observation_date(row[0] if row else None)
        if observed is None:
            continue
        if observed in seen_dates:
            duplicate_dates.add(observed)
        seen_dates.add(observed)
        dates.append(observed)
        raw_value = row[1] if len(row) > 1 else None
        if raw_value in (None, ""):
            blank_values += 1
        elif normalise_value(raw_value) is None:
            non_numeric_values += 1
    latest = max(dates, default=None)
    return {
        "latest": latest,
        "dates": dates,
        "valid_date_rows": len(dates),
        "duplicate_dates": sorted(duplicate_dates),
        "blank_values": blank_values,
        "non_numeric_values": non_numeric_values,
    }


def latest_sheet_value(
    values: list[list[object]],
    target_date: date,
    value_col: int = 1,
) -> float | None:
    """Return a numeric sheet value for one normalized observation month."""
    for row in values[1:]:
        observed = normalise_observation_date(row[0] if row else None)
        if observed != target_date:
            continue
        raw_value = row[value_col] if len(row) > value_col else None
        return normalise_value(raw_value)
    return None


def sheet_values_by_date(
    values: list[list[object]],
    value_col: int = 1,
) -> dict[date, float | None]:
    """Map normalized sheet dates to numeric values without changing the workbook."""
    mapped: dict[date, float | None] = {}
    for row in values[1:]:
        observed = normalise_observation_date(row[0] if row else None)
        if observed is None:
            continue
        raw_value = row[value_col] if len(row) > value_col else None
        mapped[observed] = normalise_value(raw_value)
    return mapped


def source_sheet_mismatches(
    values: list[list[object]],
    source_records: list[dict],
    value_col: int = 1,
) -> list[tuple[date, float | None, float]]:
    """Find dated rows where the source output and sheet value differ."""
    incoming = output_month_records_for_indicator(source_records)
    existing = sheet_values_by_date(values, value_col)
    mismatches: list[tuple[date, float | None, float]] = []
    for observed, source_value in sorted(incoming.items()):
        if observed not in existing:
            continue
        sheet_value = existing[observed]
        if sheet_value is None or not google_values_equivalent(
            source_value, sheet_value
        ):
            mismatches.append((observed, sheet_value, source_value))
    return mismatches


def format_mismatch_warning(
    mismatches: list[tuple[date, float | None, float]],
) -> str:
    """Summarize sheet/source value disagreements for audit output."""
    return "Sheet/source mismatches found: " + "; ".join(
        f"{observed.isoformat()} sheet={sheet_value} source={source_value}"
        for observed, sheet_value, source_value in mismatches[-5:]
    )


def sheet_quality_warnings(
    stats: dict,
    indicator_source: str,
    source_frequency: str,
) -> list[str]:
    """Return non-mutating sheet health warnings for one audit row."""
    warnings: list[str] = []
    if stats["duplicate_dates"]:
        warnings.append("Duplicate dates in column A.")
    if stats["blank_values"]:
        warnings.append(f"{stats['blank_values']} dated rows have blank values.")
    if stats["non_numeric_values"]:
        warnings.append(
            f"{stats['non_numeric_values']} dated rows have non-numeric values."
        )
    if indicator_source == "imf_datamapper":
        non_december_dates = [
            item for item in stats.get("dates", []) if item.month != ANNUAL_OUTPUT_MONTH
        ]
        if non_december_dates:
            warnings.append(
                "Annual IMF values should be dated to December year-end; "
                "non-December rows found: "
                + ", ".join(item.isoformat() for item in non_december_dates[-5:])
            )
    if source_frequency.startswith("Quarterly"):
        off_quarter_dates = [
            item
            for item in stats.get("dates", [])
            if item.month not in QUARTER_END_MONTHS
        ]
        if off_quarter_dates:
            warnings.append(
                "Quarterly FRED values should be dated to quarter-end month; "
                "off-quarter rows found: "
                + ", ".join(item.isoformat() for item in off_quarter_dates[-5:])
            )
    elif source_frequency.startswith("Annual"):
        off_annual_dates = [
            item for item in stats.get("dates", []) if item.month != ANNUAL_OUTPUT_MONTH
        ]
        if off_annual_dates:
            warnings.append(
                "Annual FRED values should be dated to December year-end; "
                "off-year-end rows found: "
                + ", ".join(item.isoformat() for item in off_annual_dates[-5:])
            )
    return warnings


def source_output_for_records(records: list[dict]) -> tuple[date | None, float | None]:
    """Return the newest month/value that the extraction layer would write."""
    incoming = output_month_records_for_indicator(records)
    if not incoming:
        return None, None
    latest = max(incoming)
    return latest, incoming[latest]


def source_descriptor(indicator: Indicator) -> tuple[str, str]:
    """Summarize one indicator's official source for audit output."""
    if indicator.source == "fred":
        series_id = indicator.series_id or ""
        return "FRED / ALFRED", f"https://fred.stlouisfed.org/series/{series_id}"
    if indicator.source == "ism":
        return "Institute for Supply Management", ISM_REPORT_INDEX_URL
    if indicator.source == "nyfed_sce":
        return "New York Fed SCE", NYFED_SCE_PAGE_URL
    if indicator.source in {"michigan_sca", "michigan_sentiment"}:
        return "University of Michigan Surveys of Consumers", MICHIGAN_CHARTS_PAGE_URL
    if indicator.source == "imf_datamapper":
        return "IMF DataMapper FPP", IMF_DATAMAPPER_API_BASE
    return indicator.source, ""


def audit_sheet_rows(
    sheet_names: set[str],
    read_values,
    source_data: dict[str, list[dict]] | None = None,
    source_errors: dict[str, str] | None = None,
) -> list[list[object]]:
    """Build a complete read-only workbook audit table."""
    source_data = source_data or {}
    source_errors = source_errors or {}
    headers = [
        "Indicator",
        "Sheet",
        "Source",
        "Source URL",
        "Latest sheet date",
        "Latest source date",
        "Latest sheet value",
        "Latest source value",
        "Status",
        "Warnings",
        "Valid date rows",
        "Blank value rows",
        "Non-numeric value rows",
        "Duplicate dates",
    ]
    rows: list[list[object]] = [headers]

    for indicator in INDICATORS:
        if not has_dedicated_indicator_tab(indicator.name):
            continue
        source_name, source_url = source_descriptor(indicator)
        sheet_name = indicator_sheet_name(indicator.name, sheet_names)
        if sheet_name is None:
            rows.append(
                [
                    indicator.name,
                    "",
                    source_name,
                    source_url,
                    "",
                    "",
                    "",
                    "",
                    "FAILED",
                    "Missing matching indicator tab.",
                    0,
                    0,
                    0,
                    "",
                ]
            )
            continue
        try:
            values = read_values(sheet_name, "B")
            get_sheet_layout(values, indicator.name)
            stats = sheet_date_stats(values)
        except Exception as exc:
            rows.append(
                [
                    indicator.name,
                    sheet_name,
                    source_name,
                    source_url,
                    "",
                    "",
                    "",
                    "",
                    "FAILED",
                    str(exc),
                    0,
                    0,
                    0,
                    "",
                ]
            )
            continue

        source_records = source_data.get(indicator.name, [])
        source_latest, source_value = source_output_for_records(source_records)
        latest_sheet = stats["latest"]
        sheet_value = latest_sheet_value(values, latest_sheet) if latest_sheet else None
        source_frequency = (
            str(source_records[0].get("native_frequency", "")) if source_records else ""
        )
        warnings = sheet_quality_warnings(
            stats,
            indicator.source,
            source_frequency,
        )
        mismatches = source_sheet_mismatches(values, source_records)
        if mismatches:
            warnings.append(format_mismatch_warning(mismatches))
        if indicator.name in source_errors:
            warnings.append(
                f"Source extraction failed: {source_errors[indicator.name]}"
            )
        if source_latest and latest_sheet and source_latest > latest_sheet:
            status = "NEEDS UPDATE"
        elif mismatches:
            status = "VALUE MISMATCH"
        elif (
            source_latest
            and latest_sheet == source_latest
            and source_value is not None
            and sheet_value is not None
            and not google_values_equivalent(source_value, sheet_value)
        ):
            status = "VALUE MISMATCH"
            warnings.append("Latest sheet value differs from extracted source value.")
        elif source_latest and latest_sheet and latest_sheet > source_latest:
            status = "REVIEW"
            warnings.append("Sheet is ahead of currently extracted source output.")
        elif source_latest is None and indicator.name in source_errors:
            status = "FAILED"
        else:
            status = "OK"

        rows.append(
            [
                indicator.name,
                sheet_name,
                source_name,
                source_url,
                latest_sheet.isoformat() if latest_sheet else "",
                source_latest.isoformat() if source_latest else "",
                sheet_value if sheet_value is not None else "",
                source_value if source_value is not None else "",
                status,
                " ".join(warnings),
                stats["valid_date_rows"],
                stats["blank_values"],
                stats["non_numeric_values"],
                ", ".join(item.isoformat() for item in stats["duplicate_dates"]),
            ]
        )

    central_sheet = CENTRAL_BANK_LIQUIDITY_SHEET
    source_name = "FRED / ALFRED"
    source_url = " / ".join(
        f"https://fred.stlouisfed.org/series/{indicator.series_id}"
        for indicator in INDICATORS
        if is_central_bank_component(indicator.name)
    )
    if central_sheet not in sheet_names:
        rows.append(
            [
                central_sheet,
                "",
                source_name,
                source_url,
                "",
                "",
                "",
                "",
                "FAILED",
                "Missing matching central-bank liquidity tab.",
                0,
                0,
                0,
                "",
            ]
        )
    else:
        try:
            values = read_values(central_sheet, "D")
            get_sheet_layout(values, central_sheet)
            stats = sheet_date_stats(values)
        except Exception as exc:
            values = []
            stats = {
                "latest": None,
                "dates": [],
                "valid_date_rows": 0,
                "duplicate_dates": [],
                "blank_values": 0,
                "non_numeric_values": 0,
            }
            central_layout_error = str(exc)
        else:
            central_layout_error = ""

        for component_name, target in CENTRAL_BANK_LIQUIDITY_COMPONENTS.items():
            source_latest, source_value = source_output_for_records(
                source_data.get(component_name, [])
            )
            latest_sheet = stats["latest"]
            sheet_value = (
                latest_sheet_value(values, latest_sheet, target["column"])
                if latest_sheet and values
                else None
            )
            source_records = source_data.get(component_name, [])
            mismatches = source_sheet_mismatches(
                values,
                source_records,
                target["column"],
            )
            warnings: list[str] = []
            status = "OK"
            if central_layout_error:
                status = "FAILED"
                warnings.append(central_layout_error)
            if component_name in source_errors:
                status = "FAILED"
                warnings.append(
                    f"Source extraction failed: {source_errors[component_name]}"
                )
            if source_latest and latest_sheet and source_latest > latest_sheet:
                status = "NEEDS UPDATE"
            elif mismatches:
                status = "VALUE MISMATCH"
                warnings.append(format_mismatch_warning(mismatches))
            elif (
                source_latest
                and latest_sheet == source_latest
                and source_value is not None
                and sheet_value is not None
                and not google_values_equivalent(source_value, sheet_value)
            ):
                status = "VALUE MISMATCH"
                warnings.append(
                    "Latest sheet value differs from extracted source value."
                )
            rows.append(
                [
                    target["header"],
                    central_sheet,
                    source_name,
                    source_url,
                    latest_sheet.isoformat() if latest_sheet else "",
                    source_latest.isoformat() if source_latest else "",
                    sheet_value if sheet_value is not None else "",
                    source_value if source_value is not None else "",
                    status,
                    " ".join(warnings),
                    stats["valid_date_rows"],
                    "",
                    "",
                    ", ".join(item.isoformat() for item in stats["duplicate_dates"]),
                ]
            )

    coverage_sheet = IMF_COVERAGE_SHEET
    if coverage_sheet not in sheet_names:
        rows.append(
            [
                coverage_sheet,
                "",
                "IMF DataMapper FPP",
                IMF_DATAMAPPER_API_BASE,
                "",
                "",
                "",
                "",
                "FAILED",
                "Missing matching IMF coverage tab.",
                0,
                0,
                0,
                "",
            ]
        )
    else:
        try:
            values = read_values(coverage_sheet, "D")
            get_sheet_layout(values, coverage_sheet)
            stats = sheet_date_stats(values)
            latest_sheet = stats["latest"]
        except Exception as exc:
            values = []
            latest_sheet = None
            stats = {
                "valid_date_rows": 0,
                "duplicate_dates": [],
                "blank_values": 0,
                "non_numeric_values": 0,
                "dates": [],
            }
            coverage_layout_error = str(exc)
        else:
            coverage_layout_error = ""

        coverage_columns = {
            "Government revenue % GDP": 1,
            "Interest paid on public debt % GDP": 2,
            IMF_COVERAGE_SHEET: 3,
        }
        for indicator_name, value_col in coverage_columns.items():
            indicator = next(item for item in INDICATORS if item.name == indicator_name)
            source_name, source_url = source_descriptor(indicator)
            source_records = source_data.get(indicator_name, [])
            source_latest, source_value = source_output_for_records(source_records)
            sheet_value = (
                latest_sheet_value(values, latest_sheet, value_col)
                if latest_sheet and values
                else None
            )
            warnings = sheet_quality_warnings(stats, "imf_datamapper", "")
            status = "OK"
            if coverage_layout_error:
                status = "FAILED"
                warnings.append(coverage_layout_error)
            if latest_sheet and sheet_value is None:
                status = "FAILED"
                warnings.append("Latest shared IMF row is missing this required value.")
            mismatches = source_sheet_mismatches(values, source_records, value_col)
            if mismatches:
                status = "VALUE MISMATCH"
                warnings.append(format_mismatch_warning(mismatches))
            if indicator_name in source_errors:
                status = "FAILED"
                warnings.append(
                    f"Source extraction failed: {source_errors[indicator_name]}"
                )
            if source_latest and latest_sheet and source_latest > latest_sheet:
                status = "NEEDS UPDATE"
            rows.append(
                [
                    indicator_name,
                    coverage_sheet,
                    source_name,
                    source_url,
                    latest_sheet.isoformat() if latest_sheet else "",
                    source_latest.isoformat() if source_latest else "",
                    sheet_value if sheet_value is not None else "",
                    source_value if source_value is not None else "",
                    status,
                    " ".join(warnings),
                    stats["valid_date_rows"],
                    "",
                    "",
                    ", ".join(item.isoformat() for item in stats["duplicate_dates"]),
                ]
            )

    return rows


def write_audit_csv(rows: list[list[object]]) -> None:
    OUTPUT_ROOT.mkdir(exist_ok=True)
    pd.DataFrame(rows[1:], columns=rows[0]).to_csv(GOOGLE_SHEET_AUDIT_CSV, index=False)


def print_audit_summary(rows: list[list[object]]) -> None:
    print_console_table(
        "Google Sheet audit summary:",
        [
            "Indicator",
            "Sheet",
            "Latest sheet",
            "Latest source",
            "Sheet value",
            "Source value",
            "Status",
            "Warnings",
        ],
        (
            [row[0], row[1], row[4], row[5], row[6], row[7], row[8], row[9]]
            for row in rows[1:]
        ),
    )
    counts = pd.Series([row[8] for row in rows[1:]]).value_counts().to_dict()
    count_summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"\nAudit status counts: {count_summary}")
    print(f"Full audit CSV: {GOOGLE_SHEET_AUDIT_CSV}")


def audit_google_workbook(
    spreadsheet_id: str,
    credentials_path: Path,
    source_data: dict[str, list[dict]] | None = None,
    source_errors: dict[str, str] | None = None,
) -> None:
    """Read the target workbook and print a non-mutating tab audit."""
    if not credentials_path.exists():
        raise SystemExit(f"Google credentials file not found: {credentials_path}")

    access_token = None
    sheets = None
    try:
        if os.getenv("US_PIPELINE_FORCE_DIRECT_GOOGLE_API") == "1":
            raise ImportError("Direct Google Sheets API selected by pipeline runner.")
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        access_token = get_service_account_access_token(credentials_path)
        base_url = f"{GOOGLE_SHEETS_API_BASE}/{spreadsheet_id}"
        metadata = google_sheets_request("GET", base_url, access_token)
        sheet_names = [
            sheet["properties"]["title"] for sheet in metadata.get("sheets", [])
        ]

    else:
        credentials = Credentials.from_service_account_file(
            credentials_path,
            scopes=GOOGLE_SHEETS_READONLY_SCOPES,
        )
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets()
        metadata = sheets.get(spreadsheetId=spreadsheet_id).execute()
        sheet_names = [
            sheet["properties"]["title"] for sheet in metadata.get("sheets", [])
        ]

    sheet_name_set = set(sheet_names)
    expected_tabs = expected_indicator_tabs()
    missing_tabs = [name for name in expected_tabs if name not in sheet_name_set]

    audit_ranges = [
        f"{quote_sheet_name(sheet_name)}!A:"
        f"{'D' if sheet_name in {CENTRAL_BANK_LIQUIDITY_SHEET, IMF_COVERAGE_SHEET} else 'B'}"
        for sheet_name in expected_tabs
        if sheet_name in sheet_name_set
    ]
    if access_token is not None:
        query = urlencode(
            [("ranges", item) for item in audit_ranges]
            + [("valueRenderOption", "UNFORMATTED_VALUE")]
        )
        batch_response = google_sheets_request(
            "GET",
            f"{base_url}/values:batchGet?{query}",
            access_token,
        )
    else:
        batch_response = (
            sheets.values()
            .batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=audit_ranges,
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
        )
    cached_values = {
        requested_range: value_range.get("values", [])
        for requested_range, value_range in zip(
            audit_ranges,
            batch_response.get("valueRanges", []),
        )
    }

    def read_values(sheet_name: str, end_column: str) -> list[list[object]]:
        range_name = f"{quote_sheet_name(sheet_name)}!A:{end_column}"
        return cached_values.get(range_name, [])

    print_console_table(
        "Google Sheet workbook:",
        ["Check", "Result"],
        [
            ["Workbook", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"],
            ["Tabs found", len(sheet_names)],
            [
                "Missing expected tabs",
                ", ".join(missing_tabs) if missing_tabs else "none",
            ],
        ],
    )
    audit_rows = audit_sheet_rows(
        sheet_name_set, read_values, source_data, source_errors
    )
    write_audit_csv(audit_rows)
    print_audit_summary(audit_rows)

    check_tabs = [
        "Real consumer spending growth",
        "Consumer expectations",
        "Employment growth",
        "Core consumption inflation",
        "Core producer price inflation",
        "Headline producer price inflation",
        "Consumer inflation expectations",
        CENTRAL_BANK_LIQUIDITY_SHEET,
        "Banking system reserves",
        "Government revenue growth",
        "Financial conditions index",
    ]
    for sheet_name in check_tabs:
        if sheet_name not in sheet_name_set:
            print(f"\n{sheet_name}: MISSING")
            continue
        end_column = (
            "D"
            if sheet_name in {CENTRAL_BANK_LIQUIDITY_SHEET, IMF_COVERAGE_SHEET}
            else "B"
        )
        values = read_values(sheet_name, end_column)
        rows = [row for row in values[1:] if row and normalise_observation_date(row[0])]
        blank_recent = [
            row for row in rows[-12:] if len(row) < 2 or normalise_value(row[1]) is None
        ]
        latest_date = normalise_observation_date(rows[-1][0]) if rows else None
        print_console_table(
            sheet_name,
            ["Check", "Result"],
            [
                ["Header", values[0] if values else []],
                ["Latest valid date", latest_date.isoformat() if latest_date else ""],
                ["Recent blank value rows", len(blank_recent)],
            ],
        )
        if rows:
            preview_headers = [console_cell(value) for value in values[0]]
            print_console_table(
                "Last 5 rows:",
                preview_headers,
                rows[-5:],
            )


def sync_all_indicators(
    spreadsheet_id: str,
    credentials_path: Path,
    workbook_data: list[tuple[str, list[dict]]],
    validate_only: bool = False,
    _preflight: bool = False,
) -> list[dict]:
    """Sync every indicator to its dedicated Google Sheets tab."""
    logging.getLogger("google.oauth2._client").setLevel(logging.ERROR)
    if not credentials_path.exists():
        raise SystemExit(f"Google credentials file not found: {credentials_path}")
    if not validate_only:
        preflight_results = sync_all_indicators(
            spreadsheet_id,
            credentials_path,
            workbook_data,
            validate_only=True,
            _preflight=True,
        )
        assert_sync_preflight_passed(preflight_results)

    try:
        if os.getenv("US_PIPELINE_FORCE_DIRECT_GOOGLE_API") == "1":
            raise ImportError("Direct Google Sheets API selected by pipeline runner.")
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        access_token = get_service_account_access_token(credentials_path)
        base_url = f"{GOOGLE_SHEETS_API_BASE}/{spreadsheet_id}"
        metadata = google_sheets_request("GET", base_url, access_token)
        sheet_names = {
            sheet["properties"]["title"] for sheet in metadata.get("sheets", [])
        }
        control_values = google_sheets_request(
            "GET",
            google_values_url(base_url, ANALYSIS_CONTROL_PANEL_RANGE),
            access_token,
        ).get("values", [])
        validate_analysis_workbook_topology(sheet_names, control_values)
        results = []
        central_bank_data = central_bank_component_data(workbook_data)
        if central_bank_data:
            if COMPACT_TERMINAL and not _preflight:
                print(f"Checking sheet: {CENTRAL_BANK_LIQUIDITY_SHEET}...", flush=True)
            try:
                results.append(
                    sync_central_bank_liquidity_direct(
                        base_url,
                        access_token,
                        central_bank_data,
                        sheet_names,
                        validate_only,
                    )
                )
            except Exception as exc:
                if isinstance(exc, requests.RequestException):
                    raise
                results.append(
                    sync_result_row(
                        CENTRAL_BANK_LIQUIDITY_SHEET,
                        CENTRAL_BANK_LIQUIDITY_SHEET,
                        None,
                        0,
                        0,
                        None,
                        str(exc),
                    )
                )
        coverage_data = imf_coverage_component_data(workbook_data)
        if coverage_data:
            if COMPACT_TERMINAL and not _preflight:
                print(f"Checking sheet: {IMF_COVERAGE_SHEET}...", flush=True)
            try:
                results.append(
                    sync_imf_coverage_direct(
                        base_url,
                        access_token,
                        coverage_data,
                        sheet_names,
                        validate_only,
                    )
                )
            except Exception as exc:
                if isinstance(exc, requests.RequestException):
                    raise
                results.append(
                    sync_result_row(
                        IMF_COVERAGE_SHEET,
                        IMF_COVERAGE_SHEET,
                        None,
                        0,
                        0,
                        None,
                        str(exc),
                    )
                )
        for indicator_name, records in dedicated_indicator_data(workbook_data):
            sheet_name = indicator_sheet_name(indicator_name, sheet_names)
            if sheet_name is None:
                results.append(
                    sync_result_row(
                        indicator_name,
                        "",
                        None,
                        0,
                        0,
                        None,
                        "Missing matching indicator tab; skipped.",
                    )
                )
                continue
            if COMPACT_TERMINAL and not _preflight:
                print(f"Checking sheet: {sheet_name}...", flush=True)
            try:
                results.append(
                    sync_indicator_sheet_direct(
                        base_url,
                        access_token,
                        sheet_name,
                        indicator_name,
                        records,
                        validate_only,
                    )
                )
            except Exception as exc:
                if isinstance(exc, requests.RequestException):
                    raise
                results.append(
                    sync_result_row(
                        indicator_name,
                        sheet_name,
                        None,
                        0,
                        0,
                        None,
                        str(exc),
                    )
                )
        if not validate_only and not _preflight:
            assert_sync_preflight_passed(results)
            format_requests = analysis_number_format_requests(metadata)
            if format_requests:
                google_sheets_request(
                    "POST",
                    f"{base_url}:batchUpdate",
                    access_token,
                    {"requests": format_requests},
                )
        if not _preflight:
            print_sync_summary(results, validate_only=validate_only)
        return results

    credentials = Credentials.from_service_account_file(
        credentials_path,
        scopes=GOOGLE_SHEETS_WRITE_SCOPES,
    )
    service = build("sheets", "v4", credentials=credentials)
    sheets = service.spreadsheets()
    metadata = sheets.get(spreadsheetId=spreadsheet_id).execute()
    sheet_names = {sheet["properties"]["title"] for sheet in metadata.get("sheets", [])}
    control_values = (
        sheets.values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=ANALYSIS_CONTROL_PANEL_RANGE,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
        .get("values", [])
    )
    validate_analysis_workbook_topology(sheet_names, control_values)

    results = []
    central_bank_data = central_bank_component_data(workbook_data)
    if central_bank_data:
        try:
            results.append(
                sync_central_bank_liquidity_with_service(
                    sheets,
                    spreadsheet_id,
                    central_bank_data,
                    sheet_names,
                    validate_only,
                )
            )
        except Exception as exc:
            results.append(
                sync_result_row(
                    CENTRAL_BANK_LIQUIDITY_SHEET,
                    CENTRAL_BANK_LIQUIDITY_SHEET,
                    None,
                    0,
                    0,
                    None,
                    str(exc),
                )
            )
    coverage_data = imf_coverage_component_data(workbook_data)
    if coverage_data:
        try:
            results.append(
                sync_imf_coverage_with_service(
                    sheets,
                    spreadsheet_id,
                    coverage_data,
                    sheet_names,
                    validate_only,
                )
            )
        except Exception as exc:
            results.append(
                sync_result_row(
                    IMF_COVERAGE_SHEET,
                    IMF_COVERAGE_SHEET,
                    None,
                    0,
                    0,
                    None,
                    str(exc),
                )
            )
    for indicator_name, records in dedicated_indicator_data(workbook_data):
        sheet_name = indicator_sheet_name(indicator_name, sheet_names)
        if sheet_name is None:
            results.append(
                sync_result_row(
                    indicator_name,
                    "",
                    None,
                    0,
                    0,
                    None,
                    "Missing matching indicator tab; skipped.",
                )
            )
            continue
        try:
            results.append(
                sync_indicator_sheet_with_service(
                    sheets,
                    spreadsheet_id,
                    sheet_name,
                    indicator_name,
                    records,
                    validate_only,
                )
            )
        except Exception as exc:
            results.append(
                sync_result_row(
                    indicator_name,
                    sheet_name,
                    None,
                    0,
                    0,
                    None,
                    str(exc),
                )
            )
    if not validate_only and not _preflight:
        assert_sync_preflight_passed(results)
        format_requests = analysis_number_format_requests(metadata)
        if format_requests:
            sheets.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": format_requests},
            ).execute()
    if not _preflight:
        print_sync_summary(results, validate_only=validate_only)
    return results


def collect_source_data_for_audit(
    api_key: str, months: int
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Collect source records for audit without letting one failed indicator stop the report."""
    source_data: dict[str, list[dict]] = {}
    source_errors: dict[str, str] = {}
    for indicator in INDICATORS:
        try:
            source_data[indicator.name] = get_indicator_observations_safely(
                api_key,
                indicator,
                months,
            )
        except Exception as exc:
            source_errors[indicator.name] = str(exc)
    return source_data, source_errors


def run_internal_self_tests() -> None:
    """Run fast offline checks before any external source or Google Sheet is used."""
    global SOURCE_ARCHIVE_ROOT

    def expect_failure(description: str, operation) -> None:
        try:
            operation()
        except Exception:
            return
        raise RuntimeError(f"Internal test failed: {description} was not rejected.")

    if [indicator.name for indicator in INDICATORS] != EXPECTED_HEADERS:
        raise RuntimeError("Internal test failed: indicator headers or order changed.")
    if len(EXPECTED_ANALYSIS_SHEET_ORDER) != 37:
        raise RuntimeError(
            "Internal test failed: analysis sheet topology is not 37 tabs."
        )
    valid_analysis_tabs = {
        ANALYSIS_CONTROL_PANEL_SHEET,
        *EXPECTED_ANALYSIS_SHEET_ORDER,
    }
    valid_control_values = [[name] for name in EXPECTED_ANALYSIS_SHEET_ORDER]
    validate_analysis_workbook_topology(
        valid_analysis_tabs,
        valid_control_values,
    )
    if analysis_value("1.234564") != 1.23456:
        raise RuntimeError(
            "Internal test failed: five-decimal analysis rounding changed."
        )
    if analysis_value("1.234565") != 1.23457:
        raise RuntimeError("Internal test failed: analysis half-up rounding changed.")
    format_fixture = analysis_number_format_requests(
        {
            "sheets": [
                {"properties": {"title": name, "sheetId": index}}
                for index, name in enumerate(EXPECTED_ANALYSIS_SHEET_ORDER, start=1)
            ]
        }
    )
    if len(format_fixture) != len(EXPECTED_ANALYSIS_SHEET_ORDER):
        raise RuntimeError(
            "Internal test failed: analysis number formatting lost a tab."
        )
    if any(
        request["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"]["pattern"]
        != "0.00000"
        for request in format_fixture
    ):
        raise RuntimeError(
            "Internal test failed: analysis five-decimal format changed."
        )
    expect_failure(
        "missing required analysis tab",
        lambda: validate_analysis_workbook_topology(
            valid_analysis_tabs - {EXPECTED_ANALYSIS_SHEET_ORDER[0]},
            valid_control_values,
        ),
    )
    blank_control_values = [list(row) for row in valid_control_values]
    blank_control_values[10] = [""]
    expect_failure(
        "blank analysis Control Panel row",
        lambda: validate_analysis_workbook_topology(
            valid_analysis_tabs,
            blank_control_values,
        ),
    )
    if IMF_SUPPORT_INDICATORS != {
        "Government revenue % GDP",
        "Interest paid on public debt % GDP",
    }:
        raise RuntimeError("Internal test failed: IMF support indicator rules changed.")
    if has_dedicated_indicator_tab("Government interest coverage ratio") is not False:
        raise RuntimeError("Internal test failed: IMF coverage tab rule changed.")
    if has_dedicated_indicator_tab("Government revenue % GDP") is not False:
        raise RuntimeError(
            "Internal test failed: IMF revenue support tab rule changed."
        )
    if (
        has_dedicated_indicator_tab(
            "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level"
        )
        is not False
    ):
        raise RuntimeError(
            "Internal test failed: central-bank shared tab rule changed."
        )
    expected_tabs = expected_indicator_tabs()
    if "Government interest coverage ratio" not in expected_tabs:
        raise RuntimeError("Internal test failed: coverage ratio tab was dropped.")
    if "Government revenue % GDP" in expected_tabs:
        raise RuntimeError("Internal test failed: IMF support tab was added back.")
    if CENTRAL_BANK_LIQUIDITY_SHEET not in expected_tabs:
        raise RuntimeError(
            "Internal test failed: central-bank liquidity tab was dropped."
        )
    syncable_names = [
        name
        for name, _records in dedicated_indicator_data(
            [(indicator.name, []) for indicator in INDICATORS]
        )
    ]
    if "Government revenue % GDP" in syncable_names:
        raise RuntimeError(
            "Internal test failed: IMF revenue support series was syncable."
        )
    if "Interest paid on public debt % GDP" in syncable_names:
        raise RuntimeError(
            "Internal test failed: IMF interest support series was syncable."
        )
    if "Government interest coverage ratio" in syncable_names:
        raise RuntimeError(
            "Internal test failed: derived IMF coverage ratio was syncable."
        )
    coverage_fixture = imf_coverage_sheet_values(
        {
            "Government revenue % GDP": [
                {
                    "observation_date": date(2024, 12, 31),
                    "selection_date": date(2024, 12, 31),
                    "value": 30.0,
                    "source": "IMF DataMapper FPP",
                }
            ],
            "Interest paid on public debt % GDP": [
                {
                    "observation_date": date(2024, 12, 31),
                    "selection_date": date(2024, 12, 31),
                    "value": 4.0,
                    "source": "IMF DataMapper FPP",
                }
            ],
        }
    )
    if coverage_fixture != [
        ["Date", "Government Revenue % GDP", "Government Interest % GDP"],
        [(date(2024, 12, 31) - date(1899, 12, 30)).days, 30.0, 4.0],
    ]:
        raise RuntimeError(
            "Internal test failed: IMF coverage A:C table mapping changed."
        )
    legacy_coverage = [
        ["Date", "Government Revenue % GDP", "Government Interest % GDP"],
        [(date(2022, 1, 1) - date(1899, 12, 30)).days, 28.0, 3.0],
        [(date(2023, 1, 1) - date(1899, 12, 30)).days, 29.0, 3.5],
    ]
    official_coverage = [
        ["Date", "Government Revenue % GDP", "Government Interest % GDP"],
        [(date(2023, 12, 31) - date(1899, 12, 30)).days, 29.5, ""],
        [(date(2024, 12, 31) - date(1899, 12, 30)).days, 30.0, 4.0],
    ]
    merged_coverage = merge_imf_coverage_history(
        legacy_coverage,
        official_coverage,
    )
    if merged_coverage != [
        ["Date", "Government Revenue % GDP", "Government Interest % GDP"],
        [(date(2022, 12, 31) - date(1899, 12, 30)).days, 28.0, 3.0],
        [(date(2023, 12, 31) - date(1899, 12, 30)).days, 29.5, 3.5],
        [(date(2024, 12, 31) - date(1899, 12, 30)).days, 30.0, 4.0],
    ]:
        raise RuntimeError(
            "Internal test failed: legacy IMF history was not preserved at year-end."
        )
    expect_failure(
        "conflicting duplicate IMF year",
        lambda: merge_imf_coverage_history(
            [
                legacy_coverage[0],
                legacy_coverage[1],
                [
                    (date(2022, 12, 31) - date(1899, 12, 30)).days,
                    99.0,
                    3.0,
                ],
            ],
            official_coverage,
        ),
    )
    table_buffer = StringIO()
    with redirect_stdout(table_buffer):
        print_console_table("Internal table", ["A", "B"], [[1, "two"], ["three"]])
    table_output = table_buffer.getvalue()
    if "three |" not in table_output or "A     | B" not in table_output:
        raise RuntimeError("Internal test failed: console table formatting changed.")
    audit_warnings = sheet_quality_warnings(
        {
            "duplicate_dates": [date(2026, 1, 1)],
            "blank_values": 1,
            "non_numeric_values": 1,
            "dates": [date(2026, 1, 1), date(2026, 3, 1)],
        },
        "fred",
        "Quarterly",
    )
    if not any("Duplicate dates" in warning for warning in audit_warnings):
        raise RuntimeError("Internal test failed: duplicate-date warning changed.")
    if not any("off-quarter rows" in warning for warning in audit_warnings):
        raise RuntimeError("Internal test failed: quarterly sparse warning changed.")
    mismatch_warning = format_mismatch_warning([(date(2026, 5, 1), 1.0, 2.0)])
    if "2026-05-01 sheet=1.0 source=2.0" not in mismatch_warning:
        raise RuntimeError("Internal test failed: mismatch warning format changed.")

    industrial = next(item for item in INDICATORS if item.series_id == "INDPRO")
    spending = next(item for item in INDICATORS if item.series_id == "PCEC96")
    inflation = next(
        item for item in INDICATORS if item.name == "Consumer inflation expectations"
    )
    requested_fred_sources = {
        "Headline CPI inflation": "CPIAUCSL",
        "10Y real yield": "DFII10",
        "2Y government bond yield": "DGS2",
        "10Y government bond yield": "DGS10",
        "10Y-3M yield curve spread": "T10Y3M",
        "10Y term premium": "THREEFYTP10",
        "Overnight interbank rate": "IRSTCI01USM156N",
        "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level": "WALCL",
        "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level": "WDTGAL",
        "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations": "RRPONTSYD",
        "Banking system reserves": "WRESBAL",
        "Financial conditions index": "NFCI",
        "Bank credit growth": "TOTLL",
        "Corporate credit spread": "BAA10Y",
    }
    configured_fred_sources = {
        indicator.name: indicator.series_id
        for indicator in INDICATORS
        if indicator.name in requested_fred_sources
    }
    if configured_fred_sources != requested_fred_sources:
        raise RuntimeError(
            "Internal test failed: requested FRED source mapping changed."
        )
    if any(
        indicator.monthly_aggregation != "latest"
        for indicator in INDICATORS
        if indicator.name in requested_fred_sources
    ):
        raise RuntimeError(
            "Internal test failed: requested month-end selection changed."
        )
    if (industrial.units, industrial.release_lag_months) != ("lin", 0):
        raise RuntimeError(
            "Internal test failed: industrial production settings changed."
        )
    if (spending.units, spending.release_lag_months) != ("lin", 0):
        raise RuntimeError(
            "Internal test failed: real consumer spending settings changed."
        )
    if inflation.release_lag_months != 1:
        raise RuntimeError(
            "Internal test failed: inflation expectations release dating changed."
        )

    imf_revenue_indicator = next(
        item for item in INDICATORS if item.name == "Government revenue % GDP"
    )
    imf_interest_indicator = next(
        item for item in INDICATORS if item.name == "Interest paid on public debt % GDP"
    )
    imf_coverage_indicator = next(
        item for item in INDICATORS if item.name == "Government interest coverage ratio"
    )
    original_imf_get = imf_datamapper_get
    original_statuses = dict(SOURCE_STATUSES)
    original_messages = list(VALIDATION_MESSAGES)
    original_imf_cache = dict(_IMF_FPP_CACHE)
    try:

        def fake_imf_get(path: str, params: dict[str, str] | None = None) -> dict:
            if path == "indicators":
                return {
                    "indicators": {
                        "rev": {
                            "label": "Government revenue, percent of GDP",
                            "source": "Public Finances in Modern History Database",
                            "unit": "% of GDP",
                            "dataset": "FPP",
                            "last-modified": "2026-01-15 00:00:00",
                        },
                        "ie": {
                            "label": "Interest paid on public debt, percent of GDP",
                            "source": "Public Finances in Modern History Database",
                            "unit": "% of GDP",
                            "dataset": "FPP",
                            "last-modified": "2026-01-15 00:00:00",
                        },
                    }
                }
            if path == "rev/USA":
                return {"values": {"rev": {"USA": {"2024": "29.5"}}}}
            if path == "ie/USA":
                return {"values": {"ie": {"USA": {"2024": "4.0"}}}}
            raise RuntimeError(path)

        globals()["imf_datamapper_get"] = fake_imf_get
        _IMF_FPP_CACHE.clear()
        SOURCE_STATUSES.clear()
        VALIDATION_MESSAGES.clear()
        imf_revenue_records = get_imf_public_finance_observations(
            imf_revenue_indicator,
            3,
        )
        imf_interest_records = get_imf_public_finance_observations(
            imf_interest_indicator,
            3,
        )
        imf_coverage_records = get_imf_public_finance_observations(
            imf_coverage_indicator,
            3,
        )
        if (
            len(imf_revenue_records) != 1
            or len(imf_interest_records) != 1
            or len(imf_coverage_records) != 1
        ):
            raise RuntimeError("Internal test failed: IMF annual record count changed.")
        if {record["value"] for record in imf_revenue_records} != {29.5}:
            raise RuntimeError("Internal test failed: IMF revenue values changed.")
        if {record["value"] for record in imf_interest_records} != {4.0}:
            raise RuntimeError("Internal test failed: IMF interest values changed.")
        if {record["value"] for record in imf_coverage_records} != {7.375}:
            raise RuntimeError(
                "Internal test failed: IMF coverage ratio calculation changed."
            )
        if imf_revenue_records[0]["selection_date"] != date(2024, 12, 1):
            raise RuntimeError(
                "Internal test failed: IMF annual selection date changed."
            )
        if latest_available_record(imf_revenue_records, date(2026, 6, 30)) is not None:
            raise RuntimeError(
                "Internal test failed: IMF annual value was carried forward."
            )
        selected_december = latest_available_record(
            imf_revenue_records, date(2024, 12, 31)
        )
        if not selected_december or selected_december["value"] != 29.5:
            raise RuntimeError(
                "Internal test failed: IMF annual value did not match December."
            )
    finally:
        globals()["imf_datamapper_get"] = original_imf_get
        _IMF_FPP_CACHE.clear()
        _IMF_FPP_CACHE.update(original_imf_cache)
        SOURCE_STATUSES.clear()
        SOURCE_STATUSES.update(original_statuses)
        VALIDATION_MESSAGES.clear()
        VALIDATION_MESSAGES.extend(original_messages)

    current_month = month_start(date.today())
    original_fred_get = fred_get
    try:

        def fake_fred_get(endpoint: str, params: dict[str, str]) -> dict:
            if params.get("output_type") == "4":
                return {
                    "observations": [
                        {
                            "date": "2026-04-01",
                            "realtime_start": "2026-05-15",
                            "value": "101.0",
                        }
                    ]
                }
            return {
                "observations": [
                    {
                        "date": "2026-04-01",
                        "realtime_start": "2026-06-22",
                        "value": "102.5",
                    }
                ]
            }

        globals()["fred_get"] = fake_fred_get
        current_value_records = get_fred_observations(
            "test",
            "INTERNAL",
            date(2026, 4, 1),
            date(2026, 4, 30),
            frequency="Monthly",
        )
        if (
            len(current_value_records) != 1
            or current_value_records[0]["value"] != 102.5
            or current_value_records[0]["release_date"] != date(2026, 5, 15)
            or current_value_records[0]["observation_date"] != date(2026, 4, 1)
        ):
            raise RuntimeError(
                "Internal test failed: current FRED value/date matching changed."
            )
    finally:
        globals()["fred_get"] = original_fred_get

    point_in_time_records = [
        {
            "observation_date": date(2026, 3, 1),
            "release_date": date(2026, 4, 15),
            "value": 10.0,
        },
        {
            "observation_date": date(2026, 4, 1),
            "release_date": date(2026, 5, 15),
            "value": 11.0,
        },
    ]
    validate_release_records(point_in_time_records, "Internal point-in-time")
    april_selection = latest_available_record(
        point_in_time_records,
        date(2026, 4, 30),
    )
    may_selection = latest_available_record(
        point_in_time_records,
        date(2026, 5, 31),
    )
    if (
        april_selection != point_in_time_records[0]
        or may_selection != point_in_time_records[1]
    ):
        raise RuntimeError(
            "Internal test failed: release-date-aware selection changed."
        )
    if latest_available_record(point_in_time_records, date(2026, 3, 31)) is not None:
        raise RuntimeError(
            "Internal test failed: unreleased data created look-ahead bias."
        )
    if latest_available_record(point_in_time_records, date(2026, 6, 30)) is not None:
        raise RuntimeError("Internal test failed: an older value was carried forward.")
    monthly_average_records = [
        {
            "observation_date": date(2026, 4, 1),
            "release_date": date(2026, 4, 2),
            "selection_date": date(2026, 4, 1),
            "period_aligned": True,
            "monthly_aggregation": "mean",
            "value": 10.0,
        },
        {
            "observation_date": date(2026, 4, 8),
            "release_date": date(2026, 4, 9),
            "selection_date": date(2026, 4, 1),
            "period_aligned": True,
            "monthly_aggregation": "mean",
            "value": 14.0,
        },
    ]
    averaged_selection = latest_available_record(
        monthly_average_records,
        date(2026, 4, 30),
    )
    if (
        not averaged_selection
        or averaged_selection["value"] != 12.0
        or averaged_selection.get("monthly_average_count") != 2
    ):
        raise RuntimeError("Internal test failed: monthly averaging changed.")
    latest_selection = latest_available_record(
        [
            {**record, "monthly_aggregation": "latest"}
            for record in monthly_average_records
        ],
        date(2026, 4, 30),
    )
    if not latest_selection or latest_selection["value"] != 14.0:
        raise RuntimeError(
            "Internal test failed: month-end latest-point selection changed."
        )
    observation_period_record = {
        "observation_date": date(2026, 5, 1),
        "release_date": date(2026, 6, 15),
        "selection_date": date(2026, 5, 1),
        "period_aligned": True,
        "value": 12.0,
    }
    if (
        latest_available_record(
            [observation_period_record],
            date(2026, 5, 31),
        )
        != observation_period_record
    ):
        raise RuntimeError(
            "Internal test failed: FRED observation-period dating changed."
        )
    if (
        latest_available_record(
            [observation_period_record],
            date(2026, 6, 30),
        )
        is not None
    ):
        raise RuntimeError(
            "Internal test failed: observation-period value was carried forward."
        )
    original_output_months = CURRENT_OUTPUT_MONTHS
    try:
        globals()["CURRENT_OUTPUT_MONTHS"] = 3
        pmi_like_records = [
            {
                "observation_date": date(2026, 5, 1),
                "release_date": date(2026, 6, 3),
                "selection_date": date(2026, 6, 1),
                "period_aligned": True,
                "value": 52.1,
            }
        ]
        pmi_output_records = output_month_records_for_indicator(pmi_like_records)
        if pmi_output_records != {date(2026, 6, 1): 52.1}:
            raise RuntimeError(
                "Internal test failed: PMI-style release-month tab output changed."
            )
        quarterly_fred_records = [
            {
                "observation_date": date(2026, 1, 1),
                "release_date": date(2026, 3, 27),
                "realtime_start": date(2026, 3, 27),
                "source": "FRED / ALFRED",
                "source_series_id": "INTERNALQ",
                "native_frequency": "Quarterly",
                "selection_date": date(2026, 3, 1),
                "period_aligned": True,
                "value": 123.4,
            }
        ]
        quarterly_output_records = output_month_records_for_indicator(
            quarterly_fred_records
        )
        if quarterly_output_records != {date(2026, 3, 1): 123.4}:
            raise RuntimeError(
                "Internal test failed: sparse quarterly FRED output was hidden."
            )
        cleanup_rows = sparse_cleanup_plan(
            [
                ["Date", "Value"],
                ["31/01/2026", "123.4"],
                ["31/03/2026", "123.4"],
            ],
            quarterly_fred_records,
        )
        if cleanup_rows != [2]:
            raise RuntimeError(
                "Internal test failed: sparse-date cleanup target changed."
            )
    finally:
        globals()["CURRENT_OUTPUT_MONTHS"] = original_output_months
    transformed_growth = transform_percent_change_from_year_ago(
        [
            {
                "observation_date": date(2025, 4, 1),
                "release_date": date(2025, 5, 15),
                "realtime_start": date(2025, 5, 15),
                "selection_date": date(2025, 4, 1),
                "value": 100.0,
            },
            {
                "observation_date": date(2026, 4, 1),
                "release_date": date(2026, 5, 15),
                "realtime_start": date(2026, 5, 15),
                "selection_date": date(2026, 4, 1),
                "value": 103.0,
            },
        ],
        "Monthly",
        "Internal growth",
    )
    if (
        len(transformed_growth) != 1
        or transformed_growth[0]["observation_date"] != date(2026, 4, 1)
        or transformed_growth[0]["release_date"] != date(2026, 5, 15)
        or transformed_growth[0]["realtime_start"] != date(2026, 5, 15)
        or transformed_growth[0]["source"] != "FRED / ALFRED"
        or transformed_growth[0]["source_series_id"] != "Internal growth"
        or transformed_growth[0]["selection_date"] != date(2026, 4, 1)
        or transformed_growth[0]["period_aligned"] is not True
        or transformed_growth[0]["value_vintage"] != "Current FRED value"
        or abs(transformed_growth[0]["value"] - 3.0) > 1e-12
    ):
        raise RuntimeError(
            "Internal test failed: year-over-year transformation changed."
        )
    if format_output_month(date(2026, 5, 1)) != "31/05/2026":
        raise RuntimeError("Internal test failed: output date format changed.")
    if format_output_month(date(2026, 6, 1)) != "30/06/2026":
        raise RuntimeError("Internal test failed: June month-end output date changed.")
    if normalise_observation_date("-1490-01-01") is not None:
        raise RuntimeError("Internal test failed: malformed ancient date was accepted.")
    if normalise_observation_date(-1234567) is not None:
        raise RuntimeError(
            "Internal test failed: impossible spreadsheet serial date was accepted."
        )
    if sparse_replacement_date(date(2026, 1, 1), "quarterly") != date(2026, 3, 1):
        raise RuntimeError("Internal test failed: Q1 sparse replacement date changed.")
    if sparse_replacement_date(date(2026, 10, 1), "annual") != date(2026, 12, 1):
        raise RuntimeError(
            "Internal test failed: annual sparse replacement date changed."
        )
    annual_fred = Indicator("Internal annual FRED", "INTERNAL")
    original_get_indicator = get_indicator_observations
    try:
        globals()["get_indicator_observations"] = lambda *_args, **_kwargs: []
        if get_indicator_observations_safely("test", annual_fred, 3) != []:
            raise RuntimeError(
                "Internal test failed: empty sparse FRED data was not preserved."
            )
        expect_failure(
            "empty non-FRED source",
            lambda: get_indicator_observations_safely(
                "test",
                Indicator("Internal scraped source", source="nyfed_sce"),
                3,
            ),
        )
    finally:
        globals()["get_indicator_observations"] = original_get_indicator

    manufacturing_html = """
    <h1>May 2026 ISM Manufacturing PMI Report</h1>
    <div>Manufacturing PMI 54.0 52.7 +1.3</div>
    <div>New Orders 56.8 54.1 +2.7</div>
    """
    manufacturing = parse_ism_manufacturing_report(manufacturing_html, "internal-test")
    if not manufacturing or manufacturing.values != {
        "manufacturing_pmi": 54.0,
        "manufacturing_new_orders": 56.8,
    }:
        raise RuntimeError("Internal test failed: Manufacturing ISM parser changed.")

    manufacturing_prior_value_html = """
    <h1>May 2026 ISM Manufacturing PMI Report</h1>
    <div>Manufacturing PMI 54.0 53.4 +0.6</div>
    <p>The Manufacturing PMI registered 54.0 percent in May, compared with
    the 53.4-percent reading recorded in April.</p>
    <div>New Orders 56.8 54.1 +2.7</div>
    """
    manufacturing_prior_value = parse_ism_manufacturing_report(
        manufacturing_prior_value_html,
        "internal-test",
    )
    if (
        not manufacturing_prior_value
        or manufacturing_prior_value.values["manufacturing_pmi"] != 54.0
    ):
        raise RuntimeError(
            "Internal test failed: prior-month ISM value was mistaken for current data."
        )

    services_html = """
    <h1>March 2026 ISM Services PMI Report</h1>
    <div>Services PMI 54.0 56.1 -2.1 Growing Slower 21 52.7 52.4 +0.3</div>
    <div>Business Activity/Production 53.9 59.9 -6.0 Growing Slower 21 55.1 53.5 +1.6</div>
    <div>New Orders 60.6 58.6 +2.0 Growing Faster 10 53.5 55.8 -2.3</div>
    """
    services = parse_ism_services_report(services_html, "internal-test")
    expected_services = {
        "services_pmi": 54.0,
        "services_new_orders": 60.6,
        "services_business_activity": 53.9,
        "manufacturing_pmi": 52.7,
        "manufacturing_new_orders": 53.5,
    }
    if not services or services.values != expected_services:
        raise RuntimeError(
            "Internal test failed: Services ISM parser or fallback changed."
        )

    services_table_html = """
    <h1>March 2026 ISM Services PMI Report</h1>
    <table>
      <tr><th>Index</th><th>Services Current</th><th>Services Prior</th>
          <th>Change</th><th>Direction</th><th>Months</th>
          <th>Manufacturing Current</th><th>Manufacturing Prior</th>
          <th>Change</th></tr>
      <tr><td>Services PMI®</td><td>54.0</td><td>56.1</td><td>-2.1</td>
          <td>Growing Slower</td><td>21</td><td>52.7</td><td>52.4</td><td>+0.3</td></tr>
      <tr><td>Business Activity/Production</td><td>53.9</td><td>59.9</td>
          <td>-6.0</td><td>Growing Slower</td><td>21</td>
          <td>55.1</td><td>53.5</td><td>+1.6</td></tr>
      <tr><td>New Orders</td><td>60.6</td><td>58.6</td><td>+2.0</td>
          <td>Growing Faster</td><td>10</td><td>53.5</td><td>55.8</td><td>-2.3</td></tr>
    </table>
    """
    services_table = parse_ism_services_report(services_table_html, "internal-test")
    if not services_table or services_table.values != expected_services:
        raise RuntimeError("Internal test failed: ISM HTML table parser changed.")
    services_stale_metadata_html = """
    <div class="stale-metadata">June 2026 Services Index</div>
    <h1>July 2026 ISM Services PMI Report</h1>
    <div>Services PMI 54.1 54.0 +0.1</div>
    <div>Business Activity/Production 59.1 55.4 +3.7</div>
    <div>New Orders 57.2 55.1 +2.1</div>
    """
    services_stale_metadata = parse_ism_services_report(
        services_stale_metadata_html,
        "internal-test",
    )
    if (
        not services_stale_metadata
        or services_stale_metadata.report_date != date(2026, 7, 1)
        or services_stale_metadata.values["services_pmi"] != 54.1
        or services_stale_metadata.values["services_new_orders"] != 57.2
        or services_stale_metadata.values["services_business_activity"] != 59.1
    ):
        raise RuntimeError(
            "Internal test failed: stale ISM metadata overrode the visible report heading."
        )
    ism_block = pd.DataFrame(
        {
            "date": [
                "2026-01-01",
                "2026-03-01",
                "2026-04-01",
                "2026-05-01",
                "2026-06-01",
            ],
            **{column: [50.0, 51.0, 52.0, 53.0, 54.0] for column in ISM_VALUE_COLUMNS},
        }
    )
    latest_ism_block = latest_contiguous_ism_rows(ism_block, 4)
    if latest_ism_block["date"].tolist() != [
        "2026-03-01",
        "2026-04-01",
        "2026-05-01",
        "2026-06-01",
    ]:
        raise RuntimeError("Internal test failed: ISM contiguous backfill changed.")

    previous_release_month = add_months(current_month, -1)
    latest_report_month = add_months(current_month, -1)
    previous_report_month = add_months(current_month, -2)
    partial_ism_frame = pd.DataFrame(
        {
            "date": [previous_release_month.isoformat(), current_month.isoformat()],
            "report_month": [
                previous_report_month.isoformat(),
                latest_report_month.isoformat(),
            ],
            "manufacturing_pmi": [52.0, 55.6],
            "manufacturing_new_orders": [53.0, 56.7],
            "services_pmi": [51.0, None],
            "services_new_orders": [52.0, None],
            "services_business_activity": [53.0, None],
        }
    )
    original_message_count = len(VALIDATION_MESSAGES)
    try:
        validate_ism_frame(partial_ism_frame, expected_months=2)
        partial_messages = VALIDATION_MESSAGES[original_message_count:]
        if len(partial_messages) != 3 or any(
            "existing spreadsheet cells will be preserved" not in message["message"]
            for message in partial_messages
        ):
            raise RuntimeError(
                "Internal test failed: partial latest ISM warnings changed."
            )
    finally:
        del VALIDATION_MESSAGES[original_message_count:]

    historical_gap_frame = partial_ism_frame.copy()
    historical_gap_frame.loc[0, "services_pmi"] = None
    validate_ism_frame(historical_gap_frame, expected_months=2)

    manufacturing_rows = output_month_records_for_indicator(
        [
            {
                "observation_date": latest_report_month,
                "release_date": current_month.replace(day=7),
                "selection_date": current_month,
                "period_aligned": True,
                "value": 55.6,
            }
        ]
    )
    services_rows = output_month_records_for_indicator(
        [
            {
                "observation_date": previous_report_month,
                "release_date": previous_release_month.replace(day=10),
                "selection_date": previous_release_month,
                "period_aligned": True,
                "value": 51.0,
            }
        ]
    )
    if manufacturing_rows.get(current_month) != 55.6:
        raise RuntimeError(
            "Internal test failed: available latest ISM value was not exported."
        )
    if current_month in services_rows:
        raise RuntimeError(
            "Internal test failed: missing latest ISM value was carried forward."
        )

    original_explicit_blanks = {
        name: set(months) for name, months in _EXPLICIT_BLANK_OUTPUT_MONTHS.items()
    }
    try:
        _EXPLICIT_BLANK_OUTPUT_MONTHS["Services PMI"] = {current_month}
        partial_service_sheet_rows = sheet_rows_for_indicator(
            "Services PMI",
            [
                {
                    "observation_date": previous_report_month,
                    "release_date": previous_release_month.replace(day=10),
                    "selection_date": previous_release_month,
                    "period_aligned": True,
                    "value": 51.0,
                }
            ],
        )
        if partial_service_sheet_rows.get(current_month, "missing") is not None:
            raise RuntimeError(
                "Internal test failed: unavailable latest ISM cell was not blanked."
            )

        stale_service_existing = read_existing_indicator_data(
            [["Date", "Value"], [format_output_month(current_month), 99.0]],
            {"date_col": 0, "value_col": 1, "header_row": 0},
        )
        blank_fills, _blank_appends = prepare_sheet_changes(
            stale_service_existing,
            partial_service_sheet_rows,
        )
        if blank_fills != [(2, current_month, None)]:
            raise RuntimeError(
                "Internal test failed: stale latest ISM value was not scheduled for clearing."
            )

        partial_wide = merge_analysis_values_into_wide_table(
            {
                "Manufacturing PMI": [
                    ["Date", "Manufacturing PMI"],
                    [format_output_month(current_month), 55.6],
                ],
                "Services PMI": [
                    ["Date", "Services PMI"],
                    [format_output_month(current_month), 99.0],
                ],
            },
            [
                (
                    "Services PMI",
                    [
                        {
                            "observation_date": previous_report_month,
                            "release_date": previous_release_month.replace(day=10),
                            "selection_date": previous_release_month,
                            "period_aligned": True,
                            "value": 51.0,
                        }
                    ],
                )
            ],
        )
        service_column = partial_wide[0].index("Services PMI")
        if partial_wide[-1][service_column] != "":
            raise RuntimeError(
                "Internal test failed: standalone table retained a stale ISM value."
            )

        _EXPLICIT_BLANK_OUTPUT_MONTHS["Services PMI"] = set()
        published_service_rows = sheet_rows_for_indicator(
            "Services PMI",
            [
                {
                    "observation_date": latest_report_month,
                    "release_date": current_month.replace(day=10),
                    "selection_date": current_month,
                    "period_aligned": True,
                    "value": 54.0,
                }
            ],
        )
        later_fills, later_appends = prepare_sheet_changes(
            read_existing_indicator_data(
                [["Date", "Value"], [format_output_month(current_month), ""]],
                {"date_col": 0, "value_col": 1, "header_row": 0},
            ),
            published_service_rows,
        )
        if later_fills != [(2, current_month, 54.0)] or later_appends:
            raise RuntimeError(
                "Internal test failed: later ISM publication did not fill the blank."
            )
    finally:
        _EXPLICIT_BLANK_OUTPUT_MONTHS.clear()
        _EXPLICIT_BLANK_OUTPUT_MONTHS.update(original_explicit_blanks)

    expect_failure(
        "conflicting official values",
        lambda: checked_value("test", "PMI", 50.0, 51.0),
    )
    expect_failure(
        "unidentified ISM report",
        lambda: parse_ism_manufacturing_report(
            "<html><body>unexpected page</body></html>",
            "internal-test",
        ),
    )
    login_redirect = OfficialSourceRedirectError(
        "Internal ISM test",
        "https://ecommerce.ismworld.org/SSO/Login.aspx?DPLF=Y",
    )
    if not is_ism_login_redirect(login_redirect):
        raise RuntimeError(
            "Internal test failed: ISM login redirect was not recognized."
        )
    fresh_test_url = fresh_ism_url(
        "https://www.ismworld.org/reports/services/july/?keep=yes"
    )
    fresh_test_query = dict(parse_qsl(urlparse(fresh_test_url).query))
    if (
        urlparse(fresh_test_url).path != "/reports/services/july/"
        or fresh_test_query.get("keep") != "yes"
        or not fresh_test_query.get("_ism_refresh")
    ):
        raise RuntimeError("Internal test failed: ISM cache-busting URL changed.")
    expect_failure(
        "HTML disguised as XLSX",
        lambda: validate_excel_signature(b"<html>error</html>", "Internal XLSX test"),
    )
    validate_excel_signature(b"PK\x03\x04workbook", "Internal XLSX test")
    validate_excel_signature(
        b"\xd0\xcf\x11\xe0workbook",
        "Internal XLS test",
        legacy_xls=True,
    )

    duplicate_dates = pd.DataFrame(
        {
            "date": [add_months(current_month, -1), add_months(current_month, -1)],
            "value": [1.0, 2.0],
        }
    )
    expect_failure(
        "duplicate source months",
        lambda: validate_monthly_dates(duplicate_dates, "Internal monthly source"),
    )
    expect_failure(
        "missing requested observations",
        lambda: validate_observation_tail(
            [{"date": current_month, "value": 1.0}],
            "Internal monthly source",
            2,
        ),
    )
    expect_failure(
        "Google Sheets read-back mismatch",
        lambda: verify_google_sheet_values(
            [["Date", "Value"], [current_month.isoformat(), 1.0]],
            [["Date", "Value"], [current_month.isoformat(), 2.0]],
        ),
    )
    verify_google_sheet_values(
        [["Date", "Value"], [current_month.isoformat(), 54.0]],
        [["Date", "Value"], [current_month.isoformat(), "54"]],
    )
    verify_google_sheet_values(
        [["Date", "Value"], [current_month.isoformat(), 3.50]],
        [["Date", "Value"], [current_month.isoformat(), 3.5]],
    )
    verify_google_sheet_values(
        [["Date", "Value"], [format_output_month(current_month), 3.5]],
        [
            ["Date", "Value"],
            [(month_end(current_month) - date(1899, 12, 30)).days, 3.5],
        ],
    )
    current_table = [
        ["Date", "Value", "Optional"],
        [format_output_month(current_month), 3.5, ""],
    ]
    current_table_google = [
        ["Date", "Value", "Optional"],
        [(month_end(current_month) - date(1899, 12, 30)).days, "3.5"],
    ]
    if not google_sheet_table_is_current(current_table, current_table_google):
        raise RuntimeError(
            "Internal test failed: unchanged Google table was not recognised."
        )
    if google_sheet_table_is_current(
        current_table,
        [["Date", "Value", "Optional"], [current_month.isoformat(), 3.6]],
    ):
        raise RuntimeError(
            "Internal test failed: revised Google table was treated as current."
        )
    matching_history = [
        ["Date", "Value"],
        [format_output_month(add_months(current_month, -1)), 1.0],
    ]
    if not indicator_input_table_is_current(
        matching_history,
        [["Date", "Value"], [format_output_month(add_months(current_month, -1)), "1"]],
    ):
        raise RuntimeError(
            "Internal test failed: current indicator table was not recognised."
        )
    if indicator_input_table_is_current(
        matching_history,
        [["Date", "Value"], [format_output_month(add_months(current_month, -1)), 1.25]],
    ):
        raise RuntimeError(
            "Internal test failed: revised indicator table was treated as current."
        )
    validate_historical_table_replacement(
        matching_history,
        [*matching_history, [format_output_month(current_month), 2.0]],
        "Internal replacement test",
    )
    validate_historical_table_replacement(
        [["Date", "Value"], [format_output_month(current_month), 1.0]],
        [["Date", "Value"], [format_output_month(current_month), 2.0]],
        "Internal live-month replacement test",
        mutable_months={current_month},
    )
    validate_historical_table_replacement(
        matching_history,
        [["Date", "Value"], [format_output_month(add_months(current_month, -1)), 1.25]],
        "Internal authoritative revision test",
        allow_value_revisions=True,
    )
    authoritative_history = [
        ["Date", "Value"],
        [format_output_month(add_months(current_month, -2)), 1.0],
        [format_output_month(add_months(current_month, -1)), 2.0],
        ["", 999.0],
    ]
    validate_historical_table_replacement(
        authoritative_history,
        [
            ["Date", "Value"],
            [format_output_month(add_months(current_month, -1)), 2.0],
        ],
        "Internal authoritative cleanup test",
        allow_absent_existing_dates=True,
        allow_undated_existing_rows=True,
    )
    validate_historical_table_replacement(
        matching_history,
        [["Date", "Value"], [format_output_month(add_months(current_month, -1)), ""]],
        "Internal authoritative blank test",
        allow_blank_replacements=True,
    )
    expect_failure(
        "historical table replacement mismatch",
        lambda: validate_historical_table_replacement(
            matching_history,
            [
                ["Date", "Value"],
                [format_output_month(add_months(current_month, -1)), 99.0],
                [format_output_month(current_month), 2.0],
            ],
            "Internal replacement test",
        ),
    )
    historical_existing = read_existing_indicator_data(
        matching_history,
        {"date_col": 0, "value_col": 1, "header_row": 0},
    )
    validate_overlapping_indicator_values(
        historical_existing,
        {add_months(current_month, -1): 1.0, current_month: 2.0},
        "Internal overlap test",
    )
    current_existing = read_existing_indicator_data(
        [["Date", "Value"], [format_output_month(current_month), 1.0]],
        {"date_col": 0, "value_col": 1, "header_row": 0},
    )
    validate_overlapping_indicator_values(
        current_existing,
        {current_month: 2.0},
        "Internal live-month overlap test",
        mutable_months={current_month},
    )
    validate_overlapping_indicator_values(
        historical_existing,
        {add_months(current_month, -1): 1.25},
        "Internal authoritative overlap revision test",
        allow_value_revisions=True,
    )
    expect_failure(
        "historical indicator overlap mismatch",
        lambda: validate_overlapping_indicator_values(
            historical_existing,
            {add_months(current_month, -1): 2.0},
            "Internal overlap test",
        ),
    )
    verify_google_sheet_values(
        [["Date", "Value"], [current_month.isoformat(), 837340.12424853]],
        [["Date", "Value"], [current_month.isoformat(), "837340.1242"]],
    )
    verify_google_sheet_values(
        [["Source", "Status", "Revision"], ["ISM", "OK", ""]],
        [["Source", "Status", "Revision"], ["ISM", "OK"]],
    )
    wide_fixture = merge_analysis_values_into_wide_table(
        {
            "Manufacturing PMI": [
                ["Date", "Manufacturing PMI"],
                ["31/01/2020", 50.0],
                ["31/03/2020", 42.0],
            ],
            CENTRAL_BANK_LIQUIDITY_SHEET: [
                ["Date", "Assets", "TGA", "RRP"],
                ["31/01/2020", 100.0, 20.0, 3.0],
            ],
            "Government interest coverage ratio": [
                ["Date", "Revenue", "Interest", "Coverage"],
                ["31/01/2020", 10.0, 2.0, 5.0],
            ],
        }
    )
    if len(wide_fixture) != 4:
        raise RuntimeError(
            "Internal test failed: wide table did not create contiguous months."
        )
    wide_headers = wide_fixture[0]
    january_row, february_row, march_row = wide_fixture[1:]
    if january_row[wide_headers.index("Manufacturing PMI")] != 50.0:
        raise RuntimeError(
            "Internal test failed: wide-table ordinary indicator mapping changed."
        )
    if february_row[wide_headers.index("Manufacturing PMI")] != "":
        raise RuntimeError(
            "Internal test failed: wide-table missing months must remain blank."
        )
    if march_row[wide_headers.index("Manufacturing PMI")] != 42.0:
        raise RuntimeError("Internal test failed: wide-table month ordering changed.")
    first_liquidity_component = next(iter(CENTRAL_BANK_LIQUIDITY_COMPONENTS))
    if (
        january_row[
            wide_headers.index(wide_indicator_header(first_liquidity_component))
        ]
        != 100.0
    ):
        raise RuntimeError(
            "Internal test failed: wide-table liquidity mapping changed."
        )
    if january_row[wide_headers.index("Government interest coverage ratio")] != 5.0:
        raise RuntimeError("Internal test failed: wide-table coverage mapping changed.")
    reserves_column = wide_headers.index("Banking system reserves")
    old_wide_row = list(january_row)
    old_wide_row[reserves_column] = 10452.0
    preserved_wide_row = preserve_inaccessible_wide_history(
        wide_fixture,
        [wide_headers, old_wide_row],
        None,
    )[1]
    if preserved_wide_row[reserves_column] != 10452.0:
        raise RuntimeError(
            "Internal test failed: inaccessible standalone history was deleted."
        )
    preserved_history_wide_fixture = merge_analysis_values_into_wide_table(
        {
            "Manufacturing PMI": [
                ["Date", "Manufacturing PMI"],
                ["31/01/2020", 50.0],
                ["29/02/2020", 51.0],
            ],
            "10Y real yield": [
                ["Date", "10Y real yield"],
                ["31/01/2020", 9.9],
                ["29/02/2020", 2.0],
            ],
        },
        [
            (
                "10Y real yield",
                [
                    {
                        "observation_date": date(2020, 2, 1),
                        "release_date": date(2020, 2, 29),
                        "selection_date": date(2020, 2, 1),
                        "period_aligned": True,
                        "source": "FRED / ALFRED",
                        "native_frequency": "Daily",
                        "history_complete": True,
                        "value": 2.0,
                    }
                ],
            ),
            (
                "Manufacturing PMI",
                [
                    {
                        "observation_date": date(2020, 2, 1),
                        "release_date": date(2020, 3, 1),
                        "selection_date": date(2020, 3, 1),
                        "period_aligned": True,
                        "source": "Institute for Supply Management",
                        "value": 52.0,
                    }
                ],
            ),
        ],
    )
    preserved_headers = preserved_history_wide_fixture[0]
    real_yield_column = preserved_headers.index("10Y real yield")
    if preserved_history_wide_fixture[1][real_yield_column] != 9.9:
        raise RuntimeError(
            "Internal test failed: inaccessible historical data was deleted."
        )
    if preserved_history_wide_fixture[2][real_yield_column] != 2.0:
        raise RuntimeError(
            "Internal test failed: fresh source value was not merged into preserved history."
        )
    pmi_column = preserved_headers.index("Manufacturing PMI")
    if [row[pmi_column] for row in preserved_history_wide_fixture[1:]] != [
        50.0,
        51.0,
        52.0,
    ]:
        raise RuntimeError(
            "Internal test failed: shorter PMI history did not preserve legacy values."
        )
    if has_complete_source_history([{"source": "FRED / ALFRED"}]):
        raise RuntimeError(
            "Internal test failed: provider name alone authorised destructive cleanup."
        )
    if not has_complete_source_history(
        [{"source": "FRED / ALFRED", "history_complete": True}]
    ):
        raise RuntimeError(
            "Internal test failed: verified full FRED history was not recognised."
        )
    if has_complete_source_history(
        [
            {
                "source": "Institute for Supply Management",
                "history_complete": True,
            }
        ]
    ):
        raise RuntimeError(
            "Internal test failed: limited PMI history was authorised for deletion."
        )
    expect_failure(
        "missing populated trailing Google cell",
        lambda: verify_google_sheet_values(
            [["Source", "Status", "Revision"], ["ISM", "OK", "changed"]],
            [["Source", "Status", "Revision"], ["ISM", "OK"]],
        ),
    )
    incoming_records = [
        {
            "observation_date": date(2026, 3, 1),
            "release_date": date(2026, 4, 1),
            "value": 48.7,
        },
        {
            "observation_date": date(2026, 4, 1),
            "release_date": date(2026, 5, 1),
            "value": 51.6,
        },
        {
            "observation_date": date(2026, 5, 1),
            "release_date": date(2026, 6, 1),
            "value": 52.1,
        },
        {
            "observation_date": date(2026, 5, 20),
            "release_date": date(2026, 6, 2),
            "value": 54.1,
        },
    ]
    deduped_incoming = monthly_records_for_indicator(incoming_records)
    if deduped_incoming[date(2026, 5, 1)] != 54.1:
        raise RuntimeError(
            "Internal test failed: incoming month-end selection changed."
        )

    empty_layout = get_sheet_layout([])
    empty_existing = read_existing_indicator_data([], empty_layout)
    empty_fills, empty_appends = prepare_sheet_changes(empty_existing, deduped_incoming)
    if empty_fills:
        raise RuntimeError(
            "Internal test failed: empty sheet produced fill operations."
        )
    if len(empty_appends) != 3:
        raise RuntimeError("Internal test failed: empty sheet append planning changed.")

    existing_values = [
        ["Date", "Value"],
        ["2026-03-01", "48.7"],
        ["2026-04", "51.6"],
        ["May 2026", "54.1"],
    ]
    layout = get_sheet_layout(existing_values)
    existing = read_existing_indicator_data(existing_values, layout)
    up_to_date_fills, up_to_date_appends = prepare_sheet_changes(
        existing, deduped_incoming
    )
    if up_to_date_fills or up_to_date_appends:
        raise RuntimeError("Internal test failed: up-to-date sheet sync changed.")
    malformed_tail_values = [
        ["Date", "Value"],
        ["2026-04-01", "51.6"],
        ["not a date", "ignore me"],
        ["", ""],
    ]
    malformed_existing = read_existing_indicator_data(
        malformed_tail_values,
        get_sheet_layout(malformed_tail_values),
    )
    malformed_fills, malformed_appends = prepare_sheet_changes(
        malformed_existing,
        {date(2026, 5, 1): 52.1},
    )
    if malformed_fills or malformed_appends != [(date(2026, 5, 1), 52.1)]:
        raise RuntimeError(
            "Internal test failed: malformed tail date handling changed."
        )

    one_new_incoming = {
        **deduped_incoming,
        date(2026, 6, 1): 55.0,
    }
    one_new_fills, one_new_appends = prepare_sheet_changes(existing, one_new_incoming)
    if one_new_fills or one_new_appends != [(date(2026, 6, 1), 55.0)]:
        raise RuntimeError("Internal test failed: single append planning changed.")

    rerun_existing_values = [
        *existing_values,
        ["2026-06-01", "55.0"],
    ]
    rerun_existing = read_existing_indicator_data(rerun_existing_values, layout)
    rerun_fills, rerun_appends = prepare_sheet_changes(rerun_existing, one_new_incoming)
    if rerun_fills or rerun_appends:
        raise RuntimeError("Internal test failed: rerun duplicate prevention changed.")

    revised_incoming = {
        **deduped_incoming,
        date(2026, 4, 1): 52.0,
    }
    revised_fills, revised_appends = prepare_sheet_changes(existing, revised_incoming)
    if revised_fills != [(3, date(2026, 4, 1), 52.0)] or revised_appends:
        raise RuntimeError(
            "Internal test failed: changed official values were not updated."
        )

    blank_existing_values = [
        ["Date", "Value"],
        ["2026-03-01", ""],
    ]
    blank_existing = read_existing_indicator_data(blank_existing_values, layout)
    blank_fills, blank_appends = prepare_sheet_changes(
        blank_existing,
        {date(2026, 3, 1): 48.7},
    )
    if blank_fills != [(2, date(2026, 3, 1), 48.7)] or blank_appends:
        raise RuntimeError(
            "Internal test failed: blank existing value was not filled safely."
        )
    old_history_fills, old_history_only = prepare_sheet_changes(
        existing,
        {date(2026, 2, 1): 47.0},
    )
    if old_history_fills or old_history_only:
        raise RuntimeError("Internal test failed: old missing history was backfilled.")

    expect_failure(
        "malformed indicator sheet date column",
        lambda: get_sheet_layout([["Notes", "Value"], ["bad", "1"]]),
    )
    if indicator_sheet_name("Manufacturing PMI", {"manufacturing pmi"}) is not None:
        raise RuntimeError("Internal test failed: inexact sheet matching was allowed.")
    if (
        indicator_sheet_name("Manufacturing PMI", {"Manufacturing PMI"})
        != "Manufacturing PMI"
    ):
        raise RuntimeError("Internal test failed: exact sheet matching changed.")
    if indicator_sheet_name("Missing Indicator", {"Manufacturing PMI"}) is not None:
        raise RuntimeError("Internal test failed: missing sheet detection changed.")
    month_end_header_layout = get_sheet_layout(
        [["Month-End Date", "Central Bank Total Assets"]],
        CENTRAL_BANK_LIQUIDITY_SHEET,
    )
    if (
        month_end_header_layout["date_col"] != 0
        or month_end_header_layout["value_col"] != 1
    ):
        raise RuntimeError("Internal test failed: month-end date sheet layout changed.")
    original_output_months = CURRENT_OUTPUT_MONTHS
    try:
        globals()["CURRENT_OUTPUT_MONTHS"] = 1
        central_fills, central_rows = central_bank_append_rows(
            [
                [
                    "Date",
                    "Central Bank Total Assets",
                    "Cental Bank General Account",
                    "Central Bank RRP",
                ]
            ],
            {
                "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level": [
                    {
                        "observation_date": month_start(date.today()),
                        "release_date": month_start(date.today()),
                        "selection_date": month_start(date.today()),
                        "period_aligned": True,
                        "value": 1.0,
                    }
                ],
                "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level": [
                    {
                        "observation_date": month_start(date.today()),
                        "release_date": month_start(date.today()),
                        "selection_date": month_start(date.today()),
                        "period_aligned": True,
                        "value": 2.0,
                    }
                ],
                "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations": [
                    {
                        "observation_date": month_start(date.today()),
                        "release_date": month_start(date.today()),
                        "selection_date": month_start(date.today()),
                        "period_aligned": True,
                        "value": 3.0,
                    }
                ],
            },
        )[1:3]
        if central_fills:
            raise RuntimeError(
                "Internal test failed: empty central-bank tab produced fill operations."
            )
        if central_rows != [
            [format_output_month(month_start(date.today())), 1.0, 2.0, 3.0]
        ]:
            raise RuntimeError(
                "Internal test failed: central-bank shared tab row mapping changed."
            )
        central_blank_fills, central_blank_rows = central_bank_append_rows(
            [
                [
                    "Date",
                    "Central Bank Total Assets",
                    "Cental Bank General Account",
                    "Central Bank RRP",
                ],
                [format_output_month(month_start(date.today())), "", "", ""],
            ],
            {
                "Assets: Total Assets: Total Assets (Less Eliminations from Consolidation): Wednesday Level": [
                    {
                        "observation_date": month_start(date.today()),
                        "release_date": month_start(date.today()),
                        "selection_date": month_start(date.today()),
                        "period_aligned": True,
                        "value": 1.0,
                    }
                ],
                "Liabilities and Capital: Liabilities: Deposits with F.R. Banks, Other Than Reserve Balances: U.S. Treasury, General Account: Wednesday Level": [
                    {
                        "observation_date": month_start(date.today()),
                        "release_date": month_start(date.today()),
                        "selection_date": month_start(date.today()),
                        "period_aligned": True,
                        "value": 2.0,
                    }
                ],
                "Overnight Reverse Repurchase Agreements: Treasury Securities Sold by the Federal Reserve in the Temporary Open Market Operations": [
                    {
                        "observation_date": month_start(date.today()),
                        "release_date": month_start(date.today()),
                        "selection_date": month_start(date.today()),
                        "period_aligned": True,
                        "value": 3.0,
                    }
                ],
            },
        )[1:3]
        if (
            central_blank_fills
            != [
                (2, 1, month_start(date.today()), 1.0),
                (2, 2, month_start(date.today()), 2.0),
                (2, 3, month_start(date.today()), 3.0),
            ]
            or central_blank_rows
        ):
            raise RuntimeError(
                "Internal test failed: central-bank blank columns were not filled safely."
            )
    finally:
        globals()["CURRENT_OUTPUT_MONTHS"] = original_output_months

    summary_month = add_months(current_month, -1)
    nyfed_summary = f"""
    <h1>{summary_month:%B} Survey: Inflation Expectations Down</h1>
    <p>Median inflation expectations decreased to 3.5 percent at the
    one-year-ahead horizon.</p>
    """
    parsed_summary = parse_nyfed_page_latest(nyfed_summary)
    if parsed_summary != (summary_month, 3.5):
        raise RuntimeError("Internal test failed: NY Fed summary parser changed.")

    synthetic_sce_records = []
    for source_month, value in [
        (date(2026, 3, 1), 3.4),
        (date(2026, 4, 1), 3.6),
        (date(2026, 5, 1), 3.5),
    ]:
        synthetic_sce_records.append(
            {
                "observation_date": source_month,
                "release_date": add_months(source_month, 1).replace(day=20),
                "source": "Federal Reserve Bank of New York SCE",
                "source_series_id": "Median one-year ahead expected inflation rate",
                "selection_date": add_months(source_month, 1),
                "period_aligned": True,
                "value": value,
            }
        )
    expected_sce_values = {
        date(2026, 4, 30): 3.4,
        date(2026, 5, 31): 3.6,
        date(2026, 6, 30): 3.5,
    }
    for row_date, expected_value in expected_sce_values.items():
        selected = latest_available_record(synthetic_sce_records, row_date)
        if not selected or selected["value"] != expected_value:
            raise RuntimeError(
                "Internal test failed: NY Fed SCE release-month dating changed."
            )
    if latest_available_record(synthetic_sce_records, date(2026, 7, 31)) is not None:
        raise RuntimeError(
            "Internal test failed: NY Fed SCE value was carried forward."
        )

    original_archive_root = SOURCE_ARCHIVE_ROOT
    original_statuses = dict(SOURCE_STATUSES)
    try:
        with tempfile.TemporaryDirectory() as temporary_directory:
            SOURCE_ARCHIVE_ROOT = Path(temporary_directory)
            response = requests.Response()
            response.status_code = 200
            response._content = b"<html>validated official data</html>"
            response.url = "https://www.ismworld.org/test"
            response.headers["Content-Type"] = "text/html"
            response.request = requests.Request(
                "GET",
                "https://www.ismworld.org/test",
            ).prepare()
            status = archive_validated_source(
                "Internal Archive",
                current_month.isoformat(),
                response,
                "html",
                ("value",),
                {"value": 54.0},
            )
            archived = load_archived_source(
                "Internal Archive",
                current_month.isoformat(),
            )
            if (
                archived is None
                or status.sha256 != hashlib.sha256(response.content).hexdigest()
            ):
                raise RuntimeError(
                    "Internal test failed: source archive was not readable."
                )
            raw_content, metadata = archived
            if (
                raw_content != response.content
                or metadata["parsed_values"]["value"] != 54.0
            ):
                raise RuntimeError(
                    "Internal test failed: source archive metadata changed."
                )
            archive_validated_source(
                "Internal Archive",
                add_months(current_month, -1).isoformat(),
                response,
                "html",
                ("value",),
                {"value": 53.0},
            )
            latest_archive = load_latest_archived_source("Internal Archive")
            if (
                latest_archive is None
                or latest_archive[1]["period"] != current_month.isoformat()
            ):
                raise RuntimeError(
                    "Internal test failed: source archive latest pointer moved backward."
                )
    finally:
        SOURCE_ARCHIVE_ROOT = original_archive_root
        SOURCE_STATUSES.clear()
        SOURCE_STATUSES.update(original_statuses)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync monthly indicator observations into dedicated Google Sheets tabs."
    )
    parser.add_argument(
        "--series-id",
        default=os.getenv("FRED_SERIES_ID"),
        help="Optional single FRED series ID, for example UNRATE or CPIAUCSL. If omitted, all built-in indicators are synced.",
    )
    parser.add_argument(
        "--indicator-name",
        action="append",
        default=[],
        help=(
            "Sync only the named built-in indicator. Repeat for multiple indicators. "
            "Names must exactly match --list-indicators output."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("FRED_API_KEY"),
        help="FRED API key. Required in the run command when exporting FRED data.",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=DEFAULT_OUTPUT_MONTHS,
        help=f"Number of monthly rows to include. Default: {DEFAULT_OUTPUT_MONTHS}.",
    )
    history_mode = parser.add_mutually_exclusive_group()
    history_mode.add_argument(
        "--recent-only",
        action="store_true",
        help="Compatibility flag; incremental sync is already the default.",
    )
    history_mode.add_argument(
        "--full-history",
        action="store_true",
        help="Explicitly reconcile full source history and permit value revisions.",
    )
    parser.add_argument(
        "--spreadsheet-id",
        default=os.getenv("GOOGLE_SPREADSHEET_ID", DEFAULT_GOOGLE_SPREADSHEET_ID),
        help="Existing analysis workbook ID whose individual indicator tabs are updated.",
    )
    parser.add_argument(
        "--wide-spreadsheet-id",
        default=os.getenv("GOOGLE_WIDE_SPREADSHEET_ID", DEFAULT_WIDE_SPREADSHEET_ID),
        help="Standalone workbook ID for the consolidated date-by-indicator table.",
    )
    parser.add_argument(
        "--wide-sheet-name",
        default=os.getenv("GOOGLE_WIDE_SHEET_NAME", DEFAULT_WIDE_SHEET_NAME),
        help="Tab name for the standalone consolidated table.",
    )
    parser.add_argument(
        "--skip-wide-sync",
        action="store_true",
        help="Update only the existing individual analysis tabs.",
    )
    parser.add_argument(
        "--skip-local-outputs",
        action="store_true",
        help="Do not replace the complete local CSV artifacts during a scoped recovery sync.",
    )
    parser.add_argument(
        "--wide-only-from-analysis",
        action="store_true",
        help="Rebuild only the standalone table from existing analysis-tab inputs; no source API key is required.",
    )
    parser.add_argument(
        "--credentials",
        default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_GOOGLE_CREDENTIALS),
        help="Path to the Google service account JSON file. The project default is already set.",
    )
    parser.add_argument(
        "--sheet-name",
        default=os.getenv("GOOGLE_SHEET_NAME", DEFAULT_GOOGLE_SHEET_NAME),
        help="Legacy combined-table sheet name; dedicated indicator-tab sync does not use this in the normal export.",
    )
    parser.add_argument(
        "--list-indicators",
        action="store_true",
        help="Print the built-in indicators and exit.",
    )
    parser.add_argument(
        "--exclude-ism",
        action="store_true",
        help="Do not include the official ISM PMI indicators in the built-in export.",
    )
    parser.add_argument(
        "--ism-only",
        action="store_true",
        help="Export only the official ISM PMI indicators. This does not require a FRED API key.",
    )
    parser.add_argument(
        "--check-non-api",
        action="store_true",
        help="Check official ISM, NY Fed, and Michigan non-API sources, then exit.",
    )
    parser.add_argument(
        "--audit-google-sheet",
        action="store_true",
        help="Read the target Google workbook and print a non-mutating tab audit, then exit.",
    )
    parser.add_argument(
        "--cleanup-sparse-dates",
        action="store_true",
        help="Delete old off-quarter/off-year-end rows for quarterly/annual indicators after syncing correct rows.",
    )
    parser.add_argument(
        "--cleanup-sparse-dates-only",
        action="store_true",
        help="Only delete old off-quarter/off-year-end sparse rows; do not run the full sheet sync.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in offline safety tests, then exit.",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    global CURRENT_OUTPUT_MONTHS, FULL_HISTORY_MODE
    configured_history_mode = FULL_HISTORY_MODE
    FULL_HISTORY_MODE = True
    try:
        run_internal_self_tests()
    finally:
        FULL_HISTORY_MODE = configured_history_mode
    if args.self_test:
        print("Internal self-test passed.")
        return 0
    SOURCE_STATUSES.clear()
    VALIDATION_MESSAGES.clear()
    _EXPLICIT_BLANK_OUTPUT_MONTHS.clear()
    CURRENT_OUTPUT_MONTHS = args.months
    FULL_HISTORY_MODE = bool(args.full_history)

    if args.list_indicators:
        for indicator in INDICATORS:
            if indicator.source != "fred":
                source = f"{indicator.source}:{indicator.source_column}"
            else:
                source = (
                    indicator.series_id
                    or f"{indicator.numerator_series_id}/{indicator.denominator_series_id}"
                )
            print(f"{indicator.name}: {source}")
        return 0

    if args.wide_only_from_analysis:
        summary = sync_wide_indicator_workbook(
            args.spreadsheet_id,
            args.wide_spreadsheet_id,
            Path(args.credentials),
            args.wide_sheet_name,
            validate_only=False,
        )
        action = "Would update" if False else "Updated"
        print(
            f"{action} standalone table: "
            f"https://docs.google.com/spreadsheets/d/{args.wide_spreadsheet_id} "
            f"tab={args.wide_sheet_name!r} rows={summary['rows']} "
            f"columns={summary['columns']} dates={summary['first_date']}..{summary['last_date']}"
        )
        return 0

    if args.check_non_api:
        check_non_api_sources(args.months)
        return 0

    if args.cleanup_sparse_dates_only:
        if not args.api_key:
            print(
                "Please provide a FRED API key in the run command with --api-key YOUR_KEY.",
                file=sys.stderr,
            )
            return 2
        sparse_workbook_data = []
        for indicator in INDICATORS:
            if not has_dedicated_indicator_tab(indicator.name):
                continue
            if indicator.source == "fred":
                metadata = get_fred_series_metadata(
                    args.api_key, indicator.series_id or ""
                )
                if not str(metadata.get("frequency", "")).startswith(
                    SPARSE_FRED_FREQUENCY_PREFIXES
                ):
                    continue
            elif indicator.source != "imf_datamapper":
                continue
            records = get_indicator_observations_safely(
                args.api_key, indicator, args.months
            )
            if sparse_date_rule_for_records(records) is not None:
                sparse_workbook_data.append((indicator.name, records))
        cleanup_sparse_date_rows(
            args.spreadsheet_id,
            Path(args.credentials),
            sparse_workbook_data,
            False,
        )
        action = "Checked" if False else "Updated"
        print(
            f"{action} Google Sheet: https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}"
        )
        return 0

    if args.audit_google_sheet:
        source_data = None
        source_errors = None
        if args.api_key:
            print("Collecting current source output for Google Sheet audit...")
            if any(indicator.source != "fred" for indicator in INDICATORS):
                check_non_api_sources(args.months)
            source_data, source_errors = collect_source_data_for_audit(
                args.api_key,
                args.months,
            )
        else:
            print(
                "Running sheet-only audit. Add --api-key to compare against live source output."
            )
        audit_google_workbook(
            args.spreadsheet_id,
            Path(args.credentials),
            source_data,
            source_errors,
        )
        return 0

    selected_indicators = INDICATORS
    if args.indicator_name:
        requested_names = set(args.indicator_name)
        known_names = {indicator.name for indicator in INDICATORS}
        unknown_names = sorted(requested_names - known_names)
        if unknown_names:
            print(
                "Unknown --indicator-name value(s): " + ", ".join(unknown_names),
                file=sys.stderr,
            )
            return 2
        selected_indicators = [
            indicator for indicator in INDICATORS if indicator.name in requested_names
        ]
    elif args.ism_only:
        selected_indicators = [
            indicator for indicator in INDICATORS if indicator.source == "ism"
        ]
    elif args.exclude_ism:
        selected_indicators = [
            indicator for indicator in INDICATORS if indicator.source != "ism"
        ]

    needs_fred = any(indicator.source == "fred" for indicator in selected_indicators)
    if needs_fred and not args.api_key:
        print(
            "Please provide a FRED API key in the run command with --api-key YOUR_KEY.",
            file=sys.stderr,
        )
        return 2
    if args.months < 1:
        print("--months must be at least 1.", file=sys.stderr)
        return 2

    if args.series_id:
        title = get_series_title(args.api_key, args.series_id)
        observations = get_monthly_observations(
            args.api_key, args.series_id, args.months
        )
        workbook_data = [(title, observations)]
        single_indicator = Indicator(title, args.series_id)
        sheet_values_for_google(workbook_data)
        metadata_values = indicator_metadata_values(args.api_key, [single_indicator])
        validation_values = build_validation_report(
            [single_indicator],
            workbook_data,
            args.months,
        )
        diagnostic_values = selection_diagnostic_values(
            [single_indicator],
            workbook_data,
            args.months,
        )
        if not args.skip_local_outputs:
            save_clean_csv_outputs(
                workbook_data,
                metadata_values,
                validation_values,
                diagnostic_values,
            )
        if not False:
            assert_sync_preflight_passed(
                sync_all_indicators(
                    args.spreadsheet_id,
                    Path(args.credentials),
                    workbook_data,
                    validate_only=True,
                    _preflight=True,
                )
            )
        if not args.skip_wide_sync:
            sync_wide_indicator_workbook(
                args.spreadsheet_id,
                args.wide_spreadsheet_id,
                Path(args.credentials),
                args.wide_sheet_name,
                workbook_data,
                False,
            )
        sync_results = sync_all_indicators(
            args.spreadsheet_id,
            Path(args.credentials),
            workbook_data,
            False,
        )
        assert_sync_preflight_passed(sync_results)
        action = "Checked" if False else "Updated"
        print(
            f"{action} Google Sheet: https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}"
        )
        return 0

    if any(indicator.source != "fred" for indicator in selected_indicators):
        print("Checking official non-FRED sources before export...")
        check_non_api_sources(args.months)
        print("All non-FRED source checks passed.")

    workbook_data = []
    printed_imf_audit = False
    for indicator in selected_indicators:
        observations = get_indicator_observations_safely(
            args.api_key, indicator, args.months
        )
        workbook_data.append((indicator.name, observations))
        if COMPACT_TERMINAL:
            print(f"Fetched {len(observations)} observations for {indicator.name}")
        if indicator.source == "imf_datamapper" and not printed_imf_audit:
            print_imf_public_finance_audit(
                indicator.source_column or DEFAULT_IMF_COUNTRY
            )
            printed_imf_audit = True

    validate_non_fred_output_alignment(
        selected_indicators,
        workbook_data,
        args.months,
    )
    sheet_values_for_google(workbook_data)
    metadata_values = indicator_metadata_values(args.api_key, selected_indicators)
    validation_values = build_validation_report(
        selected_indicators,
        workbook_data,
        args.months,
    )
    diagnostic_values = selection_diagnostic_values(
        selected_indicators,
        workbook_data,
        args.months,
    )
    if any(row[0] == "ERROR" for row in validation_values[1:]):
        errors = [row[2] for row in validation_values[1:] if row[0] == "ERROR"]
        raise RuntimeError("Validation report contains errors: " + " | ".join(errors))
    print_changed_cells(workbook_data)
    if not args.skip_local_outputs:
        save_clean_csv_outputs(
            workbook_data,
            metadata_values,
            validation_values,
            diagnostic_values,
        )
    if not False:
        assert_sync_preflight_passed(
            sync_all_indicators(
                args.spreadsheet_id,
                Path(args.credentials),
                workbook_data,
                validate_only=True,
                _preflight=True,
            )
        )
    if not args.skip_wide_sync:
        summary = sync_wide_indicator_workbook(
            args.spreadsheet_id,
            args.wide_spreadsheet_id,
            Path(args.credentials),
            args.wide_sheet_name,
            workbook_data,
            False,
        )
        action = "Would update" if False else "Updated"
        print(
            f"{action} standalone table: "
            f"https://docs.google.com/spreadsheets/d/{args.wide_spreadsheet_id} "
            f"tab={args.wide_sheet_name!r} rows={summary['rows']} "
            f"columns={summary['columns']}"
        )
    sync_results = sync_all_indicators(
        args.spreadsheet_id,
        Path(args.credentials),
        workbook_data,
        False,
    )
    assert_sync_preflight_passed(sync_results)
    if args.cleanup_sparse_dates:
        cleanup_sparse_date_rows(
            args.spreadsheet_id,
            Path(args.credentials),
            workbook_data,
            False,
        )
    action = "Checked" if False else "Updated"
    print(
        f"{action} Google Sheet: https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}"
    )

    return 0


from notifications.lifecycle import notify_entrypoint, record_failure


@notify_entrypoint("Indicators")
def main() -> int:
    console_handler = logging.StreamHandler()
    if COMPACT_TERMINAL:
        # Detailed retries and tracebacks remain in the log file. The terminal
        # shows the same concise progress-and-summary style as the UK pipeline.
        console_handler.setLevel(logging.CRITICAL)
    file_handler = logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            console_handler,
            file_handler,
        ],
        force=True,
    )
    args = parse_args()
    try:
        return run(args)
    except Exception as exc:
        logging.exception("Monthly Indicators export failed.")
        record_failure(f"{type(exc).__name__}: {exc}")
        action = (
            "audit failed"
            if args.audit_google_sheet
            else "Google Sheet was not updated"
        )
        print(
            f"\nERROR: The {action} because a data source or Google Sheets check "
            f"failed.\n{exc}\nDetails: {ERROR_LOG_PATH}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
