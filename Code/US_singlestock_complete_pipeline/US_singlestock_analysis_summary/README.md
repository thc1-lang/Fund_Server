# US Single-Stock Analysis Summary

This standalone module packages the published shortlist evidence from the existing US Primary and Secondary systems. It does not calculate a third model, change upstream controls, alter ranks, or write to the source workbooks.

## Inputs and selection boundary

- Primary (`1T2jn-zW7TIDMM5FK0Ih_WEP5puv1nkRHaSKHB3TK_Ms`) is the snapshot/current-state model.
- Secondary (`1vVb8EfJnsbPufiVH3ckznzu2TAOL3xW1WTdfceLALeA`) is the multi-year trend-first re-screen.
- The four published Secondary Summary tabs are the only final shortlist source. Their actual category labels and ranks are retained; the code does not manufacture a candidate universe.

The module reads the published category summaries, Primary Dataset, Secondary Analysis and Data tabs, and Secondary controls. It records raw records, score attribution, historical observations, provenance, matching status, data-quality warnings, contribution concentration, and threshold diagnostics. The latter two are explicitly diagnostic only.

## Run on macOS

```bash
cd /Users/theocooper/Documents/Codex/US_singlestock_analysis_summary
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --credentials-file /path/to/existing-service-account.json
```

The credentials file stays external. Alternatively set `GOOGLE_APPLICATION_CREDENTIALS` before running. The only write targets are:

- `~/Downloads/Equities Quantitative Analysis Summary - code interface/`
- The supplied `Equities Quantitative Analysis Summary - code interface` workbook, `Sheet1`.

Use `--skip-human-sheet` to produce and validate only the local evidence package.
If the source sheets are intentionally public, a credential-free local evidence-only run is also available:

```bash
.venv/bin/python main.py --public-read --skip-human-sheet
```

## Outputs

Each unique ticker folder contains exactly four module-managed files, even if the ticker belongs to more than one category:

- `<ticker>_income_statement.json`
- `<ticker>_cash_flow_statement.json`
- `<ticker>_balance_sheet.json`
- `<ticker>_detailed_summary.json`

The three statement files contain only published annual statement line items, period/source metadata, line-item-specific provenance, and explicit `null` values for unavailable fields. They never infer missing facts or substitute zero. The detailed summary combines the complete matched Primary Dataset row, every full Primary category result, every full Secondary shortlist and analysis result, all Secondary controls, every distinct historical record, score and metric attribution, diagnostics, risks, data quality, and provenance. Repeated category copies of an identical historical record are stored once with all source categories/locations. The output root also has `AI_master_index.json`, `run_manifest.json`, and `validation_report.json`; these are run-control evidence, not ticker-analysis payloads.

The human sheet is replace-managed on each successful run: it uses four sections in the original category ordering and is cleared/rebuilt so departed tickers and duplicate historical rows cannot persist. Company-name changes refresh in place for the same ticker. A ticker change creates a new canonical record; the prior module-managed ticker folder is moved to `retired/<run timestamp>/` rather than deleted. This deliberately avoids fuzzy matching a renamed security to the wrong issuer.

## Verification

```bash
.venv/bin/python -m pytest -q
```

The validation report counts unmatched primary/secondary records, duplicates, data-quality warnings, processing status, and output writes. A run is not marked complete when one of the published shortlist entries lacks its expected model match.
