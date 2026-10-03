# Maintenance

## Source map

All runtime modules remain together in the project root so the existing entry points and copied-folder execution continue to work.

| Files | Responsibility |
|---|---|
| `run_us_pipeline.py` | Stage order, CLI selection, progress and run summaries |
| `monthly_indicators.py` | Macro source downloads, input validation and workbook sync |
| `us_indicator_analysis.py` | Indicator calculations and macro summaries |
| `run_relationships.py` | Dataset selection, relationship publication and verification |
| `config.py` | Exact coincident/leading targets and controls |
| `spread.py`, `correlation.py`, `momentum.py` | Numerical calculations |
| `pipeline.py`, `momentum_pipeline.py` | Relationship input preparation, calculation and verification |
| `source_freshness.py` | Dataset/source checks and copied leading-link correction |
| `calculation_helper.py` | Independent selected-pair audit histories |
| `google_sheets.py`, `validation.py`, `run_lock.py` | API requests, input validation and concurrent-run prevention |

The source handlers and calculation engines contain tested fallbacks and embedded offline fixtures. Do not remove code merely because a simple static search does not show a direct caller.

## Validate without publishing

From the project folder:

```bash
bash Start.command --self-test
```

This runs both engines' internal self-tests and the regression suite. It may install missing test dependencies, but does not download economic observations or write to Google Sheets. `--self-test` is a code test, not a preview/dry-run execution mode.

For the regression suite alone after setup:

```bash
.venv/bin/python -m pytest -q tests
```

On Windows, use `.venv\Scripts\python.exe` instead.

## Code style and development setup

Install `requirements-dev.txt` into your development environment, then run:

```bash
python -m black --check .
python -m pytest -q
```

Use `python -m black .` to format maintained Python source and tests. The settings
in `pyproject.toml` exclude generated portable files, local environments and run
history. `.editorconfig` defines consistent indentation and text-file endings.
After editing source, rebuild portable files before running the regression suite
because it verifies that the embedded code matches the maintained modules.

Python environments are machine-specific. A `.venv` copied from macOS cannot be
used on Windows; create a fresh environment with `py -3 -m venv .venv` if its
`Scripts/python.exe` is missing. The scheduled server launcher currently uses the
system `python`, independently of `.venv`.

## Keep the standalone project consistent

This folder is the canonical relationship source. If you also maintain the independently runnable `us_spread_correlation_analysis` project, it contains copies of the same engines and shared tests. That optional sibling is not included in this server installation.

```bash
python3 tools/sync_relationship_code.py
python3 tools/sync_relationship_code.py --check
```

Use `--target /path/to/us_spread_correlation_analysis` if that project is elsewhere. Normal pipeline execution never calls the sibling project or this maintenance utility. Synchronisation copies only the named relationship modules/tests and standalone entry point; it does not copy credentials or run data.

## Rebuild portable files

```bash
python3 tools/build_colab.py
```

This regenerates the single-file Python export, the one-cell notebook and the portable ZIP under `portable/`. The embedded sources have integrity hashes; edit maintained source modules and rebuild instead of manually changing embedded strings. Colab's top-level `STAGE` and `DATASET` options can be changed directly.

The ZIP uses an explicit file allowlist. It includes runtime code, tests, documentation, maintenance tools and the Colab files. It excludes credentials, `.venv/`, caches, generated run data and the ZIP itself.

## Generated files and cleanup

ISM updates only verified available values within the requested recent months. Missing
months or components leave existing spreadsheet cells unchanged; missing values are
never carried forward or written as blanks over your history. The source check reports
unavailable data. Invalid values, inconsistent dates and a stale latest release still
stop the importer. Original June 2026 ISM press releases on PR Newswire are registered
as exact-URL fallbacks for retired report pages; no scores are hardcoded. Other retired
reports use checksum-verified local archives when available.

To check downloads without publishing or supplying a FRED key:

```bash
.venv/bin/python monthly_indicators.py --check-non-api
```

| Location | Keep or remove? |
|---|---|
| `.venv/` | Keep for daily use. Machine-specific; the launchers recreate it if absent. |
| `work/<timestamp>/` | Keep while the logs and recovery backups are useful. These are real run records. |
| `work/source_archive/` | Retain source evidence and fallback histories unless intentionally resetting them. |
| `work/importer_outputs/` | Generated local data exports; preserve anything needed for audit. |
| `__pycache__/`, `.pytest_cache/`, `*.pyc` | Disposable caches; Python/testing may recreate them. |
| `portable/` | Rebuild from source whenever code changes. |

Do not delete active run files or lock files while a run is in progress. Stop the run before cleaning runtime directories. For migration, use the portable ZIP instead of copying the machine-specific environment or live audit data.

Keep loose source backups outside the application folder. This server uses
`C:\Server\Backups` for recovery copies; run-specific recovery data remains in
`work/`. macOS `._*` metadata files are unnecessary for this Python project and
are excluded alongside disposable caches and local backup files.

[Back to the main README](../README.md)
