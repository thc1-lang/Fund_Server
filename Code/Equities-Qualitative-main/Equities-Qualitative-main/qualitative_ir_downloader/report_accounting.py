"""Report candidate validation and failure accounting.

The archive crawler sees links, while the acquisition layer needs to count
genuine report/document candidates.  This module keeps that distinction
explicit and gives every failed report a stable, reviewable classification.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from .models import Document

REPORT_FAILURE_CLASSIFICATIONS = {
    "REAL_DOWNLOAD_FAILURE",
    "ACCESS_BLOCKED",
    "INVALID_DOCUMENT",
    "DUPLICATE_ALREADY_REPRESENTED",
    "NON_DOCUMENT_LINK",
    "SUPERSEDED_DUPLICATE",
    "HTML_PAGE_NOT_REPORT",
    "UNSUPPORTED_CONTENT_TYPE",
    "STALE_URL",
    "TRANSIENT_HTTP_ERROR",
    "EXPECTED_EXTERNAL_LIMITATION",
}

_REPORT_WORDS = re.compile(
    r"annual|quarterly|financial|report|presentation|results|earnings|sustainability|esg",
    re.I,
)


def is_report_candidate(document: Document | Mapping) -> bool:
    """Return whether a discovered item has a document asset to validate.

    Report archives may contain event pages, webcast links, and HTML landing
    pages.  A report candidate must have an explicit PDF/static-file asset (or
    a PDF URL); those links are validated later by the downloader.
    """
    category = document.category if isinstance(document, Document) else document.get("category")
    if category != "reports":
        return False
    source_family = document.source_family if isinstance(document, Document) else document.get("source_family")
    # SEC EDGAR filings are authoritative HTML/text documents as well as
    # PDFs.  They are validated by the SEC acquisition route and therefore do
    # not need an issuer-hosted ``.pdf`` suffix to be a report candidate.
    if source_family == "SEC_EDGAR":
        return True
    if source_family == "OFFICIAL_IR_FINANCIAL_RESULTS":
        provenance = document.provenance if isinstance(document, Document) else document.get("provenance") or []
        return any(str(item.get("artifact_type")) == "financial_result_artifact" for item in provenance if isinstance(item, Mapping))
    source = str(document.source_url if isinstance(document, Document) else document.get("source_url") or "")
    pdf_source = document.pdf_source_url if isinstance(document, Document) else document.get("pdf_source_url")
    title = document.title if isinstance(document, Document) else document.get("title") or ""
    path = urlsplit(source).path.lower()
    return bool(pdf_source or path.endswith(".pdf") or "/static-files/" in path)


def report_document_type(document: Document | Mapping) -> str:
    title = str(document.title if isinstance(document, Document) else document.get("title") or "")
    source = str(document.source_url if isinstance(document, Document) else document.get("source_url") or "")
    text = f"{title} {source}".lower()
    for label, pattern in (
        ("annual_report", r"annual"),
        ("quarterly_report", r"quarterly|q[1-4](?:\b|[-_])"),
        ("presentation", r"presentation|investor[- ]?deck|business update"),
        ("sustainability_report", r"sustainability|esg"),
    ):
        if re.search(pattern, text, re.I):
            return label
    return "report_document"


def failure_stage(code: str | None, message: str = "", document: Document | Mapping | None = None) -> str:
    code = (code or "").upper()
    message = message.lower()
    if "invalid pdf structure" in message or "encrypted or empty pdf" in message:
        return "pdf_validation"
    if "not a pdf" in message or "missing pdf signature" in message:
        return "content_validation"
    if code in {"SITE_BLOCKED", "ACCESS_BLOCKED", "CAPTCHA_REQUIRED"}:
        return "access_check"
    if code in {"HTTP_FAILED", "TRANSIENT_HTTP_ERROR", "TIMEOUT", "NAVIGATION_FAILED"}:
        return "pdf_fetch"
    if document is not None and not is_report_candidate(document):
        return "candidate_validation"
    return "pdf_fetch"


def classify_failure(
    code: str | None,
    message: str = "",
    *,
    http_status: int | None = None,
    status: str | None = None,
    duplicate_of: str | None = None,
    existing_artifact: bool = False,
    document: Document | Mapping | None = None,
) -> str:
    """Map acquisition evidence to the controlled report classification set."""
    code_upper = (code or "").upper()
    text = message.lower()
    if duplicate_of:
        return "SUPERSEDED_DUPLICATE"
    if existing_artifact:
        return "DUPLICATE_ALREADY_REPRESENTED"
    if document is not None and not is_report_candidate(document):
        if "html" in text or "landing" in text or "report" not in text:
            return "HTML_PAGE_NOT_REPORT"
        return "NON_DOCUMENT_LINK"
    if status == "BLOCKED" or code_upper in {"SITE_BLOCKED", "ACCESS_BLOCKED", "CAPTCHA_REQUIRED"}:
        return "ACCESS_BLOCKED"
    if code_upper in {"UNSUPPORTED_CONTENT_TYPE"} or "unsupported content" in text:
        return "UNSUPPORTED_CONTENT_TYPE"
    if code_upper in {"STALE_URL", "NOT_FOUND", "HTTP_404"} or "404" in text:
        return "STALE_URL"
    if code_upper in {"HTTP_FAILED", "TRANSIENT_HTTP_ERROR", "TIMEOUT", "NAVIGATION_FAILED"} or http_status in {408, 425, 429} or (http_status is not None and http_status >= 500):
        return "TRANSIENT_HTTP_ERROR"
    if "invalid pdf" in text or "encrypted or empty pdf" in text or "missing pdf signature" in text:
        return "INVALID_DOCUMENT"
    if "expected pdf, received" in text and "text/html" in text:
        return "HTML_PAGE_NOT_REPORT"
    if code_upper == "PDF_DOWNLOAD_FAILED":
        return "REAL_DOWNLOAD_FAILURE"
    return "EXPECTED_EXTERNAL_LIMITATION" if code_upper else "REAL_DOWNLOAD_FAILURE"


def report_metrics(records: Iterable[Mapping], *, discovered: int | None = None, raw_candidates: int | None = None) -> dict[str, int]:
    """Return reconciled report counts for manifests and run summaries."""
    records = list(records or [])
    saved = sum(r.get("status") == "DOWNLOADED" and not r.get("cache_hit") and not r.get("duplicate_of") for r in records)
    reused = sum(bool(bool(r.get("cache_hit")) or r.get("status") in {"CACHED", "REUSED"} or (r.get("status") == "DOWNLOADED" and r.get("duplicate_of"))) for r in records)
    failed = sum(r.get("status") == "FAILED" for r in records)
    blocked = sum(r.get("status") == "BLOCKED" for r in records)
    validated = saved + reused + failed
    return {
        "raw_report_candidates": int(raw_candidates if raw_candidates is not None else discovered if discovered is not None else len(records)),
        "reports_discovered": int(discovered if discovered is not None else len(records)),
        "reports_validated": validated,
        "reports_saved": saved,
        "reports_reused": reused,
        "reports_failed": failed,
        "reports_blocked": blocked,
    }


def reconcile_report_metrics(metrics: Mapping) -> bool:
    """Check the production invariant for fully validated report sets."""
    return int(metrics.get("reports_validated", -1)) == int(metrics.get("reports_saved", 0)) + int(metrics.get("reports_reused", 0)) + int(metrics.get("reports_failed", 0))
