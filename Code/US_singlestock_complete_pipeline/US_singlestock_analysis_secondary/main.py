from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import config
from src.io import build_short_data_export, build_short_universe, preflight_and_read
from src.sec_importer import default_settings, import_to_workbook
from src.model import append_missing_primary, build_features, build_summary, score_category
from src.reporting import build_model_registry, build_run_audit, validate_short_results
from src.data_integrity import apply_integrity_gate, audit_source_rows
from src.validation import validate_results
from src.writer import delete_retired_output_tabs, write_outputs
from src.shorts import score_shorts, build_short_summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Secondary multi-year screen for current Primary Long and Short candidates")
    parser.add_argument("--spreadsheet-id", default=config.SPREADSHEET_ID)
    parser.add_argument("--credentials-file")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--as-of", help="Financial-age evaluation date (YYYY-MM-DD); does not reconstruct historical availability")
    parser.add_argument("--import-sec-data", action="store_true", help="Refresh Long and Short annual source tabs from current SEC CompanyFacts and fiscal-end Yahoo prices before screening")
    parser.add_argument("--sec-user-agent", help="SEC identification string; defaults to SEC_USER_AGENT or Theo Cooper t.h.c.1@icloud.com")
    parser.add_argument("--sec-cache-dir", help="Portable CompanyFacts cache directory; defaults to SEC_COMPANYFACTS_CACHE or ./sec_companyfacts_cache")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="Write Control Panel, long outputs, all Short process tabs, registry and audit")
    group.add_argument("--short-live", action="store_true", help="Write Control Panel, all Short process tabs and model registry")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = pd.Timestamp(args.as_of).strftime("%Y-%m-%d") if args.as_of else pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    if args.live and as_of != pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"):
        raise ValueError("Live publication requires today's model as-of date; historical PIT reconstruction is unavailable")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    import_summary = None
    if args.import_sec_data:
        import_summary = import_to_workbook(
            args.credentials_file,
            default_settings(args.sec_user_agent, args.sec_cache_dir),
            args.spreadsheet_id,
            output_dir / "source_import_backup",
        )
    book, worksheets, datasets, metadata, controls, short_history_datasets, primary_shortlist, short_imported_dataset = preflight_and_read(
        args.spreadsheet_id, args.credentials_file
    )
    # Snapshot exact inputs and effective controls for reproducible read-only replay.
    (output_dir / "effective_controls.json").write_text(json.dumps(controls, indent=2), encoding="utf-8")
    primary_shortlist.to_csv(output_dir / "primary_short_candidates.csv", index=False)
    analyses = {}
    summaries = {}
    audit_parts = []
    incomplete_categories = []
    result = {"spreadsheet_id": args.spreadsheet_id, "workbook_title": book.title,
              "primary_short_spreadsheet_id": config.PRIMARY_SHORT_SPREADSHEET_ID,
              "primary_short_tab": config.PRIMARY_SHORT_TAB,
              "live": args.live or args.short_live, "as_of": as_of,
              "historical_pit_reconstruction": "NOT_AVAILABLE",
              "out_of_sample_validation": "NOT_PERFORMED",
              "model_version": controls["MODEL_VERSION"], "categories": {}}
    if import_summary is not None:
        result["sec_import"] = import_summary
    for category in config.CATEGORIES:
        slug = config.CATEGORIES[category]["slug"].lower()
        datasets[category].to_csv(output_dir / f"{slug}_source.csv", index=False)
        metadata[category].to_csv(output_dir / f"{slug}_primary.csv", index=False)
        category_audit = audit_source_rows(category, datasets[category], metadata[category], as_of)
        audit_parts.append(category_audit)
        if datasets[category].empty or not pd.to_numeric(datasets[category]["Fiscal Year"], errors="coerce").notna().any():
            incomplete_categories.append(category)
            result["categories"][category] = {
                "status": "missing_source_history", "primary_tickers_without_category_history": int(len(metadata[category])),
                "source_rows": int(len(datasets[category])), "unique_tickers": 0,
                "rankable": 0, "selected": 0, "top_tickers": [],
            }
            continue
        features = build_features(datasets[category], int(controls["MIN_TREND_OBSERVATIONS"]))
        analysis = score_category(features, metadata[category], category, controls, as_of=as_of)
        analysis = append_missing_primary(analysis, metadata[category], as_of)
        analysis = apply_integrity_gate(analysis, category_audit)
        summary = build_summary(analysis, category, controls)
        analyses[category] = analysis
        summaries[category] = summary
        analysis.to_csv(output_dir / f"{slug}_analysis.csv", index=False)
        summary.to_csv(output_dir / f"{slug}_secondary_summary.csv", index=False)
        result["categories"][category] = {
            "primary_tickers_without_category_history": int(len(set(metadata[category]["Ticker"]) - set(datasets[category]["Ticker"]))),
            "source_rows": int(len(datasets[category])),
            "unique_tickers": int(features["Ticker"].nunique()),
            "rankable": int(pd.to_numeric(analysis["rank"], errors="coerce").notna().sum()),
            "selected": int(len(summary)),
            "top_tickers": summary["Ticker"].head(5).tolist() if "Ticker" in summary else [],
        }
    short_raw, short_metadata = build_short_universe(short_history_datasets, primary_shortlist, short_imported_dataset)
    short_audit = audit_source_rows("Short", short_raw, primary_shortlist, as_of)
    audit_parts.append(short_audit)
    short_data = build_short_data_export(short_raw, short_metadata)
    short_raw.to_csv(output_dir / "short_pooled_source.csv", index=False)
    short_data.to_csv(output_dir / "short_data.csv", index=False)
    if short_raw.empty:
        short_analysis = short_metadata.copy()
        short_analysis["history_years"] = 0
        short_analysis["trend_observations"] = 0
        short_analysis["short_eligible"] = False
        short_analysis["short_history_pass"] = False
        short_analysis["short_fail_reasons"] = "missing_source_history"
    else:
        short_features = build_features(short_raw, int(controls["SHORT_MIN_TREND_OBSERVATIONS"]))
        short_analysis = score_shorts(short_features, short_metadata, controls, as_of)
        scored_tickers = set(short_analysis["Ticker"].astype(str).str.upper())
        missing_history = short_metadata.loc[~short_metadata["Ticker"].isin(scored_tickers)].copy()
        if not missing_history.empty:
            missing_history["history_years"] = 0
            missing_history["trend_observations"] = 0
            missing_history["short_eligible"] = False
            missing_history["short_history_pass"] = False
            missing_history["short_fail_reasons"] = "missing_source_history"
            for column in [c for c in short_analysis if c.startswith("short_") and c.endswith("_pass")]:
                missing_history[column] = False
            short_analysis = pd.concat([short_analysis, missing_history], ignore_index=True, sort=False)
    short_analysis = apply_integrity_gate(short_analysis, short_audit, short=True)
    short_summary = build_short_summary(short_analysis, controls)
    short_analysis.to_csv(output_dir / "short_analysis.csv", index=False)
    short_summary.to_csv(output_dir / "short_summary.csv", index=False)
    source_audit = pd.concat(audit_parts, ignore_index=True)
    source_audit.to_csv(output_dir / "source_integrity_audit.csv", index=False)
    primary_tickers = set(primary_shortlist["Ticker"].astype(str).str.upper())
    validate_short_results(short_analysis, short_summary, controls, primary_tickers)
    reconciliation = validate_results(analyses, summaries, controls)
    registry = build_model_registry(controls)
    run_audit = build_run_audit(datasets, analyses, summaries, controls, reconciliation, short_data, short_analysis, short_summary)
    registry.to_csv(output_dir / "secondary_model_registry.csv", index=False)
    run_audit.to_csv(output_dir / "secondary_run_audit.csv", index=False)
    history_tickers = set(short_analysis.loc[pd.to_numeric(short_analysis.get("history_years"), errors="coerce").fillna(0).gt(0), "Ticker"].astype(str).str.upper())
    missing_primary_tickers = primary_tickers - history_tickers
    result["shorts"] = {"primary_short_candidates": int(len(primary_tickers)),
                        "primary_pit_status_counts": primary_shortlist["Primary PIT Verification Status"].value_counts(dropna=False).to_dict(),
                        "primary_dataset_as_of_notes": primary_shortlist["Primary Dataset As Of Note"].drop_duplicates().tolist(),
                        "primary_short_tickers_with_history": int(len(history_tickers)),
                        "primary_short_tickers_without_history": int(len(missing_primary_tickers)),
                        "tickers_without_history": sorted(missing_primary_tickers),
                        "rankable": int(pd.to_numeric(short_analysis["short_rank"], errors="coerce").notna().sum()), "selected": int(len(short_summary)),
                        "top_tickers": short_summary["Ticker"].tolist(),
                        }
    required_short_signals = (
        "Primary 4-Week Price Change (%)", "Primary 12-Week Price Change (%)",
        "Primary F1 Estimate Change 4-Week (%)", "Primary F2 Estimate Change 4-Week (%)",
    )
    missing_short_signals = [column for column in required_short_signals
                             if column not in primary_shortlist or pd.to_numeric(primary_shortlist[column], errors="coerce").notna().sum() == 0]
    short_source_date_unverified = primary_shortlist["Primary Dataset As Of Note"].astype(str).str.contains(
        "NOT PROVIDED", case=False, regex=False).any()
    result["shorts"]["operational_status"] = (
        "UNAVAILABLE_MISSING_CURRENT_SIGNALS" if missing_short_signals else
        "RESEARCH_ONLY_PIT_AND_SOURCE_DATE_UNVERIFIED" if short_source_date_unverified else
        "RESEARCH_ONLY_PIT_UNVERIFIED"
    )
    result["shorts"]["wholly_missing_current_signals"] = missing_short_signals
    result["shorts"]["source_date_unverified"] = bool(short_source_date_unverified)
    result["validation"] = reconciliation
    result["incomplete_categories"] = incomplete_categories
    result["run_status"] = "incomplete_source_history" if incomplete_categories else "complete"
    if short_summary.empty:
        short_note = (
            f"NO RANKABLE SHORTS: {len(primary_tickers)} primary Short candidates; {len(history_tickers)} have annual history, "
            "but none has a valid four-year scored history free of source errors."
        )
    else:
        short_note = (
            f"Top {len(short_summary)} scored Short research candidates from {len(primary_tickers)} primary names. "
            "Verify Primary snapshot timing, borrow and event risk before any trade."
        )
    if missing_short_signals:
        short_note = ("SHORT MODEL UNAVAILABLE: the current Primary Short source is missing "
                      + ", ".join(missing_short_signals) + ". The scored top names are a diagnostic watchlist only. "
                      + short_note)
    elif short_source_date_unverified:
        short_note = ("SHORT SOURCE DATE UNVERIFIED: the four price/estimate signals were read from Primary Dataset "
                      "and reconciled to the published Primary Short market cap and close, but its capture date is not provided. "
                      + short_note)
    if args.live and incomplete_categories:
        (output_dir / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        raise ValueError(f"Refusing full live publication with missing category history: {incomplete_categories}")
    if args.live or args.short_live:
        result["tabs_updated"] = write_outputs(
            book, worksheets, controls, analyses, summaries, registry, run_audit, short_summary,
            short_note=short_note, short_only=args.short_live, short_analysis=short_analysis,
            primary_shortlist=primary_shortlist, short_as_of=as_of, short_data=short_data,
        )
    if args.live or args.short_live:
        result["tabs_deleted"] = delete_retired_output_tabs(book, worksheets)
    (output_dir / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 2 if incomplete_categories else 0


if __name__ == "__main__":
    raise SystemExit(main())
