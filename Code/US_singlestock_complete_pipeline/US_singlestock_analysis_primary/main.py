from __future__ import annotations
import argparse
import json
import sys
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import pandas as pd
import config
from src.audit import write_outputs
from src.cleaner import clean_source
from src.peer_statistics import apply_liquidity_policy, enrich_factors
from src.ranking import rank_quant_candidates
from src.schema import validate_headers
from src.scoring import active_signal_registry, score_expanded
from src.sheets_reader import read_google_inputs, read_local_control_panel, read_local_export, _client, WorkbookMetadata
from src.sheets_writer import (archive_stale_generated_tabs, ensure_analysis_sheets, reconcile_tabs, validate_destination,
                               validate_generated_topology, write_analysis_batch, _retry_google_request)
from src.strategies import score_strategies
from src.shorts import score_shorts, short_thesis_gate, write_short_sheet
from src.transformations import build_features
from src.institutional import (balance_sheet_gates, coverage_audit, eps_audit,
                               margin_reconciliation, tradability_gates)
from src.universe import eligible_universe
from src.validation import reconciliation_flags, validate_results, validate_strategy_results
from src.zacks_importer import import_zacks_to_google_sheet


def parse_args():
    p = argparse.ArgumentParser(description="Auditable US-listed single-stock quantitative screen")
    p.add_argument("--input-xlsx", help="Read-only local export; preferred for reproducible dry runs")
    p.add_argument("--input-snapshot", help="Replay a saved source_snapshot.json without network or Sheet writes")
    p.add_argument("--historical-screen-date", help="Historical screening is blocked until field-level public dates, original versions and historical universe membership are supplied")
    p.add_argument("--output-dir", default="outputs")
    p.add_argument("--destination-tabs-file", help="Optional JSON list of destination tab names")
    p.add_argument("--live", action="store_true", help="Read the live Google Sheet and write results back to it")
    p.add_argument("--short-only", action="store_true", help="With --live, update only the Short tab")
    p.add_argument("--import-zacks", action="store_true", help="Download a Zacks screen, validate it, replace Dataset, then run analysis")
    p.add_argument("--zacks-market-cap-min", type=int, default=3000, help="Zacks Market Cap (mil) minimum for --import-zacks")
    p.add_argument("--zacks-headed", action="store_true", help="Show Chromium for a user-completed Zacks login or access check")
    p.add_argument("--zacks-export-dir", help="Directory for Zacks CSVs, profiles, and Dataset backups")
    p.add_argument("--quiet", action="store_true", help="Suppress progress messages; final JSON still prints")
    p.add_argument("--debug", action="store_true", help="Show a full traceback when a run fails")
    return p.parse_args()


def _progress(args, message: str, started: float) -> None:
    if not getattr(args, "quiet", False):
        elapsed = time.perf_counter() - started
        print(f"[analysis {elapsed:6.1f}s] {message}", file=sys.stderr, flush=True)


def run(args) -> dict:
    started = time.perf_counter()
    if getattr(args, "historical_screen_date", None):
        raise ValueError("Historical screening is unavailable: this source lacks field-level public availability dates, original as-reported values and a historical investable universe")
    snapshot_path = getattr(args, "input_snapshot", None)
    if sum(bool(x) for x in (args.live, args.input_xlsx, snapshot_path)) > 1:
        raise ValueError("Use only one of --live, --input-xlsx or --input-snapshot")
    live = bool(args.live)
    short_only = getattr(args, "short_only", False)
    if short_only and not live:
        raise ValueError("--short-only requires --live")
    import_zacks = getattr(args, "import_zacks", False)
    if import_zacks and not live:
        raise ValueError("--import-zacks requires --live because it replaces the configured Dataset tab")
    if import_zacks and short_only:
        raise ValueError("--import-zacks cannot be combined with --short-only; the refreshed Dataset requires a full analysis run")
    zacks_import = None
    if import_zacks:
        _progress(args, "Downloading and validating the Zacks screen before replacing Dataset", started)
        source_book = _client(config.GOOGLE_CREDENTIALS_FILE).open_by_key(config.SOURCE_SPREADSHEET_ID)
        zacks_import = import_zacks_to_google_sheet(
            source_book, config.SOURCE_TAB,
            Path(args.zacks_export_dir) if args.zacks_export_dir else Path(args.output_dir) / "zacks_import",
            args.zacks_market_cap_min, args.zacks_headed,
        )
    _progress(args, "Loading source workbook and Control Panel", started)
    if snapshot_path:
        snapshot = json.loads(Path(snapshot_path).read_text())
        raw = pd.DataFrame(snapshot["data"], columns=snapshot["columns"])
        meta = WorkbookMetadata(**snapshot["metadata"])
        control_rows = snapshot["control_rows"]
        spreadsheet_id = meta.spreadsheet_id
    elif args.input_xlsx:
        raw, meta = read_local_export(args.input_xlsx, config.SOURCE_TAB)
        control_rows = read_local_control_panel(args.input_xlsx)
        spreadsheet_id = config.SOURCE_SPREADSHEET_ID
    else:
        raw, meta, control_rows = read_google_inputs(
            config.SOURCE_SPREADSHEET_ID, config.SOURCE_TAB, config.GOOGLE_CREDENTIALS_FILE
        )
        spreadsheet_id = meta.spreadsheet_id
    # Save exact source inputs before normalization. Replays cannot authorize writes.
    snapshot_json = json.dumps({"columns": list(raw.columns),
        "data": raw.astype(object).where(raw.notna(), None).values.tolist(),
        "metadata": asdict(meta), "control_rows": control_rows}, default=str, allow_nan=False)
    snapshot_output = Path(args.output_dir) / "source_snapshot.json"
    snapshot_output.parent.mkdir(parents=True, exist_ok=True)
    snapshot_output.write_text(snapshot_json)
    config.apply_control_panel(control_rows)
    _progress(args, "Validating schema and eligible universe", started)
    validate_headers(list(raw.columns))
    cleaned = clean_source(raw)
    eligible, excluded = eligible_universe(cleaned, config.ALLOWED_SECURITY_TYPES)
    _progress(args, f"Building features for {len(eligible):,} eligible securities", started)
    features, feature_issues = build_features(eligible)
    features = pd.concat([features, eps_audit(features)], axis=1)
    features = pd.concat([features, margin_reconciliation(features, config.MARGIN_RECONCILIATION_ABS_TOLERANCE)], axis=1)
    features = pd.concat([features, balance_sheet_gates(
        features, financial_sectors=config.FINANCIAL_SECTORS,
        max_de=config.MAX_STANDARD_DEBT_EQUITY_RATIO,
        max_debt_capital=config.MAX_STANDARD_DEBT_TOTAL_CAPITAL_RATIO,
        min_current=config.MIN_STANDARD_CURRENT_RATIO,
        max_reit_debt_ebitda=config.MAX_REIT_DEBT_EBITDA,
        max_reit_debt_ebit=config.MAX_REIT_DEBT_EBIT,
        business_type_overrides=config.BUSINESS_TYPE_OVERRIDES,
    )], axis=1)
    features = pd.concat([features, tradability_gates(
        features, min_market_cap=config.MIN_MARKET_CAP_MIL,
        min_adv=config.MIN_AVG_DAILY_DOLLAR_VOLUME,
        min_short_adv=config.MIN_AVG_DAILY_DOLLAR_VOLUME_SHORT,
        allow_otc=config.ALLOW_OTC, allow_adr=config.ALLOW_ADR,
        allow_mlp=config.ALLOW_MLP, allow_canadian=config.ALLOW_CANADIAN,
    )], axis=1)
    factor_set = set(active_signal_registry(features)) | {
        "debt_equity_ratio", "Current Ratio", "Quick Ratio",
        "Cash Ratio", "working_capital_to_sales",
    }
    _progress(args, f"Computing peer statistics across {features['Industry'].nunique():,} industries", started)
    enriched = enrich_factors(
        features, sorted(f for f in factor_set if f in features),
        config.MIN_VALID_FACTOR_OBSERVATIONS, config.PEER_SHRINKAGE_STRENGTH,
        config.PEER_Z_SCORE_CAP, config.PEER_PERCENTILE_WEIGHT,
    )
    liquidity = apply_liquidity_policy(
        enriched, config.FINANCIAL_SECTORS, config.LIQUIDITY_DISTRESS_Z_THRESHOLD,
        config.LIQUIDITY_DISTRESS_FLOOR_SCORE, config.EXCESS_WORKING_CAPITAL_Z_THRESHOLD,
        config.PEER_Z_SCORE_CAP, config.PAYOUT_WARNING_RATIO, config.PAYOUT_DISTRESS_RATIO,
    )
    enriched = pd.concat([enriched, liquidity], axis=1)
    expanded = score_expanded(
        enriched, config.FAMILY_WEIGHTS, config.MIN_FACTOR_COVERAGE,
        config.MIN_FAMILY_COVERAGE, config.CONFIDENCE_FACTOR_WEIGHT,
        config.CONFIDENCE_FAMILY_WEIGHT, config.SIGNAL_WEIGHTS,
        config.DUPLICATE_GROUP_WEIGHTS, config.SHORT_FAMILY_WEIGHTS,
    )
    result = pd.concat([enriched, expanded], axis=1)
    result = pd.concat([result, result["economic_business_type"].isin(config.FUNDAMENTAL_ALLOWED_BUSINESS_TYPES).rename("Fundamental Business Type Gate Pass")], axis=1)
    short_thesis = short_thesis_gate(
        result, config.SHORT_MIN_WEAK_FAMILIES, config.SHORT_WEAK_FAMILY_SCORE,
        config.SHORT_FAMILY_WEIGHTS,
    )
    result = pd.concat([result, short_thesis.rename("Short Thesis Gate Pass")], axis=1)
    result = pd.concat([result, result["economic_business_type"].isin(config.SHORT_ALLOWED_BUSINESS_TYPES).rename("Short Business Type Gate Pass")], axis=1)
    result = pd.concat([result, reconciliation_flags(result, config.PB_VALIDATION_TOLERANCE, config.MARKET_CAP_PER_SHARE_TOLERANCE)], axis=1)
    result = rank_quant_candidates(
        result, config.TOP_N_LONG, config.TOP_N_SHORT, config.MIN_LONG_SCORE, config.MIN_SHORT_SCORE,
        config.MAX_QUANT_LONGS_TOTAL, config.MAX_QUANT_SHORTS_TOTAL,
        config.MIN_CANDIDATE_CONFIDENCE, config.MAX_BOOK_PRICE_DIVERGENCE,
    )
    strategy_scores = score_strategies(
        result, config.STRATEGY_WEIGHTS, config.MIN_STRATEGY_WEIGHT_COVERAGE,
        config.MIN_STRATEGY_COMPONENTS,
        config.TURNAROUND_MIN_VALUE_SCORE, config.TURNAROUND_MAX_PROFITABILITY_SCORE,
        config.EG2_REQUIRE_POSITIVE_EPS_SAFE, config.EG2_REQUIRE_POSITIVE_EPS_HIGH_GROWTH,
        config.EG2_REQUIRE_POSITIVE_EPS_TURNAROUND,
    )
    result = pd.concat([result, strategy_scores], axis=1)
    result = pd.concat([result, coverage_audit(result)], axis=1)
    result = pd.concat([result, score_shorts(result)], axis=1)
    result = pd.concat([result, pd.DataFrame({
        "PIT Verification Status": "UNVERIFIED_VENDOR_SNAPSHOT",
        "Latest Verified Information Availability Date": pd.NA,
    }, index=result.index)], axis=1)
    _progress(args, "Validating scores, ranks, and candidate constraints", started)
    validate_results(
        result,
        top_long=config.TOP_N_LONG,
        top_short=config.TOP_N_SHORT,
        min_long=config.MIN_LONG_SCORE,
        min_short=config.MIN_SHORT_SCORE,
        max_total_long=config.MAX_QUANT_LONGS_TOTAL,
        max_total_short=config.MAX_QUANT_SHORTS_TOTAL,
        min_confidence=config.MIN_CANDIDATE_CONFIDENCE,
    )
    validate_strategy_results(result, config.STRATEGY_WEIGHTS)
    destination_tabs = []
    if args.destination_tabs_file:
        destination_tabs = json.loads(Path(args.destination_tabs_file).read_text())
    elif args.input_xlsx and meta.tabs:
        # Local dry-run reconciliation against the supplied workbook topology; never authorizes a write.
        destination_tabs = meta.tabs
    elif config.DESTINATION_SPREADSHEET_ID and meta.tabs:
        destination_tabs = meta.tabs if config.DESTINATION_SPREADSHEET_ID == spreadsheet_id else []
    archived_stale_tabs = []
    if live:
        _progress(args, "Validating live destination workbook", started)
        validate_destination(spreadsheet_id, config.DESTINATION_SPREADSHEET_ID, config.ALLOW_SAME_SOURCE_AND_DESTINATION or args.live)
        gc = _client(config.GOOGLE_CREDENTIALS_FILE)
        destination_book = gc.open_by_key(config.DESTINATION_SPREADSHEET_ID)
        destination_metadata = _retry_google_request(destination_book.fetch_sheet_metadata)
        destination_tabs = [sheet["properties"]["title"] for sheet in destination_metadata["sheets"]]
        if not short_only:
            archived_stale_tabs = archive_stale_generated_tabs(
                destination_book, destination_metadata, result["Industry"].dropna().unique())
            if archived_stale_tabs:
                destination_metadata = _retry_google_request(destination_book.fetch_sheet_metadata)
                destination_tabs = [sheet["properties"]["title"] for sheet in destination_metadata["sheets"]]
    manifest = reconcile_tabs(result["Industry"].dropna().unique(), destination_tabs)
    if live and not short_only and manifest.missing and not config.ALLOW_CREATE_INDUSTRY_TABS:
        raise ValueError(
            f"Refusing write; exact destination tabs missing: {manifest.missing}. "
            "Set ALLOW_CREATE_INDUSTRY_TABS=true to create new industry tabs during publication"
        )
    topology_changed = bool(manifest.unexpected)
    summary = {
        "model_version": config.MODEL_VERSION,
        "historical_pit_verified": False,
        "historical_validation_status": "NOT_RUN_NO_PIT_HISTORY_OR_HISTORICAL_UNIVERSE",
        "source_snapshot_sha256": sha256(snapshot_json.encode()).hexdigest(),
        "source_spreadsheet_id": spreadsheet_id,
        "source_tab": config.SOURCE_TAB,
        "source_title": meta.title,
        "source_grid_rows": meta.source_row_count,
        "source_grid_columns": meta.source_column_count,
        "rows_loaded": len(cleaned), "rows_eligible": len(result), "rows_excluded": len(excluded),
        "exclusions_by_reason": excluded.get("Exclusion Reason", pd.Series(dtype=str)).value_counts().to_dict(),
        "sectors": int(result["Sector"].nunique()), "industries": int(result["Industry"].nunique()),
        "industries_below_minimum_eligible_rows": int((result.groupby("Industry").size() < config.MIN_VALID_FACTOR_OBSERVATIONS).sum()),
        "industry_sector_shrinkage_comparisons": int(sum(result.get(f"{f}__comparison_group_type", pd.Series(dtype=str)).eq("Industry/sector shrinkage").sum() for f in factor_set if f"{f}__comparison_group_type" in result)),
        "insufficient_sector_peer_comparisons": int(sum(result.get(f"{f}__comparison_group_type", pd.Series(dtype=str)).eq("INSUFFICIENT SECTOR PEERS").sum() for f in factor_set if f"{f}__comparison_group_type" in result)),
        "quant_long_count": int(result["Quantitative Candidate"].eq("QUANT LONG").sum()),
        "short_selection_count": int(result["Short Selected"].sum()),
        "quant_short_count": int(result["Quantitative Candidate"].eq("QUANT SHORT").sum()),
        "insufficient_data_count": int(result["Data Status"].eq("INSUFFICIENT DATA").sum()),
        "short_insufficient_data_count": int(result["Short Data Status"].eq("INSUFFICIENT DATA").sum()),
        "safe_scored_count": int(result["Safe Score"].notna().sum()),
        "high_growth_potential_scored_count": int(result["High Growth Potential Score"].notna().sum()),
        "turnaround_story_scored_count": int(result["Turnaround Story Score"].notna().sum()),
        "matched_tabs": len(manifest.matched), "missing_tabs": len(manifest.missing), "unexpected_tabs": len(manifest.unexpected),
        "archived_stale_generated_tabs": archived_stale_tabs,
        "tabs_updated": 0, "tabs_skipped": len(manifest.matched) + 2 if not live else 0, "dry_run": not live,
        "feature_issues": feature_issues.to_dict("records"),
        "model_controls": config.MODEL_CONTROLS,
        "warnings": ["Vendor P/E fields may be blank for loss-making firms; missing P/E remains missing.",
            "Book-price proximity gate is configured and biases toward asset-heavy companies."
            if config.MAX_BOOK_PRICE_DIVERGENCE is not None else "",
            "DATA SOURCE NOT PROVIDED" if not config.DATA_SOURCE_LABEL else "",
            "AS-OF DATE NOT PROVIDED" if not config.DATA_AS_OF else "",
            "PIT NOT VERIFIED: source lacks field-level public dates and original as-reported versions",
            f"Unexpected workbook tabs: {manifest.unexpected}" if topology_changed else ""],
    }
    if zacks_import is not None:
        summary["zacks_import"] = asdict(zacks_import)
    summary["warnings"] = [w for w in summary["warnings"] if w]
    if live and not short_only:
        _progress(args, "Creating/updating strategy summaries and auditable industry helpers", started)
        destination_metadata = ensure_analysis_sheets(
            destination_book, result["Industry"].dropna().unique(), destination_metadata,
            industry_sizes=result.groupby("Industry").size().astype(int).to_dict(),
            create_industry_tabs=config.ALLOW_CREATE_INDUSTRY_TABS,
        )
        validate_generated_topology(destination_metadata, result["Industry"].dropna().unique())
        destination_tabs = [sheet["properties"]["title"] for sheet in destination_metadata["sheets"]]
        manifest = reconcile_tabs(result["Industry"].dropna().unique(), destination_tabs)
        summary["matched_tabs"] = len(manifest.matched)
        summary["missing_tabs"] = len(manifest.missing)
        summary["unexpected_tabs"] = len(manifest.unexpected)
        _progress(args, f"Writing and formatting {len(manifest.matched) * 2 + 6:,} Google Sheets tabs", started)
        summary["tabs_updated"] = write_analysis_batch(
            book=destination_book,
            control_rows=config.control_panel_rows(),
            strategy_rows=config.strategy_weight_rows(),
            strategy_weights=config.STRATEGY_WEIGHTS,
            quant_weights=config.FAMILY_WEIGHTS,
            industry_tables=result.groupby("Industry", sort=True),
            all_rows=result,
            sheet_metadata=destination_metadata,
            strategy_summary_top_n=config.STRATEGY_SUMMARY_TOP_N,
            min_strategy_score=config.MIN_STRATEGY_SCORE,
        )
        final_metadata = _retry_google_request(destination_book.fetch_sheet_metadata)
        validate_generated_topology(final_metadata, result["Industry"].dropna().unique())
    if live:
        summary["short_sheet_url"] = write_short_sheet(destination_book, result, config.DATA_AS_OF)
        summary["tabs_updated"] += 1
    _progress(args, f"Writing audit outputs to {args.output_dir}", started)
    paths = write_outputs(result, excluded, manifest, args.output_dir, summary)
    _progress(args, "Run completed successfully", started)
    print(json.dumps({**summary, "outputs": paths}, indent=2, default=str))
    return summary


if __name__ == "__main__":
    cli_args = parse_args()
    try:
        run(cli_args)
    except Exception as exc:
        if cli_args.debug:
            raise
        raise SystemExit(f"ERROR: {exc}\nRun with --debug for a full traceback.") from None
