# US Single-Stock Institutional Scoring

An auditable peer-relative equity scoring pipeline using the configured Google Sheets `Dataset` tab. The active long strategies are `Safe`, `High Growth Potential`, and `Turnaround Story` (presented as a **Turnaround Candidate** setup). Short has a separate current-state research score.

The scoring architecture uses a configured blend of shrunk empirical percentiles and capped robust-z mappings, correlated-signal groups, family aggregation, stock-specific missing-component renormalisation, and deterministic ranks. General Long and Short are separate diagnostic research rankings.

## September 2026 snapshot-method review (`primary_snapshot_v7`)

Short coverage, confidence, and data sufficiency use the Short family weights independently of the Long model. The Short tab and summaries display the Short-specific confidence and status; the v7 Control Panel migration preserves customized v6 weights.

The two pre-existing methodology/architecture DOCX files predate v7 and are not current model documentation. Use this README, the code, and the run-specific audit outputs for the current method.

Primary scoring now excludes past earnings surprises, estimate revisions, reported YoY sales/EPS growth, price returns, price-range momentum and broker-rating changes. Those fields remain in the raw audit for provenance and possible Secondary analysis. Active growth evidence is restricted to consensus values in the supplied snapshot, including F2/F1 EPS estimates and long-term growth consensus. The saved vendor extract has no field-level public release dates, original as-reported values or historical investable universe. **Historical PIT validity and future-return performance have not been demonstrated.** `--historical-screen-date` fails closed until a verified PIT source is added.

Default family mixes sum to 100 within general Long, each long strategy and Short. They are broad economic priors, not fitted backtest weights. `FUNDAMENTAL_ALLOWED_BUSINESS_TYPES` and `SHORT_ALLOWED_BUSINESS_TYPES` default to standard operating companies because this source lacks bank capital and REIT FFO/AFFO inputs. The excluded types remain in the full audit. Short uses `SHORT_WEIGHT_*` controls and a current-state weakness gate. Turnaround requires attractive current valuation, below-peer current profitability and the existing survivability gate; it does not imply recovery has begun. Known v4/v5 default weights migrate to this method while deliberate legacy custom weights are retained unless they activate excluded temporal families. The offline comparison and complete factor inventory are in `outputs/primary_review_2026_09_26/INSTITUTIONAL_REVIEW.md`.

## September 2026 economic hardening (primary_economic_v5)

Published candidates pass configured tradability and business-classification checks before industry and universe caps. Long candidates also pass the balance-sheet gate. Strategy summaries filter their Implementation Eligible flag before their display caps. A short shortlist remains research-only until borrow availability, cost and squeeze risk are checked; unavailable borrow is never certified as a pass.

Specific industry classifications take precedence over broad sectors. Equity REITs use their debt-coverage rules, mortgage finance requires specialist data, and unresolved Finance or generic REIT labels are withheld from selection. `BUSINESS_TYPE_OVERRIDES_JSON` permits reviewed ticker-specific classifications. Raw vendor industry/sector fields are preserved. Financial-company ordinary leverage and liquidity signals, and REIT EPS payout ratios, are structurally excluded where inappropriate.

Derived accounting margins, generic cash-flow margin, unaligned forward-sales growth, PEG and EPS-growth acceleration remain auditable but do not contribute to scores while their definitions/periods are unresolved. Vendor operating and net margins remain scored together inside one group. EARNINGS_QUALITY is inactive because defined operating cash flow/accrual inputs are absent. Temporal and unsupported families have zero default weights. These changes are methodological, not fitted to returns.

Coverage Confidence describes active data availability, not return probability or verified accounting quality. The internal `Score Confidence` column remains for compatibility. Zero-weight families cannot change composite coverage, and structural exclusions are distinguished from missing observations. Constant peer groups receive neutral scores; unknown dividends remain unknown; negative PEG is excluded. Optional positive-EPS and book-price controls now enforce their documented rules.

Each run saves a source snapshot and SHA-256 digest. For offline reproducibility:

```bash
.venv/bin/python main.py --input-snapshot outputs/source_snapshot.json --output-dir outputs/replay
```

Publication verifies all cells in visible industry selections, strategy summaries, the Control Panel and Metric Registry. Helper titles are checked; the full local audit retains every helper calculation. Generated output replacement does not clear all sheets before the first write. API timeouts are bounded. Legacy summary tabs and historical archives remain outside this refresh scope.

## Data and unit policy

Exact source headers are validated on every run. Units are configured centrally in `src/normalization.py`. Raw values, normalized values, source units, transformations and scaling warnings are retained. In particular, `Debt/Equity Ratio` is already a ratio: `0.03821` remains `0.03821`. `Debt/Total Capital`, `Net Margin %`, `Div. Yield %`, ROE, ROA and ROI are configured as percentage-point fields and divided by 100 once.

Missing, invalid, unavailable and future-required data are never represented as numeric zero. `src/institutional.py` owns the metric-availability registry, future hooks, margin reconciliation, EPS interpretation, sector-aware balance-sheet gates, tradability gates, short-implementation flags and coverage architecture.

Currently unavailable institutional calculations include enterprise value, EV/EBIT, EV/EBITDA, net debt, free cash flow and FCF yield, interest coverage, ROIC/NOPAT, Altman Z, Piotroski F, DuPont, accruals and independently calculated time-series indicators. They activate only when their genuine required inputs exist; weak proxies are prohibited.

## Strategy and implementation controls

- Safe requires interpretable positive forward EPS by default and passes a sector-aware balance-sheet gate.
- High Growth Potential may retain negative-EPS firms; conventional negative-base growth is excluded and explicitly flagged.
- Turnaround Candidate may retain negative-EPS firms but requires current valuation/weak-profitability setup and a survivability gate. Recovery trajectory belongs to Secondary analysis.
- Specifically classified financial companies are exempt from inappropriate operating-company liquidity and debt/equity hard tests; missing specialist metrics reduce underwriting readiness.
- REIT and real-estate gates prefer genuine debt/EBITDA or debt/EBIT measures.
- Tradability is a hard implementation gate separate from factor and strategy scores. Defaults are $3bn market cap, $25m average daily dollar volume for longs and $50m for shorts. OTC/ADR/MLP/Canadian rules are configurable.
- Missing borrow or squeeze data does not erase a quantitative short candidate, but it forces `short_implementation_validation_required = TRUE`.

The audit distinguishes raw-factor coverage, strategy-scoring coverage, quant-ranking coverage and institutional-underwriting coverage. Missing future institutional fields reduce underwriting readiness without falsely reducing active quant scores.

## Google Sheets outputs

Live runs update `Control Panel`, `Metric Registry`, the three retained strategy summaries, industry tabs and adjacent `[Industry] - Helper` audit tabs. `Primary Summary` is deleted. Industry tabs contain `QUANT LONG CANDIDATES`, `QUANT SHORT CANDIDATES`, and `FULL INDUSTRY RANKING`; these are shared ranking diagnostics, not active strategies.

Column E of each strategy summary, the Short tab, and the industry candidate sections contains a generated shortlist reason. It names the current candidate and explains its score and rank, leading weighted family contributions, data coverage, selection gates, and available raw snapshot context. The text is rebuilt on each run from that candidate's current data; it does not claim historical performance or verified point-in-time validity.

Helpers expose raw and normalized inputs, peer statistics, group/family calculations, three-strategy contributions, EPS flags, margin reconciliation, balance-sheet and tradability gates, borrow-validation status, coverage scores and readiness summaries.

## Run and test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Live-source dry run; does not modify Sheets
python3 main.py --output-dir outputs

# Explicitly authorized live update
python3 main.py --live --output-dir outputs/strategy_live

# Reproducible tests from the project root
python3 -m pytest -q
```

Only `--live` authorizes Google Sheets writes. Writes are batched, retried for transient failures and read back after update. This is a quantitative research and decision-support layer, not an autonomous trading instruction.

### Zacks source importer

`--import-zacks` adds an optional source stage before the model runs. It opens
the Zacks screener, applies a minimum market-cap screen, requests the available
Edit View fields, downloads CSV, validates the complete source schema locally,
then replaces the configured `Dataset` tab in chunks with a local CSV backup
and a live read-back. It then runs the normal full analysis. The importer never
bypasses login, CAPTCHA, bot checks, or subscription controls.

On a Windows server, install the project requirements and Chromium once, then
set the workbook and credential locations outside the source code:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
$env:SOURCE_SPREADSHEET_ID = "YOUR_SHEET_ID"
$env:DESTINATION_SPREADSHEET_ID = "YOUR_SHEET_ID"
$env:GOOGLE_CREDENTIALS_FILE = "C:\\Code\\service_account.json"
python main.py --live --import-zacks --zacks-market-cap-min 3000 --output-dir outputs\\zacks_run
```

For a user-completed Zacks login or access check, add `--zacks-headed`. In a
headless server run, such a gate stops the importer with diagnostics rather than
trying to circumvent the site. CSV exports, browser profile data, and Dataset
backups stay beneath `outputs/zacks_import` unless `--zacks-export-dir` is set.

### Short sheet

The normal `--live` run refreshes one `Short` tab. To refresh only that tab,
run `.venv/bin/python main.py --live --short-only`. This mode preserves Dataset
and the long outputs.

Short uses the same source universe, peer factor definitions, missing-data
treatment, confidence adjustment, `MIN_CANDIDATE_CONFIDENCE`, and per-industry
limit as general Long. Its own `SHORT_WEIGHT_*` family mix and
`SHORT_MIN_WEAK_FAMILIES`/`SHORT_WEAK_FAMILY_SCORE` controls make it an independent
current-state fragility and valuation screen. It does not reverse the separate
Safe, High Growth Potential or Turnaround strategy overlays.

`SHORT_DISPLAY_TOP_N` in Control Panel sets the display cap and defaults to 50
to provide a broader candidate pool for the secondary screen; it remains
separately editable.
Changes apply after rerunning. Configured short tradability is required before selection; borrow checks remain visible and unverified. No earnings or momentum trend rule is added. TOP_N_SHORT mirrors the shared per-industry limit and MAX_QUANT_SHORTS_TOTAL mirrors SHORT_DISPLAY_TOP_N. Legacy selection thresholds remain configurable; counts depend on the current data and eligibility gates.

The standalone builder now embeds the canonical root source, includes Short and the economic policy, and updates the current Colab bundle. The directory ending in `bundle 2` is historical; use the current root or rebuilt distribution.
This is a research ranking, not a validated trading edge or borrow confirmation.
