"""Command-line entry point for offline normalization and extraction."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from qualitative_ir_downloader.config import DOWNLOAD_ROOT

from .document_store import DocumentStore
from .extraction_store import ExtractionStore
from .ingestion import ingest
from .qualitative_extraction import DeterministicExtractionProvider, extract_document
from .temporal_comparison import DeterministicTemporalComparisonProvider, claims_available_as_of, compare_latest_vs_previous
from .temporal_store import TemporalStore
from .state_store import StateStore
from .state_synthesis import DeterministicStateSynthesisProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize acquired IR documents into the qualitative-analysis store")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--ticker", help="ticker to normalize")
    scope.add_argument("--all", action="store_true", dest="all_companies", help="normalize the latest local artifact set for every ticker")
    parser.add_argument("--rebuild", action="store_true", help="replace the existing normalized store")
    parser.add_argument("--replace-ticker", action="store_true", help="replace only the selected ticker's normalized view")
    parser.add_argument("--document-type", help="only ingest one normalized document type")
    parser.add_argument("--extract", action="store_true", help="extract evidence-preserving qualitative claims from normalized documents")
    parser.add_argument("--dimension", help="only extract one controlled qualitative dimension")
    parser.add_argument("--force-extraction", action="store_true", help="re-extract even when document content and extraction version are unchanged")
    parser.add_argument("--compare", action="store_true", help="compare evidence-backed claims across fiscal periods")
    parser.add_argument("--latest-vs-previous", action="store_true", help="compare the latest fiscal period with the immediately previous period")
    parser.add_argument("--force-comparison", action="store_true", help="recompute a stored temporal comparison")
    parser.add_argument("--state", action="store_true", help="synthesize current qualitative states")
    parser.add_argument("--force-state", action="store_true", help="recompute stored qualitative states")
    parser.add_argument("--dry-run", action="store_true", help="normalize and report without writing the store")
    parser.add_argument("--as-of-date", help="historical cutoff for evidence admission")
    parser.add_argument("--download-root", type=Path, default=DOWNLOAD_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--store-root", type=Path, default=Path("artifacts/qualitative_analysis"), help=argparse.SUPPRESS)
    return parser


def _print_result(result) -> None:
    # Windows PowerShell sessions may expose a legacy cp1252 stream while IR
    # titles can legitimately contain Unicode punctuation.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {result.ticker or 'ALL'}")
    print()
    print(f"Documents discovered: {result.documents_discovered}")
    print(f"Documents normalized: {result.documents_normalized}")
    print(f"Added: {result.documents_added}")
    print(f"Updated: {result.documents_updated}")
    print(f"Unchanged: {result.documents_unchanged}")
    print(f"Failed: {result.documents_failed}")
    print()
    print("By type:")
    for kind in ("news_release", "annual_report", "quarterly_report", "investor_presentation", "earnings_transcript", "event_transcript", "other_report"):
        print(f"{kind}: {result.by_type.get(kind, 0)}")
    print()
    print(f"Fiscal periods detected: {result.fiscal_periods_detected}")
    print(f"Unknown fiscal periods: {result.unknown_fiscal_periods}")
    print()
    print(f"Characters ingested: {result.characters_ingested}")
    print("Sample:")
    for document in result.sample:
        print(f"- {document.document_id} {document.title}")
    if result.failures:
        print("Failures:")
        for failure in result.failures[:10]:
            print(f"- {failure.failure_type}: {failure.title or failure.message}")


def _print_extraction(summary: dict) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {summary['ticker']}")
    print()
    print(f"Documents eligible: {summary['documents_eligible']}")
    print(f"Documents extracted: {summary['documents_extracted']}")
    print(f"Documents reused: {summary['documents_reused']}")
    print(f"Claims created: {summary['claims_created']}")
    print(f"Claims reused: {summary['claims_reused']}")
    print(f"Claims rejected: {summary['claims_rejected']}")
    print("Claims by dimension:")
    for dimension, count in sorted(summary["claims_by_dimension"].items()):
        print(f"{dimension}: {count}")
    print(f"Evidence validation failures: {summary['evidence_validation_failures']}")
    print(f"Characters/chunks processed: {summary['characters_processed']}/{summary['chunks_processed']}")
    if summary["samples"]:
        print("Samples:")
        for claim in summary["samples"]:
            print(f"- {claim.claim_id} [{claim.dimension}] {claim.evidence_text[:180]}")


def _run_extraction(args, document_store: DocumentStore) -> int:
    # A first extraction command is allowed to hydrate the normalized store for
    # one explicitly requested ticker.  This remains offline and does not alter
    # acquisition artifacts or downloader behavior.
    documents = document_store.get_by_ticker(args.ticker) if args.ticker else document_store.list_all()
    if args.document_type:
        documents = [d for d in documents if d.document_type == args.document_type]
    if not documents and args.ticker and not args.dry_run:
        ingest(ticker=args.ticker, download_root=args.download_root, store=document_store, rebuild=False, document_type=args.document_type, dry_run=False)
        documents = document_store.get_by_ticker(args.ticker)
        if args.document_type:
            documents = [d for d in documents if d.document_type == args.document_type]

    claim_store = ExtractionStore(args.store_root)
    if args.ticker and not args.dry_run:
        claim_store.remove_ticker_except_documents(args.ticker, {document.document_id for document in documents})
    provider = DeterministicExtractionProvider()
    summary = {
        "ticker": args.ticker or "ALL",
        "documents_eligible": len(documents), "documents_extracted": 0, "documents_reused": 0,
        "claims_created": 0, "claims_reused": 0, "claims_rejected": 0,
        "claims_by_dimension": {}, "evidence_validation_failures": 0,
        "characters_processed": 0, "chunks_processed": 0, "samples": [],
    }
    for document in documents:
        if args.as_of_date and document.available_date and str(document.available_date)[:10] > args.as_of_date:
            # Known-future evidence cannot remain in the historical claim
            # store. Unknown availability is retained with an explicit status.
            if not args.dry_run:
                claim_store.remove_for_document(document.document_id)
            continue
        state = claim_store.get_document_state(document.document_id)
        existing_for_document = claim_store.get_by_document_id(document.document_id) if state is not None else []
        period_current = (not existing_for_document or all(
            claim.period_label == document.period_label
            and claim.period_type == document.period_type
            and claim.available_date == document.available_date
            for claim in existing_for_document
        ))
        reusable = (not args.force_extraction and state is not None and state.get("source_content_hash") == document.content_hash and state.get("extraction_version") == "qualitative-extraction-v8" and state.get("provider_version") == provider.provider_version and state.get("rules_version") == provider.rules_version and period_current)
        if reusable:
            existing = claim_store.get_by_document_id(document.document_id)
            if args.dimension:
                existing = [c for c in existing if c.dimension == args.dimension]
            summary["documents_reused"] += 1
            summary["claims_reused"] += len(existing)
            for claim in existing:
                summary["claims_by_dimension"][claim.dimension] = summary["claims_by_dimension"].get(claim.dimension, 0) + 1
                if len(summary["samples"]) < 5:
                    summary["samples"].append(claim)
            continue
        if state is not None and not args.dry_run:
            claim_store.remove_for_document(document.document_id)
        # Extract the complete document even when the CLI is displaying one
        # dimension.  This prevents a targeted run from deleting other valid
        # dimensions for the same document; filtering is applied only to the
        # reported counters and samples below.
        result = extract_document(document, provider)
        reported_claims = [claim for claim in result.claims if not args.dimension or claim.dimension == args.dimension]
        summary["documents_extracted"] += 1
        summary["claims_rejected"] += result.rejected
        summary["evidence_validation_failures"] += len(result.validation_failures)
        summary["characters_processed"] += result.characters_processed
        summary["chunks_processed"] += result.chunks_processed
        if not args.dry_run:
            counts = claim_store.upsert_many(result.claims)
            if args.dimension:
                existing_ids = {claim.claim_id for claim in claim_store.get_by_document_id(document.document_id)}
                summary["claims_created"] += len([claim for claim in reported_claims if claim.claim_id in existing_ids])
            else:
                summary["claims_created"] += counts["added"] + counts["updated"]
        else:
            summary["claims_created"] += len(reported_claims)
        for claim in reported_claims:
            summary["claims_by_dimension"][claim.dimension] = summary["claims_by_dimension"].get(claim.dimension, 0) + 1
            if len(summary["samples"]) < 5:
                summary["samples"].append(claim)
    _print_extraction(summary)
    return 0


def _print_comparison(summary: dict) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {summary['ticker']}")
    print()
    print(f"Periods available: {', '.join(summary['periods_available']) or 'none'}")
    print(f"Period compared: {summary['period_compared']}")
    print()
    print(f"Claims previous: {summary['claims_previous']}")
    print(f"Claims current: {summary['claims_current']}")
    print(f"Topics matched: {summary['topics_matched']}")
    print(f"Temporal changes created: {summary['changes_created']}")
    print(f"Temporal changes reused: {summary['changes_reused']}")
    print(f"Rejected comparisons: {summary['rejected']}")
    print()
    print("Changes by dimension:")
    for dimension, count in sorted(summary["by_dimension"].items()):
        print(f"{dimension}: {count}")
    print("Changes by type:")
    for change_type, count in sorted(summary["by_type"].items()):
        print(f"{change_type}: {count}")
    print()
    print(f"Evidence validation failures: {summary['evidence_failures']}")
    if summary["samples"]:
        print("Samples:")
        for change in summary["samples"]:
            print(f"- {change.change_id} [{change.change_type}] {change.summary}")


def _run_comparison(args, claim_store: ExtractionStore) -> int:
    from collections import Counter

    ticker = args.ticker
    if not ticker:
        raise SystemExit("--compare requires --ticker")
    claims = claim_store.get_by_ticker(ticker)
    if args.as_of_date:
        claims = claims_available_as_of(claims, args.as_of_date)
    temporal_store = TemporalStore(args.store_root)
    valid_periods = {claim.period_label for claim in claims if claim.period_label}
    if not args.dry_run:
        temporal_store.remove_invalid_periods(ticker, valid_periods)
    provider = DeterministicTemporalComparisonProvider()
    result = compare_latest_vs_previous(ticker, claims, dimension=args.dimension, provider=provider)
    summary = {
        "ticker": ticker.upper(),
        "periods_available": result.available_periods,
        "period_compared": f"{result.from_period} -> {result.to_period}" if result.from_period and result.to_period else "INSUFFICIENT_PERIODS",
        "claims_previous": result.previous_claims,
        "claims_current": result.current_claims,
        "topics_matched": result.topics_matched,
        "changes_created": 0,
        "changes_reused": 0,
        "rejected": len(result.rejected),
        "by_dimension": Counter(change.dimension for change in result.changes),
        "by_type": Counter(change.change_type for change in result.changes),
        "evidence_failures": len(result.evidence_validation_failures),
        "samples": result.changes[:5],
    }
    if result.status == "INSUFFICIENT_PERIODS":
        _print_comparison(summary)
        print("INSUFFICIENT_PERIODS")
        return 0

    if not args.dry_run:
        existing = temporal_store.get_by_periods(result.from_period or "", result.to_period or "")
        stale = any(item.ticker.upper() == ticker.upper() and item.comparison_version != provider.comparison_version for item in existing)
        if args.force_comparison:
            temporal_store.remove_scope(ticker, result.from_period or "", result.to_period or "", dimension=args.dimension)
        elif stale:
            temporal_store.invalidate_scope(ticker, result.from_period or "", result.to_period or "", provider.comparison_version, dimension=args.dimension)
        counts = temporal_store.upsert_many(result.changes)
        summary["changes_created"] = counts["added"] + counts["updated"]
        summary["changes_reused"] = counts["unchanged"]
    else:
        summary["changes_created"] = len(result.changes)
    _print_comparison(summary)
    return 0


def _print_state(summary: dict) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"Ticker: {summary['ticker']}")
    print(f"As-of period: {summary['period_label'] or 'UNKNOWN'}")
    print()
    print(f"Claims considered: {summary['claims_considered']}")
    print(f"Temporal changes considered: {summary['temporal_changes_considered']}")
    print(f"States created: {summary['states_created']}")
    print(f"States reused: {summary['states_reused']}")
    print(f"States updated: {summary['states_updated']}")
    print()
    print("By dimension:")
    for dimension, count in sorted(summary["by_dimension"].items()):
        print(f"{dimension}: {count}")
    print(f"Mixed states: {summary['mixed_states']}")
    print(f"Unknown states: {summary['unknown_states']}")
    print(f"Average confidence: {summary['average_confidence']:.3f}")
    print(f"Evidence validation failures: {summary['evidence_failures']}")
    if summary["samples"]:
        print("Samples:")
        for state in summary["samples"]:
            topic = f"/{state.topic}" if state.topic else ""
            print(f"- {state.dimension}{topic}: {state.current_state} (trend={state.trend}, confidence={state.confidence:.3f}) - {state.summary}")


def _run_state(args, claim_store: ExtractionStore) -> int:
    from collections import Counter

    tickers = [args.ticker] if args.ticker else sorted({claim.ticker for claim in claim_store.list_all()})
    if not tickers:
        raise SystemExit("--state requires stored claims")
    provider = DeterministicStateSynthesisProvider()
    temporal_store = TemporalStore(args.store_root)
    state_store = StateStore(args.store_root)
    for ticker in tickers:
        claims = claim_store.get_by_ticker(ticker)
        if args.as_of_date:
            claims = claims_available_as_of(claims, args.as_of_date)
        if args.dimension:
            claims = [claim for claim in claims if claim.dimension == args.dimension]
        changes = temporal_store.get_by_ticker(ticker)
        if args.dimension:
            changes = [change for change in changes if change.dimension == args.dimension]
        result = provider.synthesize(claims, changes)
        valid_periods = {claim.period_label for claim in claims if claim.period_label}
        if not args.dry_run:
            state_store.remove_invalid_periods(ticker, valid_periods)
        if not result.states:
            _print_state({
                "ticker": ticker.upper(), "period_label": result.period_label,
                "claims_considered": result.claims_considered, "temporal_changes_considered": result.temporal_changes_considered,
                "states_created": 0, "states_reused": 0, "states_updated": 0, "by_dimension": {},
                "mixed_states": 0, "unknown_states": 0, "average_confidence": 0.0,
                "evidence_failures": len(result.validation_failures), "samples": [],
            })
            continue
        summary = {
            "ticker": ticker.upper(),
            "period_label": result.period_label,
            "claims_considered": result.claims_considered,
            "temporal_changes_considered": result.temporal_changes_considered,
            "states_created": 0,
            "states_reused": 0,
            "states_updated": 0,
            "by_dimension": Counter(state.dimension for state in result.states),
            "mixed_states": sum(state.current_state == "mixed" for state in result.states),
            "unknown_states": sum(state.current_state == "unknown" for state in result.states),
            "average_confidence": sum(state.confidence for state in result.states) / len(result.states),
            "evidence_failures": len(result.validation_failures),
            "samples": [state for state in result.states if state.topic is None][:8],
        }
        if not args.dry_run:
            existing = state_store.get_by_period(result.period_label or "")
            stale = any(
                state.ticker.upper() == ticker.upper()
                and (state.state_version != provider.state_version or state.rules_version != provider.rules_version or state.provider != provider.provider_version)
                for state in existing
            )
            if args.force_state:
                state_store.remove_scope(ticker, result.period_label or "", dimension=args.dimension)
            elif stale:
                state_store.invalidate_scope(ticker, result.period_label or "", provider.state_version, dimension=args.dimension)
            counts = state_store.upsert_many(result.states)
            summary["states_created"] = counts["added"]
            summary["states_reused"] = counts["unchanged"]
            summary["states_updated"] = counts["updated"]
        else:
            summary["states_created"] = len(result.states)
        _print_state(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = DocumentStore(args.store_root)
    if args.state:
        return _run_state(args, ExtractionStore(args.store_root))
    if args.compare:
        return _run_comparison(args, ExtractionStore(args.store_root))
    if args.extract:
        return _run_extraction(args, store)
    result = ingest(ticker=args.ticker, all_companies=args.all_companies, download_root=args.download_root, store=store, rebuild=args.rebuild, replace_ticker=args.replace_ticker, document_type=args.document_type, dry_run=args.dry_run)
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
