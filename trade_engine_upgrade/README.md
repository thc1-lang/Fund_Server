# Trade Backtest & Monte Carlo v2

The executable filename and existing Google Sheets input layout are retained.
Normal execution is now strict: invalid data/configuration fails before expensive
work, a structure and exposure are frozen before final TEST, and TEST can only
confirm, review or veto that structure. It can never choose a replacement.

Keep these files together:

* `Trade Backtest & Monte Carlo.py` — backward-compatible executable and Sheets helpers
* `research_core.py` — typed configuration, data checks, cached paths, execution and search
* `research_diagnostics.py` — walk-forward, statistics, regimes, stresses, MC and sizing
* `research_pipeline.py` — orchestration, freeze ledger, JSON and batched Sheets output

Run against the current Google Sheet:

```powershell
python "Trade Backtest & Monte Carlo.py" --config research_config.json
```

Copy `research_config.example.json` to `research_config.json`, then replace the
instrument name and cost assumptions. If `research_config.json` is absent, the
engine maps the existing Control Panel horizon to trading-day candidates and uses
safe defaults. Zero cost assumptions are allowed for compatibility but generate
an analyst warning.

Offline/reproducible mode:

```powershell
python "Trade Backtest & Monte Carlo.py" --config research_config.json `
  --raw ohlc.csv --entries entries.csv --output research_runs
```

The offline JSON config can include `metric_rules` and `gate_rules`. The engine
writes a frozen pre-test manifest, a strict JSON result, `latest.json`, and an OOS
consumption ledger. Move or replace the holdout period for a new independent
experiment; deleting the ledger does not make previously observed data untouched.

`--legacy` invokes the original pipeline during migration. It retains the original
methodological defects documented in `AUDIT.md` and should not be used to approve
capital. Imported original functions remain available to existing callers.

The following semantics are explicit:

* Raw Data columns are resolved by header, not position. The existing `Date | Price | Open | High | Low | Date UK` layout maps `Date UK` to the session date and `Price` to raw close;
* `close_type` is `auto`, `raw` or `adjusted`. `auto` prefers raw `Price`/`Close`; adjusted close is labelled separately and is paired with adjusted open/high/low when those columns exist;
* `price_type` declares `close`, `adjusted_close`, `settlement` or `continuous_futures`, while `ohlc_adjustment` declares `raw` or `adjusted`. Raw close, adjusted OHLC and continuous-futures OHLC are range-validated consistently. Declared settlement and adjusted-close/raw-OHLC mismatches are recorded under their own semantics rather than mislabeled as corrupt raw closes;
* `ohlc_validation_mode` is `strict`, `warn` or `repair_rounding_only`. The default `strict` mode rejects material errors, while microscopic discrepancies use `max(abs(price) * 1e-8, 1e-10)` tolerance;
* `ohlc_invalid_row_policy` defaults to `halt`. Optional `quarantine` is applied only below the configured fraction and consecutive-run limits and only when no enabled entry or maximum-horizon path would lose an observation; otherwise validation still fails;
* validation never clips material errors. `repair_rounding_only` changes only microscopic open/close boundary discrepancies and records every change;
* entry occurs at the supplied entry price at the entry-date close;
* stop/target monitoring starts on the following bar;
* short returns are cash P&L divided by original notional;
* opening gaps take priority, and conservative unresolved OHLC conflicts are stop-first;
* percentage and volatility-scaled distances are fractions, not percentage points;
* horizons are trading sessions unless `calendar_months` is explicitly set;
* each split purges every entry unable to complete the maximum candidate horizon;
* embargo dates and all exclusions are recorded exactly;
* cost inputs are total round-trip bps except separately named entry/exit slippage;
* sequential trade drawdown and calendar portfolio drawdown are reported separately;
* infinite profit factor remains `null` in JSON with an explanatory note;
* PSR/DSR are withheld unless simple screens support their IID/asymptotic assumptions;
* confidence is a transparent policy score, not a probability of future profit.

Google Sheet output is written once per output tab. It adds Walk Forward, Stress
Tests and Performance Diagnostics while retaining Train, Validation, Test, Gate,
MC, Position Sizing and Summary. The strict local JSON is authoritative because
large nested diagnostics may be abbreviated in Sheets.

When OHLC validation fails, the local run directory receives complete CSV and
JSON diagnostics, a separate analysis JSON, and a top-20 CSV. Each record
includes source row number, source/canonical dates, OHLC values, violation type,
absolute difference and relative difference. The analysis contains per-type
statistics, yearly/monthly clustering, semantic root-cause classification,
numeric parsing audit and guarded-quarantine impact.

Run tests and benchmark:

```powershell
python -m unittest discover -s tests -v
python benchmark.py
```
