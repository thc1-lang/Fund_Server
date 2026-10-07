"""Chronological, cost-aware numeric research core. No network or Sheets I/O.

Raw OHLC and adjusted OHLC are distinguished explicitly. Signals execute at the
supplied entry price at the close of the entry date; only subsequent bars are executable.
All returns are cash P&L / original notional, including shorts.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict, replace
from typing import Any
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import numpy as np
import pandas as pd

VERSION = '2.0.0'
FAMILIES = ('fixed', 'horizon', 'trailing', 'breakeven', 'time_decay', 'partial_trailing')
LOG = logging.getLogger('trade_research')

@dataclass(frozen=True)
class RawDataMapping:
    date: str
    close: str
    open: str
    high: str
    low: str
    close_type: str
    ohlc_basis: str
    source_date: str | None = None
    price_type: str = 'close'
    ohlc_adjustment: str = 'raw'

class OHLCValidationError(ValueError):
    def __init__(self, message, diagnostics, summary):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.summary = summary

class MarketDataReadinessError(OHLCValidationError):
    def __init__(self, message, diagnostics, summary, readiness):
        super().__init__(message, diagnostics, summary)
        self.readiness = readiness

def _header_key(value):
    return ' '.join(str(value).strip().lower().replace('_',' ').replace('-',' ').split())

def parse_numeric_series(values):
    """Parse plain or correctly grouped-thousands numbers without coercion tricks."""
    series=pd.Series(values)
    text=series.astype(str).str.strip()
    numeric=pd.to_numeric(series,errors='coerce')
    grouped=text.str.match(r'^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[eE][+-]?\d+)?$')
    if grouped.any():
        numeric.loc[grouped]=pd.to_numeric(text.loc[grouped].str.replace(',','',regex=False),errors='coerce')
    return numeric

def resolve_raw_data_columns(columns, close_type='auto', price_type='close', ohlc_adjustment='raw'):
    """Resolve semantic OHLC fields, preferring the existing Sheets schema.

    Adjusted close is never silently treated as raw close. If an adjusted close
    is selected and adjusted O/H/L exist, the whole adjusted set is preferred.
    """
    actual=list(columns); keys={_header_key(c):c for c in actual}
    def pick(names):
        return next((keys[_header_key(name)] for name in names if _header_key(name) in keys),None)
    date=pick(['Date UK','Trading Date','Session Date','Datetime','Timestamp','Date'])
    source_date=pick(['Date','Source Date','source_date'])
    if price_type=='settlement':
        raw_close=pick(['Settlement','Settlement Price','Settle','Price','Close','Closing Price','Close Price','Last Price'])
    elif price_type=='continuous_futures':
        raw_close=pick(['Continuous Close','Back Adjusted Close','Price','Close','Closing Price','Close Price','Last Price'])
    else:
        raw_close=pick(['Price','Close','Closing Price','Close Price','Last Price'])
    adjusted_close=pick(['Adj Close','Adjusted Close','Adjusted Closing Price'])
    if close_type not in ('auto','raw','adjusted'):
        raise ValueError('close_type must be auto, raw or adjusted')
    if close_type=='auto':
        selected_type='adjusted' if price_type=='adjusted_close' else ('raw' if raw_close is not None else 'adjusted')
    else:
        selected_type=close_type
    if selected_type=='raw':
        close=raw_close
    elif adjusted_close is not None:
        close=adjusted_close
    elif price_type=='adjusted_close':
        # Some vendors expose the adjusted series under the generic Price/Close
        # heading. The declared price_type supplies the missing semantic label.
        close=raw_close
    else:
        close=None
    if close is None:
        expected='raw Price/Close' if selected_type=='raw' else 'Adj Close/Adjusted Close'
        raise ValueError(f'Raw Data has no {expected} column')
    adjusted_open=pick(['Adj Open','Adjusted Open','Adjusted Opening Price'])
    adjusted_high=pick(['Adj High','Adjusted High'])
    adjusted_low=pick(['Adj Low','Adjusted Low'])
    if selected_type=='adjusted' and all(x is not None for x in (adjusted_open,adjusted_high,adjusted_low)):
        opening,high,low=adjusted_open,adjusted_high,adjusted_low; basis='adjusted'
    else:
        opening=pick(['Open','Opening Price','Open Price'])
        high=pick(['High','High Price','Session High'])
        low=pick(['Low','Low Price','Session Low'])
        basis='raw'
    missing=[name for name,value in [('date',date),('open',opening),('high',high),('low',low)] if value is None]
    if missing:
        raise ValueError(f'Raw Data missing resolvable columns: {missing}; available headings={actual}')
    if ohlc_adjustment=='adjusted': basis='adjusted'
    return RawDataMapping(date,close,opening,high,low,selected_type,basis,source_date,price_type,ohlc_adjustment)

def standardise_raw_data(frame, close_type='auto', date_parser=None, price_type='close', ohlc_adjustment='raw'):
    mapping=resolve_raw_data_columns(frame.columns,close_type,price_type,ohlc_adjustment)
    parse=date_parser if date_parser is not None else lambda s: pd.to_datetime(s,errors='coerce')
    numeric_sources={'open':mapping.open,'high':mapping.high,'low':mapping.low,'close':mapping.close}
    parsed={name:parse_numeric_series(frame[source]) for name,source in numeric_sources.items()}
    out=pd.DataFrame({'source_date':frame[mapping.source_date if mapping.source_date is not None else mapping.date].astype(str),
                      'date':frame[mapping.date].map(parse),**parsed})
    if 'source_row_number' in frame:
        out['source_row_number']=frame['source_row_number'].to_numpy()
    else:
        out['source_row_number']=np.arange(2,len(frame)+2)
    out.attrs['raw_data_mapping']=asdict(mapping)
    numeric_audit={}
    for name,source in numeric_sources.items():
        text=frame[source].astype(str); stripped=text.str.strip(); empty=stripped.eq('')
        numeric_audit[name]={'source_column':source,'rows':len(frame),'empty_values':int(empty.sum()),
            'parse_failures':int((parsed[name].isna()&~empty).sum()),
            'values_with_outer_spaces':int(text.ne(stripped).sum()),
            'values_with_commas':int(stripped.str.contains(',',regex=False).sum()),
            'values_with_percent_signs':int(stripped.str.contains('%',regex=False).sum()),
            'values_with_currency_symbols':int(stripped.str.contains(r'[$£€¥]',regex=True).sum()),
            'values_with_scientific_notation':int(stripped.str.match(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+$').sum()),
            'values_with_decimal_comma_pattern':int(stripped.str.match(r'^[+-]?\d+,\d+$').sum())}
    out.attrs['numeric_parsing_audit']=numeric_audit
    out.attrs['loaded_first_10']=out[['source_date','date','close','open','high','low']].head(10).copy()
    return out,mapping

def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()

def frame_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()

@dataclass(frozen=True)
class ExecutionConfig:
    mode: str = 'gap_aware'  # threshold_fill, gap_aware, pessimistic_ohlc
    same_bar: str = 'stop_first'  # target_first or random (sensitivity only)
    spread_bps: float = 0.0  # total round-trip
    commission_bps: float = 0.0  # total round-trip, proportional to original notional
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    impact_bps: float = 0.0  # total round-trip approximation, not a liquidity model
    financing_bps_year: float = 0.0
    borrow_bps_year: float = 0.0
    overnight_bps_day: float = 0.0

    def validate(self):
        if self.mode not in ('threshold_fill', 'gap_aware', 'pessimistic_ohlc'):
            raise ValueError('Unknown execution mode')
        if self.same_bar not in ('stop_first', 'target_first', 'random'):
            raise ValueError('Unknown same-bar mode')
        for k, v in asdict(self).items():
            if k not in ('mode', 'same_bar') and (not np.isfinite(v) or not 0 <= v <= 10000):
                raise ValueError(f'Invalid cost {k}: expected 0..10000 bps')

    def costs(self, days, short=False):
        annual = self.financing_bps_year + (self.borrow_bps_year if short else 0)
        return (self.spread_bps + self.commission_bps + self.entry_slippage_bps
                + self.exit_slippage_bps + self.impact_bps
                + annual * np.asarray(days) / 365.0
                + self.overnight_bps_day * np.asarray(days)) / 10000.0

@dataclass(frozen=True)
class ResearchConfig:
    direction: str
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    instrument: str = 'UNSPECIFIED'
    mode: str = 'STANDARD'
    horizons: tuple[int, ...] = (5, 10, 20, 30, 45, 60, 90)
    calendar_months: int | None = None  # optional exact legacy horizon
    stops: tuple[float, ...] = (.01, .02, .04, .06, .08, .12)
    targets: tuple[float, ...] = (.01, .02, .04, .08, .12, .20)
    normalisations: tuple[str, ...] = ('percent',)
    volatility_multipliers: tuple[float, ...] = (.5, 1., 1.5, 2., 3.)
    families: tuple[str, ...] = ('fixed',)
    adaptive: bool = True
    fine_regions: int = 3
    shortlist: int = 12
    embargo_unit: str = 'trading_days'
    embargo: float = 0
    walk_forward: str = 'expanding'  # none, expanding, rolling
    walk_forward_fold_method: str = 'adaptive'  # time, trade_count, adaptive
    folds: int = 4
    min_train_bars: int = 252
    rolling_train_bars: int = 756
    min_trades: int = 30
    min_train_trades_per_fold: int = 30
    min_validation_trades_per_fold: int = 30
    temporal_robustness_method: str = 'equal_trade_count_periods'  # calendar_periods, equal_trade_count_periods
    temporal_min_trades_per_block: int = 10
    temporal_min_blocks: int = 3
    temporal_max_blocks: int = 5
    temporal_cv_enabled: bool = True
    min_temporal_robustness_score: float = 60.
    min_evidence_sufficiency_score: float = 60.
    min_temporal_positive_fraction: float = .67
    min_temporal_pf_positive_fraction: float = .67
    min_temporal_expected_r_positive_fraction: float = .67
    min_ess: float = 20
    min_pf: float = 1.05
    min_parameter_score: float = 55
    min_fold_pass: float = .5
    max_conflict_rate: float = .1
    min_stress_survival: float = .6
    min_bootstrap_positive: float = .95
    min_regime_score: float = 50
    max_mc_loss_probability: float = .25
    max_mc_drawdown: float = .5
    max_sequential_drawdown: float = .6
    seed: int = 42
    mc_sims: int = 10000
    bootstrap_reps: int = 2000
    block_length: int = 5
    chunk_size: int = 64
    mc_batch: int = 256
    memory_mb: int = 256
    n_jobs: int = field(default_factory=lambda: max(1, (os.cpu_count() or 2) - 1))
    parallel_min_work: int = 2_000_000
    risk_fraction: float = .005
    max_exposure: float = .25
    portfolio_gross_cap: float = 1.0
    ruin_equity: float = .5
    losing_streak_threshold: int = 10
    cost_uncertainty: float = .25  # lognormal sd of nonnegative cost multiplier
    partial_fraction: float = .5
    breakeven_trigger_r: float = 1.
    decay_fraction: float = .5
    feature_lookback: int = 20
    discontinuity_limit: float = .5
    allow_discontinuities: bool = False
    close_type: str = 'auto'
    price_type: str = 'close'  # close, adjusted_close, settlement, continuous_futures
    ohlc_adjustment: str = 'raw'  # raw or adjusted, applied consistently to OHLC
    asset_class: str = 'unknown'
    data_vendor: str = 'UNSPECIFIED'
    data_source: str = 'google_sheet'
    market_data_path: str | None = None
    secondary_data_path: str | None = None
    secondary_provider: str = 'UNSPECIFIED'
    require_secondary_source: bool = False
    require_provenance: bool = False
    ticker: str = 'UNSPECIFIED'
    provider_symbol: str = 'UNSPECIFIED'
    frequency: str = '1D'
    trading_calendar: str = 'UNSPECIFIED'
    timezone: str = 'UNSPECIFIED'
    currency: str = 'UNSPECIFIED'
    adjustment_method: str = 'UNSPECIFIED'
    source_identifier: str = 'UNSPECIFIED'
    signal_instrument: str = 'UNSPECIFIED'
    execution_instrument: str = 'UNSPECIFIED'
    allow_test_execution: bool = True
    allow_final_oos: bool = False
    volatility_lookback: int = 20
    regime_lookback: int = 0
    coverage_safety_buffer_sessions: int = 5
    require_warmup_coverage: bool = False
    require_full_horizon_coverage: bool = False
    ohlc_validation_mode: str = 'strict'  # strict, warn, repair_rounding_only
    ohlc_invalid_row_policy: str = 'halt'  # halt or quarantine
    max_ohlc_quarantine_fraction: float = .01
    max_ohlc_quarantine_run: int = 1
    ohlc_rounding_rtol: float = 1e-8
    ohlc_rounding_atol: float = 1e-10
    expected_sessions: tuple[str, ...] = ()  # supplied exchange sessions, not guessed holidays
    prior_research_trials: int = 0
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    def validate(self):
        if self.direction not in ('LONG', 'SHORT'):
            raise ValueError('direction must be LONG or SHORT')
        dates = [pd.Timestamp(getattr(self, k)) for k in ('train_start', 'train_end', 'validation_start', 'validation_end', 'test_start', 'test_end')]
        if any(pd.isna(d) or d.tzinfo is not None for d in dates):
            raise ValueError('Dates must be valid timezone-naive session dates')
        if not (dates[0] <= dates[1] < dates[2] <= dates[3] < dates[4] <= dates[5]):
            raise ValueError('TRAIN, VALIDATION, TEST must be strictly chronological and non-overlapping')
        if not self.horizons or any(int(h) != h or h < 1 for h in self.horizons):
            raise ValueError('horizons must contain positive integer trading days')
        if self.calendar_months is not None and (self.calendar_months < 1 or int(self.calendar_months) != self.calendar_months):
            raise ValueError('calendar_months must be a positive integer')
        for name in ('stops', 'targets', 'volatility_multipliers'):
            vals = getattr(self, name)
            if not vals or any(not np.isfinite(x) or x <= 0 for x in vals):
                raise ValueError(f'{name} must be positive and finite')
        if max(self.stops) >= 1 or (self.direction == 'SHORT' and max(self.targets) >= 1):
            raise ValueError('Percentage stop/short target must be below 100%')
        if not self.families or set(self.families) - set(FAMILIES):
            raise ValueError('Invalid exit family')
        if not self.normalisations or set(self.normalisations) - {'percent', 'atr', 'volatility'}:
            raise ValueError('Invalid normalisation')
        if self.mode not in ('FAST', 'STANDARD', 'DEEP') or self.walk_forward not in ('none', 'expanding', 'rolling'):
            raise ValueError('Invalid performance or walk-forward mode')
        if self.walk_forward_fold_method not in ('time', 'trade_count', 'adaptive'):
            raise ValueError('Invalid walk-forward fold method')
        if self.temporal_robustness_method not in ('calendar_periods', 'equal_trade_count_periods'):
            raise ValueError('Invalid temporal robustness method')
        if self.embargo_unit not in ('trading_days', 'calendar_days', 'percentage') or not np.isfinite(self.embargo) or self.embargo < 0:
            raise ValueError('Invalid embargo')
        if self.embargo_unit == 'percentage' and self.embargo >= 1:
            raise ValueError('Percentage embargo must be a fraction < 1')
        if self.embargo_unit != 'percentage' and int(self.embargo) != self.embargo:
            raise ValueError('Day embargo must be an integer')
        for name in ('folds', 'min_train_bars', 'rolling_train_bars', 'min_trades', 'min_train_trades_per_fold', 'min_validation_trades_per_fold', 'temporal_min_trades_per_block', 'temporal_min_blocks', 'temporal_max_blocks', 'seed', 'mc_sims', 'bootstrap_reps', 'block_length', 'chunk_size', 'mc_batch', 'memory_mb', 'n_jobs', 'feature_lookback', 'shortlist', 'fine_regions', 'parallel_min_work', 'losing_streak_threshold'):
            v = getattr(self, name)
            if not isinstance(v, (int, np.integer)) or v < (0 if name == 'seed' else 1):
                raise ValueError(f'{name} must be a positive integer')
        for name in ('risk_fraction', 'max_exposure', 'ruin_equity', 'partial_fraction', 'decay_fraction', 'min_fold_pass', 'max_conflict_rate', 'min_stress_survival', 'min_bootstrap_positive', 'max_mc_loss_probability', 'max_mc_drawdown', 'max_sequential_drawdown', 'min_temporal_positive_fraction', 'min_temporal_pf_positive_fraction', 'min_temporal_expected_r_positive_fraction'):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f'{name} must be in (0, 1]')
        for name in ('min_ess', 'min_pf', 'portfolio_gross_cap', 'breakeven_trigger_r', 'discontinuity_limit'):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'Invalid {name}')
        if not np.isfinite(self.cost_uncertainty) or self.cost_uncertainty < 0:
            raise ValueError('Invalid cost_uncertainty')
        if not 0 <= self.min_parameter_score <= 100 or not 0 <= self.min_regime_score <= 100 or not 0 <= self.min_temporal_robustness_score <= 100 or not 0 <= self.min_evidence_sufficiency_score <= 100:
            raise ValueError('Scores must be 0..100')
        if self.temporal_min_blocks > self.temporal_max_blocks:
            raise ValueError('temporal_min_blocks cannot exceed temporal_max_blocks')
        if self.prior_research_trials < 0 or int(self.prior_research_trials) != self.prior_research_trials:
            raise ValueError('Invalid prior_research_trials')
        if self.close_type not in ('auto','raw','adjusted'):
            raise ValueError('close_type must be auto, raw or adjusted')
        if self.price_type not in ('close','adjusted_close','settlement','continuous_futures'):
            raise ValueError('price_type must be close, adjusted_close, settlement or continuous_futures')
        if self.ohlc_adjustment not in ('raw','adjusted'):
            raise ValueError('ohlc_adjustment must be raw or adjusted')
        if self.price_type=='continuous_futures' and self.ohlc_adjustment!='adjusted':
            raise ValueError('continuous_futures requires consistently adjusted OHLC')
        if self.price_type=='adjusted_close' and self.close_type=='raw':
            raise ValueError('adjusted_close cannot use close_type=raw')
        if self.price_type=='close' and self.ohlc_adjustment!='raw':
            raise ValueError('price_type=close requires raw OHLC; declare adjusted_close for adjusted prices')
        if self.data_source not in ('csv','parquet','feather','google_sheet','provider','cache'):
            raise ValueError('data_source must be csv, parquet, feather, google_sheet, provider or cache')
        if self.asset_class.upper() not in ('EQUITY','INDEX','FX','FUTURES','COMMODITY','FIXED_INCOME','CRYPTO','OTHER','UNKNOWN'):
            raise ValueError('Invalid asset_class')
        if not isinstance(self.trading_calendar, str) or not self.trading_calendar.strip():
            raise ValueError('trading_calendar must be a non-empty string')
        if not isinstance(self.require_secondary_source, bool) or not isinstance(self.require_provenance, bool):
            raise ValueError('require_secondary_source and require_provenance must be boolean')
        if not isinstance(self.allow_test_execution, bool) or not isinstance(self.allow_final_oos, bool):
            raise ValueError('allow_test_execution and allow_final_oos must be boolean')
        if not isinstance(self.require_warmup_coverage, bool) or not isinstance(self.require_full_horizon_coverage, bool):
            raise ValueError('require_warmup_coverage and require_full_horizon_coverage must be boolean')
        for name in ('volatility_lookback','regime_lookback','coverage_safety_buffer_sessions'):
            if not isinstance(getattr(self,name),(int,np.integer)) or getattr(self,name)<0:
                raise ValueError(f'{name} must be a nonnegative integer')
        if self.ohlc_invalid_row_policy not in ('halt','quarantine'):
            raise ValueError('ohlc_invalid_row_policy must be halt or quarantine')
        if not 0 <= self.max_ohlc_quarantine_fraction <= 1:
            raise ValueError('max_ohlc_quarantine_fraction must be in [0, 1]')
        if not isinstance(self.max_ohlc_quarantine_run,(int,np.integer)) or self.max_ohlc_quarantine_run<1:
            raise ValueError('max_ohlc_quarantine_run must be a positive integer')
        if self.ohlc_validation_mode not in ('strict','warn','repair_rounding_only'):
            raise ValueError('ohlc_validation_mode must be strict, warn or repair_rounding_only')
        if not np.isfinite(self.ohlc_rounding_rtol) or self.ohlc_rounding_rtol<=0 or not np.isfinite(self.ohlc_rounding_atol) or self.ohlc_rounding_atol<=0:
            raise ValueError('OHLC rounding tolerances must be positive and finite')
        self.execution.validate()
        if self.execution.same_bar != 'stop_first':
            raise ValueError('Selection must use conservative stop_first; other modes are diagnostics')

    @classmethod
    def from_legacy(cls, cfg, overrides=None):
        hm = int(cfg.horizon_months)
        if hm < 1:
            raise ValueError('Control Panel horizon months must be positive')
        days = max(1, hm * 21)
        args = {k: str(getattr(cfg, k).date()) for k in ('train_start','train_end','validation_start','validation_end','test_start','test_end')}
        args.update(direction=cfg.direction, horizons=tuple(sorted(set([max(1, days//4), max(1, days//2), days]))),
                    data_source='google_sheet', require_provenance=True,
                    require_warmup_coverage=True, require_full_horizon_coverage=True,
                    regime_lookback=252,
                    instrument=getattr(cfg,'instrument','UNSPECIFIED'))
        overrides = dict(overrides or {})
        mode = overrides.get('mode', 'STANDARD')
        if mode == 'FAST':
            args.update(mc_sims=1000, bootstrap_reps=500, folds=2, shortlist=6, fine_regions=1)
        elif mode == 'DEEP':
            args.update(mc_sims=25000, bootstrap_reps=5000, folds=6, shortlist=20, fine_regions=5)
        args.update(overrides)
        if isinstance(args.get('execution'), dict):
            args['execution'] = ExecutionConfig(**args['execution'])
        out = cls(**args)
        out.validate()
        return out

    @classmethod
    def from_dict(cls, data):
        """Apply mode budgets only when they were not explicitly overridden."""
        args = dict(data)
        presets = {
            'FAST': {'mc_sims':1000, 'bootstrap_reps':500, 'folds':2, 'shortlist':6, 'fine_regions':1},
            'STANDARD': {'mc_sims':10000, 'bootstrap_reps':2000, 'folds':4, 'shortlist':12, 'fine_regions':3},
            'DEEP': {'mc_sims':25000, 'bootstrap_reps':5000, 'folds':6, 'shortlist':20, 'fine_regions':5},
        }
        for key, value in presets.get(args.get('mode', 'STANDARD'), {}).items():
            args.setdefault(key, value)
        if isinstance(args.get('execution'), dict):
            args['execution'] = ExecutionConfig(**args['execution'])
        out = cls(**args)
        out.validate()
        return out

@dataclass(frozen=True, order=True)
class Candidate:
    stop: float
    target: float
    horizon: int
    family: str = 'fixed'
    normalisation: str = 'percent'

    @property
    def key(self):
        return digest(asdict(self))[:16]

@dataclass
class Market:
    dates: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    vol: np.ndarray
    trend: np.ndarray
    fingerprint: str
    quality: dict

_OHLC_VIOLATION_TYPES = (
    'CLOSE_ABOVE_HIGH','CLOSE_BELOW_LOW','OPEN_ABOVE_HIGH','OPEN_BELOW_LOW',
    'HIGH_BELOW_LOW','NON_POSITIVE_PRICE','NON_FINITE_PRICE',
)

def _ohlc_statistics(diagnostics, raw):
    """Return JSON-safe, row-level statistics without changing any observations."""
    rows=len(raw); result={}
    def describe(part):
        absolute=part.difference.abs().to_numpy(float) if len(part) else np.array([],float)
        relative=part.relative_difference.abs().to_numpy(float) if len(part) else np.array([],float)
        return {'count':int(len(part)), 'percentage_of_all_rows':float(100*len(part)/rows) if rows else 0.,
            'mean_absolute_discrepancy':float(absolute.mean()) if len(absolute) else 0.,
            'median_absolute_discrepancy':float(np.median(absolute)) if len(absolute) else 0.,
            'maximum_absolute_discrepancy':float(absolute.max()) if len(absolute) else 0.,
            'mean_relative_discrepancy':float(relative.mean()) if len(relative) else 0.,
            'median_relative_discrepancy':float(np.median(relative)) if len(relative) else 0.,
            'maximum_relative_discrepancy':float(relative.max()) if len(relative) else 0.}
    observed=list(dict.fromkeys(_OHLC_VIOLATION_TYPES+tuple(diagnostics.get('violation_type',pd.Series(dtype=str)).tolist())))
    for label in observed:
        part=diagnostics[diagnostics.violation_type.eq(label)] if len(diagnostics) else diagnostics
        result[label]=describe(part)
    known=set(_OHLC_VIOLATION_TYPES)
    other=diagnostics[~diagnostics.violation_type.isin(known)] if len(diagnostics) else diagnostics
    result['OTHER']=describe(other)
    top_relative=(diagnostics.assign(_magnitude=diagnostics.relative_difference.abs())
         .sort_values(['_magnitude','source_row_number'],ascending=[False,True],kind='stable')
         .drop(columns='_magnitude').head(20)) if len(diagnostics) else diagnostics
    top_absolute=(diagnostics.assign(_magnitude=diagnostics.difference.abs())
         .sort_values(['_magnitude','source_row_number'],ascending=[False,True],kind='stable')
         .drop(columns='_magnitude').head(20)) if len(diagnostics) else diagnostics
    invalid_dates=pd.DatetimeIndex(pd.to_datetime(diagnostics.date.unique())) if len(diagnostics) else pd.DatetimeIndex([])
    dates=pd.to_datetime(raw.date)
    years=[]
    for year,total in dates.dt.year.value_counts().sort_index().items():
        invalid=int((invalid_dates.year==year).sum())
        years.append({'year':int(year),'total_rows':int(total),'ohlc_violations':invalid,
                      'violation_percentage':float(100*invalid/total)})
    months=[]
    if len(invalid_dates):
        period=dates.dt.to_period('M')
        bad_period=pd.Series(invalid_dates.to_period('M')).value_counts()
        for month,invalid in bad_period.sort_index().items():
            total=int(period.eq(month).sum())
            months.append({'month':str(month),'total_rows':total,'ohlc_violations':int(invalid),
                           'violation_percentage':float(100*invalid/total)})
    return result,top_relative.to_dict('records'),top_absolute.to_dict('records'),years,months

def _quarantine_impact(raw, active, invalid_mask, cfg):
    """Assess whether removing invalid rows would touch any enabled trade path."""
    order=np.argsort(raw.date.to_numpy(),kind='stable')
    chronological=raw.iloc[order].reset_index(drop=True)
    invalid=np.asarray(invalid_mask,bool)[order]
    maximum_horizon=max(int(x) for x in cfg.horizons)
    prefix=np.r_[0,np.cumsum(invalid.astype(int))]
    positions={pd.Timestamp(d):i for i,d in enumerate(chronological.date)}
    details=[]
    for row in active.itertuples(index=False):
        position=positions.get(pd.Timestamp(row.entry_date))
        if position is None:
            continue
        end=min(len(chronological),position+1+maximum_horizon)
        crossed=int(prefix[end]-prefix[position+1])
        details.append({'entry_id':str(row.entry_id),'split':str(row.split),'entry_date':str(pd.Timestamp(row.entry_date).date()),
                        'entry_row_invalid':bool(invalid[position]),'invalid_rows_in_max_horizon':crossed,
                        'max_horizon':maximum_horizon})
    by_split=[]
    detail_frame=pd.DataFrame(details)
    for split in ('TRAIN','VALIDATION','TEST'):
        part=detail_frame[detail_frame.split.eq(split)] if len(detail_frame) else detail_frame
        by_split.append({'split':split,'active_entries':int(len(part)),
                         'entries_on_invalid_rows':int(part.entry_row_invalid.sum()) if len(part) else 0,
                         'entries_whose_path_crosses_invalid_rows':int(part.invalid_rows_in_max_horizon.gt(0).sum()) if len(part) else 0,
                         'invalid_row_crossings':int(part.invalid_rows_in_max_horizon.sum()) if len(part) else 0})
    longest=run=0
    for value in invalid:
        run=run+1 if value else 0; longest=max(longest,run)
    kept=chronological.loc[~invalid,'date']
    gaps=kept.diff().dt.days
    fraction=float(invalid.mean()) if len(invalid) else 0.
    active_affected=bool(len(detail_frame) and (detail_frame.entry_row_invalid.any() or detail_frame.invalid_rows_in_max_horizon.gt(0).any()))
    reasons=[]
    if fraction>cfg.max_ohlc_quarantine_fraction:
        reasons.append(f'invalid fraction {fraction:.6%} exceeds {cfg.max_ohlc_quarantine_fraction:.6%}')
    if longest>cfg.max_ohlc_quarantine_run:
        reasons.append(f'longest consecutive run {longest} exceeds {cfg.max_ohlc_quarantine_run}')
    if active_affected:
        reasons.append('one or more enabled entry paths would lose an observation')
    return {'candidate_rows':int(invalid.sum()),'candidate_fraction':fraction,'longest_consecutive_run':int(longest),
            'largest_resulting_calendar_gap_days':int(gaps.max()) if len(gaps) and pd.notna(gaps.max()) else 0,
            'resulting_gaps_over_seven_days':int(gaps.gt(7).sum()) if len(gaps) else 0,
            'maximum_horizon_assessed':maximum_horizon,'by_split':by_split,'affected_entries':details,
            'safe_to_quarantine':not reasons,'rejection_reasons':reasons}

def validate_data(raw, entries, cfg):
    required = {'date','open','high','low','close'}
    if not required <= set(raw):
        raw,_=standardise_raw_data(raw,cfg.close_type,None,cfg.price_type,cfg.ohlc_adjustment)
    er = {'entry_id','entry_date','entry_price','direction','split','valid','source'}
    if not er <= set(entries):
        raise ValueError(f'Missing entry columns: {er-set(entries)}')
    if raw.empty or entries.empty:
        raise ValueError('OHLC and entries must not be empty')
    source_attrs=dict(raw.attrs)
    mapping=dict(raw.attrs.get('raw_data_mapping',{'source_date':'source_date' if 'source_date' in raw else 'date',
        'date':'date','close':'close','open':'open','high':'high','low':'low','close_type':'raw',
        'ohlc_basis':cfg.ohlc_adjustment,'price_type':cfg.price_type,'ohlc_adjustment':cfg.ohlc_adjustment}))
    mapping.setdefault('source_date','source_date' if 'source_date' in raw else mapping.get('date','date'))
    mapping.setdefault('price_type',cfg.price_type); mapping.setdefault('ohlc_adjustment',cfg.ohlc_adjustment)
    if cfg.close_type!='auto' and mapping.get('close_type')!=cfg.close_type:
        raise ValueError(f'Configured close_type={cfg.close_type}, but resolved close is {mapping.get("close_type")}')
    if mapping.get('price_type')!=cfg.price_type or mapping.get('ohlc_adjustment')!=cfg.ohlc_adjustment:
        raise ValueError('Configured price_type/ohlc_adjustment do not match the resolved Raw Data mapping')
    raw = raw.copy()
    entries = entries.copy()
    raw['date'] = pd.to_datetime(raw['date'], errors='coerce')
    entries['entry_date'] = pd.to_datetime(entries['entry_date'], errors='raise')
    for col in ('open','high','low','close'):
        raw[col] = pd.to_numeric(raw[col], errors='coerce')
    if raw['date'].isna().any() or entries['entry_date'].isna().any():
        raise ValueError('Missing dates')
    if raw['date'].dt.tz is not None or entries['entry_date'].dt.tz is not None:
        raise ValueError('Use timezone-naive exchange session dates')
    if (raw['date'] != raw['date'].dt.normalize()).any() or (entries['entry_date'] != entries['entry_date'].dt.normalize()).any():
        raise ValueError('Expected daily session dates, not intraday timestamps')
    if raw['date'].duplicated().any():
        raise ValueError('Duplicate OHLC dates; refusing ambiguous paths')
    if 'source_row_number' not in raw:
        raw['source_row_number']=np.arange(2,len(raw)+2)
    ep = parse_numeric_series(entries['entry_price'])
    if ep.isna().any():
        raise ValueError('Entry prices contain values that are not plain or correctly grouped numeric values')
    if not np.isfinite(ep).all() or (ep <= 0).any():
        raise ValueError('Entry prices must be positive and finite')
    entries['entry_price'] = ep
    for key in ('direction','split'):
        entries[key] = entries[key].astype(str).str.strip().str.upper()
    entries['valid'] = entries['valid'].astype(str).str.upper().isin(['TRUE','YES','1'])
    enabled=entries['valid']
    if (~entries.loc[enabled,'direction'].isin(['LONG','SHORT'])).any() or (~entries.loc[enabled,'split'].isin(['TRAIN','VALIDATION','TEST'])).any():
        raise ValueError('Invalid entry direction or split')
    active = entries[entries['valid'] & entries['direction'].eq(cfg.direction)]
    if active['entry_id'].duplicated().any() or active.duplicated(['entry_date','direction','entry_price']).any():
        raise ValueError('Duplicate active entry IDs or signals')
    missing = active.loc[~active['entry_date'].isin(raw['date']), 'entry_date']
    if len(missing):
        raise ValueError(f'Entries without OHLC data: {missing.astype(str).tolist()[:10]}')
    for split in ('TRAIN','VALIDATION','TEST'):
        a,b = (pd.Timestamp(getattr(cfg, f'{split.lower()}_{k}')) for k in ('start','end'))
        sel = active[active['split'].eq(split)]
        if ((sel.entry_date < a) | (sel.entry_date > b)).any():
            raise ValueError(f'{split} entry label conflicts with configured dates')
    o,h,l,c = (raw[k].to_numpy(float) for k in ('open','high','low','close'))
    matrix=np.column_stack([o,h,l,c]); finite=np.isfinite(matrix).all(axis=1)
    source=raw['source_row_number'].to_numpy()
    source_dates=(raw['source_date'] if 'source_date' in raw else raw['date']).astype(str).to_numpy()
    diagnostics=[]
    def record(mask,label,difference,reference,classification):
        idx=np.flatnonzero(mask)
        if not len(idx): return
        ref=np.maximum(np.abs(np.asarray(reference,float)[idx]),cfg.ohlc_rounding_atol)
        diagnostics.append(pd.DataFrame({'source_date':source_dates[idx],
            'date':raw['date'].iloc[idx].astype(str).to_numpy(),
            'open':o[idx],'high':h[idx],'low':l[idx],'close':c[idx],
            'source_row_number':source[idx],'violation_type':label,
            'classification':classification,'difference':np.asarray(difference,float)[idx],
            'relative_difference':np.asarray(difference,float)[idx]/ref}))
    zeros=np.zeros(len(raw)); nonfinite=~finite
    record(nonfinite,'NON_FINITE_PRICE',np.where(nonfinite,1.,0.),np.ones(len(raw)),'MATERIAL_OHLC_ERROR')
    nonpositive=finite&(matrix<=0).any(axis=1)
    record(nonpositive,'NON_POSITIVE_PRICE',np.maximum(0,-np.nanmin(matrix,axis=1)),np.nanmax(np.abs(matrix),axis=1),'MATERIAL_OHLC_ERROR')
    high_below=finite&(h<l)
    record(high_below,'HIGH_BELOW_LOW',l-h,np.maximum(np.abs(h),np.abs(l)),'MATERIAL_OHLC_ERROR')
    tol_oh=np.maximum(np.maximum(np.abs(o),np.abs(h))*cfg.ohlc_rounding_rtol,cfg.ohlc_rounding_atol)
    tol_ol=np.maximum(np.maximum(np.abs(o),np.abs(l))*cfg.ohlc_rounding_rtol,cfg.ohlc_rounding_atol)
    tol_ch=np.maximum(np.maximum(np.abs(c),np.abs(h))*cfg.ohlc_rounding_rtol,cfg.ohlc_rounding_atol)
    tol_cl=np.maximum(np.maximum(np.abs(c),np.abs(l))*cfg.ohlc_rounding_rtol,cfg.ohlc_rounding_atol)
    checks=[('OPEN_ABOVE_HIGH',o-h,h,tol_oh),('OPEN_BELOW_LOW',l-o,l,tol_ol)]
    price_type=mapping.get('price_type',cfg.price_type)
    if price_type=='close' and mapping.get('close_type')=='adjusted':
        price_type='adjusted_close'  # backward-compatible interpretation of legacy close_type
    adjustment=mapping.get('ohlc_adjustment',cfg.ohlc_adjustment)
    validate_close=((price_type=='close' and adjustment=='raw') or
                    (price_type in ('adjusted_close','continuous_futures') and adjustment=='adjusted'))
    noncontained_close_class=('SETTLEMENT_RANGE_MISMATCH' if price_type=='settlement' else 'ADJUSTED_CLOSE_MISMATCH')
    close_checks=[('CLOSE_ABOVE_HIGH',c-h,h,tol_ch),('CLOSE_BELOW_LOW',l-c,l,tol_cl)]
    material_containment=np.zeros(len(raw),bool); rounding=np.zeros(len(raw),bool)
    for label,diff,ref,tol in checks:
        microscopic=finite&(diff>0)&(diff<=tol); material=finite&(diff>tol)
        record(microscopic,label,diff,ref,'ROUNDING_ONLY'); record(material,label,diff,ref,'MATERIAL_OHLC_ERROR')
        rounding|=microscopic; material_containment|=material
    for label,diff,ref,tol in close_checks:
        outside=finite&(diff>0)
        if validate_close:
            microscopic=outside&(diff<=tol); material=outside&(diff>tol)
            record(microscopic,label,diff,ref,'ROUNDING_ONLY'); record(material,label,diff,ref,'MATERIAL_OHLC_ERROR')
            rounding|=microscopic; material_containment|=material
        else:
            record(outside,label,diff,ref,noncontained_close_class)
    diagnostic_df=pd.concat(diagnostics,ignore_index=True) if diagnostics else pd.DataFrame(columns=['source_date','date','open','high','low','close','source_row_number','violation_type','classification','difference','relative_difference'])
    if cfg.ohlc_validation_mode=='repair_rounding_only' and rounding.any():
        raw.loc[finite&(o>h)&((o-h)<=tol_oh),'open']=h[finite&(o>h)&((o-h)<=tol_oh)]
        raw.loc[finite&(o<l)&((l-o)<=tol_ol),'open']=l[finite&(o<l)&((l-o)<=tol_ol)]
        if validate_close:
            raw.loc[finite&(c>h)&((c-h)<=tol_ch),'close']=h[finite&(c>h)&((c-h)<=tol_ch)]
            raw.loc[finite&(c<l)&((l-c)<=tol_cl),'close']=l[finite&(c<l)&((l-c)<=tol_cl)]
    hard_error=nonfinite|nonpositive|high_below
    material_mask=hard_error|material_containment
    material_rows=int(np.unique(diagnostic_df.loc[diagnostic_df.classification=='MATERIAL_OHLC_ERROR','source_row_number']).size)
    largest_close_high=float(diagnostic_df.loc[diagnostic_df.violation_type=='CLOSE_ABOVE_HIGH','relative_difference'].max()) if (diagnostic_df.violation_type=='CLOSE_ABOVE_HIGH').any() else 0.
    statistics,top20,top20_absolute,yearly,monthly=_ohlc_statistics(diagnostic_df,raw)
    quarantine_impact=_quarantine_impact(raw,active,material_mask,cfg)
    numeric_audit=source_attrs.get('numeric_parsing_audit',{})
    parse_failures=sum(int(v.get('parse_failures',0)) for v in numeric_audit.values())
    if parse_failures:
        root_cause={'code':'B','classification':'NUMERIC_PARSING_BUG','evidence':f'{parse_failures} numeric parse failures were recorded'}
    elif material_rows:
        root_cause={'code':'F','classification':'GENUINELY_BAD_SOURCE_DATA','evidence':'Material inconsistencies remain after direct header mapping and numeric conversion'}
    elif (diagnostic_df.classification=='ADJUSTED_CLOSE_MISMATCH').any():
        root_cause={'code':'D','classification':'ADJUSTED_UNADJUSTED_MIX','evidence':'Declared adjusted close is being compared with raw intraday OHLC only for diagnostics'}
    elif (diagnostic_df.classification=='SETTLEMENT_RANGE_MISMATCH').any():
        root_cause={'code':'E','classification':'SETTLEMENT_VS_TRADED_OHLC','evidence':'Declared settlement is not required to lie within traded intraday OHLC'}
    else:
        root_cause={'code':None,'classification':'NO_MATERIAL_OHLC_ERROR','evidence':'No material semantic violation found'}
    ohlc_summary={'rows':len(raw),'diagnostic_records':len(diagnostic_df),'material_rows':material_rows,
                  'rounding_rows':int(np.unique(diagnostic_df.loc[diagnostic_df.classification=='ROUNDING_ONLY','source_row_number']).size),
                  'adjusted_close_mismatch_rows':int(np.unique(diagnostic_df.loc[diagnostic_df.classification=='ADJUSTED_CLOSE_MISMATCH','source_row_number']).size),
                  'settlement_range_mismatch_rows':int(np.unique(diagnostic_df.loc[diagnostic_df.classification=='SETTLEMENT_RANGE_MISMATCH','source_row_number']).size),
                  'largest_close_high_relative_difference':largest_close_high,'validation_mode':cfg.ohlc_validation_mode,
                  'invalid_row_policy':cfg.ohlc_invalid_row_policy,'mapping':mapping,'numeric_parsing_audit':numeric_audit,
                  'statistics_by_violation_type':statistics,'top_20_largest':top20,
                  'top_20_largest_absolute_discrepancy':top20_absolute,'yearly_clustering':yearly,
                  'monthly_clustering':monthly,'root_cause':root_cause,'quarantine_impact':quarantine_impact,
                  'quarantine_action':'NOT_REQUESTED'}
    if material_rows:
        LOG.warning('OHLC integrity diagnostics (%d records; 20 largest shown):\n%s',
                    len(diagnostic_df),pd.DataFrame(top20).to_string(index=False))
    elif len(diagnostic_df):
        LOG.info('OHLC validation recorded %d nonfatal diagnostic records (%d rounding rows, %d adjusted-close mismatch rows)',
                 len(diagnostic_df),ohlc_summary['rounding_rows'],ohlc_summary['adjusted_close_mismatch_rows'])
    strict_material=material_mask if cfg.ohlc_validation_mode!='warn' else np.zeros(len(raw),bool)
    fatal=hard_error|(strict_material if cfg.ohlc_invalid_row_policy=='halt' else False)
    if cfg.ohlc_invalid_row_policy=='quarantine' and strict_material.any():
        if hard_error.any() or not quarantine_impact['safe_to_quarantine']:
            ohlc_summary['quarantine_action']='REJECTED'
            if hard_error.any():
                quarantine_impact['rejection_reasons'].append('non-finite, non-positive, or high-below-low errors require source correction')
            fatal=strict_material
        else:
            quarantined=raw.loc[strict_material,['source_row_number','date']].copy()
            ohlc_summary['quarantine_action']='APPLIED'
            ohlc_summary['quarantined_rows']=quarantined.assign(date=quarantined.date.astype(str)).to_dict('records')
            raw=raw.loc[~strict_material].copy()
            material_mask=np.zeros(len(raw),bool)
            fatal=np.zeros(len(raw),bool)
    if fatal.any():
        reason='; '.join(quarantine_impact['rejection_reasons']) if ohlc_summary['quarantine_action']=='REJECTED' else 'the configured policy is halt'
        message=(f'OHLC integrity check failed: {int(fatal.sum())} / {ohlc_summary["rows"]} rows violate declared price/OHLC semantics; {reason}. '
                 f'Largest close/high discrepancy: {largest_close_high:.6%}. See diagnostics for every violation.')
        LOG.error(message)
        raise OHLCValidationError(message,diagnostic_df,ohlc_summary)
    unsorted = not raw['date'].is_monotonic_increasing
    raw = raw.sort_values('date', kind='stable').reset_index(drop=True)
    raw_start,raw_end=raw.date.min(),raw.date.max()
    required_start,required_test_start=pd.Timestamp(cfg.train_start),pd.Timestamp(cfg.test_start)
    if raw_start > required_start or raw_end < required_test_start:
        missing=[]
        if raw_start > required_start:
            missing.append(f'history from configured TRAIN start {required_start.date()} through {raw_start.date()}')
        if raw_end < required_test_start:
            missing.append(f'data through configured TEST start {required_test_start.date()}')
        raise ValueError(f'Insufficient OHLC coverage: Raw Data spans {raw_start.date()} to {raw_end.date()}; missing {" and ".join(missing)}')
    close = raw.close.to_numpy(float)
    jump = np.r_[False, np.abs(close[1:]/close[:-1]-1) > cfg.discontinuity_limit]
    if jump.any() and not cfg.allow_discontinuities:
        raise ValueError('Large unexplained discontinuity / possible split adjustment; supply consistently adjusted data or explicitly acknowledge')
    missing_sessions = []
    if cfg.expected_sessions:
        expected = pd.DatetimeIndex(pd.to_datetime(list(cfg.expected_sessions)))
        expected = expected[(expected >= raw.date.min()) & (expected <= raw.date.max())]
        missing_sessions = expected.difference(pd.DatetimeIndex(raw.date)).strftime('%Y-%m-%d').tolist()
        if missing_sessions:
            raise ValueError(f'Missing required exchange sessions: {missing_sessions[:10]}')
    gaps = raw.date.diff().dt.days
    stale = raw[['open','high','low','close']].eq(raw[['open','high','low','close']].shift()).all(axis=1)
    safely_quarantined=ohlc_summary['quarantine_action']=='APPLIED'
    quality = {'status':'FAIL' if material_rows and not safely_quarantined else ('REVIEW' if safely_quarantined or jump.any() or stale.any() or len(diagnostic_df) else 'PASS'),
               'raw_data_mapping':mapping,'close_type':mapping.get('close_type'),'ohlc_basis':mapping.get('ohlc_basis'),
               'price_type':price_type,'ohlc_adjustment':adjustment,'numeric_parsing_audit':numeric_audit,
               'market_data_provenance':source_attrs.get('market_data_provenance',{}),
               'loaded_first_10':source_attrs.get('loaded_first_10',pd.DataFrame()).to_dict('records') if isinstance(source_attrs.get('loaded_first_10'),pd.DataFrame) else source_attrs.get('loaded_first_10',[]),
               'ohlc_validation':ohlc_summary,'ohlc_diagnostics':diagnostic_df.to_dict('records'),
               'unsorted_bars_corrected':unsorted, 'large_discontinuity_dates':raw.loc[jump,'date'].astype(str).tolist(),
               'stale_bar_dates':raw.loc[stale,'date'].astype(str).tolist(),
               'calendar_gaps_over_four_days':raw.loc[gaps>4,'date'].astype(str).tolist(),
               'missing_sessions':missing_sessions,
               'calendar_check':'exchange sessions supplied' if cfg.expected_sessions else 'Calendar gaps diagnostic only; exchange holidays unknown',
               'entry_price_assumption':'Supplied close-time fill on entry date; execution starts next bar',
               'corporate_actions':'Consistent split/dividend adjustment is a caller responsibility; discontinuity screening is not verification'}
    return raw, entries.sort_values(['entry_date','entry_id'],kind='stable').reset_index(drop=True), quality

def make_market(raw, cfg, quality=None):
    close = raw.close.to_numpy(float)
    # DataFrame-level provenance attrs include nested audit DataFrames.  Pandas
    # propagates those attrs to column Series, and newer pandas versions can
    # fail when concat compares the nested DataFrame values.  Rebuild the
    # numeric inputs without attrs at this boundary.
    high = pd.Series(raw.high.to_numpy(float))
    low = pd.Series(raw.low.to_numpy(float))
    previous_close = pd.Series(close).shift()
    tr = pd.concat([high-low, (high-previous_close).abs(), (low-previous_close).abs()], axis=1).max(axis=1)
    # Entry is at session close, so all features here are available at that close.
    atr = tr.rolling(cfg.feature_lookback, min_periods=cfg.feature_lookback).mean().to_numpy()/close
    vol = raw.close.pct_change().rolling(cfg.feature_lookback, min_periods=cfg.feature_lookback).std().to_numpy()
    trend = raw.close.pct_change(cfg.feature_lookback).to_numpy()
    return Market(raw.date.to_numpy(dtype='datetime64[ns]'), *(raw[k].to_numpy(float) for k in ('open','high','low','close')), atr, vol, trend, frame_hash(raw), quality or {})

@dataclass
class PathCache:
    ids: np.ndarray
    entry_dates: np.ndarray
    entry_prices: np.ndarray
    entry_indices: np.ndarray
    lengths: np.ndarray
    fav: np.ndarray
    adv: np.ndarray
    close: np.ndarray  # directional return, not price
    opening: np.ndarray
    dates: np.ndarray
    days: np.ndarray
    atr: np.ndarray
    vol: np.ndarray
    trend: np.ndarray
    fingerprint: str
    exclusions: list[dict]
    embargo_dates: list[str]
    direction: str

    def __len__(self):
        return len(self.ids)

def embargo_start(market, start, end, cfg):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    days = market.dates[(market.dates >= start.to_datetime64()) & (market.dates <= end.to_datetime64())]
    if cfg.embargo_unit == 'calendar_days':
        cutoff = start + pd.Timedelta(days=cfg.embargo)
        excluded = days[days < cutoff.to_datetime64()]
    else:
        n = math.ceil(len(days)*cfg.embargo) if cfg.embargo_unit == 'percentage' else int(cfg.embargo)
        excluded = days[:n]
        cutoff = pd.Timestamp(days[n]) if n < len(days) else end + pd.Timedelta(days=1)
    return cutoff, [str(pd.Timestamp(d).date()) for d in excluded]

def build_paths(market, entries, cfg, start, end, split=None, embargo=True, delay=0):
    """Purge by maximum proposed horizon, common universe for every candidate.

    The market object can contain later samples; numerical paths are copied only
    after boundary checks. No candidate can access observations beyond its sample.
    """
    start,end = pd.Timestamp(start),pd.Timestamp(end)
    cutoff, embargo_dates = embargo_start(market,start,end,cfg) if embargo else (start,[])
    e = entries[entries.valid & entries.direction.eq(cfg.direction) & entries.entry_date.between(start,end)]
    if split:
        e = e[e.split.eq(split)]
    e = e.sort_values(['entry_date','entry_id'],kind='stable')
    exclusions=[]; included=[]
    maxh=max(cfg.horizons)
    for r in e.itertuples(index=False):
        idx=int(np.searchsorted(market.dates, r.entry_date.to_datetime64()))
        if idx >= len(market.dates) or market.dates[idx] != r.entry_date.to_datetime64():
            raise ValueError('Entry without market data')
        idx += delay
        reason='embargo' if r.entry_date < cutoff else None
        if idx >= len(market.dates):
            reason='incomplete delayed entry'
            last=len(market.dates)
        elif cfg.calendar_months is not None:
            he=pd.Timestamp(market.dates[idx])+pd.DateOffset(months=cfg.calendar_months)
            last=int(np.searchsorted(market.dates,he.to_datetime64(),side='right'))-1
            if he>end or he>pd.Timestamp(market.dates[-1]):
                reason='purged calendar horizon / incomplete coverage'
        else:
            last=idx+maxh
        if reason is None and (last >= len(market.dates) or last <= idx or market.dates[last] > end.to_datetime64()):
            reason='purged maximum horizon / incomplete coverage'
        if reason:
            exclusions.append({'entry_id':str(r.entry_id),'entry_date':str(r.entry_date.date()),'reason':reason})
            continue
        scales=[]
        if 'atr' in cfg.normalisations: scales.append(market.atr[idx])
        if 'volatility' in cfg.normalisations: scales.append(market.vol[idx])
        if scales and (not np.isfinite(scales).all() or min(scales)<=0):
            exclusions.append({'entry_id':str(r.entry_id),'entry_date':str(r.entry_date.date()),'reason':'requested volatility normalisation warmup / zero scale'})
            continue
        included.append((r,idx,last,market.close[idx] if delay else r.entry_price))
    n=len(included); h=max((last-idx for _,idx,last,_ in included),default=maxh)
    if n*h*8*7 > cfg.memory_mb*1024**2:
        raise ValueError('Path cache exceeds memory_mb; reduce horizon/sample or increase budget explicitly')
    fav=np.full((n,h),np.nan); adv=fav.copy(); cl=fav.copy(); op=fav.copy()
    dt=np.full((n,h),np.datetime64('NaT'),dtype='datetime64[ns]'); days=np.zeros((n,h),dtype=np.int32)
    ids=[]; ed=[]; prices=[]; indices=[]; lengths=[]
    sign=1 if cfg.direction=='LONG' else -1
    for k,(r,i,j,p) in enumerate(included):
        sl=slice(i+1,j+1); length=j-i
        fav[k,:length]=sign*((market.high if sign==1 else market.low)[sl]/p-1)
        adv[k,:length]=sign*((market.low if sign==1 else market.high)[sl]/p-1)
        cl[k,:length]=sign*(market.close[sl]/p-1)
        op[k,:length]=sign*(market.open[sl]/p-1)
        dt[k,:length]=market.dates[sl]
        days[k,:length]=(market.dates[sl]-market.dates[i]).astype('timedelta64[D]').astype(int)
        ids.append(str(r.entry_id)); ed.append(market.dates[i]); prices.append(p); indices.append(i); lengths.append(length)
    ii=np.array(indices,dtype=int)
    fp=digest({'market':market.fingerprint,'entries':frame_hash(e),'config':asdict(cfg),'start':str(start),'end':str(end),'delay':delay})
    return PathCache(np.array(ids),np.array(ed,dtype='datetime64[ns]'),np.array(prices),ii,np.array(lengths,dtype=int),fav,adv,cl,op,dt,days,market.atr[ii],market.vol[ii],market.trend[ii],fp,exclusions,embargo_dates,cfg.direction)

def build_paths_cached(cache_dir,market,entries,cfg,start,end,split=None,embargo=True,delay=0):
    """Versioned, content-addressed path cache with atomic replacement.

    The key includes all inputs and configuration. Corrupt/incompatible entries
    are ignored and rebuilt; changing code version invalidates every old cache.
    """
    cache_dir=Path(cache_dir); cache_dir.mkdir(parents=True,exist_ok=True)
    key=digest({'version':VERSION,'market':market.fingerprint,'entries':frame_hash(entries),
                'config':asdict(cfg),'start':str(start),'end':str(end),'split':split,
                'embargo':embargo,'delay':delay})
    path=cache_dir/f'paths-{key}.npz'
    if path.exists():
        try:
            with np.load(path,allow_pickle=False) as z:
                meta=json.loads(str(z['metadata'].item()))
                if meta['key']!=key or meta['version']!=VERSION: raise ValueError('stale cache metadata')
                out=PathCache(*(z[name] for name in ('ids','entry_dates','entry_prices','entry_indices','lengths','fav','adv','close','opening','dates','days','atr','vol','trend')),
                              meta['fingerprint'],meta['exclusions'],meta['embargo_dates'],meta['direction'])
                out.cache_hit=True; out.cache_file=str(path)
                return out
        except Exception:
            pass
    out=build_paths(market,entries,cfg,start,end,split,embargo,delay)
    meta={'key':key,'version':VERSION,'fingerprint':out.fingerprint,'exclusions':out.exclusions,
          'embargo_dates':out.embargo_dates,'direction':out.direction}
    temporary=path.with_suffix('.npz.tmp')
    with temporary.open('wb') as handle:
        np.savez_compressed(handle,metadata=np.array(json.dumps(meta)),ids=out.ids,entry_dates=out.entry_dates,
            entry_prices=out.entry_prices,entry_indices=out.entry_indices,lengths=out.lengths,fav=out.fav,adv=out.adv,
            close=out.close,opening=out.opening,dates=out.dates,days=out.days,atr=out.atr,vol=out.vol,trend=out.trend)
    temporary.replace(path)
    out.cache_hit=False; out.cache_file=str(path)
    return out

@dataclass
class Evaluation:
    candidate: Candidate
    gross: np.ndarray
    net: np.ndarray
    hold_days: np.ndarray
    exit_index: np.ndarray
    outcome: np.ndarray  # 0 timeout, 1 stop, 2 target, 3 time decay, 4 partial remainder
    conflict: np.ndarray
    stop_distance: np.ndarray
    cost: np.ndarray
    partial_index: np.ndarray

def distances(cache,candidate):
    scale=np.ones(len(cache)) if candidate.normalisation=='percent' else getattr(cache,'atr' if candidate.normalisation=='atr' else 'vol')
    return candidate.stop*scale,candidate.target*scale

def first_hits(paths,levels,below,cfg):
    """Unique-barrier crossing tables, bounded temporaries; reused over horizons."""
    n,h=paths.shape
    out=np.full((len(levels),n),h,dtype=np.int32)
    if not n:
        return out
    # levels has shape unique levels × trades, avoiding candidate × trade × bar RAM.
    trade_chunk=max(1,min(n,cfg.memory_mb*1024**2//max(1,h*cfg.chunk_size*4)))
    for a in range(0,len(levels),cfg.chunk_size):
        for b in range(0,n,trade_chunk):
            p=paths[None,b:b+trade_chunk,:]
            lv=levels[a:a+cfg.chunk_size,b:b+trade_chunk,None]
            hits=(p<=-lv+1e-12) if below else (p>=lv-1e-12)
            out[a:a+cfg.chunk_size,b:b+trade_chunk]=np.where(hits.any(axis=2),hits.argmax(axis=2),h)
    return out

def evaluate_candidates(cache,candidates,cfg,execution=None):
    ex=execution or cfg.execution
    ex.validate()
    if len(cache)==0:
        return []
    results={}; rng=np.random.default_rng(cfg.seed)
    for norm in sorted({c.normalisation for c in candidates}):
        group=[c for c in candidates if c.normalisation==norm and c.family in ('fixed','horizon')]
        if not group:
            continue
        scale=np.ones(len(cache)) if norm=='percent' else getattr(cache,'atr' if norm=='atr' else 'vol')
        stops=sorted({c.stop for c in group}); targets=sorted({c.target for c in group})
        st=np.array(stops)[:,None]*scale; tg=np.array(targets)[:,None]*scale
        if not np.isfinite(st).all() or (st<=0).any() or (st>=1).any() or not np.isfinite(tg).all() or (tg<=0).any() or (cache.direction=='SHORT' and (tg>=1).any()):
            raise ValueError('Volatility-scaled barriers invalid; revise multiplier grid')
        si=first_hits(cache.adv,st,True,cfg); ti=first_hits(cache.fav,tg,False,cfg)
        sr={x:i for i,x in enumerate(stops)}; tr={x:i for i,x in enumerate(targets)}
        rows=np.arange(len(cache)); maxbars=cache.fav.shape[1]
        for c in group:
            limit=cache.lengths if cfg.calendar_months is not None else np.minimum(cache.lengths,c.horizon)
            sidx=si[sr[c.stop]].copy(); tidx=ti[tr[c.target]].copy()
            sidx=np.where(sidx<limit,sidx,maxbars)
            tidx=np.where((tidx<limit)&(c.family!='horizon'),tidx,maxbars)
            conflict=(sidx==tidx)&(sidx<maxbars)
            ix=np.minimum(np.minimum(sidx,tidx),limit-1)
            sd=st[sr[c.stop]]; target=tg[tr[c.target]]
            opening=cache.opening[rows,ix]
            # Opening print has known priority before intrabar extremes.
            gap_stop=(opening<=-sd+1e-12)&(sidx<maxbars)&(ex.mode!='threshold_fill')
            gap_target=(opening>=target-1e-12)&(tidx<maxbars)&(ex.mode!='threshold_fill')
            unresolved=conflict&~gap_stop&~gap_target
            choose_target=(tidx<sidx)|gap_target
            if ex.same_bar=='target_first': choose_target |= unresolved
            elif ex.same_bar=='random': choose_target |= unresolved & (rng.random(len(cache))<.5)
            stop_hit=(sidx<maxbars)&~choose_target
            target_hit=(tidx<maxbars)&choose_target
            gross=cache.close[rows,ix].copy()
            fill=-sd
            if ex.mode!='threshold_fill': fill=np.minimum(fill,opening)
            if ex.mode=='pessimistic_ohlc': fill=np.minimum(fill,cache.adv[rows,ix])
            gross=np.where(stop_hit,fill,np.where(target_hit,target,gross))
            outcome=np.where(stop_hit,1,np.where(target_hit,2,0)).astype(np.int8)
            hold=cache.days[rows,ix]; costs=ex.costs(hold,cache.direction=='SHORT')
            results[c]=Evaluation(c,gross,gross-costs,hold,ix,outcome,unresolved,sd,costs,np.full(len(cache),-1,dtype=int))
    for c in candidates:
        if c not in results:
            results[c]=evaluate_dynamic(cache,c,cfg,ex)
    return [results[c] for c in candidates]

def evaluate_dynamic(cache,c,cfg,ex):
    """Stateful exits vectorised across trades; trailing updates apply next bar."""
    if c.family not in FAMILIES:
        raise ValueError('Invalid exit family')
    n=len(cache); sd,tg=distances(cache,c)
    if (sd<=0).any() or (sd>=1).any() or not np.isfinite(sd).all() or not np.isfinite(tg).all() or (cache.direction=='SHORT' and (tg>=1).any()):
        raise ValueError('Invalid scaled barrier')
    limit=cache.lengths if cfg.calendar_months is not None else np.minimum(cache.lengths,c.horizon)
    ix=limit-1; gross=np.zeros(n); stop=-sd.copy(); best=np.zeros(n)
    active=np.ones(n,dtype=bool); partial=np.zeros(n,dtype=bool); pi=np.full(n,-1,dtype=int)
    out=np.zeros(n,dtype=np.int8); conflict=np.zeros(n,dtype=bool)
    rng=np.random.default_rng(cfg.seed)
    for j in range(int(limit.max())):
        s=cache.adv[:,j]<=stop+1e-12
        t=(cache.fav[:,j]>=tg-1e-12)&~partial
        gap_s=(cache.opening[:,j]<=stop+1e-12)&(ex.mode!='threshold_fill')
        gap_t=(cache.opening[:,j]>=tg-1e-12)&(ex.mode!='threshold_fill')&~partial
        both=active&s&t&~gap_s&~gap_t
        conflict |= both
        target_first=gap_t | (both & ((ex.same_bar=='target_first') | ((ex.same_bar=='random') & (rng.random(n)<.5))))
        stop_hit=active&s&~target_first
        target_hit=active&t&(~s|target_first)
        remaining=np.where(partial,1-cfg.partial_fraction,1.)
        fill=stop.copy()
        if ex.mode!='threshold_fill': fill=np.minimum(fill,cache.opening[:,j])
        if ex.mode=='pessimistic_ohlc': fill=np.minimum(fill,cache.adv[:,j])
        gross[stop_hit]+=remaining[stop_hit]*fill[stop_hit]
        ix[stop_hit]=j; out[stop_hit]=1; active[stop_hit]=False
        if c.family=='partial_trailing':
            gross[target_hit]+=cfg.partial_fraction*tg[target_hit]
            partial[target_hit]=True; pi[target_hit]=j
            # A target followed by an unknown adverse move is conservatively
            # stopped at the existing stop on that bar; trailing level updates later.
            remainder_stop=target_hit&s
            gross[remainder_stop]+=(1-cfg.partial_fraction)*fill[remainder_stop]
            ix[remainder_stop]=j; out[remainder_stop]=4; active[remainder_stop]=False
        else:
            gross[target_hit]=tg[target_hit]; ix[target_hit]=j; out[target_hit]=2; active[target_hit]=False
        timeout=active&(j==limit-1)
        decay=active&(c.family=='time_decay')&(j+1>=np.ceil(limit*cfg.decay_fraction))&(cache.close[:,j]<=0)
        done=timeout|decay
        remaining=np.where(partial,1-cfg.partial_fraction,1.)
        gross[done]+=remaining[done]*cache.close[done,j]
        ix[done]=j; out[done]=np.where(decay[done],3,np.where(partial[done],4,0)); active[done]=False
        best=np.fmax(best,cache.fav[:,j])
        if c.family=='trailing': stop=np.maximum(stop,best-sd)
        elif c.family=='partial_trailing': stop=np.where(partial,np.maximum(stop,best-sd),stop)
        elif c.family=='breakeven': stop=np.where(best>=cfg.breakeven_trigger_r*sd,np.maximum(stop,0),stop)
    hold=cache.days[np.arange(n),ix]
    # Financing/borrow accrue on remaining notional after partial realization.
    charged_days=hold.astype(float)
    has_partial=pi>=0
    charged_days[has_partial]-=cfg.partial_fraction*(hold[has_partial]-cache.days[np.arange(n)[has_partial],pi[has_partial]])
    cost=ex.costs(charged_days,cache.direction=='SHORT')
    return Evaluation(c,gross,gross-cost,hold,ix,out,conflict,sd,cost,pi)

def equity_paths(paths):
    a=np.asarray(paths,float)
    # Once equity is exhausted it stays at zero; ruin returns are never removed.
    return np.cumprod(np.maximum(0,1+a),axis=-1)

def drawdowns(paths):
    e=equity_paths(paths)
    peaks=np.maximum(1,np.maximum.accumulate(e,axis=-1))
    return np.min(e/peaks-1,axis=-1)

def pf(a):
    a=np.asarray(a,float)
    if not len(a): return np.nan
    loss=-a[a<0].sum(); win=a[a>0].sum()
    return float(win/loss) if loss>0 else (np.inf if win>0 else np.nan)

def streaks(a):
    a=np.atleast_2d(a)
    if a.shape[1]==0: return np.zeros(a.shape[0],int)
    nonloss=np.where(a>=0,np.arange(a.shape[1])+1,0)
    lengths=np.arange(a.shape[1])+1-np.maximum.accumulate(nonloss,axis=1)
    return lengths.max(axis=1)

def dependence(cache,result):
    n=len(cache)
    if n==0: return {'raw_trades':0,'effective_observations':0.,'sample_quality':'INSUFFICIENT'}
    starts=cache.entry_indices+1; ends=cache.entry_indices+result.exit_index+1
    lo=starts.min(); hi=ends.max(); delta=np.zeros(hi-lo+2,int)
    np.add.at(delta,starts-lo,1); np.add.at(delta,ends-lo+1,-1)
    concurrent=np.cumsum(delta)[:-1]
    inverse=np.divide(1.,concurrent,out=np.zeros(len(concurrent)),where=concurrent>0)
    prefix=np.r_[0,np.cumsum(inverse)]
    uniqueness=(prefix[ends-lo+1]-prefix[starts-lo])/(ends-starts+1)
    overlap_ess=float(uniqueness.sum())
    centered=result.net-result.net.mean(); denom=np.dot(centered,centered)
    rho=[]
    if denom>0:
        for lag in range(1,min(n//3,100)+1):
            v=float(np.dot(centered[:-lag],centered[lag:])/denom)
            if v<=0: break
            rho.append(v)
    ac_ess=n/(1+2*sum(rho))
    ess=min(n,overlap_ess,ac_ess)
    active=concurrent[concurrent>0]
    return {'raw_trades':n,'effective_observations':ess,'overlap_effective_observations':overlap_ess,
            'autocorrelation_effective_observations':ac_ess,'overlap_ratio':float(1-uniqueness.mean()),
            'average_concurrent_trades':float(active.mean()),'maximum_concurrent_trades':int(active.max()),
            'average_entry_spacing_days':float(np.mean(np.diff(np.sort(cache.entry_dates)).astype('timedelta64[D]').astype(float))) if n>1 else np.nan,
            'sample_quality':'STRONG' if ess>=100 else 'MODERATE' if ess>=50 else 'WEAK' if ess>=20 else 'VERY WEAK',
            'method':'Minimum of summed holding-interval uniqueness and positive-sequence autocorrelation ESS; conservative diagnostic, not an exact independent count'}

def metrics(result,cache=None,expensive=False,requested=()):
    a=result.net; n=len(a)
    if n==0: return {'raw_trades':0}
    sd=float(np.std(a,ddof=1)) if n>1 else np.nan
    mean=float(np.mean(a)); downs=np.minimum(a,0)
    dd=float(drawdowns(a)); final=float(equity_paths(a)[-1]-1)
    r=a/result.stop_distance
    hold_sessions=result.exit_index.astype(int)+1
    hold_calendar_days=result.hold_days.astype(float)
    out={'raw_trades':n,'mean_return':mean,'median_return':float(np.median(a)),
         'expected_r':float(r.mean()),'median_r':float(np.median(r)),'profit_factor':pf(a),
         'win_probability':float(np.mean(a>0)),'loss_probability':float(np.mean(a<0)),
         'stop_probability':float(np.mean(result.outcome==1)),'target_probability':float(np.mean(result.outcome==2)),
         'timeout_probability':float(np.mean(result.outcome==0)),
         'partial_exit_probability':float(np.mean(result.partial_index>=0)),
         'same_bar_conflict_count':int(result.conflict.sum()),'same_bar_conflict_rate':float(result.conflict.mean()),
         'sequential_trade_max_drawdown':dd,'sequential_compounded_return':final,
         'geometric_mean_trade_return':float((1+final)**(1/n)-1),
         'standard_deviation':sd,'trade_sharpe_like':mean/sd if sd>0 else np.nan,
         'downside_deviation':float(np.sqrt(np.mean(downs**2))),
         'sortino_like':mean/np.sqrt(np.mean(downs**2)) if (downs<0).any() else np.nan,
         'calmar_style_ratio':final/abs(dd) if dd<0 else np.nan,
         'payoff_ratio':float(a[a>0].mean()/-a[a<0].mean()) if (a>0).any() and (a<0).any() else np.nan,
         'average_holding_sessions':float(np.mean(hold_sessions)),'median_holding_sessions':float(np.median(hold_sessions)),
         'average_holding_calendar_days':float(np.mean(hold_calendar_days)),'median_holding_calendar_days':float(np.median(hold_calendar_days)),
         'average_time_to_target_sessions':float(np.mean(hold_sessions[result.outcome==2])) if (result.outcome==2).any() else np.nan,
         'average_time_to_stop_sessions':float(np.mean(hold_sessions[result.outcome==1])) if (result.outcome==1).any() else np.nan,
         'average_time_to_target_calendar_days':float(np.mean(hold_calendar_days[result.outcome==2])) if (result.outcome==2).any() else np.nan,
         'average_time_to_stop_calendar_days':float(np.mean(hold_calendar_days[result.outcome==1])) if (result.outcome==1).any() else np.nan,
         # Legacy aliases retained for consumers that already read these keys;
         # their values remain calendar-day differences.
         'average_holding_days':float(np.mean(hold_calendar_days)),'median_holding_days':float(np.median(hold_calendar_days)),
         'average_time_to_target':float(np.mean(hold_calendar_days[result.outcome==2])) if (result.outcome==2).any() else np.nan,
         'average_time_to_stop':float(np.mean(hold_calendar_days[result.outcome==1])) if (result.outcome==1).any() else np.nan,
         'longest_losing_streak':int(streaks(a)[0]),
         'consecutive_stop_outs':int(streaks(np.where(result.outcome==1,-1,1))[0]),
         'worst_trade':float(a.min()),'best_trade':float(a.max()),'average_cost':float(result.cost.mean()),
         'gross_mean_return':float(result.gross.mean()),'ruin_trade_count':int((a<=-1).sum()),
         'profit_factor_note':'No losing trades observed; PF infinite/undefined, confidence limited' if not (a<0).any() else None}
    k=max(1,math.ceil(.05*n)); out['expected_shortfall_return_5']=float(np.sort(a)[:k].mean())
    out['expected_shortfall_r_5']=float(np.sort(r)[:k].mean())
    target_holds=hold_sessions[result.outcome==2]
    out['time_efficiency_ratio']=float(1-target_holds.mean()/max(1,result.candidate.horizon)) if len(target_holds) else np.nan
    excursion_keys={'winner_stop_usage_ratio','target_capture_efficiency','post_stop_recovery_ratio','maximum_adverse_excursion','maximum_favourable_excursion'}
    need_excursions=expensive or bool(excursion_keys.intersection(requested))
    if need_excursions and cache is not None:
        mask=np.arange(cache.fav.shape[1])[None,:]<=result.exit_index[:,None]
        mfe=np.nanmax(np.where(mask,cache.fav,np.nan),axis=1)
        mae=np.nanmin(np.where(mask,cache.adv,np.nan),axis=1)
        winners=a>0
        out['winner_stop_usage_ratio']=float(np.mean(np.abs(mae[winners])/result.stop_distance[winners])) if winners.any() else np.nan
        captured=np.divide(np.maximum(result.gross[winners],0),mfe[winners],out=np.full(winners.sum(),np.nan),where=mfe[winners]>0)
        out['target_capture_efficiency']=float(np.nanmean(captured)) if winners.any() and np.isfinite(captured).any() else np.nan
        recovered=[]
        for i in np.flatnonzero(result.outcome==1):
            limit=min(cache.lengths[i],result.candidate.horizon)
            recovered.append(bool(np.any(cache.close[i,result.exit_index[i]+1:limit]>=0)))
        out['post_stop_recovery_ratio']=float(np.mean(recovered)) if recovered else np.nan
        out['maximum_adverse_excursion']=float(mae.min())
        out['maximum_favourable_excursion']=float(mfe.max())
    if expensive:
        for q in (.05,.01):
            suffix=str(int(round(100*(1-q))))
            out['var_'+suffix]=float(-np.quantile(a,q))
            out['expected_shortfall_'+suffix]=float(-np.sort(a)[:max(1,math.ceil(q*n))].mean())
        out.update(bottom_five_trades=np.sort(a)[:5].tolist(),bottom_decile_mean=float(np.sort(a)[:max(1,math.ceil(n*.1))].mean()),lower_partial_moment_2=float(np.mean(downs**2)))
        if n>=4 and np.std(a)>0:
            z=(a-a.mean())/np.std(a)
            out.update(skewness=float(np.mean(z**3)),excess_kurtosis=float(np.mean(z**4)-3))
        else: out.update(skewness=None,excess_kurtosis=None)
        for label,code in [('time_to_target',2),('time_to_stop',1)]:
            vals_sessions=hold_sessions[result.outcome==code]
            vals_calendar=hold_calendar_days[result.outcome==code]
            out[label+'_sessions_quantiles']=dict(zip(['p10','median','p90'],np.quantile(vals_sessions,[.1,.5,.9]).tolist())) if len(vals_sessions) else None
            out[label+'_calendar_days_quantiles']=dict(zip(['p10','median','p90'],np.quantile(vals_calendar,[.1,.5,.9]).tolist())) if len(vals_calendar) else None
            out[label+'_quantiles']=out[label+'_calendar_days_quantiles']
        if cache is not None:
            out['excursions']={'maximum_favourable':float(mfe.max()),'maximum_adverse':float(mae.min()),
                'mfe_quantiles':np.quantile(mfe,[.1,.5,.9]).tolist(),'mae_quantiles':np.quantile(mae,[.1,.5,.9]).tolist(),
                'mae_winners':float(mae[a>0].mean()) if (a>0).any() else None,'mae_losers':float(mae[a<0].mean()) if (a<0).any() else None,
                'mfe_winners':float(mfe[a>0].mean()) if (a>0).any() else None,'mfe_losers':float(mfe[a<0].mean()) if (a<0).any() else None,
                'mfe_on_stop_bars':float(mfe[result.outcome==1].mean()) if (result.outcome==1).any() else None,
                'mae_on_target_bars':float(mae[result.outcome==2].mean()) if (result.outcome==2).any() else None,
                'note':'Exit-bar extremes may occur after fill; OHLC gives bounds, not intrabar event ordering'}
    return out

def candidate_grid(cache,cfg):
    stops=list(cfg.stops); targets=list(cfg.targets)
    if cfg.adaptive and len(cache):
        stops+=np.quantile(-np.minimum(np.nanmin(cache.adv,axis=1),0),[.25,.5,.75]).tolist()
        targets+=np.quantile(np.maximum(np.nanmax(cache.fav,axis=1),0),[.25,.5,.75]).tolist()
    stops=sorted({round(x,6) for x in stops if 0<x<1})
    targets=sorted({round(x,6) for x in targets if x>0 and (cfg.direction!='SHORT' or x<1)})
    horizons=(max(cfg.horizons),) if cfg.calendar_months is not None else sorted(set(cfg.horizons))
    return [Candidate(s,t,h,f,norm) for norm in cfg.normalisations for f in cfg.families
            for s in (stops if norm=='percent' else cfg.volatility_multipliers)
            for t in (targets if norm=='percent' else cfg.volatility_multipliers)
            for h in horizons]

def surface(rows):
    """Local lattice neighbourhood excluding self, rank-space distances in 3D."""
    if not rows: return []
    for row in rows:
        c=row['candidate']; group=[r for r in rows if r['candidate'].family==c.family and r['candidate'].normalisation==c.normalisation]
        axes=[sorted({getattr(r['candidate'],k) for r in group}) for k in ('stop','target','horizon')]
        centre=[axis.index(getattr(c,k)) for axis,k in zip(axes,('stop','target','horizon'))]
        neighbours=[r for r in group if r is not row and all(abs(axis.index(getattr(r['candidate'],k))-p)<=1 for axis,k,p in zip(axes,('stop','target','horizon'),centre))]
        means=np.array([r['mean_return'] for r in neighbours]); pfs=np.array([r['profit_factor'] for r in neighbours])
        ers=np.array([r['expected_r'] for r in neighbours]); dd=np.array([r['sequential_trade_max_drawdown'] for r in neighbours])
        if not len(means):
            row.update(parameter_robustness_score=0.,neighbour_count=0,local_median_return=np.nan,local_median_pf=np.nan,local_median_r=np.nan,local_median_drawdown=np.nan,neighbour_profitable_fraction=0.,neighbour_pf_above_one=0.,neighbour_r_positive=0.,parameter_sensitivity=np.nan,coefficient_of_variation=np.nan,local_rank_stability=0.)
            continue
        med=float(np.median(means)); spread=float(np.median(np.abs(means-med)))
        cv=float(np.std(means)/max(abs(np.mean(means)),1e-12))
        peak=max(0,row['mean_return']-med)/max(abs(med),spread,1e-8)
        profitable=float(np.mean(means>0)); pf1=float(np.mean(pfs>1)); rpos=float(np.mean(ers>0))
        stability=1/(1+cv+peak)
        score=100*(.4*profitable+.25*pf1+.15*rpos+.2*stability)*min(1,len(means)/4)
        # Isolated peaks and high coefficient of variation cannot score as robust.
        score*=1/(1+.25*peak+.1*cv)
        row.update(parameter_robustness_score=score,neighbour_count=len(means),local_median_return=med,
                   local_median_pf=float(np.median(pfs)),local_median_r=float(np.median(ers)),local_median_drawdown=float(np.median(dd)),
                   neighbour_profitable_fraction=profitable,neighbour_pf_above_one=pf1,neighbour_r_positive=rpos,
                   parameter_sensitivity=spread,coefficient_of_variation=cv,local_rank_stability=stability)
    return sorted(rows,key=lambda r:(-r['parameter_robustness_score'],-r['local_median_return'] if np.isfinite(r['local_median_return']) else np.inf,r['candidate']))

def search(cache,cfg,requested=()):
    if not len(cache): raise ValueError('No usable train trades after purge/embargo')
    candidates=candidate_grid(cache,cfg)
    evaluated=evaluate_candidates(cache,candidates,cfg)
    rows=[{'candidate':r.candidate,**metrics(r,cache,requested=requested),**dependence(cache,r)} for r in evaluated]
    rows=surface(rows)
    if cfg.adaptive:
        extra=set()
        for row in rows[:cfg.fine_regions]:
            c=row['candidate']
            for sm in (.9,1.,1.1):
                for tm in (.9,1.,1.1):
                    extra.add(replace(c,stop=round(c.stop*sm,6),target=round(c.target*tm,6)))
        extra=sorted(c for c in extra-set(candidates) if c.normalisation!='percent' or (c.stop<1 and (cfg.direction!='SHORT' or c.target<1)))
        more=evaluate_candidates(cache,extra,cfg)
        rows+= [{'candidate':r.candidate,**metrics(r,cache,requested=requested),**dependence(cache,r)} for r in more]
        candidates+=extra
        rows=surface(rows)
    return rows,candidates
