# Baseline audit

Original: `C:\Code\Trade Backtest & Monte Carlo.py`, 1782 lines, SHA256
`7f907e9386f524ec7ae0b0530b283c70f26052a743ff412b810091420aa0e304`.
Full source read before edits, including late-defined stages. Baseline retained in
`baseline/original.py`; no live Sheets writes were needed for the audit.

## Execution map

`run_all` connects to Sheets, reads Control Panel, Raw Data and Entries once;
then invokes Train → Validation/robustness → Test → Gate → MC → Position Sizing
→ Summary. The Trade calculator exists but is not invoked by `run_all`.

* Config: C3:C17 direction, calendar-month horizon, three date windows;
  E4:L20 weighted/hard metric rules; N4:R14 gate rules.
* Input: positional OHLC B/C/D/E/F = close/open/high/low/date; Entries A:G =
  ID/date/price/direction/split/valid/source. Invalid rows are silently dropped.
* Cache: slices bars strictly after entry date through calendar-month horizon,
  excludes horizon end beyond split end, caches highs/lows/closes and MFE/MAE.
* Train: MAE/MFE quantiles plus fixed stop/target grid (26 × 30), Python nested
  candidate/trade loops, gross metrics, weighted rules and best-return tie-break.
* Validation: first 24 approved/watch/matrix candidates, gross metrics and decay,
  validation neighbours with fallback to train neighbours, base/stress costs,
  two chronological halves. No walk-forward or horizon search.
* Test: falls back to validation or train candidates; tests up to 30 and ranks by
  test score/return. Gate then selects the best test candidate and reads metrics
  back from Sheets, sometimes substituting unrelated summary maxima.
* MC: only final-OOS-pass candidates; validation, test and combined samples;
  10,000 IID and circular-block paths; base/stress; returns <= -100% removed.
* Sizing: six exposure multipliers on all those MC windows; recommendation and
  risk frontier chosen from combined validation+test stressed paths.
* Sheets: batch section writes, but many read-backs and clears; Train,
  Validation, Test, Gate, MC, Position Sizing, Summary, optional Trade.
* Existing metrics: return mean/median/SD, trade Sharpe-like ratio, expected R,
  PF/payoff, target-hit rate labelled win rate, positive/loss/stop/target/timeout
  rates, conflicts, holding times, ES5, sequential DD, Calmar-style ratio,
  streaks, best/worst trade, entry spacing, overlap/ESS; validation decay,
  sensitivity, cluster, cost/stress and half consistency; MC return percentiles,
  loss/DD/streak probabilities; sizing frontiers. Several reported metrics
  (excursion efficiencies, test regimes, validation streaks) are zero placeholders.

## Material defects to correct

1. Short P&L uses entry/exit - 1 rather than (entry-exit)/entry; stop/target
   thresholds and short losses are therefore wrong for cash notional exposure.
2. Opens are discarded from cache; stops always fill at threshold even on gaps.
3. Test rankings, MC candidate selection and sizing feed test information into
   the recommendation. Statements claiming no optimisation are inaccurate.
4. No split chronology validation; incomplete end-of-data horizons can survive.
5. Validation/test ESS is trades × .8. Train approximation ignores actual overlap.
6. Equity peaks omit starting capital, understating initial losing drawdowns.
7. PF/undefined values become 9999; missing metrics become fabricated zeroes.
8. MC negative drawdown P95 is the benign tail; some gates use it as adverse tail.
9. MC removes ruin observations, which biases all risk estimates.
10. Entries are not sorted consistently; duplicate and impossible data are hidden.
11. Train/validation/test selection uses gross returns before cost assessment.
12. Gate aliases miss Average Test Return and Effective Sample Size; summary
    fallback can attach another candidate's performance to the chosen candidate.
13. Several summary maxima describe different candidates as one candidate.
14. Block resampling, losing-streak and candidate loops repeat Python work;
    repeated division, datetime parsing, caches and full MC matrices waste time/RAM.
15. Live calculator contains Python-concatenated malformed formula strings,
    clears inputs, and gross notional is not constrained by the risk budget.

## Compatibility plan

Keep the executable filename, original Sheets input positions and original sheet
names. Add a small numeric core, diagnostics and pipeline module. Preserve the
baseline for reproducible comparisons; default command uses the safe pipeline.
Legacy low-level interfaces retain regression coverage. New structured JSON is
the authoritative interface for analysts; final-test failure never triggers a
new search. Persist selection before opening final-test paths.
