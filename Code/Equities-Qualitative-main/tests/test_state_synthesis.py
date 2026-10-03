from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qualitative_analysis.extraction_models import QualitativeClaim
from qualitative_analysis.state_models import QualitativeState
from qualitative_analysis.state_store import StateStore
from qualitative_analysis.state_synthesis import DeterministicStateSynthesisProvider
from qualitative_analysis.temporal_models import TemporalChange


def _claim(
    claim_id: str,
    *,
    ticker: str = "ZM",
    period: str = "Q2 2027",
    year: int = 2027,
    quarter: int = 2,
    dimension: str = "demand",
    topic: str = "demand",
    text: str = "Demand is improving.",
    direction: str = "improving",
    certainty: str = "actual",
    period_type: str = "QUARTER_ACTUAL",
) -> QualitativeClaim:
    return QualitativeClaim(
        claim_id=claim_id,
        ticker=ticker,
        company_name="Zoom" if ticker == "ZM" else "Incyte",
        document_id=f"doc-{ticker}-{period.replace(' ', '-')}",
        document_type="earnings_transcript",
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_label=period,
        dimension=dimension,
        topic=topic,
        subtopic=None,
        claim_text=text,
        direction=direction,
        magnitude=None,
        certainty=certainty,
        evidence_text=text,
        source_location={"start_char": 0, "end_char": len(text), "segment_ids": [0]},
        source_url=f"https://example.test/{ticker}/{period}",
        local_path=f"artifacts/{ticker}/{period}.json",
        extraction_method="test",
        extraction_confidence=0.9,
        created_at="2027-01-01T00:00:00+00:00",
        source_content_hash=f"hash-{claim_id}",
        extraction_version="qualitative-extraction-v8",
        provider_version="deterministic-rules-v8",
        rules_version="rules-v8",
        period_type=period_type,
    )


def _change(
    change_id: str,
    *,
    dimension: str = "demand",
    subject: str = "demand",
    direction: str = "improving",
    change_type: str = "improving",
    to_claim_ids: list[str] | None = None,
) -> TemporalChange:
    return TemporalChange(
        change_id=change_id,
        ticker="ZM",
        company_name="Zoom",
        dimension=dimension,
        topic=dimension,
        subtopic=None,
        from_period="Q1 2027",
        to_period="Q2 2027",
        change_type=change_type,
        direction=direction,
        summary="Demand changed between periods.",
        from_claim_ids=[],
        to_claim_ids=to_claim_ids or [],
        from_evidence=[],
        to_evidence=[],
        confidence=0.8,
        comparison_method="test",
        comparison_version="temporal-comparison-v2",
        created_at="2027-01-01T00:00:00+00:00",
        normalized_subject=subject,
        rules_version="temporal-rules-v2",
        provider="deterministic-temporal-v2",
    )


def _dimension_state(result, dimension: str, topic: str | None = None):
    return next(state for state in result.states if state.dimension == dimension and state.topic == topic)


class StateSynthesisTests(unittest.TestCase):
    def test_current_state_ignores_full_year_guidance_horizon(self):
        claims = [
            _claim("q1", period="Q1 2026", year=2026, quarter=1, text="Demand was stable.", direction="stable"),
            _claim("q2", period="Q2 2026", year=2026, quarter=2, text="Demand is improving."),
            _claim("fy", period="FY 2026", year=2026, quarter=0, text="FY 2026 guidance was maintained.", period_type="GUIDANCE_HORIZON"),
        ]
        result = DeterministicStateSynthesisProvider().synthesize(claims, [])
        self.assertEqual(result.period_label, "Q2 2026")
    def test_current_state_uses_latest_period_claims(self):
        previous = _claim("old", period="Q1 2027", quarter=1, text="Demand deteriorated.", direction="deteriorating")
        current = _claim("new", text="Demand improved.", direction="improving")
        result = DeterministicStateSynthesisProvider().synthesize([previous, current], [])
        state = _dimension_state(result, "demand")
        self.assertIn(state.current_state, {"healthy", "strong"})
        self.assertEqual(state.period_label, "Q2 2027")
        self.assertEqual(state.supporting_claim_ids, ["new"])

    def test_temporal_change_drives_trend(self):
        claim = _claim("current")
        provider = DeterministicStateSynthesisProvider()
        baseline = _dimension_state(provider.synthesize([claim], []), "demand")
        result = provider.synthesize([claim], [_change("change", to_claim_ids=["current"])])
        state = _dimension_state(result, "demand")
        self.assertEqual(state.current_state, baseline.current_state)
        self.assertEqual(state.trend, "improving")
        self.assertEqual(state.supporting_change_ids, ["change"])

    def test_one_period_without_explicit_comparison_has_unknown_trend(self):
        claim = _claim("only", text="Demand is positive.", direction="positive")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([claim], []), "demand")
        self.assertEqual(state.trend, "unknown")

    def test_healthy_state_can_have_deteriorating_trend(self):
        claim = _claim("current", text="Demand remains strong.", direction="positive")
        change = _change("change", direction="deteriorating", change_type="deteriorating", to_claim_ids=["current"])
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([claim], [change]), "demand")
        self.assertIn(state.current_state, {"healthy", "strong"})
        self.assertEqual(state.trend, "deteriorating")

    def test_contradictory_evidence_is_mixed_and_preserved(self):
        claims = [
            _claim("up", text="Enterprise demand increased.", direction="increasing"),
            _claim("down", text="SMB demand decreased.", direction="decreasing", topic="demand"),
        ]
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize(claims, []), "demand")
        self.assertEqual(state.current_state, "mixed")
        self.assertEqual(set(state.supporting_claim_ids), {"up", "down"})
        self.assertTrue(state.contradicting_claim_ids)
        self.assertEqual(state.contradiction_count, 1)

    def test_subtopics_aggregate_to_mixed_dimension(self):
        claims = [
            _claim("enterprise", topic="enterprise", text="Enterprise demand increased.", direction="increasing"),
            _claim("smb", topic="smb", text="SMB demand decreased.", direction="decreasing"),
        ]
        result = DeterministicStateSynthesisProvider().synthesize(claims, [])
        self.assertEqual(_dimension_state(result, "demand").current_state, "mixed")

    def test_actual_result_outweighs_vague_possibility(self):
        claims = [
            _claim("actual", text="Demand decreased in the quarter.", direction="decreasing", certainty="actual"),
            _claim("possible", text="Demand may improve later.", direction="improving", certainty="possibility"),
        ]
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize(claims, []), "demand")
        self.assertIn(state.current_state, {"weak", "stressed"})
        self.assertIn("possible", state.contradicting_claim_ids)

    def test_duplicate_evidence_is_not_double_counted(self):
        claims = [
            _claim("a", text="Demand increased.", direction="increasing"),
            _claim("b", text="Demand increased.", direction="increasing"),
        ]
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize(claims, []), "demand")
        self.assertEqual(state.evidence_count, 1)
        self.assertEqual(state.duplicate_groups_removed, 1)
        self.assertEqual(set(state.supporting_claim_ids), {"a", "b"})

    def test_missing_evidence_is_not_neutral(self):
        claim = _claim("unknown", text="Management may discuss demand later.", direction="unknown", certainty="possibility")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([claim], []), "demand")
        self.assertEqual(state.current_state, "unknown")

    def test_directional_growth_does_not_automatically_mean_strong(self):
        claim = _claim("growth", text="Revenue increased 6% year over year.", direction="increasing", dimension="revenue")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([claim], []), "revenue")
        self.assertEqual(state.current_state, "healthy")
        self.assertIsNone(state.unknown_reason)

    def test_risk_mention_requires_explicit_impact_for_elevated_state(self):
        mention = _claim("risk", dimension="risks", topic="risks", text="A regulatory risk remains.", direction="deteriorating", certainty="risk")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([mention], []), "risks")
        self.assertEqual(state.current_state, "unknown")
        self.assertEqual(state.unknown_reason, "direction_without_absolute_anchor")

        impact = _claim("impact", dimension="risks", topic="risks", text="A headwind has a 40 bps impact to revenue.", direction="deteriorating", certainty="risk")
        impacted = _dimension_state(DeterministicStateSynthesisProvider().synthesize([impact], []), "risks")
        self.assertEqual(impacted.current_state, "elevated")

    def test_churn_metric_mention_is_not_stressed_without_direction(self):
        mention = _claim("churn", dimension="customer_retention", topic="retention", text="We monitor the online churn rate.", direction="unknown", certainty="estimate")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([mention], []), "customer_retention")
        self.assertEqual(state.current_state, "unknown")
        rising = _claim("rising-churn", dimension="customer_retention", topic="retention", text="Online churn increased this quarter.", direction="deteriorating", certainty="actual")
        rising_state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([rising], []), "customer_retention")
        self.assertEqual(rising_state.current_state, "stressed")

    def test_cost_increases_are_negative_for_cost_dimension(self):
        claim = _claim("cost-up", dimension="costs", topic="costs", text="Expenses increased 10%.", direction="increasing")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([claim], []), "costs")
        self.assertEqual(state.current_state, "weak")

    def test_guidance_metric_conflict_is_mixed(self):
        claims = [
            _claim("raise", dimension="guidance", topic="guidance", text="We raised revenue guidance.", direction="raised", certainty="guidance"),
            _claim("lower", dimension="guidance", topic="guidance", text="We lowered cost guidance.", direction="lowered", certainty="guidance"),
        ]
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize(claims, []), "guidance")
        self.assertEqual(state.current_state, "mixed")
        self.assertEqual(set(state.supporting_claim_ids), {"raise", "lower"})

    def test_low_value_sentence_topics_are_omitted_and_parent_records_children(self):
        claims = [
            _claim("ai", dimension="product", topic="product", text="We launched an AI product.", direction="positive"),
            _claim("fragment", dimension="product", topic="product", text="Employee engagement is becoming important.", direction="improving"),
        ]
        result = DeterministicStateSynthesisProvider().synthesize(claims, [])
        parent = _dimension_state(result, "product")
        self.assertEqual([state.topic for state in result.states if state.dimension == "product" and state.topic], ["ai"])
        self.assertEqual(parent.child_state_ids, [_dimension_state(result, "product", "ai").state_id])

    def test_confidence_decreases_with_contradiction_and_increases_with_independent_agreement(self):
        one = _dimension_state(DeterministicStateSynthesisProvider().synthesize([_claim("one")], []), "demand")
        three = _dimension_state(DeterministicStateSynthesisProvider().synthesize([_claim("a", text="Demand increased in enterprise.", topic="enterprise"), _claim("b", text="Demand increased in channels.", topic="channels"), _claim("c", text="Demand increased in products.", topic="products")], []), "demand")
        mixed = _dimension_state(DeterministicStateSynthesisProvider().synthesize([_claim("up", direction="increasing"), _claim("down", text="Demand decreased.", direction="decreasing")], []), "demand")
        self.assertGreater(three.confidence, one.confidence)
        self.assertLess(mixed.confidence, one.confidence)

    def test_single_document_confidence_is_capped_without_cross_source_support(self):
        claims = [
            _claim("a", text="Demand increased in enterprise.", topic="enterprise"),
            _claim("b", text="Demand increased in channels.", topic="channels"),
            _claim("c", text="Demand increased in products.", topic="products"),
        ]
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize(claims, []), "demand")
        self.assertLessEqual(state.confidence, 0.90)
        self.assertEqual(state.confidence_factors["source_document_count"], 1)

    def test_state_id_and_provenance_are_stable(self):
        claim = _claim("current")
        provider = DeterministicStateSynthesisProvider()
        first = _dimension_state(provider.synthesize([claim], []), "demand")
        second = _dimension_state(provider.synthesize([claim], []), "demand")
        self.assertEqual(first.state_id, second.state_id)
        self.assertEqual(first.supporting_document_ids, [claim.document_id])
        self.assertEqual(first.source_urls, [claim.source_url])
        self.assertEqual(first.source_local_paths, [claim.local_path])

    def test_store_is_idempotent_and_queries(self):
        claim = _claim("current")
        state = _dimension_state(DeterministicStateSynthesisProvider().synthesize([claim], []), "demand")
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp))
            self.assertEqual(store.upsert_many([state])["added"], 1)
            self.assertEqual(store.upsert_many([state])["unchanged"], 1)
            self.assertEqual(len(store.query(ticker="ZM", dimension="demand", period_label="Q2 2027")), 1)
            self.assertEqual(store.invalidate_scope("ZM", "Q2 2027", "qualitative-state-v0"), 1)
            self.assertEqual(store.list_all(), [])


if __name__ == "__main__":
    unittest.main()
