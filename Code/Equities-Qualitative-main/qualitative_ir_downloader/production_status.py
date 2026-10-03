"""Production-facing status, completeness, and incremental-cache accounting.

The event subsystem has detailed outcomes of its own.  This module keeps those
outcomes intact while deriving a small company-level status for reports and
run summaries.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence


SUCCESS_EVENT_OUTCOMES = {
    "GENERATED_TRANSCRIPT_ALREADY_AVAILABLE",
    "GENERATED_TRANSCRIPT_SUCCESS",
    "OFFICIAL_TRANSCRIPT_USED",
    "OFFICIAL_CAPTIONS_USED",
    "EVENT_ALREADY_COMPLETE",
    "REGISTRATION_NOT_REQUIRED",
    "REGISTRATION_DESTINATION_DISCOVERED",
    "TRANSCRIPT_READY",
}
LIMITATION_EVENT_OUTCOMES = {
    "CACHED_EVENT_MEDIA_PROTECTED",
    "EVENT_MEDIA_PROTECTED",
    "MEDIA_NOT_FOUND",
    "IR_VERIFIED_ACCESS_BLOCKED",
    "NO_WEBCAST",
    "NO_REPLAY_AVAILABLE",
    "FUTURE_EVENT_NO_REPLAY",
}
EVENT_FAILURE_OUTCOMES = {
    "FAILED",
    "TRANSCRIPTION_FAILED",
    "REGISTRATION_FAILED",
    "PLAYER_HTTP_403",
    "PLAYER_BLOCKED",
    "EVENT_DISCOVERY_FAILED",
    "REGISTRATION_REQUIRED",
    "REGISTRATION_APPROVAL_REQUIRED",
    "AUTH_REQUIRED",
    "CAPTCHA_REQUIRED",
    "EMAIL_VERIFICATION_REQUIRED",
}


def document_metrics(data: Mapping, category: str) -> dict[str, int]:
    """Count discovered, newly saved, cached, blocked, and failed documents."""
    records = list(data.get(category, []) or [])
    discovered_records = list(data.get(f"discovered_{category}", []) or [])
    discovered = max(len(discovered_records), len(records))
    newly_saved = sum(
        record.get("status") == "DOWNLOADED"
        and not record.get("duplicate_of")
        and not record.get("cache_hit")
        for record in records
    )
    cached = sum(
        bool(record.get("cache_hit"))
        or record.get("status") in {"CACHED", "REUSED"}
        or (
            record.get("status") == "DOWNLOADED"
            and bool(record.get("duplicate_of"))
        )
        for record in records
    )
    blocked = sum(record.get("status") == "BLOCKED" for record in records)
    failed = sum(record.get("status") == "FAILED" for record in records)
    return {
        "discovered": discovered,
        "newly_saved": newly_saved,
        "cached": cached,
        "blocked": blocked,
        "failed": failed,
    }


def category_status(
    data: Mapping,
    category: str,
    *,
    applicable: bool = True,
    scanned: bool | None = None,
) -> str:
    """Return an explicit completeness state for news or reports."""
    if not applicable:
        return "NOT_APPLICABLE"
    metrics = document_metrics(data, category)
    errors = list((data.get("archive_errors") or {}).get(category, []) or [])
    codes = {str(error.get("code", "")) for error in errors}
    coverage = (data.get("coverage") or {}).get(category)
    if scanned is False:
        return "NOT_SCANNED"
    if metrics["blocked"] and not metrics["newly_saved"] and not metrics["cached"]:
        return "ACCESS_BLOCKED"
    if codes & {"SITE_BLOCKED", "ACCESS_BLOCKED", "IR_ACCESS_BLOCKED", "CAPTCHA_REQUIRED"} and not (
        metrics["newly_saved"] or metrics["cached"]
    ):
        return "ACCESS_BLOCKED"
    if metrics["discovered"] == 0:
        if coverage == "cached_listing":
            return "CACHE_HIT_EMPTY"
        if errors:
            return "DISCOVERY_FAILED"
        return "NO_DOCUMENTS_FOUND"
    if metrics["failed"] + metrics["blocked"] >= metrics["discovered"] and not (
        metrics["newly_saved"] or metrics["cached"]
    ):
        return "ACCESS_BLOCKED" if metrics["blocked"] else "DISCOVERY_FAILED"
    return "SUCCESS"


def ir_status(data: Mapping, *, winner=None) -> str:
    if not winner and not data.get("ir_official"):
        return "DISCOVERY_FAILED"
    if winner is not None and getattr(winner, "access", None) is not None:
        access = winner.access
        if getattr(access, "blocked", False) and not (
            getattr(access, "browser_accessible", False)
            or getattr(access, "http_accessible", False)
        ):
            return "ACCESS_BLOCKED"
    if not data.get("ir_official"):
        return "DISCOVERY_FAILED"
    if data.get("ir_accessible") or data.get("ir_content_validated"):
        return "SUCCESS"
    # A verified identity whose current root probe is unavailable remains
    # useful provenance, but its present IR access state is explicit.
    return "ACCESS_BLOCKED"


def event_status(event_metrics: Mapping | None, data: Mapping) -> str:
    if not event_metrics:
        if data.get("event_discovery_diagnostics", {}).get("access_blocked"):
            return "IR_VERIFIED_ACCESS_BLOCKED"
        return "NOT_SCANNED"
    outcome = event_metrics.get("event_outcome") or (
        "EVENTS_FOUND" if event_metrics.get("events_discovered") else "EVENT_DISCOVERY_FAILED"
    )
    if not event_metrics.get("events_discovered") and data.get("event_discovery_diagnostics", {}).get("access_blocked"):
        return "IR_VERIFIED_ACCESS_BLOCKED"
    return str(outcome)


def _event_has_meaningful_artifact(event_metrics: Mapping, events_status: str) -> bool:
    return bool(
        event_metrics.get("generated_transcript_created")
        or event_metrics.get("generated_transcript_reused")
        or event_metrics.get("official_transcript_saved")
        or event_metrics.get("official_captions_saved")
        or events_status in SUCCESS_EVENT_OUTCOMES
        or event_metrics.get("media_blocked")
        or event_metrics.get("events_discovered")
    )


def aggregate_status(
    *,
    ir: str,
    news: str,
    reports: str,
    events: str,
    news_metrics: Mapping,
    reports_metrics: Mapping,
    event_metrics: Mapping | None,
    official_evidence: bool = False,
    source_limitations: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """Derive overall status without allowing an event outcome to overwrite it."""
    event_metrics = event_metrics or {}
    meaningful = bool(
        news_metrics.get("newly_saved")
        or news_metrics.get("cached")
        or reports_metrics.get("newly_saved")
        or reports_metrics.get("cached")
        or _event_has_meaningful_artifact(event_metrics, events)
        or official_evidence
    )
    limitations: list[str] = list(source_limitations or [])
    if events in {"CACHED_EVENT_MEDIA_PROTECTED", "EVENT_MEDIA_PROTECTED"}:
        limitations.append("event_media_protected")
    if events == "MEDIA_NOT_FOUND":
        limitations.append("event_media_unavailable")
    if events == "IR_VERIFIED_ACCESS_BLOCKED":
        limitations.append("event_access_blocked")
    if ir == "ACCESS_BLOCKED":
        limitations.append("PRIMARY_IR_BLOCKED")
    if news == "ACCESS_BLOCKED":
        limitations.append("news_access_blocked")
    if reports == "ACCESS_BLOCKED":
        limitations.append("reports_access_blocked")
    if news in {"NO_DOCUMENTS_FOUND", "CACHE_HIT_EMPTY"}:
        limitations.append("news_empty")
    if reports in {"NO_DOCUMENTS_FOUND", "CACHE_HIT_EMPTY"}:
        limitations.append("reports_empty")

    hard_failures = {
        "DISCOVERY_FAILED",
        "NOT_SCANNED",
    }
    module_failures = [status for status in (ir, news, reports) if status in hard_failures]
    if events in EVENT_FAILURE_OUTCOMES:
        module_failures.append(events)
    if not meaningful:
        return "FAILED", limitations
    if module_failures:
        # IR discovery is a recoverable coverage failure when an independent
        # official source family (most importantly SEC EDGAR) supplied usable
        # artifacts.  Keep the limitation explicit without discarding the
        # acquisition result.
        if official_evidence and module_failures == ["DISCOVERY_FAILED"]:
            limitations.append("IR_DISCOVERY_FAILED")
            return "SUCCESS_WITH_LIMITATIONS", list(dict.fromkeys(limitations))
        # An access limitation with usable cached/acquired artifacts remains a
        # useful company result; processing/discovery failures are partial.
        if all(f == "DISCOVERY_FAILED" for f in module_failures) and not limitations:
            return "PARTIAL", limitations
        return "PARTIAL", limitations
    if limitations:
        return "SUCCESS_WITH_LIMITATIONS", limitations
    return "SUCCESS", limitations


def completeness(
    *,
    ir: str,
    news: str,
    reports: str,
    events: str,
    event_metrics: Mapping | None,
    limitations: Sequence[str],
) -> dict:
    event_metrics = event_metrics or {}
    success_categories = {"SUCCESS", "NO_DOCUMENTS_FOUND", "CACHE_HIT_EMPTY"}
    transcript = bool(
        event_metrics.get("generated_transcript_created")
        or event_metrics.get("generated_transcript_reused")
        or event_metrics.get("official_transcript_saved")
        or event_metrics.get("official_captions_saved")
    )
    return {
        "ir": ir == "SUCCESS",
        "news": True if news in success_categories else None if news == "NOT_APPLICABLE" else False,
        "reports": True if reports in success_categories else None if reports == "NOT_APPLICABLE" else False,
        "events": None if events == "NOT_APPLICABLE" else bool(event_metrics.get("events_discovered") or events in SUCCESS_EVENT_OUTCOMES or events in LIMITATION_EVENT_OUTCOMES),
        "transcript": transcript,
        "limitations": list(dict.fromkeys(limitations)),
    }


def cache_metrics(*, document_cache_hits: int = 0, event_metrics: Mapping | None = None, ir_cache_hit: bool = False) -> dict[str, int]:
    event_metrics = event_metrics or {}
    event_cache_hits = int(event_metrics.get("events_resumed", 0))
    if event_metrics.get("events_resumed") or event_metrics.get("media_attempts_skipped_cached"):
        event_cache_hits = max(event_cache_hits, int(event_metrics.get("media_attempts_skipped_cached", 0)))
    artifact_hits = int(document_cache_hits) + event_cache_hits + int(event_metrics.get("generated_transcript_reused", 0))
    return {
        "artifact_cache_hits": artifact_hits,
        "documents_skipped_existing": int(document_cache_hits),
        "event_cache_hits": event_cache_hits,
        "network_requests_avoided": artifact_hits + int(ir_cache_hit),
    }
