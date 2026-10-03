# Configuration and workbook targets

Before relationship publication, the code checks all 37 macro summary helper
formulas for each selected dataset. Coincident inputs must select `Coincident
Score`; leading inputs must select `Leading Score`. A mismatched score name or
indicator reference stops the run before output writes. The existing macro
summary layout is `A:AL`, with paired helper formulas in `AN:DI`.

## Credentials

Keep Google credentials outside the project. Set `GOOGLE_APPLICATION_CREDENTIALS` to the absolute path of your service-account JSON, or pass `--credentials /path/to/file.json` to the launcher. Existing `google_credentials.json` files can also be discovered in the project or its parent folders.

Set `FRED_API_KEY` for unattended importer/full runs. Otherwise the local launcher prompts for it. Relationship-only and macro-analysis-only runs use data already in Sheets and do not need a FRED key.

The Google service account must have access to all selected workbooks. Existing tabs, controls, pair definitions and authorised `IMPORTRANGE` connections are required. Code migration does not create a new spreadsheet system.

## Existing destinations

| Role | Spreadsheet ID |
|---|---|
| Macro analysis | `19E_Za0DOHMY_9AFSPatK8Cp2vCQypI81QnB3Hz4ve9c` |
| Standalone All data | `1qIyTo-8esI23kaobPgnQT65Q6ykbjCXtPyNXC9Xo3KM` |
| Coincident correlation | `1h-70prpKWYT7Cstg8CR6VRJdfJXaVaxeJL2xX5cXK_k` |
| Coincident spread | `1TpE-sMW3mxdeXfWavdSfK2EIkz_WzbQbpc0AyjAJXPo` |
| Coincident spread momentum | `1PPVvOClJ3Ttk5We81pDIQyrYKtywuWuxhFITOulfXZA` |
| Coincident correlation momentum | `1ZoApVQIQmm0UUuG4A1s1CJ-b7ITbql5eWwZYvrsb8zc` |
| Leading correlation | `1Ma0pirU2pmMSFgZXKB4v5y1gw9W8vzBI8xt6tPxiz90` |
| Leading spread | `1sOkwKFe0d42NC7phUkGGOrhhwhDoff78rlQCL-1GUt0` |
| Leading spread momentum | `18uNC1bykf_a-F9tHTpgCgPk2e_Bw3TPMsJ6BQuZiL4g` |
| Leading correlation momentum | `1b-TWGIzqmzfG76ssiMAwROdCQQh7fhXEh2u4NTPRCvA` |

Relationship definitions, including output tab names and sheet IDs, live in `config.py`. Each workbook retains its own controls, pair definitions, history and Calculation Helper.

Macro destinations can be overridden with `--spreadsheet-id`, `--wide-spreadsheet-id` and `--wide-sheet-name`, or the corresponding `GOOGLE_SPREADSHEET_ID`, `GOOGLE_WIDE_SPREADSHEET_ID` and `GOOGLE_WIDE_SHEET_NAME` environment variables. If moving to new workbooks, update IDs, output sheet IDs and import formulas together.

## Controls and calculation definitions

Read/write targets are separate for coincident and leading scores. Base inputs must import the matching macro tab: `Coincident Score` or `Leading Score`. Momentum uses the matching dataset's base workbook. A selected dataset's Sheets client cannot write to the other dataset's workbooks.

Windows and momentum lookback are read from each workbook's Control Panel. The verified leading setup uses window 24 and momentum lookback 3; these are editable workbook settings, not fixed assumptions.

| Calculation | Definition |
|---|---|
| Correlation score | Fisher transform of Pearson correlation over strictly prior valid paired observations. |
| Spread score | Signed `X − Y`, standardised against strictly prior valid paired spreads. |
| Spread momentum | Change in the raw signed spread over the configured valid-observation lookback, standardised against prior valid changes. |
| Correlation momentum | Change in Fisher score over genuine paired-data updates, standardised against prior update changes. |

Standardisation uses sample standard deviation. Missing, insufficient-history and mathematically undefined results stay blank. No forward filling is added. Full selected histories are rebuilt and stale output tails are cleared. Helpers independently reconstruct the selected pair's history.


## Mixed-frequency momentum

Correlation momentum advances only when a new dated observation with **both**
inputs present has entered the strictly prior correlation window. Repeated
monthly Fisher values between annual or quarterly updates are not new samples:
those momentum cells are blank. A real update that leaves Fisher unchanged still
counts; values are never deduplicated. Undefined Fisher observations stay missing.
Spread momentum already requires both current inputs and uses the same principle
of counting real observations, without forward filling.

The existing controls remain 3 / 24: change over 3 valid updates, normalised against
24 prior valid changes. These are observation counts, not universally months.
For annual pairs, a lookback of 3 can span roughly 3 years and the normalisation
window roughly 24 years. With a base correlation window of 24, at least 51 paired
observations are needed before correlation momentum can be defined, and more if
Fisher values or variances are undefined. Insufficient history remains blank;
the program never silently shortens windows or caps large scores.

The Calculation Helper shows the lag and normalisation dates, last paired input
date, its age in days, and whether a new paired observation arrived. Reports also
include every correlation pair's last input, update, lag and momentum dates.
Before publishing correlation momentum, Python recomputes the upstream Fisher
history from its dated inputs and controls. A mismatch stops publication and
requests a correlation refresh. Inputs and controls are checked again before
publication and saved in the backup for reproducibility.

Dates here are the source observation-period dates, **not verified publication
or vintage timestamps**. Already-forward-filled upstream data cannot be identified
reliably from equal values alone. Historical revisions and release lags still
need a separate point-in-time dataset for a tradable backtest. The correction
removes repeated-window sampling; it does not validate predictive performance.

## Refresh and failure handling

A full run performs importer → macro analysis → selected base relationships → selected momentum histories. Independent stages use current upstream data. After changing base inputs or windows, run the full relationship stage before using correlation momentum.

The importer retains historical-overlap, schema and read-back checks. Analysis outputs are verified. Relationship inputs must match their upstream evaluated data, and source/date/control changes during calculation stop publication. These checks do not establish that Google's entire formula dependency graph recalculates atomically.

If a new `IMPORTRANGE` needs Google's **Allow access** connection, connect the requested source in the relevant workbook and rerun. Quota or source failures are reported with bounded retries. A failed/interrupted run can leave partial updates because the workbooks are not one transaction; inspect `work/` reports and rerun the affected stage after resolving the error.

[Back to the main README](../README.md)
