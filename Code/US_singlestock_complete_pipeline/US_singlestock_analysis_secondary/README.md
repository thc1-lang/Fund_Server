# US Single-Stock Secondary Analysis

This pipeline re-screens the current Primary shortlist with four years of annual data and publishes four score-ranked research lists:

- `Safe Secondary Summary`
- `High Growth Potential Secondary Summary`
- `Turnaround Story Secondary Summary`
- `Short Secondary Summary`

Each list displays the highest-scoring names up to its editable cap in `Control Panel`:

- `TOP_N_SAFE`
- `TOP_N_HIGH_GROWTH_POTENTIAL`
- `TOP_N_TURNAROUND_STORY`
- `SHORT_TOP_N`

The default cap is three. A name needs a finite score, four usable annual observations and no recorded source error to be comparable for ranking. The cap controls how many comparable names are displayed; it does not turn the list into a trade recommendation.

## Workbook tabs

The four `Secondary Summary` tabs are the selection surface. The `Data` tabs preserve the annual source rows, the `Analysis` tabs expose the inputs and score calculation, `Secondary Model Registry` records the active weights and metric directions, and `Secondary Run Audit` records run-level coverage and reconciliation checks. `Short` and `Short Summary` retain the imported Primary Short source and its dashboard. Primary source tabs remain read-only.

The Short list uses the current Primary Short candidates plus the four price and estimate-change fields reconciled from the Primary `Dataset`. Its source date is currently unverified, so borrow, liquidity, event and timing checks still require separate review.

The model uses score contributions from metric families, multi-year trend features, recent inflection features and explicit missing-data penalties. Missing values remain `NO DATA`; available weights are renormalised only when the required data checks pass. The workbook exposes the resulting score, coverage, reliability and data status for every candidate without publishing a second selection queue.

Historical point-in-time reconstruction and out-of-sample validation are not available from the current source data. Current or restated SEC data should therefore be treated as a live research snapshot, not as a historical performance test.

## Run locally

From the project folder:

```bash
cd /Users/theocooper/Documents/Codex/US_singlestock_analysis_secondary
.venv/bin/python main.py --live --output-dir outputs/live
```

Omit `--live` for a local-only run that writes CSV and JSON reports without changing Google Sheets.

## Refresh annual source data and publish

The optional importer refreshes the three Long `Data` tabs and `Short Data` from current SEC CompanyFacts and fiscal-end Yahoo prices before running the screen:

```bash
.venv/bin/python main.py --import-sec-data --live --output-dir outputs/sec_live
```

Provide the existing service-account path with `--credentials-file` when required. Credentials stay outside the repository. The importer stores its cache and the prior source-tab backup under the selected output directory. This import uses current or restated filings and is not a point-in-time historical feed.

## Controls

Edit the `Value` column in `Control Panel`, then rerun the program. The four display caps are the `TOP_N_*` controls above. Family weights, metric weights, trend-history requirements, coverage requirements and missing-data penalties remain available because they change the score itself or determine whether a score can be compared. Source `Data` tabs should not be edited manually.
