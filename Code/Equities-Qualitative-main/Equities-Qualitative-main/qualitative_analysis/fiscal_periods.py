"""Issuer-aware, evidence-preserving fiscal-period extraction."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import unquote

from .fiscal_calendar_registry import lookup_fiscal_calendar
from .models import FiscalPeriod, IssuerFiscalCalendar

PERIOD_UNKNOWN = "UNKNOWN"
PERIOD_QUARTER_ACTUAL = "QUARTER_ACTUAL"
PERIOD_ANNUAL_ACTUAL = "ANNUAL_ACTUAL"
PERIOD_YTD_ACTUAL = "YTD_ACTUAL"
PERIOD_GUIDANCE_HORIZON = "GUIDANCE_HORIZON"
PERIOD_EVENT_DATE = "EVENT_DATE"

_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
    "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
    "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


def _period(*, year=None, quarter=None, label=None, method=None, confidence=None,
            warning=None, period_type=PERIOD_UNKNOWN, evidence=None, sources=None,
            period_end_date=None, issuer_fiscal_calendar=None) -> FiscalPeriod:
    return FiscalPeriod(
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_label=label,
        detection_method=method,
        confidence=confidence,
        warning=warning,
        period_type=period_type,
        detection_evidence=evidence,
        detection_sources=list(sources or []),
        period_end_date=period_end_date,
        issuer_fiscal_calendar=dict(issuer_fiscal_calendar or {}),
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_from_parts(parts: tuple[int, int, int] | None) -> str | None:
    if not parts:
        return None
    month, day, year = parts
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _date_parts(value: str) -> tuple[int, int, int] | None:
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2})(?:,|\s)\s*(20\d{2}|19\d{2})\b", value, re.I,
    )
    if match:
        return _MONTHS[match.group(1).upper()], int(match.group(2)), int(match.group(3))
    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2}|19\d{2})\b", value)
    if match:
        return int(match.group(2)), int(match.group(1)), int(match.group(3))
    return None


def _period_end_date(value: str) -> str | None:
    """Extract a reporting period end, never an arbitrary narrative date."""
    if not value:
        return None
    patterns = (
        r"\b(?:FOR\s+THE\s+)?(?:QUARTERLY\s+PERIOD|THREE\s+MONTHS|QUARTER)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
        r"\b(?:FISCAL\s+YEAR|YEAR)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
        r"\b(?:RESULTS|RESULT)\s+FOR\s+THE\s+(?:QUARTER|YEAR)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return _date_from_parts(_date_parts(match.group(0)))
    return None


def _fiscal_calendar(metadata: Mapping[str, Any] | None, value: str = "") -> IssuerFiscalCalendar:
    """Resolve calendar facts from explicit metadata and filing language."""
    metadata = metadata or {}
    registry = lookup_fiscal_calendar(metadata)
    nested = metadata.get("issuer_fiscal_calendar")
    source_values = {**registry, **(nested if isinstance(nested, Mapping) else {}), **metadata}
    month = _int(source_values.get("fiscal_year_end_month") or source_values.get("fiscal_year_end_month_number"))
    day = _int(source_values.get("fiscal_year_end_day"))
    end_text = str(source_values.get("fiscal_year_end") or source_values.get("fiscal_year_end_date") or "")
    evidence = end_text or None
    search_text = " ".join((end_text, value))
    month_match = re.search(
        r"(?:last\s+(?:Sunday|day)\s+in|fiscal\s+year\s+ended|fiscal\s+year(?:[- ]end)?(?:ing)?(?:\s+on)?|year[- ]end)\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)",
        search_text, re.I,
    )
    if month is None and month_match:
        month = _MONTHS[month_match.group(1).upper()]
    week_based = bool(source_values.get("week_based")) or bool(re.search(r"last\s+(?:Sunday|day)\s+in\s+[A-Za-z]+|\b(?:52|53)[- ]week", search_text, re.I))
    if month is None:
        month = 12
    if not week_based and day is None:
        explicit_end = re.search(r"(?:fiscal\s+year|year)\s+ended\s+([A-Za-z]+\s+\d{1,2},?\s+20\d{2})", value, re.I)
        if explicit_end:
            parts = _date_parts(explicit_end.group(1))
            if parts and parts[0] == month:
                day = parts[1]
                evidence = explicit_end.group(0)
    weeks = _int(source_values.get("weeks_per_year"))
    week_match = re.search(r"\b(52|53)[- ]week\s+(?:fiscal\s+)?year", search_text, re.I)
    if weeks is None and week_match:
        weeks = int(week_match.group(1))
    convention = str(source_values.get("fiscal_year_label_convention") or "ending_year").lower()
    if convention not in {"ending_year", "start_year"}:
        convention = "ending_year"
    calendar_type = "calendar" if month == 12 and convention == "ending_year" else "non_calendar"
    return IssuerFiscalCalendar(
        fiscal_year_end_month=month,
        fiscal_year_end_day=day,
        fiscal_year_label_convention=convention,
        quarter_boundaries=dict(source_values.get("quarter_boundaries") or {}),
        week_based=week_based,
        weeks_per_year=weeks,
        calendar_type=calendar_type,
        source=(str(source_values.get("source")) if registry and not nested and not end_text else ("metadata" if metadata and (month != 12 or end_text or nested) else ("filing_text" if value and month != 12 else "default"))),
        evidence=evidence,
    )


def _calendar_dict(calendar: IssuerFiscalCalendar) -> dict[str, Any]:
    return calendar.to_dict()


def _fiscal_year_for_date(period_end: str, calendar: IssuerFiscalCalendar) -> int | None:
    try:
        end = date.fromisoformat(period_end)
    except (TypeError, ValueError):
        return None
    end_year = end.year
    month = calendar.fiscal_year_end_month
    if end.month > month:
        fiscal_end_year = end_year + 1
    elif end.month < month:
        fiscal_end_year = end_year
    elif calendar.fiscal_year_end_day and end.day > calendar.fiscal_year_end_day:
        fiscal_end_year = end_year + 1
    else:
        fiscal_end_year = end_year
    return fiscal_end_year if calendar.fiscal_year_label_convention == "ending_year" else fiscal_end_year - 1


def _quarter_from_end_date(period_end: str, calendar: IssuerFiscalCalendar) -> int | None:
    try:
        end = date.fromisoformat(period_end)
    except (TypeError, ValueError):
        return None
    boundaries = calendar.quarter_boundaries
    if isinstance(boundaries, Mapping):
        candidates: list[tuple[str, date]] = []
        for key, value in boundaries.items():
            if isinstance(value, str):
                try:
                    candidates.append((str(key), date.fromisoformat(value)))
                except ValueError:
                    pass
            elif isinstance(value, Mapping) and value.get("end_date"):
                try:
                    candidates.append((str(key), date.fromisoformat(str(value["end_date"]))))
                except ValueError:
                    pass
        for key, boundary in sorted(candidates, key=lambda item: item[1]):
            if end <= boundary:
                q = _int(re.sub(r"[^0-9]", "", key))
                if q in {1, 2, 3, 4}:
                    return q
    return ((end.month - calendar.fiscal_year_end_month - 1) % 12) // 3 + 1


def _format_label(year: int | None, quarter: int | None, calendar: IssuerFiscalCalendar, *, explicit_fiscal: bool = False) -> str | None:
    if year is None:
        return None
    if quarter is None:
        return f"FY {year}"
    if explicit_fiscal or calendar.calendar_type == "non_calendar":
        return f"Q{quarter} FY{year}"
    return f"Q{quarter} {year}"


def _explicit(metadata: Mapping[str, Any] | None) -> FiscalPeriod | None:
    if not metadata:
        return None
    fy, fq, label = metadata.get("fiscal_year"), metadata.get("fiscal_quarter"), metadata.get("period_label")
    if fy is None and fq is None and label is None:
        return None
    fy, fq = _int(fy), _int(fq)
    if fq is None and label:
        match = re.search(r"\bQ([1-4])\s*(?:FY\s*)?(20\d{2}|19\d{2})\b", str(label), re.I)
        if match:
            fq, fy = int(match.group(1)), int(match.group(2))
    if fq not in {None, 1, 2, 3, 4}:
        fq = None
    calendar = _fiscal_calendar(metadata)
    if label is None:
        label = _format_label(fy, fq, calendar, explicit_fiscal=True)
    end = metadata.get("period_end_date") or metadata.get("period_end")
    return _period(year=fy, quarter=fq, label=str(label) if label else None, method="metadata", confidence=1.0,
                   period_type=str(metadata.get("period_type") or (PERIOD_QUARTER_ACTUAL if fq else PERIOD_ANNUAL_ACTUAL)),
                   evidence="explicit metadata", sources=["metadata"], period_end_date=str(end) if end else None,
                   issuer_fiscal_calendar=_calendar_dict(calendar))


def _explicit_fiscal_candidates(value: str) -> list[tuple[int, int, str]]:
    if not value:
        return []
    candidates: list[tuple[int, int, str]] = []
    # Keep the captures named because the year-first form has the opposite
    # ordering from the other forms.  Positional ``group(2)`` access here
    # previously crashed on ordinary proxy text such as ``2024 first quarter``.
    patterns = (
        (r"\bQ(?P<quarter>[1-4])\s*(?:FY|FISCAL\s*YEAR|FISCAL)\s*(?P<year>20\d{2}|19\d{2})\b", "q"),
        (r"\b(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTERS?\s+(?:OF\s+)?FISCAL\s+(?:YEARS?\s+)?(?P<year>20\d{2}|19\d{2})\b", "word"),
        (r"\b(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTER\s+(?:FISCAL\s+)?(?:YEAR\s*)?(?P<year>20\d{2}|19\d{2})\b", "word"),
        (r"\b(?P<year>20\d{2}|19\d{2})\s+(?:FISCAL\s+)?(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTER\b", "year"),
    )
    for pattern, kind in patterns:
        for match in re.finditer(pattern, value, re.I):
            if kind == "q":
                quarter, year = int(match.group("quarter")), int(match.group("year"))
            elif kind == "word":
                quarter, year = _QUARTER_WORDS[match.group("quarter_word").lower()], int(match.group("year"))
            else:
                year, quarter = int(match.group("year")), _QUARTER_WORDS[match.group("quarter_word").lower()]
            candidates.append((quarter, year, match.group(0)))
    return candidates


def _filing_header(value: str, metadata: Mapping[str, Any] | None) -> FiscalPeriod | None:
    if not value:
        return None
    pattern = re.compile(
        r"\b(?:FOR\s+THE\s+)?(?:QUARTERLY\s+PERIOD|THREE\s+MONTHS|QUARTER)\s+ENDED\s+"
        r"(?:[A-Z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}[/-]\d{1,2}[/-]20\d{2})", re.I,
    )
    match = pattern.search(value)
    period_end = _date_from_parts(_date_parts(match.group(0))) if match else _period_end_date(value)
    if not period_end:
        return None
    calendar = _fiscal_calendar(metadata, value)
    derived_year = _fiscal_year_for_date(period_end, calendar)
    derived_quarter = _quarter_from_end_date(period_end, calendar)
    explicit = _explicit_fiscal_candidates(value)
    # An issuer's explicit fiscal statement outranks month arithmetic.  Use
    # the calendar-derived match when several explicit comparative statements
    # are present; otherwise retain the first explicit statement verbatim.
    chosen = next((item for item in explicit if item[0] == derived_quarter and item[1] == derived_year), None) or (explicit[0] if explicit else None)
    filename_period = _filename_period(
        str((metadata or {}).get("source_url") or ""),
        str((metadata or {}).get("local_path") or (metadata or {}).get("source_path") or ""),
    )
    filename_matches = bool(
        filename_period
        and filename_period.fiscal_year == derived_year
        and filename_period.fiscal_quarter == derived_quarter
    )
    if chosen:
        quarter, year, evidence = chosen
        sources = ["document_type", "filing_header", "explicit_fiscal_label"]
        if filename_matches:
            sources.append("source_filename")
            evidence = f"{evidence}; {filename_period.detection_evidence}"
        return _period(year=year, quarter=quarter, label=_format_label(year, quarter, calendar, explicit_fiscal=True), method="explicit_fiscal_label", confidence=1.0,
                       period_type=PERIOD_QUARTER_ACTUAL, evidence=f"{match.group(0) if match else period_end}; {evidence}",
                       sources=sources, period_end_date=period_end,
                       issuer_fiscal_calendar=_calendar_dict(calendar))
    sources = ["document_type", "filing_header", "issuer_fiscal_calendar"]
    evidence = match.group(0) if match else period_end
    if filename_matches:
        sources.append("source_filename")
        evidence = f"{evidence}; {filename_period.detection_evidence}"
    return _period(year=derived_year, quarter=derived_quarter, label=_format_label(derived_year, derived_quarter, calendar), method="fiscal_calendar", confidence=0.97,
                   period_type=PERIOD_QUARTER_ACTUAL, evidence=evidence,
                   sources=sources, period_end_date=period_end,
                   issuer_fiscal_calendar=_calendar_dict(calendar))


def _filename_period(source_url: str | None, source_path: str | None) -> FiscalPeriod | None:
    value = unquote(" ".join(item for item in (source_url, source_path) if item))
    for pattern, _order in (
        (r"\bQ(?P<quarter>[1-4])\s*(?:FY|FISCAL\s*YEAR|FISCAL)?\s*(?P<year>20\d{2}|19\d{2})\b", "quarter_first"),
        (r"\b(?P<year>20\d{2}|19\d{2})\s*Q(?P<quarter>[1-4])\b", "year_first"),
    ):
        match = re.search(pattern, value, re.I)
        if not match:
            continue
        quarter, year = int(match.group("quarter")), int(match.group("year"))
        explicit = bool(re.search(r"FY|FISCAL", match.group(0), re.I))
        label = f"Q{quarter} FY{year}" if explicit else f"Q{quarter} {year}"
        return _period(year=year, quarter=quarter, label=label, method="source_filename", confidence=0.90,
                       period_type=PERIOD_QUARTER_ACTUAL, evidence=match.group(0), sources=["source_filename"])
    return None


def _annual_only(value: str | None, method: str, confidence: float, metadata: Mapping[str, Any] | None = None) -> FiscalPeriod | None:
    if not value:
        return None
    upper = str(value).upper()
    match = re.search(r"\b(?:FY|FISCAL\s+YEAR|ANNUAL\s+REPORT|YEAR\s+ENDED)\s*(20\d{2}|19\d{2})\b", upper)
    if not match:
        match = re.search(r"\b(20\d{2}|19\d{2})\s+ANNUAL\s+REPORT\b", upper)
    if not match:
        return None
    year = int(match.group(1))
    calendar = _fiscal_calendar(metadata, str(value))
    end = _period_end_date(str(value))
    return _period(year=year, label=f"FY {year}", method=method, confidence=max(0.75, confidence - .03),
                   period_type=PERIOD_ANNUAL_ACTUAL, evidence=match.group(0), sources=[method],
                   period_end_date=end, issuer_fiscal_calendar=_calendar_dict(calendar))


def _scan(value: str | None, method: str, confidence: float, *, allow_annual=True, metadata=None) -> FiscalPeriod | None:
    if not value:
        return None
    explicit = _explicit_fiscal_candidates(str(value))
    if explicit:
        quarter, year, evidence = explicit[0]
        calendar = _fiscal_calendar(metadata, str(value))
        return _period(year=year, quarter=quarter, label=_format_label(year, quarter, calendar, explicit_fiscal=True), method="explicit_fiscal_label", confidence=1.0,
                       period_type=PERIOD_QUARTER_ACTUAL, evidence=evidence, sources=[method, "explicit_fiscal_label"],
                       period_end_date=_period_end_date(str(value)), issuer_fiscal_calendar=_calendar_dict(calendar))
    # Preserve ordinary calendar-quarter titles when no issuer fiscal marker
    # is present.  These labels are still useful for calendar issuers and are
    # deliberately lower priority than filing headers and explicit fiscal
    # statements above.
    generic_patterns = (
        r"\bQ(?P<quarter>[1-4])\s*(?P<year>20\d{2}|19\d{2})\b",
        r"\b(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\s+QUARTERS?\s+(?P<year>20\d{2}|19\d{2})\b",
    )
    for pattern in generic_patterns:
        match = re.search(pattern, str(value), re.I)
        if not match:
            continue
        if match.groupdict().get("quarter"):
            quarter, year = int(match.group("quarter")), int(match.group("year"))
        else:
            quarter, year = _QUARTER_WORDS[match.group("quarter_word").lower()], int(match.group("year"))
        calendar = _fiscal_calendar(metadata, str(value))
        return _period(year=year, quarter=quarter, label=_format_label(year, quarter, calendar),
                       method=method, confidence=confidence, period_type=PERIOD_QUARTER_ACTUAL,
                       evidence=match.group(0), sources=[method],
                       issuer_fiscal_calendar=_calendar_dict(calendar))
    if not allow_annual:
        return None
    return _annual_only(str(value), method, confidence, metadata)


def detect_fiscal_period(metadata: Mapping[str, Any] | None = None, title: str = "", body: str = "", *, document_type: str | None = None, source_url: str | None = None, source_path: str | None = None) -> FiscalPeriod:
    """Return the issuer reporting period without calendar-year leakage."""
    dtype = (document_type or "").lower()
    period_metadata = dict(metadata or {})
    if source_url and not period_metadata.get("source_url"):
        period_metadata["source_url"] = source_url
    if source_path and not period_metadata.get("source_path"):
        period_metadata["source_path"] = source_path
    # Proxy and governance documents may contain comparative fiscal language
    # for compensation or peer disclosures.  Unless the source supplies an
    # explicit period in metadata, that language is not the issuer's reporting
    # period and must remain UNKNOWN.
    if dtype in {"proxy_statement", "governance_filing"} and not _explicit(period_metadata):
        return _period(
            warning="PERIOD_NOT_APPLICABLE",
            period_type=PERIOD_UNKNOWN,
            method="document_type_not_applicable",
            confidence=0.0,
            sources=["document_type"],
            issuer_fiscal_calendar=_calendar_dict(_fiscal_calendar(period_metadata, body)),
        )
    header = _filing_header(body, period_metadata) if dtype in {"quarterly_report", "", "other_report"} else None
    if header:
        return header
    found = _explicit(period_metadata)
    if found:
        return found
    if dtype == "annual_report":
        found = _annual_only(title, "title", .98, period_metadata) or _annual_only(body, "body", .82, period_metadata)
        if found:
            return found
    filename = _filename_period(source_url, source_path)
    if filename and dtype in {"quarterly_report", ""}:
        return filename
    allow_annual = dtype not in {"quarterly_report", "news_release", "event_transcript", "earnings_transcript", "investor_presentation"}
    found = _scan(title, "title", .98, allow_annual=allow_annual, metadata=period_metadata)
    if found:
        return found
    found = _scan(body, "body", .82, allow_annual=allow_annual, metadata=period_metadata)
    return found or _period(warning="PERIOD_UNRESOLVED", period_type=PERIOD_UNKNOWN,
                            issuer_fiscal_calendar=_calendar_dict(_fiscal_calendar(metadata, body)))


def issuer_fiscal_calendar(metadata: Mapping[str, Any] | None = None, text: str = "") -> dict[str, Any]:
    """Public adapter hook for callers that need calendar provenance."""
    return _calendar_dict(_fiscal_calendar(metadata, text))


extract_fiscal_period = detect_fiscal_period
classify_fiscal_period = detect_fiscal_period
