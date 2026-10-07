# Trade Backtest & Monte Carlo v2 — final implementation audit

## Changes made

The original 1,782-line executable remains the command entry point and its public
functions remain importable. Normal execution now delegates to three focused
modules: a typed numerical core, dependence/risk diagnostics, and safe
orchestration/reporting. `--legacy` remains available only as a migration escape
hatch. A pre-v2 backup is installed beside the executable.

The numerical engine now provides strict configuration/data validation, common
sample purging using the maximum candidate horizon, exact embargo records,
expanding/rolling nested walk-forward folds, percentage/ATR/realised-volatility
barriers, trading-session horizon search, fixed/horizon/trailing/breakeven/
time-decay/partial-trailing exit families, coarse quantile grids and robust local
fine searches. Stop/target crossing tables are vectorised and chunked. Paths,
features and excursion arrays are precomputed; versioned content hashes support
safe persistent cache reuse.

Execution is cash-notional consistent for long and short trades. Conservative
research mode uses next-open gap-aware stops and stop-first unresolved OHLC bars.
Threshold and pessimistic modes remain configurable. Target-first and seeded
random-order cases are sensitivity diagnostics. Costs include spread, commission,
entry/exit slippage, impact approximation, financing, overnight funding and short
borrow. Partial-exit carrying cost is charged only to remaining exposure.

Selection uses a parameter plateau score based on neighbouring stop/target/
horizon results. TRAIN creates regions, nested walk-forward refits within TRAIN,
outer VALIDATION ranks a small frozen shortlist, and exactly one candidate plus
its exposure and gates is persisted before any TEST path is constructed. TEST can
confirm, review or veto; it cannot re-rank or select a replacement. An OOS ledger
flags repeated holdout use and prevents a reused sample from receiving PASS.

Statistical output includes overlap/autocorrelation ESS, block-bootstrap
confidence intervals, selection-count disclosure and a multiple-testing
diagnostic, empirical tails, excursions, temporal/regime tables, stress tests,
IID/circular-block/stationary Monte Carlo, two-stage parameter uncertainty, cost
uncertainty, execution-order uncertainty and ruin-preserving equity paths. PSR/DSR
are emitted only when simple dependence screens support their assumptions; CSCV,
White Reality Check and Hansen SPA are explicitly null rather than fabricated.

Position sizing is frozen from VALIDATION using a fixed risk budget and exposure
cap. Kelly variants are diagnostics only. Sequential trade-equity drawdown and a
daily close, concurrent-exposure calendar portfolio drawdown are separately named.
The authoritative output is strict JSON (`null`, never fake zero/9999), with a
frozen manifest, run/config/data/code hashes, assumptions, warnings, confidence
components/caps, gate evidence and exact exclusions. Sheets writes are batched to
the preserved tabs plus Walk Forward, Stress Tests and Performance Diagnostics.

## Important bugs found and fixed

| Problem | Why it mattered | Fix |
|---|---|---|
| Short return used `entry/exit - 1` | Cash P&L, stop losses and gaps were asymmetric and wrong | Uses signed `(exit/entry - 1)` consistently |
| Opens were dropped and stops filled at thresholds | Gap losses were understated | Caches opens; gap-aware and pessimistic fills implemented |
| Test score chose the final candidate; validation+test chose size | Final OOS contaminated model and sizing decisions | Persisted pre-test freeze; TEST is a one-candidate veto only |
| MC discarded returns at or below -100% | Ruin and drawdown risk looked artificially benign | Equity is absorbing at zero; ruin observations remain |
| Validation/test ESS was `trades × 0.8` | Confidence was unrelated to actual overlap/dependence | Uses holding-interval uniqueness and positive-sequence autocorrelation ESS |
| Drawdown peak omitted starting equity | Initial losses understated drawdown | Peak always includes starting capital of 1 |
| PF and Calmar used 9999 | Downstream AI received fake precision | Keeps mathematical infinity internally and outputs JSON null plus explanation |
| Negative-drawdown P95 was treated as the adverse tail | Some MC risk gates used an optimistic percentile | Uses positive drawdown severity and explicit adverse P95 |
| Entries/bars were silently dropped | Data defects could masquerade as clean samples | Duplicate/impossible/missing/invalid inputs fail early; exclusions are explicit |
| Test/summary fallback mixed metrics from different candidates | A synthetic “best candidate” could combine unrelated maxima | Structured in-memory results flow to reports; no numerical Sheets read-backs |
| Several reported metrics were hard-coded zero | Analysts saw invented evidence | Real calculations added or unavailable values become null |
| Dynamic exits could not be represented | Research was restricted to one exit style | Added five opt-in alternatives while preserving fixed stop/target |
| Live position choice maximised post-test MC outcomes | Sizing was another hidden optimisation | Frozen validation risk budget/cap; TEST never selects exposure |
| Large MC/block/streak loops were Python-heavy | Standard runs spent most time repeating path work | Vectorised/chunked resampling, drawdown and streak kernels; conditional parallel MC |

## Benchmark

Deterministic seed 20260926; 1,100 bars, 225 trades, 36 stop/target candidates;
median of five warm runs. The comparison includes numerical evaluation, metrics
and ranking and excludes Google network time.

| Measurement | Original | Refactored |
|---|---:|---:|
| Optimisation runtime | 0.449401 s | 0.064524 s |
| Speedup | 1.00× | **6.965×** |

The local end-to-end stage profile used 1,096 bars, 154 signals and 1,000 MC paths:

| Stage | Seconds |
|---|---:|
| Data validation/loading | 0.04152 |
| Trade-cache generation and persistence | 0.05549 |
| TRAIN search | 0.03249 |
| Outer VALIDATION | 0.00172 |
| Walk-forward | 0.09738 |
| Statistical diagnostics | 0.02280 |
| Stress suite | 0.05071 |
| Monte Carlo | 0.02892 |
| Final TEST | 0.04742 |
| Total compute | 0.44447 |
| Google Sheets write | N/A — no live credential/API mutation in this audit |

On this small workload walk-forward refitting is the remaining bottleneck. On
large Standard/Deep runs, advanced diagnostics and Monte Carlo are expected to
dominate after the vectorised candidate search.

## Statistical methodology

TRAIN alone derives coarse/adaptive levels and assesses three-dimensional local
plateaus. Walk-forward folds live entirely inside TRAIN and repeat grid creation,
selection and forward evaluation under expanding or rolling history. Outer
VALIDATION evaluates the bounded TRAIN shortlist, calculates costs, dependence,
regimes, tails, stresses, ambiguity and MC, freezes one candidate and freezes
exposure. Purging removes any entry unable to complete the maximum search horizon;
embargo dates are excluded from sample starts and recorded. FINAL TEST is opened
only after the frozen manifest exists and is executed once against that structure.

The block bootstrap preserves local serial clusters subject to a stationarity and
block-length assumption. Stationary bootstrap randomises block length. The
parameter-uncertainty scenario uses a two-stage predictive bootstrap; cost shocks
are independent mean-one lognormal multipliers. These are scenario distributions,
not guaranteed future probabilities. Multiple-testing trial counts include every
search candidate, every nested-fold candidate and a configurable prior-research
count. DSR follows its published role of adjusting Sharpe evidence for selection
bias and non-normal returns, but is withheld when the working assumptions fail.

## Analyst output

Downstream AI should consume `research_runs/latest.json` or the immutable
run-ID JSON. The principal objects are `recommended_structure`,
`expected_performance`, `risk`, `statistics`, `walk_forward`, `regime_analysis`,
`stress_tests`, `monte_carlo`, `oos`, `confidence`, `warnings`,
`failure_conditions` and `final_status`. A structure may be present for audit when
status is REVIEW/FAIL; it is deployable only when `final_status == "PASS"` and the
portfolio/risk functions approve it. The frozen manifest and OOS ledger establish
the experiment boundary. Sheets are a human view, not the machine-readable source.

## Remaining limitations

Daily OHLC cannot reveal intrabar order; the engine quantifies that uncertainty but
cannot eliminate it without intraday data. The corporate-action test detects large
discontinuities but does not verify vendor adjustment methodology. Calendar gaps
are only definitive when the caller provides expected exchange sessions. Regimes
are endogenous volatility/trend regimes; macro, liquidity and cross-asset regimes
need aligned external data.

Calendar portfolio simulation uses daily-close marking, fixed entry notional,
simple gross caps and no cross-asset margin/netting, forced liquidation or market
depth model. Impact is a configured approximation. Borrow availability is not
modelled. Statistical tests cannot make nonstationary financial returns IID, and a
confidence score is a transparent policy score rather than a probability of
profitability. The ledger begins with v2 and cannot prove that historical TEST data
was never viewed before this upgrade; use a new holdout for a defensible fresh OOS
claim. Google Sheets API integration was compiled and its batch interfaces were
retained, but it was not written live in this audit because the isolated test
runtime has no `gspread` package or user-approved production write step.

## Verification

Installed files were executed from `C:\Code`, not only from staging. Results:

`61 tests passed, 0 failed` in 3.073 seconds before final installation sync. The
same installed files then passed 61/61 from `C:\Code` in 4.762 seconds. Syntax compilation and executable
`--help` both succeeded. Tests cover long/short stops and targets, gap fills,
same-bar ordering, horizons, costs/carry/borrow, all exit families, purge/embargo,
leakage, invalid data, volatility barriers, ESS, ruin, bootstrap seeds, cache
invalidation/reuse, holdout reuse and the frozen-structure invariant.
