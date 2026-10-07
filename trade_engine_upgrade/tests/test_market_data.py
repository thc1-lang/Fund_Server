import tempfile
import unittest
from pathlib import Path

import pandas as pd

from market_data import (_calendar_sessions, cache_market_data, coverage_audit, load_cached_market_data,
                         load_market_data, market_data_quality_score, provenance_audit,
                         readiness_report, reconcile_sources)
from research_core import ResearchConfig


def cfg(**kwargs):
    base=dict(direction='LONG',train_start='2020-01-01',train_end='2020-01-31',
              validation_start='2020-02-01',validation_end='2020-02-29',
              test_start='2020-03-01',test_end='2020-03-31',horizons=(2,),
              feature_lookback=5,volatility_lookback=5,regime_lookback=5,
              coverage_safety_buffer_sessions=2,require_warmup_coverage=True,
              require_full_horizon_coverage=True)
    base.update(kwargs)
    return ResearchConfig(**base)


def bars(start='2019-12-15', end='2020-04-10'):
    dates=pd.date_range(start,end,freq='B')
    close=pd.Series(100.,index=dates)
    return pd.DataFrame({'date':dates,'open':close.to_numpy(),'high':close.to_numpy()+1,
                         'low':close.to_numpy()-1,'close':close.to_numpy()})


class CoverageTests(unittest.TestCase):
    def test_current_spx_range_and_closure_regression(self):
        sessions, provider, error = _calendar_sessions('NYSE', pd.Timestamp('1979-12-26'), pd.Timestamp('2026-09-25'))
        self.assertTrue(len(sessions), f'{provider}: {error}')
        close = pd.Series(100.0, index=sessions)
        frame = pd.DataFrame({'date': sessions, 'open': close.to_numpy(), 'high': (close + 1).to_numpy(),
                              'low': (close - 1).to_numpy(), 'close': close.to_numpy()})
        audit = coverage_audit(frame, cfg(train_start='2010-01-01', train_end='2017-12-31',
                                          validation_start='2018-01-01', validation_end='2021-12-31',
                                          test_start='2022-01-01', test_end='2025-12-31',
                                          horizons=(126,), feature_lookback=252, volatility_lookback=20,
                                          regime_lookback=252, coverage_safety_buffer_sessions=5,
                                          trading_calendar='NYSE'))
        self.assertTrue(audit['range_coverage_pass'])
        self.assertTrue(audit['session_completeness_pass'])
        self.assertTrue(audit['session_consistency_pass'])
        self.assertEqual(audit['genuine_missing_session_count'], 0)
        self.assertEqual(audit['special_closure_gap_count'], 3)
        self.assertEqual(audit['status'], 'PASS')
        metadata = {
            'instrument': 'S&P 500 Index', 'ticker': 'SPX', 'asset_class': 'INDEX',
            'provider': 'Investing.com', 'provider_symbol': 'S&P 500 / US SPX 500',
            'frequency': '1D', 'trading_calendar': 'NYSE', 'timezone': 'America/New_York',
            'currency': 'USD', 'price_type': 'close', 'ohlc_adjustment': 'raw',
            'adjustment_method': 'raw source OHLC', 'retrieval_timestamp': '2026-09-27T00:00:00Z',
            'first_date': '1979-12-26', 'last_date': '2026-09-25', 'row_count': len(frame),
            'source_identifier': 'Investing.com: S&P 500 historical daily data', 'data_hash': 'fixture'
        }
        readiness = readiness_report(frame, cfg(train_start='2010-01-01', train_end='2017-12-31',
                                                validation_start='2018-01-01', validation_end='2021-12-31',
                                                test_start='2022-01-01', test_end='2025-12-31',
                                                horizons=(126,), feature_lookback=252, volatility_lookback=20,
                                                regime_lookback=252, coverage_safety_buffer_sessions=5,
                                                trading_calendar='NYSE', require_provenance=True),
                                  {'material_rows': 0}, metadata, {'status': 'NOT_CONFIGURED'})
        self.assertTrue(readiness['train_authorised'])

    def test_full_coverage_includes_warmup_and_horizon_tail(self):
        audit=coverage_audit(bars(),cfg())
        self.assertEqual(audit['status'],'PASS')
        self.assertEqual(audit['warmup_sessions'],12)
        self.assertEqual(audit['required_market_data_start'],'2019-12-16')
        self.assertTrue(audit['required_period_complete'])

    def test_missing_beginning_coverage_fails(self):
        audit=coverage_audit(bars('2020-01-02'),cfg())
        self.assertEqual(audit['status'],'FAIL')
        self.assertGreater(audit['missing_start_days'],0)

    def test_missing_ending_coverage_fails(self):
        audit=coverage_audit(bars(end='2020-03-20'),cfg())
        self.assertEqual(audit['status'],'FAIL')
        self.assertGreater(audit['missing_end_days'],0)

    def test_warmup_shortage_is_distinct_from_train_start(self):
        audit=coverage_audit(bars('2019-12-30'),cfg())
        self.assertEqual(audit['configured_train_start'],'2020-01-01')
        self.assertLess(pd.Timestamp(audit['actual_market_data_start']),pd.Timestamp(audit['configured_train_start']))
        self.assertEqual(audit['status'],'FAIL')

    def test_nyse_special_closure_is_not_missing_session(self):
        frame = bars('2001-09-10', '2001-09-17')
        frame = frame[frame['date'].isin([pd.Timestamp('2001-09-10'), pd.Timestamp('2001-09-17')])]
        audit = coverage_audit(frame, cfg(train_start='2001-09-10', train_end='2001-09-10',
                                          validation_start='2001-09-11', validation_end='2001-09-11',
                                          test_start='2001-09-17', test_end='2001-09-17',
                                          require_warmup_coverage=False, require_full_horizon_coverage=False,
                                          trading_calendar='NYSE'))
        self.assertEqual(audit['unexplained_gap_count'], 0)
        self.assertEqual(audit['special_closure_gap_count'], 1)
        self.assertEqual(audit['long_gap_details'][0]['calendar_classification'], 'SPECIAL MARKET CLOSURE')

    def test_genuine_missing_nyse_session_fails(self):
        frame = bars('2012-10-31', '2012-11-02')
        frame = frame[frame['date'].isin([pd.Timestamp('2012-10-31'), pd.Timestamp('2012-11-02')])]
        audit = coverage_audit(frame, cfg(train_start='2012-10-31', train_end='2012-10-31',
                                          validation_start='2012-10-31', validation_end='2012-10-31',
                                          test_start='2012-11-02', test_end='2012-11-02',
                                          require_warmup_coverage=False, require_full_horizon_coverage=False,
                                          trading_calendar='NYSE'))
        self.assertEqual(audit['status'], 'FAIL')
        self.assertEqual(audit['genuine_missing_session_dates'], ['2012-11-01'])


class SourceAndReconciliationTests(unittest.TestCase):
    def test_csv_provider_provenance_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'bars.csv'; bars().to_csv(path,index=False)
            one=load_market_data('TEST',path,price_type='close',adjustment='raw',provider='fixture',
                                 provider_symbol='TEST',asset_class='INDEX',timezone_name='UTC',currency='USD')
            two=load_market_data('TEST',path,price_type='close',adjustment='raw',provider='fixture',
                                 provider_symbol='TEST',asset_class='INDEX',timezone_name='UTC',currency='USD')
            self.assertEqual(one.provenance.data_hash,two.provenance.data_hash)
            self.assertEqual(one.provenance.row_count,len(bars()))

    def test_reconciliation_classifies_matches_near_and_material(self):
        primary=bars('2020-01-01','2020-01-06')
        secondary=primary.copy()
        secondary.loc[1,'close']+=0.0001
        secondary.loc[2,'high']+=1.
        secondary=secondary.iloc[:-1]
        diagnostics,summary=reconcile_sources(primary,secondary,tolerance_bps=5)
        self.assertEqual(summary['exact_matches'],1)
        self.assertIn('ROUNDING_DIFFERENCE',summary['classification_counts'])
        self.assertIn('HIGH_MISMATCH',summary['classification_counts'])
        self.assertEqual(summary['missing_secondary_bars'],1)
        self.assertEqual(len(diagnostics),len(primary))

    def test_cache_round_trip_and_hash_check(self):
        with tempfile.TemporaryDirectory() as td:
            bundle=load_market_data('TEST',bars(),provider='fixture',provider_symbol='TEST')
            saved=cache_market_data(bundle,td,'test_daily')
            loaded=load_cached_market_data(saved['path'],saved['metadata_path'])
            self.assertEqual(loaded.provenance.data_hash,bundle.provenance.data_hash)

    def test_provenance_gate_and_quality_hard_caps(self):
        audit=provenance_audit({'instrument':'X'},cfg(require_provenance=True),)
        self.assertEqual(audit['status'],'FAIL')
        quality=market_data_quality_score('FAIL',{'status':'PASS'},audit)
        self.assertEqual(quality['status'],'FAIL')
        self.assertEqual(quality['score'],0.)

    def test_spx_provenance_passes_without_secondary_source(self):
        metadata = {
            'instrument': 'S&P 500 Index', 'ticker': 'SPX', 'asset_class': 'INDEX',
            'provider': 'Investing.com', 'provider_symbol': 'S&P 500 / US SPX 500',
            'frequency': '1D', 'trading_calendar': 'NYSE', 'timezone': 'America/New_York',
            'currency': 'USD', 'price_type': 'close', 'ohlc_adjustment': 'raw',
            'adjustment_method': 'raw source OHLC', 'retrieval_timestamp': '2026-09-27T00:00:00Z',
            'first_date': '1979-12-26', 'last_date': '2026-09-25', 'row_count': len(bars()),
            'source_identifier': 'Investing.com: S&P 500 historical daily data', 'data_hash': 'fixture'
        }
        audit = provenance_audit(metadata, cfg(require_provenance=True, trading_calendar='NYSE'))
        self.assertEqual(audit['status'], 'PASS')
        coverage = coverage_audit(bars(), cfg())
        quality = market_data_quality_score('PASS', coverage, audit, {'status': 'NOT_CONFIGURED'})
        self.assertGreater(quality['score'], 0)
        self.assertIn(quality['status'], ('PASS', 'PASS WITH WARNINGS'))

    def test_unknown_provider_still_fails_provenance(self):
        audit = provenance_audit({'instrument': 'S&P 500 Index', 'ticker': 'SPX',
                                  'asset_class': 'INDEX', 'provider': 'UNSPECIFIED',
                                  'provider_symbol': 'S&P 500 / US SPX 500', 'frequency': '1D',
                                  'trading_calendar': 'NYSE', 'timezone': 'America/New_York',
                                  'currency': 'USD', 'price_type': 'close', 'ohlc_adjustment': 'raw',
                                  'adjustment_method': 'raw source OHLC', 'retrieval_timestamp': 'x',
                                  'first_date': 'x', 'last_date': 'x', 'row_count': 1,
                                  'source_identifier': 'x', 'data_hash': 'x'},
                                 cfg(require_provenance=True, trading_calendar='NYSE'))
        self.assertEqual(audit['status'], 'FAIL')


if __name__=='__main__':
    unittest.main()
