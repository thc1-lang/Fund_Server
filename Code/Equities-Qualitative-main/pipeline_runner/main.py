from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from qualitative_ir_downloader.config import Config, DOWNLOAD_ROOT, WORKSHEETS
from qualitative_ir_downloader.google_sheets import read_stocks
from qualitative_ir_downloader.models import Stock

from .runner import (
    DEFAULT_DATA_ROOT,
    EXIT_INVALID_CONFIGURATION,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED_FAILURE,
    PipelineRunner,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the frozen qualitative research pipeline",
        epilog="Exit codes: 0=successful or valid partial run, 1=unexpected runner failure, "
               "2=acquisition failure, 3=normalization failure, 4=invalid configuration/path.",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--ticker")
    scope.add_argument("--tickers", nargs="+")
    scope.add_argument(
        "--from-spreadsheet",
        action="store_true",
        help="Run every unique ticker listed in the configured secondary-summary worksheets",
    )
    parser.add_argument(
        "--worksheet",
        action="append",
        choices=WORKSHEETS,
        help="Limit --from-spreadsheet to one or more named worksheets (repeatable)",
    )
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--profile", default="core_v1")
    parser.add_argument("--mode", choices=("existing", "fresh"), default="existing")
    parser.add_argument("--skip-acquisition", action="store_true")
    parser.add_argument("--run-validation-audit", action="store_true")
    parser.add_argument("--artifacts-root")
    parser.add_argument("--download-root")
    parser.add_argument("--config-root", default="config/scoring")
    parser.add_argument("--output-root")
    parser.add_argument("--validation-config", default="config/calibration/validation_v1.json")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--clean-output", action="store_true", help="Delete only the selected ticker production folder before running")
    parser.add_argument("--verbose", action="store_true", help="Print resolved paths, stage details, and exception tracebacks")
    return parser


def _unique_tickers(stocks: list[Stock]) -> list[str]:
    """Keep workbook order while producing one production folder per ticker."""
    seen: set[str] = set()
    tickers: list[str] = []
    for stock in stocks:
        ticker = stock.ticker.upper()
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def _selection_sources(stocks: list[Stock]) -> dict[str, list[dict[str, object]]]:
    """Preserve all worksheet locations when a ticker appears in multiple lists."""
    sources: dict[str, list[dict[str, object]]] = defaultdict(list)
    for stock in stocks:
        sources[stock.ticker.upper()].append(asdict(stock))
    return dict(sources)


def _read_spreadsheet_selection(args: argparse.Namespace) -> list[Stock]:
    download_root = Path(args.download_root) if args.download_root else DOWNLOAD_ROOT
    stocks = read_stocks(Config(download_root=download_root))
    selected_worksheets = set(args.worksheet or WORKSHEETS)
    return [stock for stock in stocks if stock.worksheet in selected_worksheets]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.worksheet and not args.from_spreadsheet:
        build_parser().error("--worksheet requires --from-spreadsheet")

    selected_stocks: list[Stock] = []
    if args.from_spreadsheet:
        try:
            selected_stocks = _read_spreadsheet_selection(args)
        except Exception as exc:
            _print_pipeline_exception(exc, verbose=args.verbose)
            print(f"SPREADSHEET SELECTION FAILED: {exc}")
            return EXIT_INVALID_CONFIGURATION
        tickers = _unique_tickers(selected_stocks)
        if not tickers:
            print("No valid tickers were found in the selected worksheet(s).")
            return EXIT_SUCCESS
        print(f"Selected {len(tickers)} unique ticker(s) from {len(selected_stocks)} worksheet row(s).")
    else:
        tickers = [args.ticker] if args.ticker else list(args.tickers or [])
    results = []
    errors = []
    for ticker in tickers:
        values = vars(args).copy()
        values.pop("ticker", None)
        values.pop("tickers", None)
        values.pop("from_spreadsheet", None)
        values.pop("worksheet", None)
        values["ticker"] = ticker
        # An explicit --ticker is authoritative. Batch --tickers continues to
        # use the worksheet universe and its selection metadata.
        values["manual_ticker"] = bool(args.ticker)
        try:
            result = PipelineRunner(**values).run()
        except Exception as exc:
            code = EXIT_INVALID_CONFIGURATION if isinstance(exc, (ValueError, OSError)) else EXIT_UNEXPECTED_FAILURE
            _print_pipeline_exception(exc, verbose=args.verbose)
            print(f"{ticker.upper()} FAILED: {exc}")
            errors.append({"ticker": ticker.upper(), "status": "FAILED", "error": str(exc), "exit_code": code})
            continue
        results.append(result)
        _print_result(result)
    if len(tickers) > 1 or args.from_spreadsheet:
        batch_manifest = write_batch_artifacts(
            data_root=Path(args.data_root),
            as_of_date=args.as_of_date,
            profile=args.profile,
            tickers=tickers,
            results=results,
            errors=errors,
            selection_sources=_selection_sources(selected_stocks),
        )
        for result in results:
            result.batch_manifest_path = str(batch_manifest)
        print(f"Batch manifest: {batch_manifest}")
    exit_codes = [result.exit_code for result in results]
    exit_codes.extend(error.get("exit_code", EXIT_UNEXPECTED_FAILURE) for error in errors)
    exit_code = max(exit_codes, default=EXIT_UNEXPECTED_FAILURE if not results else EXIT_SUCCESS)
    print(f"Exit status: {'SUCCESS' if exit_code == EXIT_SUCCESS else f'FAILED ({exit_code})'}")
    return exit_code


def write_batch_artifacts(*, data_root: Path, as_of_date: str, profile: str,
                          tickers: list[str], results: list, errors: list[dict],
                          selection_sources: dict[str, list[dict[str, object]]] | None = None) -> Path:
    """Write only cross-company metadata outside the isolated ticker roots."""
    batch_root = data_root / "_Batch_Runs" / f"{as_of_date}_{len(tickers)}_stock_run"
    batch_root.mkdir(parents=True, exist_ok=True)
    manifest_path = batch_root / "batch_manifest.json"
    for result in results:
        result.batch_manifest_path = str(manifest_path)
    payload = {
        "as_of_date": as_of_date,
        "profile": profile,
        "tickers": [ticker.upper() for ticker in tickers],
        "selection_sources": selection_sources or {},
        "results": [result.to_dict() for result in results],
        "errors": errors,
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = ["# Batch run", ""]
    summary.extend(f"- {ticker.upper()}: {next((r.final_status.value for r in results if r.ticker == ticker.upper()), 'FAILED')}"
                   for ticker in tickers)
    if errors:
        summary.extend(["", "## Errors"])
        summary.extend(f"- {error['ticker']}: {error['error']}" for error in errors)
    (batch_root / "batch_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return manifest_path


def _print_result(result) -> None:
    print(result.ticker)
    print(f"\nRoot:\n{result.production_root}")
    print("\nAcquisition:")
    print(next((stage.status.value for stage in result.stages if stage.name == "acquisition"), "UNKNOWN"))
    print("\nAnalysis:")
    for stage in result.stages:
        if stage.name == "acquisition":
            continue
        print(f"{stage.name}: {stage.status.value}")
    dossier_json = result.dossier_json if result.dossier_json and Path(result.dossier_json).exists() else "NOT_CREATED"
    dossier_markdown = result.dossier_markdown if result.dossier_markdown and Path(result.dossier_markdown).exists() else "NOT_CREATED"
    print(f"\nFinal AI handoff:\n{dossier_json}")
    print(f"\nHuman report:\n{dossier_markdown}")
    print(f"\nRun summary:\n{result.manifest_path}")
    print(f"\nExit status: {'SUCCESS' if result.exit_code == EXIT_SUCCESS else f'FAILED ({result.exit_code})'}")


def _print_pipeline_exception(exc: BaseException, *, verbose: bool = False) -> None:
    print("PIPELINE FAILED")
    print("Stage: orchestration")
    print(f"Exception type: {type(exc).__name__}")
    print(f"Message: {exc}")
    if verbose:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    raise SystemExit(main())
