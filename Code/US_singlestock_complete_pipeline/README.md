# US single-stock complete pipeline

This is a portable wrapper around unchanged copies of the Primary, Secondary,
and summary projects. It does not create or copy credentials, virtual
environments, browser profiles, caches, or prior run output.

## Run on the server

Copy this whole folder to the server. From its root, install the runtime once:

```bash
./setup-server
```

If `google_credentials.json` (or `service_account.json`) is in this package's
root, the launcher uses it automatically. Otherwise set a service-account
credential path, then start the complete live refresh:

```bash
export GOOGLE_CREDENTIALS_FILE=/secure/path/service_account.json
./run-complete-pipeline
```

The credential filenames are ignored by Git; keep the JSON private and do not
commit it. An explicit `--credentials-file` path or credential environment
variable overrides the package-root fallback.

The launcher runs the following dependency chain and stops immediately if any
stage fails:

1. Primary Zacks import, then Primary analysis and publication.
2. Secondary SEC import, using the freshly published Primary selections.
3. Secondary analysis and publication.
4. AI evidence-package creation and human-summary publication.

The Zacks and SEC importers write to their configured Google Sheets. Each run
also writes local audit artifacts under `outputs/<UTC timestamp>/primary` and
`outputs/<UTC timestamp>/secondary`, then writes the AI evidence package under
`outputs/<UTC timestamp>/summary`. The final stage creates `AI_master_index.json`,
per-ticker statement and detailed-summary JSON files, a validation report and a
run manifest. It also refreshes the configured human-facing summary Google
Sheet. Use `--skip-human-sheet` when only the local AI evidence package should
be rebuilt. Inspect each `run_summary.json` and the summary `run_manifest.json`
after a run. The models remain research tools: their current data sources do not
establish historical point-in-time validity or out-of-sample performance.

Use `./run-complete-pipeline --help` for operational options. In particular,
`--zacks-headed` lets an operator complete a permitted Zacks login or access
check; the importer does not attempt to bypass access controls.

## Windows server operation

`run-complete-pipeline` remains the portable Bash launcher.  On this Windows
server use the checked-in Windows runner instead; it uses the server Python,
the existing private credential file in this package (or
`GOOGLE_CREDENTIALS_FILE`), writes an audit folder under `outputs/`, and is
the entry point used by Telegram and Task Scheduler:

```powershell
cd C:\Fund_Server\Code\US_singlestock_complete_pipeline
python .\run_single_stock_pipeline.py --stage all
```

The only supported stages are `primary`, `secondary`, `summary`, and `all`.
`all` preserves the required order. A stage command never accepts arbitrary
shell text.

Telegram commands are restricted to the same group members configured for the
macro pipeline:

| Command | Effect |
| --- | --- |
| `/us_stock_run` | Primary, then Secondary, then evidence summary |
| `/us_stock_primary` | Zacks import and Primary publication only |
| `/us_stock_secondary` | SEC import and Secondary publication only |
| `/us_stock_summary` | Evidence package and human summary only |
| `/us_stock_status` | Current state and latest run record |
| `/us_stock_logs` | A live, redacted single-stock log view; repeat to stop |
| `/us_stock_stop` | Request a safe stop of this pipeline only |

The completion message always reports success or failure. It stores the last
published shortlist and includes ticker/category detail only when membership
changes; an unchanged run is reported simply as `Shortlists unchanged.`

To install the weekly job, open **PowerShell as Administrator** and run the
following. It creates a task named `US single-stock pipeline`, every Sunday at
08:00 local server time; it does not alter the macro or Telegram tasks.

```powershell
Set-Location C:\Fund_Server
.\Scripts\install_us_single_stock_weekly.ps1
```

For a different time/day, for example Monday 06:30:

```powershell
.\Scripts\install_us_single_stock_weekly.ps1 -Day Monday -Hour 6 -Minute 30
```

Confirm it with `Get-ScheduledTask -TaskName 'US single-stock pipeline'`, and
test it once with `Start-ScheduledTask -TaskName 'US single-stock pipeline'`.
Use `/us_stock_status` in Telegram to confirm the recorded result. The task
uses `IgnoreNew`, so a scheduled run never overlaps a live Telegram run.

## Package layout

```
US_singlestock_complete_pipeline/
├── run-complete-pipeline
├── setup-server
├── requirements.txt
├── US_singlestock_analysis_primary/
├── US_singlestock_analysis_secondary/
└── US_singlestock_analysis_summary/
```

The three nested folders retain their original separate codebases. The wrapper
adds orchestration only; it does not alter scoring formulas, model controls, or
the existing workbook validation and read-back checks.
