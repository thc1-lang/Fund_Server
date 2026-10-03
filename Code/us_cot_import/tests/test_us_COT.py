"""Focused regression checks for CFTC date and contract-audit behavior."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import us_COT


class CotAuditTests(unittest.TestCase):
    def test_parse_mixed_date_formats_without_losing_compact_year(self):
        values = pd.Series(['260106', '2026-01-13', '01/20/2026', '46058', 'bad'])
        parsed = us_COT._parse_cot_dates(values)
        self.assertEqual(str(parsed.iloc[0].date()), '2026-01-06')
        self.assertEqual(str(parsed.iloc[1].date()), '2026-01-13')
        self.assertEqual(str(parsed.iloc[2].date()), '2026-01-20')
        self.assertEqual(str(parsed.iloc[3].date()), '2026-02-05')
        self.assertTrue(pd.isna(parsed.iloc[4]))

    def test_internal_gap_is_distinct_from_later_source_lag(self):
        frame = pd.DataFrame({
            'Market_and_Exchange_Names': ['A old', 'A old', 'A new', 'A new', 'A new', 'A new',
                                          'B', 'B', 'C'],
            'CFTC_Contract_Market_Code': ['001'] * 6 + ['002'] * 2 + ['003'],
            'Report_Date_as_YYYY-MM-DD': ['2026-01-06', '2026-01-20', '2026-01-20',
                                          '2026-01-27', 'bad', '2026-02-03',
                                          '2026-01-06', '2026-01-13', 'bad'],
        })
        audit, summary = us_COT.build_data_quality_audit(
            frame, today='2026-01-30', inactive_after_reports=2)
        a = audit.set_index('CFTC_Contract_Market_Code').loc['001']
        b = audit.set_index('CFTC_Contract_Market_Code').loc['002']
        c = audit.set_index('CFTC_Contract_Market_Code').loc['003']

        self.assertEqual(a['Asset'], 'A new')
        self.assertEqual(a['Missing_Source_Dates'], '2026-01-13')
        self.assertEqual(a['Duplicate_Date_Rows'], 1)
        self.assertEqual(a['Invalid_Date_Rows'], 1)
        self.assertEqual(a['Future_Date_Rows'], 1)
        self.assertEqual(b['Missing_Source_Date_Count'], 0)
        self.assertEqual(b['Reports_Behind_Source'], 2)
        self.assertEqual(b['Reporting_Status'], 'POSSIBLY_INACTIVE_OR_DISCONTINUED')
        self.assertEqual(c['Freshness'], 'NO_VALID_DATES')
        self.assertEqual(summary['source_latest'], '2026-01-27')

    def test_live_run_sends_one_start_and_one_detailed_finish(self):
        delivered = []
        result = [{
            'sheet': 'COT', 'year': 2026, 'rows': 100,
            'summary': {
                'asset_count': 8, 'source_latest': '2026-09-22',
                'assets_with_integrity_flags': 2, 'lagging_count': 1,
                'possibly_inactive_count': 1,
            },
        }, {
            'sheet': 'COT2', 'year': 2026, 'rows': 50,
            'summary': {
                'asset_count': 4, 'source_latest': '2026-09-22',
                'assets_with_integrity_flags': 0, 'lagging_count': 0,
                'possibly_inactive_count': 0,
            },
        }]

        def sender(message, title, **_kwargs):
            delivered.append((title, message))
            return True

        with patch.object(us_COT, '_telegram_sender', return_value=sender), \
             patch.object(us_COT, '_run', return_value=result):
            us_COT.main([])

        self.assertEqual([item[0] for item in delivered],
                         ['COT import started', 'COT import finished'])
        self.assertIn('You will receive one completion report', delivered[0][1])
        self.assertIn('Reports completed: 2/2', delivered[1][1])
        self.assertIn('100 raw rows', delivered[1][1])
        self.assertIn('Google Sheets: COT, COT_Audit, COT2 and COT2_Audit updated',
                      delivered[1][1])

    def test_dry_run_does_not_send_telegram(self):
        args = SimpleNamespace(dry_run=True, no_telegram=False)
        with patch.object(us_COT, 'parse_args', return_value=args), \
             patch.object(us_COT, '_run', return_value=[]), \
             patch.object(us_COT, 'notify_cot_started') as started, \
             patch.object(us_COT, 'notify_cot_finished') as finished:
            us_COT.main([])
        started.assert_not_called()
        finished.assert_not_called()


if __name__ == '__main__':
    unittest.main()
