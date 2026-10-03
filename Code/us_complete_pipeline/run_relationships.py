#!/usr/bin/env python3
"""Rebuild coincident and leading level and momentum histories in the configured Google Sheets."""

import argparse
from datetime import datetime, timezone
from run_lock import acquire_lock
import json
import logging
import sys
from config import ROOT, WORKBOOKS, credentials_path, select_workbooks
from google_sheets import Sheets
import momentum_pipeline
import calculation_helper
from pipeline import load, validate_before_publish, verify
from notifications.lifecycle import notify_entrypoint, record_failure, relationship_name


@notify_entrypoint(relationship_name)
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--correlation", action="store_true", help="Run only rolling correlation"
    )
    selection.add_argument(
        "--spread", action="store_true", help="Run only spread z-scores"
    )
    selection.add_argument("--spread-momentum", action="store_true")
    selection.add_argument("--correlation-momentum", action="store_true")
    selection.add_argument(
        "--momentum",
        action="store_true",
        help="Only momentum workbooks; original workbooks remain read-only",
    )
    parser.add_argument(
        "--dataset",
        choices=("all", "coincident", "leading"),
        default="all",
        help="Keep each dataset in its own four workbooks (default: both datasets)",
    )
    parser.add_argument(
        "--credentials-file",
        help="Existing Google credential file; otherwise environment/parent discovery",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    run_dir = ROOT / "work" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir.mkdir(parents=True)
    reports = []
    sheets = None
    with (ROOT / "work" / "pipeline.lock").open("w") as lock:
        try:
            acquire_lock(lock)
        except BlockingIOError:
            record_failure("Another pipeline run is active.")
            print("Another pipeline run is active.", file=sys.stderr)
            return 1
        try:
            choice = next(
                (
                    k
                    for k in (
                        "correlation",
                        "spread",
                        "spread_momentum",
                        "correlation_momentum",
                    )
                    if getattr(args, k)
                ),
                None,
            )
            selected = select_workbooks(args.dataset, choice, args.momentum)
            logging.info(
                "Selected %s: %s", args.dataset, ", ".join(b.key for b in selected)
            )
            sheets = Sheets(
                credentials_path(args.credentials_file),
                write_ids={b.spreadsheet_id for b in selected},
            )
            from source_freshness import validate_macro_summary

            checked_datasets = set()
            for book in selected:
                if book.dataset not in checked_datasets:
                    validate_macro_summary(sheets, book)
                    checked_datasets.add(book.dataset)
            # Publish base results first; momentum then requires refreshed imported values.
            for momentum_phase in (False, True):
                states = []
                for book in selected:
                    if book.kind.endswith("_momentum") != momentum_phase:
                        continue
                    logging.info("Reading %s (%s)", book.title, book.output_tab)
                    from source_freshness import prepare_dataset_links

                    prepare_dataset_links(sheets, book, run_dir)
                    loader = momentum_pipeline.load if momentum_phase else load
                    state = loader(sheets, book)
                    state["helper"] = calculation_helper.prepare(sheets, state)
                    state["report"]["helper_preview"] = {
                        "pair": state["helper"]["selection"],
                        "dates": len(state["helper"]["table"]),
                        "numeric_checks": state["helper"]["matched"],
                    }
                    states.append(state)
                    reports.append(state["report"])
                    (run_dir / (book.key + "_report.json")).write_text(
                        json.dumps(state["report"], indent=2)
                    )
                for state in states:
                    if momentum_phase:
                        momentum_pipeline.assert_fresh(
                            sheets,
                            state["book"],
                            state["source"],
                            state["output"],
                            state.get("pair_source"),
                            state.get("pair_controls"),
                        )
                    state["formulas"] = validate_before_publish(sheets, state)
                for state in states:
                    b = state["book"]
                    if momentum_phase:
                        momentum_pipeline.assert_fresh(
                            sheets,
                            b,
                            state["source"],
                            state["output"],
                            state.get("pair_source"),
                            state.get("pair_controls"),
                        )
                    state["report"]["status"] = "write_started"
                    snapshot = {
                        k: state[k]
                        for k in (
                            "meta",
                            "source",
                            "output",
                            "controls",
                            "formulas",
                            "report",
                        )
                    }
                    snapshot.update(
                        {
                            k: state[k]
                            for k in ("pair_source", "pair_controls")
                            if k in state
                        }
                    )
                    snapshot["spreadsheet_id"] = b.spreadsheet_id
                    backup = run_dir / (b.key + "_before.json.gz")
                    state["report"]["backup"] = str(backup)
                    state["report"]["cells_written"] = sheets.publish(
                        b, state["properties"], state["values"], backup, snapshot
                    )
                    verify(sheets, state)
                    calculation_helper.publish(sheets, state, state["helper"], run_dir)
                    if momentum_phase:
                        momentum_pipeline.assert_fresh(
                            sheets,
                            b,
                            state["source"],
                            state["output"],
                            state.get("pair_source"),
                            state.get("pair_controls"),
                        )
                    state["report"]["status"] = "published_and_verified"
                    from notifications.changes import record

                    difference = state["report"]["comparison"]
                    record(
                        "Relationship scores",
                        b.key.replace("_", " "),
                        added=difference["added_numeric"],
                        updated=difference["numeric_mismatches"],
                        removed=difference["removed_numeric"],
                    )

                    (run_dir / (b.key + "_report.json")).write_text(
                        json.dumps(state["report"], indent=2)
                    )
            (run_dir / "summary.json").write_text(json.dumps(reports, indent=2))
            for report in reports:
                print(
                    f"{report['key'].upper()}: {report['status']} | window={report['window']} | source dates={report['source_dates']} | indicators={report['indicators']} | pairs={report['pairs']}"
                )
                print(
                    f"  Calculated: {report['first_calculated_date']} to {report['latest_calculated_date']} | numeric={report['numeric_cells']:,} | blank/insufficient/undefined={report['blank_observations']:,}"
                )
                print(
                    f"  Invalid mappings={report['invalid_indicator_mappings']} | score cells written={report['cells_written']:,}"
                )
                if "momentum_lookback" in report:
                    print(
                        f"  Momentum lookback={report['momentum_lookback']} paired updates; normalisation uses prior changes only"
                    )
                if "helper" in report:
                    print(
                        f"  Helper: {report['helper']['pair']} | verified dates={report['helper']['dates']}"
                    )
                comparison = report["comparison"]
                print(
                    f"  Compared={comparison['compared_numeric']:,} | numeric differences={comparison['numeric_mismatches']} | historical blank differences={comparison['historical_blank_mismatches']} | recovered tail={comparison['new_tail_numeric']}"
                )
            print(f"Audit directory: {run_dir}")
            return 0
        except KeyboardInterrupt:
            possible_writes = bool(getattr(sheets, "writes_attempted", False))
            message = (
                "Stopped by Ctrl+C. Writes may be incomplete; rerun to rebuild and verify all selected outputs."
                if possible_writes
                else "Stopped by Ctrl+C before any Google Sheets writes. Existing outputs were not changed."
            )
            (run_dir / "interrupted.json").write_text(
                json.dumps(
                    {
                        "status": "interrupted",
                        "writes_attempted": possible_writes,
                        "reports": reports,
                    },
                    indent=2,
                )
            )
            logging.warning(message)
            record_failure(message)
            print(f"Audit directory: {run_dir}", file=sys.stderr)
            return 130
        except Exception as exc:
            record_failure(f"{type(exc).__name__}: {exc}")
            failure = {
                "error": str(exc),
                "reports": reports,
                "status": "failed; inspect reports/backups before retrying",
            }
            (run_dir / "failure.json").write_text(json.dumps(failure, indent=2))
            logging.error("%s: %s", type(exc).__name__, exc)
            print(f"Audit directory: {run_dir}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
