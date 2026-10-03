"""Pure normalization helpers and source classification."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .fiscal_periods import detect_fiscal_period, issuer_fiscal_calendar
from .models import FiscalPeriod, NormalizedDocument


def normalize_whitespace(text: str) -> str:
    text = str(text or "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(line)
            blank = False
    return "\n".join(out).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_whitespace(text).encode("utf-8")).hexdigest()


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))
    except Exception:
        return str(url).strip()


def stable_document_id(*, ticker: str, source_url: str | None, document_type: str, publication_date: str | None, event_date: str | None, text: str, local_path: str | None = None) -> str:
    identity = {
        "ticker": ticker.upper().strip(),
        "source_url": canonical_url(source_url),
        "document_type": document_type,
        "content_hash": content_hash(text),
    }
    if not identity["source_url"]:
        # Without a durable source URL, dates help distinguish otherwise
        # identical local artifacts.  With a source URL, dates are metadata:
        # downloader manifests may fill them in on a later run.
        identity["publication_date"] = publication_date or ""
        identity["event_date"] = event_date or ""
        identity["local_path"] = str(local_path or "")
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"doc_{digest}"


def sec_form_type(metadata: dict[str, Any], title: str, path: str | None = None) -> str | None:
    """Return the official SEC form when the source identifies one."""
    explicit = metadata.get("sec_form") or metadata.get("form_type") or metadata.get("form")
    if explicit:
        value = str(explicit).upper().strip()
        if value in {"DEF 14A", "DEFA14A", "10-K", "10-Q", "8-K", "8-K/A"}:
            return value
    combined = " ".join((title or "", path or "", str(metadata.get("source_url") or ""))).upper()
    for form in ("DEF 14A", "DEFA14A", "10-K", "10-Q", "8-K/A", "8-K"):
        if form in combined or form.replace("-", "") in combined:
            return form
    return None


def classify_document(metadata: dict[str, Any], title: str, path: str | None = None) -> tuple[str, str | None]:
    category = str(metadata.get("category") or metadata.get("document_type") or "").lower()
    event_type = str(metadata.get("event_type") or metadata.get("type") or "").lower()
    combined = " ".join([title or "", path or "", category, event_type]).lower()
    method = str(metadata.get("method") or metadata.get("transcript_method") or "").lower()
    if metadata.get("segments") or metadata.get("transcription") or metadata.get("generated_by_whisper") or "transcript" in method or "earnings_call" in event_type:
        subtype = "generated_whisper" if metadata.get("generated_by_whisper") or "generated" in method else "official"
        return ("earnings_transcript" if "earning" in combined or "quarter" in combined else "event_transcript", subtype)
    form = sec_form_type(metadata, title, path)
    if form in {"DEF 14A", "DEFA14A"}:
        return "proxy_statement", form
    if "news" in category or "news release" in combined or "press release" in combined:
        return "news_release", None
    if "annual" in combined or "10-k" in combined or "10k" in combined:
        return "annual_report", form or None
    if "quarterly" in combined or "10-q" in combined or "10q" in combined or (category in {"reports", "report"} and re.search(r"\bQ[1-4]\b", title, re.I)):
        return "quarterly_report", form or None
    if "presentation" in combined or "investor deck" in combined or "slides" in combined:
        return "investor_presentation", None
    if category in {"reports", "report"}:
        return "other_report", form or None
    return "other_report", None


def _sections(text: str) -> list[dict[str, Any]]:
    # Only retain headings with unmistakable transcript/report conventions.
    matches = list(re.finditer(r"(?im)^(prepared remarks|question and answer|q&a|financial outlook)\s*:?[ \t]*$", text))
    sections: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append({"heading": match.group(1), "text": text[match.end():end].strip(), "start_char": start, "end_char": end})
    return sections


def _date_from_metadata(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value:
            return str(value)
    return None


def _filing_date_from_provenance(metadata: dict[str, Any]) -> str | None:
    provenance = metadata.get("provenance")
    if not isinstance(provenance, list):
        return None
    for item in provenance:
        if isinstance(item, dict) and item.get("filing_date"):
            return str(item["filing_date"])
    return None


def _failed_period_detection(metadata: dict[str, Any]) -> FiscalPeriod:
    """Return an explicit unknown period when period parsing is unavailable."""
    try:
        calendar = issuer_fiscal_calendar(metadata)
    except Exception:
        calendar = {}
    return FiscalPeriod(
        detection_method="FAILED_OR_UNKNOWN",
        confidence=0.0,
        warning="PERIOD_DETECTION_ERROR",
        period_type="UNKNOWN",
        detection_sources=["period_detection_error"],
        issuer_fiscal_calendar=calendar,
    )


def _availability_fields(metadata: dict[str, Any], publication_date: str | None, event_date: str | None) -> tuple[str | None, str | None, str | None, str | None, str]:
    filing_date = _date_from_metadata(metadata, "filing_date", "filed_date", "filed") or _filing_date_from_provenance(metadata)
    source_date = _date_from_metadata(metadata, "source_date", "official_publication_date", "published_date")
    download_date = _date_from_metadata(metadata, "download_date", "acquired_at")
    available_date = _date_from_metadata(metadata, "available_date")
    # Keep semantic dates distinct. A manifest publication date is the strongest
    # known availability signal when present; event dates are not silently used
    # as publication dates.
    if available_date:
        status = "EXPLICIT"
    elif filing_date:
        available_date, status = filing_date, "FILING_DATE"
    elif publication_date:
        available_date, status = publication_date, "PUBLICATION_DATE"
    elif source_date:
        available_date, status = source_date, "SOURCE_DATE"
    else:
        status = "UNKNOWN"
    return filing_date, source_date, download_date, available_date, status


def build_normalized_document(*, ticker: str, company_name: str, title: str, metadata: dict[str, Any], text: str, local_path: str | None, publication_date: str | None = None, event_date: str | None = None) -> NormalizedDocument:
    clean = normalize_whitespace(text)
    document_type, subtype = classify_document(metadata, title, local_path)
    period_meta = dict(metadata)
    period_meta.setdefault("ticker", ticker.upper())
    if not period_meta.get("issuer_cik") and isinstance(period_meta.get("provenance"), list):
        first_provenance = next((item for item in period_meta["provenance"] if isinstance(item, dict)), None)
        if first_provenance and first_provenance.get("issuer_cik"):
            period_meta["issuer_cik"] = first_provenance["issuer_cik"]
    form = sec_form_type(period_meta, title, local_path)
    if form:
        period_meta["sec_form"] = form
        metadata["sec_form"] = form
    source_url = metadata.get("source_url") or metadata.get("official_ir_url") or metadata.get("event_url")
    period_detection_error: str | None = None
    try:
        period = detect_fiscal_period(period_meta, title, clean, document_type=document_type, source_url=source_url, source_path=local_path)
        period_detection_status = "RESOLVED" if period.period_label or period.period_type != "UNKNOWN" else (
            "NOT_APPLICABLE" if period.detection_method == "document_type_not_applicable" else "UNKNOWN"
        )
    except Exception as exc:
        # Fiscal-period metadata is valuable but is not required to preserve a
        # valid acquired document.  Keep the failure explicit and continue so
        # proxies, 8-Ks, news and governance documents remain available.
        period_detection_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        period = _failed_period_detection(period_meta)
        period_detection_status = "ERROR"
    identifier = stable_document_id(ticker=ticker, source_url=metadata.get("source_url") or metadata.get("official_ir_url") or metadata.get("event_url"), document_type=document_type, publication_date=publication_date, event_date=event_date, text=clean, local_path=local_path)
    metadata = dict(metadata)
    metadata.setdefault("provenance", metadata.get("provenance") or [])
    metadata["period_detection_method"] = period.detection_method
    metadata["period_detection_confidence"] = period.confidence
    metadata["period_detection_evidence"] = period.detection_evidence
    metadata["period_detection_sources"] = list(period.detection_sources)
    metadata["period_warning"] = period.warning
    metadata["period_detection_status"] = period_detection_status
    metadata["period_detection_error"] = period_detection_error
    filing_date, source_date, download_date, available_date, availability_status = _availability_fields(metadata, publication_date, event_date)
    metadata["filing_date"] = filing_date
    metadata["source_date"] = source_date
    metadata["download_date"] = download_date
    metadata["available_date"] = available_date
    metadata["availability_status"] = availability_status
    return NormalizedDocument(
        document_id=identifier,
        ticker=ticker.upper(),
        company_name=company_name,
        document_type=document_type,
        document_subtype=subtype,
        title=title or Path(local_path).stem if local_path else (title or "Untitled"),
        publication_date=publication_date,
        event_date=event_date,
        fiscal_year=period.fiscal_year,
        fiscal_quarter=period.fiscal_quarter,
        period_label=period.period_label,
        source_url=source_url,
        local_path=local_path,
        source_method=metadata.get("source_method") or metadata.get("method") or metadata.get("transcript_method"),
        text=clean,
        text_length=len(clean),
        content_hash=content_hash(clean),
        metadata=metadata,
        created_at=datetime.now(timezone.utc).isoformat(),
        period_detection_method=period.detection_method,
        period_detection_confidence=period.confidence,
        sections=_sections(clean),
        period_type=period.period_type,
        period_detection_evidence=period.detection_evidence,
        period_detection_sources=list(period.detection_sources),
        filing_date=filing_date,
        source_date=source_date,
        download_date=download_date,
        available_date=available_date,
        availability_status=availability_status,
        period_end_date=period.period_end_date,
        issuer_fiscal_calendar=dict(period.issuer_fiscal_calendar),
    )


normalize_text = normalize_whitespace
compute_content_hash = content_hash
make_document_id = stable_document_id
