"""Deterministic representative optimisation benchmark; writes benchmark_results.json."""
from __future__ import annotations
import importlib.util
import itertools
import json
import statistics
import sys
import time
import types
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
from research_core import *
from research_pipeline import run_research

ROOT=Path(__file__).resolve().parent
if importlib.util.find_spec('gspread') is None:
    sys.modules.setdefault('gspread',types.ModuleType('gspread'))
spec=importlib.util.spec_from_file_location('baseline_original',ROOT/'baseline'/'original.py')
old=importlib.util.module_from_spec(spec); sys.modules[spec.name]=old; spec.loader.exec_module(old)

def data():
    rng=np.random.default_rng(20260926)
    dates=pd.bdate_range('2015-01-01',periods=1100)
    close=100*np.exp(np.cumsum(rng.normal(.00025,.012,len(dates))))
    opening=close*np.exp(rng.normal(0,.003,len(dates)))
    high=np.maximum(opening,close)*(1+rng.uniform(0,.012,len(dates)))
    low=np.minimum(opening,close)*(1-rng.uniform(0,.012,len(dates)))
    raw=pd.DataFrame({'date':dates,'open':opening,'high':high,'low':low,'close':close})
    chosen=np.arange(30,930,4)
    entries=pd.DataFrame({'entry_id':[f'e{i}' for i in chosen],'entry_date':dates[chosen],
        'entry_price':close[chosen],'direction':'LONG','split':'TRAIN','valid':True,'source':'benchmark'})
    return raw,entries

def median_time(call,repeats=5):
    values=[]
    for _ in range(repeats):
        start=time.perf_counter(); call(); values.append(time.perf_counter()-start)
    return statistics.median(values),values

def stage_profile():
    dates=pd.date_range('2020-01-01','2022-12-31',freq='D')
    close=100*np.exp(np.arange(len(dates))*.002)
    raw=pd.DataFrame({'date':dates,'open':close*.999,'high':close*1.004,'low':close*.997,'close':close})
    periods={'TRAIN':('2020-02-01','2020-12-31'),'VALIDATION':('2021-01-01','2021-12-31'),'TEST':('2022-01-01','2022-12-31')}
    rows=[]
    for split,(start,end) in periods.items():
        for i,r in raw[raw.date.between(start,end)].iloc[::7].iterrows():
            rows.append({'entry_id':f'{split}-{i}','entry_date':r.date,'entry_price':r.close,
                         'direction':'LONG','split':split,'valid':True,'source':'stage-benchmark'})
    entries=pd.DataFrame(rows)
    cfg=ResearchConfig(direction='LONG',instrument='STAGE_BENCHMARK',train_start='2020-02-01',train_end='2020-12-31',
        validation_start='2021-01-01',validation_end='2021-12-31',test_start='2022-01-01',test_end='2022-12-31',
        horizons=(5,10),stops=(.01,.02,.04),targets=(.005,.01,.02),adaptive=False,walk_forward='expanding',folds=2,
        min_train_bars=80,min_trades=8,min_ess=5,min_parameter_score=1,min_fold_pass=.5,min_stress_survival=.2,
        min_bootstrap_positive=.8,min_regime_score=1,mc_sims=1000,bootstrap_reps=500,mc_batch=100,block_length=3,
        n_jobs=1,execution=ExecutionConfig(spread_bps=1,commission_bps=1,entry_slippage_bps=1,exit_slippage_bps=1))
    with tempfile.TemporaryDirectory() as td:
        report=run_research(raw,entries,cfg,td)
    return {'status':report['final_status'],'workload':{'bars':len(raw),'entries':len(entries),'mc_simulations':cfg.mc_sims},
            'seconds':report['timings'],'note':'Local synthetic end-to-end compute; Sheets write is N/A and live runs record it.'}

def main():
    raw,entries=data(); stops=(.01,.02,.04,.06,.08,.12); targets=(.01,.02,.04,.08,.12,.2)
    end=raw.date.max()
    oldcfg=types.SimpleNamespace(direction='LONG',horizon_months=2)
    old_cache=old.build_trade_cache(entries,raw,oldcfg,end)[0]
    candidates=list(itertools.product(stops,targets))
    cfg=ResearchConfig(direction='LONG',train_start=str(raw.date.min().date()),train_end=str(end.date()),
        validation_start=str((end+pd.Timedelta(days=1)).date()),validation_end=str((end+pd.Timedelta(days=2)).date()),
        test_start=str((end+pd.Timedelta(days=3)).date()),test_end=str((end+pd.Timedelta(days=4)).date()),
        calendar_months=2,horizons=(40,),stops=stops,targets=targets,adaptive=False,walk_forward='none',
        min_trades=1,min_ess=1,mc_sims=100,bootstrap_reps=100,execution=ExecutionConfig(mode='threshold_fill'))
    market=make_market(raw,cfg); new_cache=build_paths(market,entries,cfg,cfg.train_start,cfg.train_end,split='TRAIN',embargo=False)
    # Warm both implementations to remove import/allocation startup distortion.
    old.evaluate_matrix(candidates,old_cache,oldcfg); search(new_cache,cfg)
    before,samples_before=median_time(lambda:old.evaluate_matrix(candidates,old_cache,oldcfg))
    after,samples_after=median_time(lambda:search(new_cache,cfg))
    result={'benchmark':'stop-target optimisation including metrics and ranking','seed':20260926,
            'raw_bars':len(raw),'trades':len(new_cache),'candidates':len(candidates),'repetitions':5,
            'original_runtime_seconds':before,'refactored_runtime_seconds':after,'speedup':before/after,
            'original_samples_seconds':samples_before,'refactored_samples_seconds':samples_after,
            'scope':'Local numerical optimisation only; Google Sheets/network I/O intentionally excluded.',
            'remaining_bottleneck':'Advanced diagnostics and Monte Carlo after vectorised stop/target evaluation.',
            'stage_profile':stage_profile()}
    (ROOT/'benchmark_results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
