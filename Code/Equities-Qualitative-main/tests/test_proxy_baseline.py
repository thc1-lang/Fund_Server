from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from insider_intelligence.models import InsiderOwnershipPosition, InsiderPerson, InsiderTransaction, SECFiling
from insider_intelligence.alignment import build_alignment_snapshot
from insider_intelligence.proxy_baseline import parse_proxy_html, reconcile_baseline
from insider_intelligence.store import InsiderStore


FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def proxy_filing(ticker: str, accession: str = "0000000000-26-000001") -> SECFiling:
    return SECFiling(ticker, "0000000001", "DEF 14A", accession, "2026-04-01", None, "proxy.htm", "https://www.sec.gov/Archives/edgar/data/1/proxy.htm", str(FIXTURES / "proxy_baseline_exel.html"), False, False)


class ProxyBaselineTests(unittest.TestCase):
    def test_parser_preserves_beneficial_class_breakdown_percent_display_and_context(self):
        filing = proxy_filing("EXEL")
        people = [InsiderPerson("sec:1", "EXEL", "Example", "Doe Jane Q")]
        result = parse_proxy_html((FIXTURES / "proxy_baseline_exel.html").read_bytes(), filing, existing_people=people)
        self.assertEqual(result.as_of_date, "2026-02-27")
        jane = next(item for item in result.baselines if item.person_name == "Jane Q. Doe")
        self.assertEqual(jane.person_id, "sec:1")
        self.assertEqual(jane.beneficial_shares, 1000.0)
        self.assertEqual(jane.class_breakdown["Common Stock"], 1000.0)
        self.assertEqual(jane.beneficial_percent_display, "<1%")
        self.assertEqual(jane.footnotes, ["(1)"])
        self.assertIn("raw row", jane.ownership_notes or "")
        fund = next(item for item in result.baselines if item.person_name == "Big Fund LLC")
        self.assertTrue(fund.is_major_beneficial_owner)
        group = next(item for item in result.baselines if item.is_group_record)
        self.assertEqual(group.role, "group")

    def test_multiclass_proxy_is_separate_from_section16(self):
        filing = proxy_filing("PLTR")
        result = parse_proxy_html((FIXTURES / "proxy_baseline_pltr.html").read_bytes(), filing)
        alex = next(item for item in result.baselines if item.person_name == "Alex Karp")
        self.assertEqual(alex.class_breakdown, {"Class A": 100.0, "Class B": 200.0, "Class F": 300.0})
        self.assertEqual(alex.beneficial_shares, 600.0)
        self.assertEqual(alex.security_class, "multiple")
        self.assertEqual(alex.voting_power_percent, 4.0)
        self.assertTrue(next(item for item in result.baselines if item.is_group_record))

    def test_store_history_and_idempotency(self):
        filing = proxy_filing("EXEL")
        baselines = parse_proxy_html((FIXTURES / "proxy_baseline_exel.html").read_bytes(), filing).baselines
        with tempfile.TemporaryDirectory() as temp:
            store = InsiderStore(temp)
            first = store.upsert_ownership_baselines(baselines)
            second = store.upsert_ownership_baselines(baselines)
            self.assertEqual(first["added"], len(baselines))
            self.assertEqual(second["unchanged"], len(baselines))
            self.assertEqual(len(store.get_latest_ownership_baselines("EXEL")), len(baselines))

    def test_conservative_reconciliation_updates_only_compatible_single_class(self):
        filing = proxy_filing("EXEL")
        baseline = next(item for item in parse_proxy_html((FIXTURES / "proxy_baseline_exel.html").read_bytes(), filing).baselines if item.person_name == "Jane Q. Doe")
        baseline.person_id = "sec:1"
        position = InsiderOwnershipPosition("o1", "EXEL", "sec:1", "2026-09-01", shares_owned_direct=1100.0, shares_beneficially_owned=1100.0, security_title="Common Stock", direct_indirect="D", filing_id="f1", filing_date="2026-09-01", source_url="https://www.sec.gov/f1")
        tx = InsiderTransaction(transaction_id="t1", ticker="EXEL", person_id="sec:1", transaction_date="2026-06-01", filing_date="2026-06-02", security_type="common_stock", transaction_code="S", transaction_type="open_market_sale", shares=100.0, price=1.0, transaction_value=100.0, acquired_or_disposed="D", shares_owned_after=1100.0, ownership_form="D", is_open_market=True, is_option_exercise=False, is_equity_award=False, is_tax_withholding=False, is_gift=False, is_automatic_sale=False, is_10b5_1=None, footnotes=[], source_url="https://www.sec.gov/f2", filing_id="f2", change_in_direct_holdings=100.0, security_title="Common Stock")
        reconciled = reconcile_baseline(baseline, [position], [tx])
        self.assertEqual(reconciled.current_ownership_method, "proxy_plus_section16_reconciled")
        self.assertEqual(reconciled.reconstructed_current_shares, 1100.0)

    def test_multiclass_and_group_rows_remain_proxy_only(self):
        filing = proxy_filing("PLTR")
        result = parse_proxy_html((FIXTURES / "proxy_baseline_pltr.html").read_bytes(), filing)
        alex = next(item for item in result.baselines if item.person_name == "Alex Karp")
        self.assertEqual(reconcile_baseline(alex, [], []).current_ownership_method, "proxy_only")

    def test_alignment_snapshot_labels_proxy_ownership_separately(self):
        filing = proxy_filing("EXEL")
        baseline = next(item for item in parse_proxy_html((FIXTURES / "proxy_baseline_exel.html").read_bytes(), filing).baselines if item.person_name == "Jane Q. Doe")
        baseline.role = "CEO"
        snapshot, _ = build_alignment_snapshot("EXEL", "2026-09-23", [], [InsiderPerson("sec:1", "EXEL", "Example", "Doe Jane Q", is_ceo=True)], [], baselines=[baseline], reconciliations=[])
        self.assertEqual(snapshot.current_ownership_method, "proxy_only")
        self.assertEqual(snapshot.proxy_ceo_shares, 1000.0)
        self.assertIsNone(snapshot.section16_reported_shares)


if __name__ == "__main__":
    unittest.main()
