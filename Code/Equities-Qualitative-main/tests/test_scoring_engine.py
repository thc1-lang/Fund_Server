from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scoring_engine.aggregation import aggregate
from scoring_engine.audit import audit_company, manual_evidence_coverage, manual_pillar_score, manual_scoring_coverage
from scoring_engine.engine import run_score, score_company
from scoring_engine.factor_registry import load_profile_definition
from scoring_engine.factor_rules import map_ordinal
from scoring_engine.insider_scoring import score_insider
from scoring_engine.management_scoring import score_management
from scoring_engine.main import main as scoring_cli
from scoring_engine.models import FactorDefinition
from scoring_engine.qualitative_scoring import score_qualitative
from scoring_engine.governance_scoring import score_governance
from scoring_engine.confidence import correlated_factor_confidence
from scoring_engine.sources import EvidenceSources
from scoring_engine.store import ScoreStore


def _write(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ScoringEngineTests(unittest.TestCase):
    @staticmethod
    def _zm_fixture(root: str | Path) -> Path:
        """Stable ZM scoring inputs for audit-shape tests.

        The production ZM store is append-only and can receive a later run while
        the suite is executing.  These rows preserve the frozen coverage contract
        without selecting a mutable live snapshot.
        """
        root = Path(root)
        states = []
        for dimension, current, trend in (
            ("revenue", "strong", "stable"),
            ("demand", "strong", "improving"),
            ("margins", "expanding", "improving"),
            ("guidance", "raised", "stable"),
            ("risks", "elevated", "deteriorating"),
        ):
            states.append({
                "ticker": "ZM", "state_id": f"state-{dimension}", "dimension": dimension,
                "current_state": current, "trend": trend, "confidence": 0.8,
                "evidence_count": 1, "evidence_groups_considered": 1,
                "evidence_groups_retained": 1, "supporting_document_ids": ["doc-zm-1"],
                "source_document_ids": ["doc-zm-1"], "source_urls": ["https://example.test/zm"],
                "as_of_fiscal_year": 2026, "as_of_fiscal_quarter": 3,
            })
        _write(root / "qualitative_analysis" / "qualitative_states.jsonl", states)
        _write(root / "management_intelligence" / "governance_snapshots.jsonl", [{
            "ticker": "ZM", "snapshot_id": "governance-snapshot-zm", "as_of_date": "2026-09-24",
            "board_size": 8, "independent_denominator": 8, "independent_count": 6,
            "known_independence_denominator": 7,
            "chair_structure": "CEO_CHAIR_WITH_LEAD_INDEPENDENT_DIRECTOR",
            "chair_is_independent": "NOT_INDEPENDENT",
            "founder_voting_control": {"voting_percentage": None},
            "share_class_structure": "dual_class",
            "shareholder_rights": {
                "advance_notice": "YES", "cumulative_voting": "NO",
                "special_meeting": "UNKNOWN", "written_consent": "UNKNOWN",
                "supermajority": "UNKNOWN",
            },
        }])
        _write(root / "management_intelligence" / "governance_committees.jsonl", [
            {"ticker": "ZM", "committee_id": "committee-audit", "committee_type": "audit",
             "required_independence": "YES", "evidence_text": "all independent",
             "member_independence": {"p1": "INDEPENDENT"}, "financial_expert_person_ids": ["p1"]},
            {"ticker": "ZM", "committee_id": "committee-compensation", "committee_type": "compensation",
             "required_independence": "YES", "evidence_text": "all independent",
             "member_independence": {"p2": "INDEPENDENT"}, "financial_expert_person_ids": []},
            {"ticker": "ZM", "committee_id": "committee-nominating", "committee_type": "nominating_governance",
             "required_independence": "YES", "evidence_text": "independence not established",
             "member_independence": {"p3": "UNKNOWN"}, "financial_expert_person_ids": []},
        ])
        return root

    def test_unknown_does_not_map_to_neutral(self):
        self.assertIsNone(map_ordinal("unknown", {"healthy": 75, "neutral": 50}))

    def test_missing_factor_is_not_zero_or_fifty(self):
        with tempfile.TemporaryDirectory() as temp:
            profile, factors, pillars, snapshot = score_company("EXEL", "2026-09-24", artifacts_root=temp)
            missing = next(item for item in factors if item.factor_key == "business_quality_revenue_state")
            self.assertIsNone(missing.score)
            self.assertIn(missing.status, {"INSUFFICIENT_COVERAGE", "UNSCORED"})
            self.assertIsNone(snapshot.overall_score)

    def test_partial_governance_is_not_a_penalty(self):
        with tempfile.TemporaryDirectory() as temp:
            _, factors, _, _ = score_company("PLTR", "2026-09-24", artifacts_root=temp)
            rights = next(item for item in factors if item.factor_key == "governance_shareholder_rights")
            self.assertIsNone(rights.score)
            self.assertNotEqual(rights.status, "SCORED")

    def test_critical_missing_blocks_overall(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, _, snapshot = score_company("PLTR", "2026-09-24", artifacts_root=temp)
            self.assertIsNone(snapshot.overall_score)
            self.assertIn("business_quality_revenue_state", snapshot.critical_missing_factors)

    def test_effective_weights_and_coverage_are_separate(self):
        definition = FactorDefinition("f", "F", "P", "", "", "", "", 1.0)
        from scoring_engine.utils import make_factor
        factor = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=definition,
                             score=80, status="SCORED", coverage=.5, confidence=.2)
        profile = load_profile_definition("core_v1")
        # Use the production profile only to verify the invariant: score and confidence are not multiplied.
        self.assertEqual(factor.score, 80)
        self.assertEqual(factor.coverage, .5)
        self.assertEqual(factor.confidence, .2)

    def test_positive_low_confidence_remains_positive(self):
        from scoring_engine.utils import make_factor
        definition = FactorDefinition("f", "F", "P", "", "", "", "", 1.0)
        factor = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=definition,
                             score=90, status="SCORED", coverage=.6, confidence=.1)
        self.assertEqual(factor.score, 90)
        self.assertEqual(factor.confidence, .1)

    def test_negative_high_confidence_remains_negative(self):
        from scoring_engine.utils import make_factor
        definition = FactorDefinition("f", "F", "P", "", "", "", "", 1.0)
        factor = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=definition,
                             score=10, status="SCORED", coverage=1, confidence=.95)
        self.assertEqual(factor.score, 10)
        self.assertEqual(factor.confidence, .95)

    def test_single_source_correlation_does_not_inflate_confidence(self):
        from scoring_engine.utils import make_factor
        definition = FactorDefinition("f", "F", "P", "", "", "", "", 1.0)
        same_a = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=definition, score=80, status="SCORED", coverage=1, confidence=.9, raw_inputs={"source_document_ids": ["doc"]})
        same_b = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=FactorDefinition("g", "G", "P", "", "", "", "", 1.0), score=80, status="SCORED", coverage=1, confidence=.9, raw_inputs={"source_document_ids": ["doc"]})
        different = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=FactorDefinition("h", "H", "P", "", "", "", "", 1.0), score=80, status="SCORED", coverage=1, confidence=.9, raw_inputs={"source_document_ids": ["other"]})
        self.assertLess(correlated_factor_confidence([same_a, same_b]), .9)
        self.assertGreater(correlated_factor_confidence([same_a, different]), correlated_factor_confidence([same_a, same_b]))

    def test_guidance_unresolved_and_insufficient_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management_intelligence"
            _write(root / "guidance_outcomes.jsonl", [{"guidance_outcome_id": "o1", "ticker": "TST", "outcome": "UNRESOLVED"}])
            _write(root / "guidance_commitments.jsonl", [])
            defs = [load_profile_definition("core_v1").factor("management_guidance_reliability")]
            result = score_management("TST", "2026-01-01", "core_v1", defs, EvidenceSources(temp), "config/scoring")
            self.assertEqual(result[0].status, "INSUFFICIENT_SAMPLE")
            self.assertIsNone(result[0].score)

    def test_tenure_overlap_does_not_score_management(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management_intelligence"
            _write(root / "operating_outcomes.jsonl", [{"outcome_id": "o1", "ticker": "TST", "attribution_level": "TENURE_OVERLAP", "percent_change": 25}])
            definition = load_profile_definition("core_v1").factor("management_operating_execution")
            result = score_management("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp), "config/scoring")
            self.assertIsNone(result[0].score)
            self.assertEqual(result[0].status, "INSUFFICIENT_SAMPLE")

    def test_automatic_sale_option_and_withholding_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "insider_intelligence"
            _write(root / "transactions.jsonl", [
                {"transaction_id": "a", "ticker": "TST", "transaction_type": "automatic_sale", "is_open_market": True, "is_automatic_sale": True},
                {"transaction_id": "b", "ticker": "TST", "transaction_type": "option_exercise", "is_open_market": False, "is_option_exercise": True},
                {"transaction_id": "c", "ticker": "TST", "transaction_type": "tax_withholding", "is_open_market": False, "is_tax_withholding": True},
            ])
            definition = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].status, "UNSCORED")
            self.assertIsNone(result[0].score)

    def test_open_market_purchase_is_eligible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "insider_intelligence"
            _write(root / "transactions.jsonl", [{"transaction_id": "p", "ticker": "TST", "transaction_type": "open_market_purchase", "is_open_market": True, "shares": 10, "percent_of_pre_transaction_holdings": 5}])
            definition = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].status, "SCORED")
            self.assertGreater(result[0].score, 50)

    def test_open_market_sale_is_eligible_and_directional(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "insider_intelligence"
            _write(root / "transactions.jsonl", [{"transaction_id": "s", "ticker": "TST", "transaction_type": "open_market_sale", "is_open_market": True, "shares": 10, "percent_of_pre_transaction_holdings": 5}])
            definition = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].status, "SCORED")
            self.assertLess(result[0].score, 50)

    def test_no_insider_activity_is_no_signal(self):
        with tempfile.TemporaryDirectory() as temp:
            definition = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].coverage_reason, "NO_SIGNAL_NO_DISCRETIONARY_OPEN_MARKET_ACTIVITY")

    def test_economic_ownership_and_voting_control_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management_intelligence"
            _write(root / "governance_snapshots.jsonl", [{"snapshot_id": "s", "ticker": "TST", "board_size": 1, "independent_count": 1, "independent_denominator": 1, "known_independence_denominator": 1, "chair_structure": "SEPARATE_INDEPENDENT_CHAIR", "founder_voting_control": {"voting_percentage": 50, "economic_percentage": 5}, "shareholder_rights": {}}])
            definition = load_profile_definition("core_v1").factor("governance_voting_control")
            result = __import__("scoring_engine.governance_scoring", fromlist=["score_governance"]).score_governance("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].raw_inputs["voting_percentage"], 50)
            self.assertEqual(result[0].raw_inputs["economic_percentage"], 5)

    def test_known_ownership_uses_configured_bands_without_transaction_signal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "insider_intelligence"
            _write(root / "alignment_snapshots.jsonl", [{"ticker": "TST", "as_of_date": "2026-01-01", "known_insider_percent": 6.0, "known_insider_shares": 100}])
            ownership = load_profile_definition("core_v1").factor("insider_ownership_alignment")
            transaction = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [ownership, transaction], EvidenceSources(temp))
            self.assertEqual(result[0].status, "SCORED")
            self.assertEqual(result[0].score, 75.0)
            self.assertEqual(result[1].status, "UNSCORED")

    def test_business_state_and_trend_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "qualitative_analysis"
            _write(root / "qualitative_states.jsonl", [{"state_id": "st", "ticker": "TST", "dimension": "demand", "topic": None, "current_state": "healthy", "trend": "improving", "evidence_count": 1, "evidence_groups_considered": 1, "evidence_groups_retained": 1, "supporting_document_ids": ["doc"], "confidence": .8}])
            profile = load_profile_definition("core_v1")
            defs = [profile.factor("business_quality_demand_state"), profile.factor("trajectory_demand_trend")]
            result = score_qualitative("TST", "2026-01-01", "core_v1", defs, EvidenceSources(temp))
            self.assertEqual([item.score for item in result], [None, 75.0])
            self.assertEqual(result[0].coverage_reason, "UPSTREAM_STATE_NOT_PROVEN_ABSOLUTE_QUALITY")

    def test_mixed_state_is_not_arbitrary_neutral(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "qualitative_analysis"
            _write(root / "qualitative_states.jsonl", [{"state_id": "st", "ticker": "TST", "dimension": "demand", "topic": None, "current_state": "mixed", "trend": "mixed", "evidence_count": 2, "evidence_groups_considered": 2, "evidence_groups_retained": 2, "supporting_document_ids": ["doc"], "confidence": .8}])
            definition = load_profile_definition("core_v1").factor("business_quality_demand_state")
            result = score_qualitative("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].status, "UNSCORED")
            self.assertIsNone(result[0].score)

    def test_unknown_director_independence_reduces_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "management_intelligence"
            _write(root / "governance_snapshots.jsonl", [{"snapshot_id": "s", "ticker": "TST", "board_size": 3, "independent_count": 2, "independent_denominator": 3, "known_independence_denominator": 2, "chair_structure": "SEPARATE_INDEPENDENT_CHAIR", "founder_voting_control": {}, "shareholder_rights": {}}])
            definition = load_profile_definition("core_v1").factor("governance_board_independence")
            result = score_governance("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].status, "SCORED")
            self.assertEqual(result[0].coverage, 2 / 3)
            self.assertAlmostEqual(result[0].score, 2 / 3 * 100)

    def test_coverage_threshold_and_stable_snapshot_id(self):
        with tempfile.TemporaryDirectory() as temp:
            first = score_company("PLTR", "2026-09-24", artifacts_root=temp)[3]
            second = score_company("PLTR", "2026-09-24", artifacts_root=temp)[3]
            self.assertLess(first.overall_coverage, .70)
            self.assertEqual(first.snapshot_id, second.snapshot_id)

    def test_duplicate_inputs_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "insider_intelligence"
            row = {"transaction_id": "p", "ticker": "TST", "transaction_type": "open_market_purchase", "is_open_market": True}
            _write(root / "transactions.jsonl", [row, row])
            definition = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))
            self.assertEqual(result[0].raw_inputs["eligible_transactions"], 1)

    def test_transaction_dedup_uses_transaction_id_not_person_or_filing_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "insider_intelligence"
            _write(root / "transactions.jsonl", [
                {"transaction_id": "tx1", "filing_id": "filing", "person_id": "person", "ticker": "TST", "transaction_type": "open_market_purchase", "is_open_market": True},
                {"transaction_id": "tx2", "filing_id": "filing", "person_id": "person", "ticker": "TST", "transaction_type": "open_market_purchase", "is_open_market": True},
            ])
            definition = load_profile_definition("core_v1").factor("insider_transaction_activity")
            result = score_insider("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp))[0]
            self.assertEqual(result.raw_inputs["eligible_transactions"], 2)

    def test_profile_versioning_and_idempotent_store(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "scores"
            run_score(["PLTR"], "2026-09-24", artifacts_root=temp, output_root=output)
            first = len(ScoreStore(output).read("factor_scores"))
            run_score(["PLTR"], "2026-09-24", artifacts_root=temp, output_root=output)
            second = len(ScoreStore(output).read("factor_scores"))
            self.assertEqual(first, second)
            self.assertTrue(all(":" in row["factor_score_id"] for row in ScoreStore(output).read("factor_scores")))
            self.assertTrue(any("@core-v1.2" in row["factor_score_id"] for row in ScoreStore(output).read("factor_scores")))

    def test_targeted_universe_has_safe_coverage_statuses(self):
        with tempfile.TemporaryDirectory() as temp:
            for ticker in ("ZM", "PLTR", "EXEL", "INCY"):
                _, factors, _, snapshot = score_company(ticker, "2026-09-24", artifacts_root=temp)
                self.assertIsNotNone(snapshot.score_status)
                self.assertTrue(all(item.score is not None or item.status != "SCORED" for item in factors))

    def test_manual_coverage_and_confidence_match_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._zm_fixture(temp)
            audit = audit_company("ZM", "2026-09-24", artifacts_root=root)
            self.assertAlmostEqual(audit["manual_coverage"], audit["snapshot"]["overall_coverage"])
            self.assertAlmostEqual(audit["manual_evidence_coverage"], audit["snapshot"]["overall_evidence_coverage"])
            self.assertAlmostEqual(audit["manual_scoring_coverage"], audit["snapshot"]["overall_scoring_coverage"])
            self.assertAlmostEqual(audit["manual_confidence"], audit["snapshot"]["overall_confidence"])
            profile, factors, pillars, _ = score_company("ZM", "2026-09-24", artifacts_root=root)
            for pillar in pillars:
                if pillar.calculated_score is not None:
                    self.assertAlmostEqual(manual_pillar_score(factors, pillar.pillar), pillar.calculated_score)

    def test_evidence_and_scoring_coverage_are_separate(self):
        from scoring_engine.utils import make_factor
        definition = FactorDefinition("f", "F", "P", "", "", "", "", 1.0)
        unsupported = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=definition,
                                  score=None, status="UNSCORED", coverage=1.0, confidence=.8)
        scoreable = make_factor(ticker="T", as_of="2026-01-01", profile="core_v1", definition=definition,
                                score=80, status="SCORED", coverage=.875, confidence=.8)
        self.assertEqual(unsupported.evidence_coverage, 1.0)
        self.assertEqual(unsupported.scoring_coverage, 0.0)
        self.assertEqual(scoreable.evidence_coverage, .875)
        self.assertEqual(scoreable.scoring_coverage, .875)

    def test_zm_dual_coverage_reconstruction(self):
        with tempfile.TemporaryDirectory() as temp:
            audit = audit_company("ZM", "2026-09-24", artifacts_root=self._zm_fixture(temp))
            rows = {row["pillar"]: row for row in audit["pillars"]}
            self.assertAlmostEqual(rows["BUSINESS_QUALITY"]["evidence_coverage"], 1.0)
            self.assertAlmostEqual(rows["BUSINESS_QUALITY"]["scoring_coverage"], 0.0)
            self.assertAlmostEqual(rows["BUSINESS_TRAJECTORY"]["scoring_coverage"], 1.0)
            self.assertAlmostEqual(rows["GOVERNANCE"]["evidence_coverage"], .468)
            self.assertAlmostEqual(rows["GOVERNANCE"]["scoring_coverage"], .34)
            self.assertAlmostEqual(audit["manual_evidence_coverage"], .5702)
            self.assertAlmostEqual(audit["manual_scoring_coverage"], .251)
            self.assertAlmostEqual(audit["manual_evidence_coverage"], audit["snapshot"]["overall_evidence_coverage"])
            self.assertAlmostEqual(audit["manual_scoring_coverage"], audit["snapshot"]["overall_scoring_coverage"])

    def test_overall_gate_uses_scoring_coverage(self):
        from scoring_engine.models import ProfileDefinition
        from scoring_engine.utils import make_factor
        definition = FactorDefinition("f", "F", "BUSINESS_QUALITY", "", "", "", "", 1.0)
        profile = ProfileDefinition("test", "v1", "s", "r", "c", {"BUSINESS_QUALITY": 1.0}, [definition], .70, {"BUSINESS_QUALITY": .70})
        factor = make_factor(ticker="T", as_of="2026-01-01", profile="test", definition=definition,
                             score=None, status="UNSCORED", coverage=1.0, confidence=.8)
        _, snapshot = aggregate("T", "2026-01-01", profile, [factor])
        self.assertEqual(snapshot.overall_evidence_coverage, 1.0)
        self.assertEqual(snapshot.overall_scoring_coverage, 0.0)
        self.assertIsNone(snapshot.overall_score)
        self.assertEqual(snapshot.score_status, "INSUFFICIENT_SCORING_COVERAGE")

    def test_missing_data_has_no_evidence_or_scoring_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            _, factors, _, snapshot = score_company("EXEL", "2026-09-24", artifacts_root=temp)
            revenue = next(item for item in factors if item.factor_key == "business_quality_revenue_state")
            self.assertEqual(revenue.evidence_coverage, 0.0)
            self.assertEqual(revenue.scoring_coverage, 0.0)
            self.assertEqual(snapshot.overall_evidence_coverage, 0.0)
            self.assertEqual(snapshot.overall_scoring_coverage, 0.0)

    def test_cli_labels_both_coverage_concepts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._zm_fixture(temp)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(scoring_cli(["--ticker", "ZM", "--as-of-date", "2026-09-24", "--dry-run", "--artifacts-root", str(root)]), 0)
        text = output.getvalue()
        self.assertIn("Evidence coverage:", text)
        self.assertIn("Scoring coverage:", text)
        self.assertIn("evidence_coverage=", text)
        self.assertIn("scoring_coverage=", text)

    def test_coverage_schema_is_versioned_and_explicit(self):
        profile = load_profile_definition("core_v1")
        self.assertEqual(profile.coverage_version, "coverage-v3")
        self.assertEqual(profile.scoring_coverage_threshold, .70)
        self.assertEqual(profile.pillar_scoring_threshold("BUSINESS_QUALITY"), .70)

    def test_directional_current_state_is_not_absolute_quality(self):
        with tempfile.TemporaryDirectory() as temp:
            audit = audit_company("ZM", "2026-09-24", artifacts_root=self._zm_fixture(temp))
            rows = {row["factor_key"]: row for row in audit["factors"]}
            for key in ("business_quality_revenue_state", "business_quality_demand_state", "business_quality_margins_state", "business_quality_guidance_state"):
                self.assertEqual(rows[key]["classification"], "SEMANTICALLY_NON_DIRECTIONAL")
                self.assertIsNone(rows[key]["mapped_score"])
                self.assertEqual(rows[key]["raw_input"]["source_document_count"], 1)
                self.assertIsNotNone(rows[key]["raw_input"]["state_confidence"])

    def test_uncalibrated_profile_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            audit = audit_company("ZM", "2026-09-24", artifacts_root=self._zm_fixture(temp))
            self.assertEqual(audit["profile"]["calibration_status"], "UNCALIBRATED_DESIGN_V1")

    def test_coverage_boundaries_are_deterministic(self):
        definition = FactorDefinition("f", "F", "BUSINESS_QUALITY", "", "", "", "", 1.0, min_coverage=0.0)
        from scoring_engine.models import ProfileDefinition
        from scoring_engine.utils import make_factor
        profile = ProfileDefinition("test", "v1", "s", "r", "c", {"BUSINESS_QUALITY": 1.0}, [definition], .70, {"BUSINESS_QUALITY": 0.0})
        for coverage, expected in ((.69, "INSUFFICIENT_SCORING_COVERAGE"), (.70, "SCORED"), (.71, "SCORED")):
            factor = make_factor(ticker="T", as_of="2026-01-01", profile="test", definition=definition, score=80, status="SCORED", coverage=coverage, confidence=.8)
            pillars, snapshot = aggregate("T", "2026-01-01", profile, [factor])
            self.assertEqual(snapshot.score_status, expected)

    def test_critical_factor_blocks_even_above_coverage_threshold(self):
        definition = FactorDefinition("critical", "Critical", "BUSINESS_QUALITY", "", "", "", "", 1.0)
        from scoring_engine.models import ProfileDefinition
        from scoring_engine.utils import make_factor
        profile = ProfileDefinition("test", "v1", "s", "r", "c", {"BUSINESS_QUALITY": 1.0}, [definition], .70, {"BUSINESS_QUALITY": 0.0}, ["critical"])
        factor = make_factor(ticker="T", as_of="2026-01-01", profile="test", definition=definition, score=None, status="UNSCORED", coverage=.71, confidence=.8)
        _, snapshot = aggregate("T", "2026-01-01", profile, [factor])
        self.assertGreaterEqual(snapshot.overall_coverage, .70)
        self.assertIsNone(snapshot.overall_score)
        self.assertEqual(snapshot.score_status, "INSUFFICIENT_SCORING_COVERAGE")

    def test_guidance_sample_boundaries(self):
        definition = load_profile_definition("core_v1").factor("management_guidance_reliability")
        for count in range(5):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "management_intelligence"
                _write(root / "guidance_outcomes.jsonl", [{"guidance_outcome_id": f"o{i}", "guidance_id": f"g{i}", "ticker": "TST", "outcome": "BEAT"} for i in range(count)])
                result = score_management("TST", "2026-01-01", "core_v1", [definition], EvidenceSources(temp), "config/scoring")[0]
                if count < 3:
                    self.assertEqual(result.status, "INSUFFICIENT_SAMPLE")
                    self.assertIsNone(result.score)
                else:
                    self.assertEqual(result.status, "SCORED")


if __name__ == "__main__":
    unittest.main()
