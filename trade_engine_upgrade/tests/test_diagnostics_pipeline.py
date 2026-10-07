import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import numpy as np
import pandas as pd
from research_core import *
from research_diagnostics import *
from research_pipeline import run_research

def synthetic():
    dates=pd.date_range('2020-01-01','2022-12-31',freq='D')
    close=100*np.exp(np.arange(len(dates))*.002)
    raw=pd.DataFrame({'date':dates,'open':close*.999,'high':close*1.004,'low':close*.997,'close':close})
    periods={'TRAIN':('2020-02-01','2020-12-31'),'VALIDATION':('2021-01-01','2021-12-31'),'TEST':('2022-01-01','2022-12-31')}
    rows=[]
    for split,(start,end) in periods.items():
        subset=raw[raw.date.between(start,end)].iloc[::7]
        for i,r in subset.iterrows():
            rows.append({'entry_id':f'{split}-{i}','entry_date':r.date,'entry_price':r.close,'direction':'LONG','split':split,'valid':True,'source':'synthetic'})
    entries=pd.DataFrame(rows)
    cfg=ResearchConfig(direction='LONG',instrument='SYNTH',train_start='2020-02-01',train_end='2020-12-31',
        validation_start='2021-01-01',validation_end='2021-12-31',test_start='2022-01-01',test_end='2022-12-31',
        horizons=(5,10),stops=(.01,.02,.04),targets=(.005,.01,.02),adaptive=False,walk_forward='expanding',folds=2,
        min_train_bars=80,min_trades=8,min_ess=5,min_parameter_score=1,min_fold_pass=.5,min_stress_survival=.2,
        min_bootstrap_positive=.8,min_regime_score=1,mc_sims=100,bootstrap_reps=100,mc_batch=25,block_length=3,
        n_jobs=1,execution=ExecutionConfig(spread_bps=1,commission_bps=1,entry_slippage_bps=1,exit_slippage_bps=1))
    return raw,entries,cfg

class ResamplingTests(unittest.TestCase):
    def test_indices_deterministic(self):
        for method in ('iid','block','stationary'):
            a=sample_indices(10,4,8,np.random.default_rng(4),method,3)
            b=sample_indices(10,4,8,np.random.default_rng(4),method,3)
            np.testing.assert_array_equal(a,b)
            self.assertEqual(a.shape,(4,8))
            self.assertTrue(((a>=0)&(a<10)).all())
    def test_stationary_sequences_continue(self):
        a=sample_indices(100,20,50,np.random.default_rng(1),'stationary',10)
        self.assertGreater(np.mean(np.diff(a,axis=1)%100==1),.7)
    def test_mc_reproducible_and_keeps_ruin(self):
        c=Candidate(.1,.2,3)
        r=Evaluation(c,np.array([.1,-1.2,.2]),np.array([.1,-1.2,.2]),np.ones(3,int),np.zeros(3,int),np.zeros(3,np.int8),np.zeros(3,bool),np.full(3,.1),np.zeros(3),np.full(3,-1))
        cfg=synthetic()[2]
        a=monte_carlo(r,cfg,'iid'); b=monte_carlo(r,cfg,'iid')
        self.assertEqual(a,b)
        self.assertGreater(a['probability_ruin_threshold'],0)
    def test_bootstrap_all_winners_infinite_pf_interval(self):
        raw,entries,cfg=synthetic(); raw,entries,q=validate_data(raw,entries,cfg); m=make_market(raw,cfg,q)
        cache=build_paths(m,entries,cfg,cfg.validation_start,cfg.validation_end,'VALIDATION')
        r=evaluate_candidates(cache,[Candidate(.02,.005,5)],cfg)[0]
        stats=bootstrap_statistics(r,cache,cfg,10)
        self.assertTrue(np.isinf(stats['profit_factor_interval_95'][0]))
        self.assertIsNone(stats['probabilistic_sharpe_ratio'])

class PipelineTests(unittest.TestCase):
    def test_ohlc_failure_writes_complete_diagnostics(self):
        raw,entries,cfg=synthetic()
        raw=raw.copy(); raw['source_row_number']=np.arange(2,len(raw)+2)
        raw.loc[0,'close']=raw.loc[0,'high']+0.01
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(OHLCValidationError) as caught:
                run_research(raw,entries,cfg,td)
            csvs=list(Path(td).glob('*.ohlc_diagnostics.csv'))
            jsons=list(Path(td).glob('*.ohlc_diagnostics.json'))
            self.assertEqual((len(csvs),len(jsons)),(1,1))
            table=pd.read_csv(csvs[0])
            self.assertEqual(table.loc[0,'source_row_number'],2)
            self.assertEqual(table.loc[0,'violation_type'],'CLOSE_ABOVE_HIGH')
            payload=json.loads(jsons[0].read_text())
            self.assertEqual(payload['summary']['material_rows'],1)
            self.assertIn(str(csvs[0]),str(caught.exception))

    def test_end_to_end_and_holdout_ledger(self):
        raw,entries,cfg=synthetic()
        with tempfile.TemporaryDirectory() as td:
            first=run_research(raw,entries,cfg,td)
            self.assertIn(first['final_status'],('PASS','REVIEW'))
            self.assertEqual(first['oos']['holdout_reused'],False)
            self.assertTrue((Path(td)/'latest.json').exists())
            parsed=json.loads((Path(td)/'latest.json').read_text())
            self.assertEqual(parsed['schema_version'],'2.0')
            self.assertNotIn('NaN',(Path(td)/'latest.json').read_text())
            second=run_research(raw,entries,cfg,td)
            self.assertTrue(second['oos']['holdout_reused'])
            self.assertNotEqual(second['final_status'],'PASS')
            self.assertTrue(second['cache_usage']['train'])
            self.assertTrue(second['cache_usage']['validation'])
    def test_fast_mode_defaults_and_explicit_override(self):
        _,_,cfg=synthetic(); data=asdict(cfg); data['mode']='FAST'
        for key in ('mc_sims','bootstrap_reps','folds','shortlist','fine_regions'): data.pop(key)
        fast=ResearchConfig.from_dict(data)
        self.assertEqual((fast.mc_sims,fast.bootstrap_reps,fast.folds),(1000,500,2))
        data['mc_sims']=1234
        self.assertEqual(ResearchConfig.from_dict(data).mc_sims,1234)
    def test_test_mutation_cannot_change_frozen_structure(self):
        raw,entries,cfg=synthetic()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            x=run_research(raw,entries,cfg,a)
            changed=raw.copy(); mask=changed.date>=cfg.test_start
            k=np.arange(mask.sum()); down=float(changed.loc[mask,'close'].iloc[0])*.7*np.exp(-k*.003)
            changed.loc[mask,'close']=down; changed.loc[mask,'open']=down*1.001
            changed.loc[mask,'high']=down*1.004; changed.loc[mask,'low']=down*.996
            # Rebase test entries so data remain internally valid while test path changes.
            e2=entries.copy()
            lookup=changed.set_index('date').close
            test=e2.split.eq('TEST'); e2.loc[test,'entry_price']=e2.loc[test,'entry_date'].map(lookup)
            y=run_research(changed,e2,replace(cfg,allow_discontinuities=True),b)
            self.assertEqual(x['recommended_structure']['stop_loss'],y['recommended_structure']['stop_loss'])
            self.assertEqual(x['recommended_structure']['take_profit'],y['recommended_structure']['take_profit'])
            self.assertEqual(x['recommended_structure']['max_holding_days'],y['recommended_structure']['max_holding_days'])

if __name__=='__main__': unittest.main()
