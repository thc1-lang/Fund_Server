from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--profile", default="core_v1")
    parser.add_argument("--mode", choices=("existing", "fresh"), default="existing")
    parser.add_argument("--skip-acquisition", action="store_true")
    parser.add_argument("--run-validation-audit", action="store_true")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--download-root")
    parser.add_argument("--config-root", default="config/scoring")
    parser.add_argument("--output-root", default="artifacts/research_output")
    parser.add_argument("--validation-config", default="config/calibration/validation_v1.json")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--clean-output", action="store_true", help="Delete only the selected ticker production folder before running")
    parser.add_argument("--verbose", action="store_true", help="Print resolved paths, stage details, and exception tracebacks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tickers = [args.ticker] if args.ticker else list(args.tickers or [])
    results = []
    errors = []
    for ticker in tickers:
        values = vars(args).copy()
        values.pop("ticker", None)
        values.pop("tickers", None)
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
    if len(tickers) > 1:
        batch_manifest = write_batch_artifacts(
            data_root=Path(args.data_root),
            as_of_date=args.as_of_date,
            profile=args.profile,
            tickers=tickers,
            results=results,
            errors=errors,
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
                          tickers: list[str], results: list, errors: list[dict]) -> Path:
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
