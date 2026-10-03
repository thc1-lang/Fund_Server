from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from empirical_validation.audit import run_data_audit
from empirical_validation.bootstrap import bootstrap
from empirical_validation.calibration import calibration_refusal
from empirical_validation.config import ValidationConfig
from empirical_validation.factor_matrix import factor_matrix
from empirical_validation.information_coefficient import pearson_ic, spearman_ic
from empirical_validation.leakage_audit import LeakageAuditor, audit_rows
from empirical_validation.market_data import FixtureMarketDataProvider, PriceBar, NullMarketDataProvider
from empirical_validation.models import HistoricalObservation
from empirical_validation.observation_builder import build_observations, deduplicate_observations
from empirical_validation.outcome_labels import forward_outcome, mark_overlapping_windows
from empirical_validation.profile_builder import build_candidate
from empirical_validation.quantile_analysis import quantile_outcomes
from empirical_validation.store import ValidationStore
from empirical_validation.walk_forward import chronological_split


def obs(ticker="ZZ", date="2024-01-01", overall=None):
    return HistoricalObservation(
        f"obs:{ticker}:{date}", ticker, ticker, date, date, None, None, None,
        "core_v1", "qualitative-scoring-v1.2", {"factor": 1.0}, {"factor": "SCORED"},
        {"P": overall}, {"P": 1.0}, {"P": 1.0}, {"P": .8}, overall, 1.0, 1.0, .8, [], {}, "fp", True, date,
    )


class PointInTimeTests(unittest.TestCase):
    def test_publication_after_cutoff_rejected(self):
        self.assertEqual(audit_rows([{"id":"x","publication_date":"2025-01-02"}], "2025-01-01")[0].status, "FAIL")

    def test_filing_event_and_price_future_dates_rejected(self):
        rows=[{"id":"f","filing_date":"2025-01-02"},{"id":"e","event_date":"2025-01-03"}]
        findings=audit_rows(rows,"2025-01-01")
        self.assertEqual({x.rule for x in findings},{"filing_date","event_date"})

    def test_null_overall_score_retained(self):
        row=obs(overall=None)
        self.assertIsNone(row.overall_score)
        self.assertEqual(factor_matrix([row])["rows"][0]["overall_scoring_coverage"],1.0)

    def test_duplicate_observation_removal_and_stable_id(self):
        a=obs(); unique,count=deduplicate_observations([a,a])
        self.assertEqual(len(unique),1); self.assertEqual(count,1); self.assertEqual(a.observation_id,"obs:ZZ:2024-01-01")

    def test_leakage_auditor_rejects_invalid_observation_and_future_outcome(self):
        bad=obs(); bad.point_in_time_valid=False; bad.exclusion_reason="POINT_IN_TIME_UNAVAILABLE"
        audit=LeakageAuditor().audit([bad])
        self.assertEqual(audit.status,"FAIL")


class OutcomeTests(unittest.TestCase):
    def _provider(self):
        rows=[]; benchmark=[]
        for i in range(400):
            d=(date(2024,1,1)+timedelta(days=i)).isoformat()
            rows.append(PriceBar("ZZ",d,100+i,100+i)); benchmark.append(PriceBar("SPY",d,100+i*.5,100+i*.5))
        return FixtureMarketDataProvider(rows,benchmark)

    def test_price_outcome_starts_after_cutoff_and_excess_return(self):
        out=forward_outcome(obs(),self._provider(),"SPY",21)
        self.assertEqual(out.execution_date,"2024-01-02"); self.assertEqual(out.status,"AVAILABLE"); self.assertIsNotNone(out.excess_return)

    def test_all_forward_horizons_and_overlap_flag(self):
        provider=self._provider(); rows=[forward_outcome(obs(date=f"2024-01-0{i}"),provider,"SPY",h) for i,h in enumerate((21,63,126,252),1)]
        self.assertEqual([x.horizon_days for x in rows],[21,63,126,252]); self.assertTrue(any(x.overlap_flag for x in mark_overlapping_windows(rows)))

    def test_missing_provider_does_not_fabricate_zero(self):
        out=forward_outcome(obs(),NullMarketDataProvider(),"SPY",21)
        self.assertIsNone(out.raw_adjusted_return); self.assertEqual(out.status,"UNAVAILABLE")


class DiagnosticsTests(unittest.TestCase):
    def test_ic_rank_ic_and_quantile_minimum(self):
        self.assertAlmostEqual(pearson_ic([1,2,3],[2,4,6]),1.0)
        self.assertAlmostEqual(spearman_ic([1,3,2],[2,6,4]),1.0)
        self.assertEqual(quantile_outcomes([1,2,3],[1,2,3],3,10)["status"],"INSUFFICIENT_SAMPLE")

    def test_bootstrap_is_deterministic(self):
        a=bootstrap([1,2,3],lambda x:sum(x)/len(x),100,7); b=bootstrap([1,2,3],lambda x:sum(x)/len(x),100,7)
        self.assertEqual(a,b)

    def test_random_and_shuffled_controls_have_no_forced_signal(self):
        from empirical_validation.benchmark_models import random_signal
        self.assertEqual(len(random_signal(5,7)),5)
        self.assertIsNone(pearson_ic([1,1,1],[1,2,3]))


class WalkForwardTests(unittest.TestCase):
    def test_chronological_split_refuses_single_date(self):
        split=chronological_split([obs("A"),obs("B")])
        self.assertEqual(split.status,"INSUFFICIENT_HISTORY_FOR_CALIBRATION")

    def test_calibration_refuses_pilot_sample(self):
        config=ValidationConfig(minimum_observations=30,minimum_unique_companies=10,minimum_unique_dates=4)
        result=calibration_refusal([obs(ticker=t) for t in ("A","B","C","D")],config)
        self.assertEqual(result["status"],"INSUFFICIENT_HISTORICAL_DATA"); self.assertFalse(result["candidate_created"])

    def test_candidate_is_separate_from_frozen_profile(self):
        c=build_candidate("core-v1.2","3m_excess_return",63,["factor"],{"factor":1.0},{},"dataset")
        self.assertEqual(c.parent_profile,"core-v1.2"); self.assertEqual(c.calibration_status,"CANDIDATE")


class RealArtifactAuditTests(unittest.TestCase):
    @staticmethod
    def _fixture_root(root: Path) -> Path:
        """Build a stable point-in-time fixture instead of reading live artifacts.

        The production artifact root is intentionally mutable: a later scoring run
        may append a newer row.  These tests exercise the audit contract, so their
        inputs must remain fixed and must all be available by the historical cutoff.
        """
        score_dir = root / "scoring_engine"
        score_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for ticker in ("ZM", "PLTR", "EXEL", "INCY"):
            rows.append({
                "ticker": ticker,
                "profile": "core_v1",
                "profile_version": "core-v1.2",
                "scoring_version": "qualitative-scoring-v1.2",
                "coverage_version": "coverage-v3",
                "as_of_date": "2026-09-24",
                "created_at": "2026-09-24T12:00:00+00:00",
                "overall_score": None,
                "overall_evidence_coverage": 0.5,
                "overall_scoring_coverage": 0.1,
                "overall_confidence": 0.56,
            })
        (score_dir / "company_scores.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf8"
        )
        # Empty companion stores are explicit so the fixture has no accidental
        # dependency on a fallback file in the live artifact tree.
        for name in ("factor_scores.jsonl", "pillar_scores.jsonl"):
            (score_dir / name).write_text("", encoding="utf8")
        return root

    def test_current_pilot_is_insufficient_without_claiming_backtest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_root(Path(tmp))
            config=ValidationConfig(artifacts_root=str(root))
            rows=build_observations(config)
            audit,leakage=run_data_audit(rows,config)
            self.assertEqual(audit.companies_discovered,4)
            self.assertEqual(audit.unique_dates,1)
            self.assertEqual(audit.market_data_status,"MARKET_DATA_PROVIDER_NOT_CONFIGURED")
            self.assertFalse(audit.adequate_for_calibration)
            self.assertFalse(audit.adequate_for_out_of_sample)
            self.assertEqual(leakage.status,"PASS")

    def test_dataset_fingerprint_and_store_idempotency_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = self._fixture_root(Path(tmp))
            config=ValidationConfig(artifacts_root=str(fixture_root))
            rows=build_observations(config); a,_=run_data_audit(rows,config); b,_=run_data_audit(rows,config)
            self.assertEqual(a.input_fingerprint,b.input_fingerprint)
            store=ValidationStore(fixture_root / "validation_store"); path=store.write_observations(rows)
            self.assertTrue(path.exists()); self.assertEqual(len(path.read_text().splitlines()),len(rows))


if __name__ == "__main__": unittest.main()
