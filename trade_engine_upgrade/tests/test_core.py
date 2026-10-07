import unittest
from dataclasses import replace
import numpy as np
import pandas as pd
from research_core import *

def configuration(**kwargs):
    base=dict(direction='LONG',train_start='2020-01-01',train_end='2020-04-30',
              validation_start='2020-05-01',validation_end='2020-08-31',test_start='2020-09-01',test_end='2020-12-31',
              horizons=(2,),adaptive=False,walk_forward='none',min_trades=2,min_ess=1.,mc_sims=100,bootstrap_reps=100)
    base.update(kwargs)
    return ResearchConfig(**base)

def fixture(bars, direction='LONG', **kw):
    raw=pd.DataFrame([(100,100,100,100)]+bars,columns=['open','high','low','close'])
    raw['date']=pd.date_range('2020-01-01',periods=len(raw))
    entries=pd.DataFrame([dict(entry_id='a',entry_date=raw.date[0],entry_price=100.,direction=direction,split='TRAIN',valid=True,source='test')])
    cfg=configuration(direction=direction,horizons=(len(bars),),**kw)
    market=make_market(raw,cfg)
    return cfg,market,entries,build_paths(market,entries,cfg,cfg.train_start,cfg.train_end,embargo=False)

class ExecutionTests(unittest.TestCase):
    def check_case(self,d,bars,expected,outcome,**kw):
        cfg,_,_,cache=fixture(bars,d,**kw)
        result=evaluate_candidates(cache,[Candidate(.05,.1,len(bars))],cfg)[0]
        self.assertAlmostEqual(result.gross[0],expected)
        self.assertEqual(result.outcome[0],outcome)
        return result

    def test_long_stop(self): self.check_case('LONG',[(100,102,94,96)],-.05,1)
    def test_short_stop(self): self.check_case('SHORT',[(100,106,99,104)],-.05,1)
    def test_long_target(self): self.check_case('LONG',[(100,112,99,110)],.1,2)
    def test_short_target(self): self.check_case('SHORT',[(100,101,88,90)],.1,2)
    def test_long_gap(self): self.check_case('LONG',[(90,92,85,88)],-.1,1)
    def test_short_gap(self): self.check_case('SHORT',[(110,120,109,115)],-.1,1)
    def test_long_timeout(self): self.check_case('LONG',[(100,104,97,102)],.02,0)
    def test_short_timeout(self): self.check_case('SHORT',[(100,104,97,102)],-.02,0)
    def test_long_conflict(self): self.assertTrue(self.check_case('LONG',[(100,112,94,103)],-.05,1).conflict[0])
    def test_short_conflict(self): self.assertTrue(self.check_case('SHORT',[(100,106,88,97)],-.05,1).conflict[0])
    def test_gap_priority_over_intrabar_conflict(self):
        result=self.check_case('LONG',[(112,115,90,95)],.1,2)
        self.assertFalse(result.conflict[0])
    def test_threshold_compatibility(self): self.check_case('LONG',[(90,92,85,88)],-.05,1,execution=ExecutionConfig(mode='threshold_fill'))
    def test_pessimistic(self): self.check_case('LONG',[(100,102,90,96)],-.1,1,execution=ExecutionConfig(mode='pessimistic_ohlc'))
    def test_horizon_no_future_bars(self):
        cfg,_,_,cache=fixture([(100,102,99,101),(101,130,80,100)])
        r=evaluate_candidates(cache,[Candidate(.05,.1,1)],cfg)[0]
        self.assertEqual(r.exit_index[0],0)
        self.assertAlmostEqual(r.net[0],.01)
    def test_target_first_sensitivity(self):
        cfg,_,_,cache=fixture([(100,112,90,101)])
        r=evaluate_candidates(cache,[Candidate(.05,.1,1)],cfg,ExecutionConfig(same_bar='target_first'))[0]
        self.assertAlmostEqual(r.net[0],.1)
    def test_entry_bar_excluded(self):
        cfg,market,entries,cache=fixture([(100,101,99,100)])
        market.high[0]=500; market.low[0]=1
        cache=build_paths(market,entries,cfg,cfg.train_start,cfg.train_end,embargo=False)
        self.assertEqual(evaluate_candidates(cache,[Candidate(.05,.1,1)],cfg)[0].outcome[0],0)
    def test_horizon_family(self):
        cfg,_,_,cache=fixture([(100,112,99,105)])
        r=evaluate_candidates(cache,[Candidate(.05,.1,1,'horizon')],cfg)[0]
        self.assertAlmostEqual(r.net[0],.05)
    def test_trailing_applies_next_bar(self):
        cfg,_,_,cache=fixture([(100,104,97,103),(103,104,98,99)])
        r=evaluate_candidates(cache,[Candidate(.05,.5,2,'trailing')],cfg)[0]
        self.assertAlmostEqual(r.gross[0],-.01)
        self.assertEqual(r.exit_index[0],1)
    def test_breakeven(self):
        cfg,_,_,cache=fixture([(100,106,99,105),(104,105,99,101)])
        r=evaluate_candidates(cache,[Candidate(.05,.5,2,'breakeven')],cfg)[0]
        self.assertAlmostEqual(r.gross[0],0)
    def test_partial(self):
        cfg,_,_,cache=fixture([(100,111,99,110),(108,109,100,102)])
        r=evaluate_candidates(cache,[Candidate(.05,.1,2,'partial_trailing')],cfg)[0]
        self.assertAlmostEqual(r.gross[0],.08)
        self.assertEqual(r.partial_index[0],0)
    def test_time_decay(self):
        cfg,_,_,cache=fixture([(100,101,99,99),(99,102,98,102)])
        r=evaluate_candidates(cache,[Candidate(.05,.1,2,'time_decay')],cfg)[0]
        self.assertEqual(r.outcome[0],3)
        self.assertAlmostEqual(r.net[0],-.01)

class CostsAndMetrics(unittest.TestCase):
    def test_cost_components(self):
        ex=ExecutionConfig(spread_bps=2,commission_bps=3,entry_slippage_bps=4,exit_slippage_bps=5,impact_bps=6,financing_bps_year=100,borrow_bps_year=50,overnight_bps_day=1)
        self.assertAlmostEqual(ex.costs(365,False),(20+100+365)/10000)
        self.assertAlmostEqual(ex.costs(365,True),(20+150+365)/10000)
    def test_costs_in_search(self):
        cfg,_,_,cache=fixture([(100,101,99,100)],execution=ExecutionConfig(spread_bps=10))
        r=evaluate_candidates(cache,[Candidate(.05,.1,1)],cfg)[0]
        self.assertAlmostEqual(r.net[0],-.001)
    def test_selected_excursion_metrics_are_real(self):
        cfg,_,_,cache=fixture([(100,106,99,105),(105,110,103,108)])
        r=evaluate_candidates(cache,[Candidate(.05,.5,2)],cfg)[0]
        m=metrics(r,cache,expensive=True)
        self.assertGreater(m['maximum_favourable_excursion'],0)
        self.assertLessEqual(m['maximum_adverse_excursion'],0)
        self.assertIn('winner_stop_usage_ratio',m)
    def test_first_loss_drawdown(self): self.assertAlmostEqual(drawdowns([-.2,.1]),-.2)
    def test_ruin_absorbing(self): np.testing.assert_equal(equity_paths([-.1,-1.2,.5]),[.9,0,0])
    def test_all_winners_pf(self): self.assertTrue(np.isinf(pf([.1,.2])))
    def test_all_losers_pf(self): self.assertEqual(pf([-.1,-.2]),0)
    def test_flat_pf(self): self.assertTrue(np.isnan(pf([0,0])))
    def test_empty_pf(self): self.assertTrue(np.isnan(pf([])))
    def test_streaks(self): np.testing.assert_equal(streaks([[-1,-1,0,-1],[1,-1,-1,-1]]),[2,3])
    def test_one_trade_ess(self):
        cfg,_,_,cache=fixture([(100,101,99,100)])
        r=evaluate_candidates(cache,[Candidate(.05,.1,1)],cfg)[0]
        self.assertEqual(dependence(cache,r)['effective_observations'],1)

class DataAndBoundaryTests(unittest.TestCase):
    def test_overlapping_dates_rejected(self):
        with self.assertRaises(ValueError): configuration(validation_start='2020-04-30').validate()
    def test_invalid_direction(self):
        with self.assertRaises(ValueError): configuration(direction='WRONG').validate()
    def test_zero_horizon(self):
        with self.assertRaises(ValueError): configuration(horizons=(0,)).validate()
    def test_negative_cost(self):
        with self.assertRaises(ValueError): ExecutionConfig(spread_bps=-1).validate()
    def test_purge_boundary(self):
        cfg,market,entries,_=fixture([(100,101,99,100)]*3)
        cache=build_paths(market,entries,cfg,'2020-01-01','2020-01-03',embargo=False)
        self.assertEqual(len(cache),0)
        self.assertIn('purged',cache.exclusions[0]['reason'])
    def test_incomplete_coverage(self):
        cfg,market,entries,_=fixture([(100,101,99,100)])
        cache=build_paths(market,entries,replace(cfg,horizons=(10,)),'2020-01-01','2020-04-01',embargo=False)
        self.assertEqual(len(cache),0)
    def test_coverage_error_names_actual_and_required_dates(self):
        cfg=configuration(); dates=pd.date_range('2020-02-01','2020-12-31')
        raw=pd.DataFrame({'date':dates,'open':99.,'high':101.,'low':98.,'close':100.})
        entry=dict(entry_id='a',entry_date=dates[0],entry_price=100.,direction='LONG',split='TRAIN',valid=True,source='test')
        with self.assertRaisesRegex(ValueError,'Raw Data spans 2020-02-01.*TRAIN start 2020-01-01'):
            validate_data(raw,pd.DataFrame([entry]),cfg)
    def test_embargo_exact_dates(self):
        cfg,market,entries,_=fixture([(100,101,99,100)]*3,embargo=1)
        cache=build_paths(market,entries,cfg,'2020-01-01','2020-04-01')
        self.assertEqual(len(cache),0)
        self.assertEqual(cache.embargo_dates,['2020-01-01'])
    def test_embargo_calendar(self):
        cfg,market,_,_=fixture([(100,101,99,100)]*3,embargo=2,embargo_unit='calendar_days')
        self.assertEqual(len(embargo_start(market,'2020-01-01','2020-01-04',cfg)[1]),2)
    def test_embargo_percentage(self):
        cfg,market,_,_=fixture([(100,101,99,100)]*3,embargo=.5,embargo_unit='percentage')
        self.assertEqual(len(embargo_start(market,'2020-01-01','2020-01-04',cfg)[1]),2)
    def test_no_test_leakage(self):
        cfg,market,entries,cache=fixture([(100,102,99,101)]*5)
        cfg=replace(cfg,horizons=(2,))
        a=build_paths(market,entries,cfg,'2020-01-01','2020-01-03',embargo=False)
        market.high[3:]=10000; market.close[3:]=5000
        b=build_paths(market,entries,cfg,'2020-01-01','2020-01-03',embargo=False)
        np.testing.assert_equal(a.fav,b.fav)
        self.assertEqual(candidate_grid(a,cfg),candidate_grid(b,cfg))
    def test_duplicate_bars_rejected(self):
        cfg,market,entries,_=fixture([(100,101,99,100)])
        raw=pd.DataFrame({'date':['2020-01-01','2020-01-01'],'open':[100,100],'high':[101,101],'low':[99,99],'close':[100,100]})
        with self.assertRaisesRegex(ValueError,'Duplicate OHLC'):
            validate_data(raw,entries,cfg)
    def test_impossible_ohlc_rejected(self):
        cfg,_,entries,_=fixture([(100,101,99,100)])
        raw=pd.DataFrame({'date':['2020-01-01','2020-01-02'],'open':[100,100],'high':[101,98],'low':[99,99],'close':[100,100]})
        with self.assertRaisesRegex(ValueError,'OHLC integrity check failed'):
            validate_data(raw,entries,cfg)
    def test_duplicate_entries_rejected(self):
        cfg,market,entries,_=fixture([(100,101,99,100)])
        raw=pd.DataFrame({'date':market.dates,'open':market.open,'high':market.high,'low':market.low,'close':market.close})
        with self.assertRaisesRegex(ValueError,'Duplicate active'):
            validate_data(raw,pd.concat([entries,entries]),cfg)
    def test_inactive_entry_may_have_blank_split(self):
        cfg=configuration(); dates=pd.date_range('2020-01-01','2020-12-31')
        raw=pd.DataFrame({'date':dates,'open':99.,'high':101.,'low':98.,'close':100.})
        active=dict(entry_id='active',entry_date=dates[0],entry_price=100.,direction='LONG',split='TRAIN',valid=True,source='test')
        inactive=dict(entry_id='inactive',entry_date=dates[1],entry_price=100.,direction='LONG',split='',valid=False,source='test')
        validate_data(raw,pd.DataFrame([active,inactive]),cfg)
    def test_active_entry_may_not_have_blank_split(self):
        cfg=configuration(); dates=pd.date_range('2020-01-01','2020-12-31')
        raw=pd.DataFrame({'date':dates,'open':99.,'high':101.,'low':98.,'close':100.})
        entry=dict(entry_id='active',entry_date=dates[0],entry_price=100.,direction='LONG',split='',valid=True,source='test')
        with self.assertRaisesRegex(ValueError,'direction or split'):
            validate_data(raw,pd.DataFrame([entry]),cfg)
    def test_missing_expected_session_rejected(self):
        cfg=configuration(train_end='2020-01-01',validation_start='2020-01-02',validation_end='2020-01-02',test_start='2020-01-04',test_end='2020-01-06',expected_sessions=('2020-01-01','2020-01-02','2020-01-03','2020-01-04','2020-01-05','2020-01-06'))
        raw=pd.DataFrame({'date':pd.to_datetime(['2020-01-01','2020-01-02','2020-01-04','2020-01-05','2020-01-06']),'open':[100]*5,'high':[101]*5,'low':[99]*5,'close':[100]*5})
        entries=pd.DataFrame([dict(entry_id='a',entry_date=pd.Timestamp('2020-01-01'),entry_price=100.,direction='LONG',split='TRAIN',valid=True,source='test')])
        with self.assertRaisesRegex(ValueError,'Missing required exchange sessions'):
            validate_data(raw,entries,cfg)
    def test_large_discontinuity_rejected(self):
        cfg=configuration(train_end='2020-01-01',validation_start='2020-01-02',validation_end='2020-01-02',test_start='2020-01-04',test_end='2020-01-06')
        raw=pd.DataFrame({'date':pd.date_range('2020-01-01',periods=6),'open':[100,20,20,20,20,20],'high':[101,21,21,21,21,21],'low':[99,19,19,19,19,19],'close':[100,20,20,20,20,20]})
        entries=pd.DataFrame([dict(entry_id='a',entry_date=pd.Timestamp('2020-01-01'),entry_price=100.,direction='LONG',split='TRAIN',valid=True,source='test')])
        with self.assertRaisesRegex(ValueError,'split adjustment'):
            validate_data(raw,entries,cfg)
    def test_volatility_scaled_barriers(self):
        cfg,market,entries,_=fixture([(100,101,99,100)]*30)
        cfg=replace(cfg,normalisations=('atr',),horizons=(2,),feature_lookback=5)
        raw=pd.DataFrame({'date':market.dates,'open':market.open,'high':market.high,'low':market.low,'close':market.close})
        market=make_market(raw,cfg)
        entries=entries.copy(); entries.loc[0,'entry_date']=pd.Timestamp('2020-01-10'); entries.loc[0,'entry_price']=100.
        cache=build_paths(market,entries,cfg,cfg.train_start,cfg.train_end,embargo=False)
        self.assertEqual(len(cache),1)
        r=evaluate_candidates(cache,[Candidate(1.,1.,2,normalisation='atr')],cfg)[0]
        self.assertGreater(r.stop_distance[0],0)

class HeaderAndOHLCValidationTests(unittest.TestCase):
    def make_data(self):
        cfg=configuration()
        dates=pd.date_range('2020-01-01','2020-12-31',freq='D')
        drift=np.arange(len(dates))*0.01
        raw=pd.DataFrame({'date':dates,'open':99.+drift,'high':102.+drift,'low':98.+drift,'close':100.+drift,'source_row_number':np.arange(2,len(dates)+2)})
        entries=pd.DataFrame([dict(entry_id='a',entry_date=dates[0],entry_price=100.,direction='LONG',split='TRAIN',valid=True,source='test')])
        return cfg,raw,entries

    def test_exact_spreadsheet_mapping(self):
        frame=pd.DataFrame({'Date':['source-date'],'Price':[100.],'Open':[99.],'High':[102.],'Low':[98.],'Date UK':['2020-01-01']})
        raw,m=standardise_raw_data(frame)
        self.assertEqual((m.date,m.close,m.open,m.high,m.low,m.close_type),('Date UK','Price','Open','High','Low','raw'))
        self.assertEqual((m.source_date,m.price_type,m.ohlc_adjustment),('Date','close','raw'))
        self.assertEqual(raw.loc[0,'source_date'],'source-date')
        self.assertEqual(raw.loc[0,'close'],100.)
        self.assertEqual(raw.attrs['numeric_parsing_audit']['close']['parse_failures'],0)

    def test_grouped_thousands_numeric_values_are_parsed_without_other_coercion(self):
        frame=pd.DataFrame({'Date':['01/02/2020'],'Price':['1,032.10'],'Open':['1,031.00'],
                            'High':['1,040.00'],'Low':['1,020.00'],'Date UK':['02/01/2020']})
        raw,_=standardise_raw_data(frame)
        self.assertEqual(raw.loc[0,'close'],1032.10)
        self.assertEqual(raw.attrs['numeric_parsing_audit']['close']['values_with_commas'],1)
        self.assertEqual(raw.attrs['numeric_parsing_audit']['close']['parse_failures'],0)

    def test_valid_row(self):
        cfg,raw,entries=self.make_data(); _,_,q=validate_data(raw,entries,cfg)
        self.assertEqual(q['status'],'PASS')

    def test_close_at_high_and_low(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,'close']=raw.loc[0,'high']; raw.loc[1,'close']=raw.loc[1,'low']
        validate_data(raw,entries,cfg)

    def test_tiny_close_discrepancy_passes_with_rounding_diagnostic(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,['high','close']]=[100.,100.0000000001]
        _,_,q=validate_data(raw,entries,cfg)
        self.assertEqual(q['ohlc_validation']['rounding_rows'],1)
        self.assertEqual(q['ohlc_diagnostics'][0]['classification'],'ROUNDING_ONLY')

    def test_material_close_above_high_has_full_diagnostic(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,['high','close']]=[100.,100.01]
        with self.assertRaises(OHLCValidationError) as caught: validate_data(raw,entries,cfg)
        d=caught.exception.diagnostics
        self.assertEqual(d.iloc[0].violation_type,'CLOSE_ABOVE_HIGH')
        self.assertEqual(d.iloc[0].source_row_number,2)
        self.assertGreater(d.iloc[0].relative_difference,0)
        self.assertTrue({'date','open','high','low','close','difference','classification'}<=set(d.columns))

    def test_open_below_low_fails(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,'open']=97.
        with self.assertRaises(OHLCValidationError) as caught: validate_data(raw,entries,cfg)
        self.assertIn('OPEN_BELOW_LOW',caught.exception.diagnostics.violation_type.tolist())

    def test_high_below_low_fails(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,['high','low']]=[97.,98.]
        with self.assertRaises(OHLCValidationError) as caught: validate_data(raw,entries,cfg)
        self.assertIn('HIGH_BELOW_LOW',caught.exception.diagnostics.violation_type.tolist())

    def test_adjusted_close_outside_raw_range_is_explicit_and_permitted(self):
        cfg,raw,entries=self.make_data(); raw=raw.rename(columns={'close':'Adj Close'}); raw['Adj Close']=120.
        standardized,m=standardise_raw_data(raw,close_type='adjusted')
        entries.loc[0,'entry_price']=120.
        _,_,q=validate_data(standardized,entries,replace(cfg,close_type='adjusted'))
        self.assertEqual(m.close_type,'adjusted')
        self.assertGreater(q['ohlc_validation']['adjusted_close_mismatch_rows'],0)
        self.assertEqual(q['ohlc_diagnostics'][0]['classification'],'ADJUSTED_CLOSE_MISMATCH')

    def test_declared_settlement_outside_range_is_recorded_not_rejected(self):
        cfg,raw,entries=self.make_data(); raw=raw.rename(columns={'close':'Settlement'}); raw['Settlement']=120.
        standardized,m=standardise_raw_data(raw,price_type='settlement')
        entries.loc[0,'entry_price']=120.
        _,_,q=validate_data(standardized,entries,replace(cfg,price_type='settlement'))
        self.assertEqual(m.close,'Settlement')
        self.assertGreater(q['ohlc_validation']['settlement_range_mismatch_rows'],0)
        self.assertEqual(q['ohlc_diagnostics'][0]['classification'],'SETTLEMENT_RANGE_MISMATCH')

    def test_declared_adjusted_close_can_use_generic_price_heading(self):
        cfg,raw,entries=self.make_data(); raw=raw.rename(columns={'close':'Price'}); raw['Price']=120.
        standardized,m=standardise_raw_data(raw,price_type='adjusted_close')
        entries.loc[0,'entry_price']=120.
        _,_,q=validate_data(standardized,entries,replace(cfg,price_type='adjusted_close'))
        self.assertEqual(m.close,'Price')
        self.assertGreater(q['ohlc_validation']['adjusted_close_mismatch_rows'],0)

    def test_continuous_futures_requires_adjusted_ohlc(self):
        with self.assertRaisesRegex(ValueError,'requires consistently adjusted OHLC'):
            configuration(price_type='continuous_futures').validate()

    def test_guarded_quarantine_removes_only_unaffected_row(self):
        cfg,raw,entries=self.make_data(); raw.loc[len(raw)-2,'close']=raw.loc[len(raw)-2,'high']+.01
        checked,_,q=validate_data(raw,entries,replace(cfg,ohlc_invalid_row_policy='quarantine'))
        self.assertEqual(len(checked),len(raw)-1)
        self.assertEqual(q['status'],'REVIEW')
        self.assertEqual(q['ohlc_validation']['quarantine_action'],'APPLIED')

    def test_guarded_quarantine_rejects_affected_entry_path(self):
        cfg,raw,entries=self.make_data(); raw.loc[1,'close']=raw.loc[1,'high']+.01
        with self.assertRaises(OHLCValidationError) as caught:
            validate_data(raw,entries,replace(cfg,ohlc_invalid_row_policy='quarantine'))
        self.assertEqual(caught.exception.summary['quarantine_action'],'REJECTED')
        self.assertIn('enabled entry paths',' '.join(caught.exception.summary['quarantine_impact']['rejection_reasons']))

    def test_repair_rounding_only_never_repairs_material_error(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,['high','close']]=[100.,100.01]
        with self.assertRaises(OHLCValidationError):
            validate_data(raw,entries,replace(cfg,ohlc_validation_mode='repair_rounding_only'))

    def test_repair_rounding_only_corrects_only_microscopic_difference(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,['high','close']]=[100.,100.0000000001]
        checked,_,q=validate_data(raw,entries,replace(cfg,ohlc_validation_mode='repair_rounding_only'))
        self.assertEqual(checked.loc[0,'close'],checked.loc[0,'high'])
        self.assertEqual(q['ohlc_validation']['rounding_rows'],1)

    def test_warn_mode_preserves_and_reports_material_value(self):
        cfg,raw,entries=self.make_data(); raw.loc[0,['high','close']]=[100.,100.01]
        checked,_,q=validate_data(raw,entries,replace(cfg,ohlc_validation_mode='warn'))
        self.assertEqual(checked.loc[0,'close'],100.01)
        self.assertEqual(q['status'],'FAIL')
        self.assertEqual(q['ohlc_validation']['material_rows'],1)

if __name__=='__main__': unittest.main()
