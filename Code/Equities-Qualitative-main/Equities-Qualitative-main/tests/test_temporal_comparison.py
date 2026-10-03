from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qualitative_analysis.extraction_models import QualitativeClaim
from qualitative_analysis.temporal_comparison import (
    DeterministicTemporalComparisonProvider,
    claims_available_as_of,
    compare_latest_vs_previous,
    period_sort_key,
    stable_change_id,
)
from qualitative_analysis.temporal_rules import classify_matched, extract_metric
from qualitative_analysis.temporal_store import TemporalStore
from qualitative_analysis.topic_normalization import normalize_topic, topic_identity


def _claim(
    claim_id: str,
    *,
    period: str = "Q1 2027",
    year: int = 2027,
    quarter: int = 1,
    dimension: str = "guidance",
    topic: str = "revenue",
    text: str = "Revenue guidance was reiterated.",
    direction: str = "unknown",
    certainty: str = "guidance",
    magnitude=None,
    period_type: str = "QUARTER_ACTUAL",
    guidance_horizon: str | None = None,
) -> QualitativeClaim:
    return QualitativeClaim(
        claim_id=claim_id,
        ticker="ZM",
        company_name="Zoom",
        document_id=f"doc-{period.replace(' ', '-')}",
        document_type="earnings_transcript",
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_label=period,
        dimension=dimension,
        topic=topic,
        subtopic=None,
        claim_text=text,
        direction=direction,
        magnitude=magnitude,
        certainty=certainty,
        evidence_text=text,
        source_location={"start_char": 0, "end_char": len(text), "segment_ids": [0], "start_timestamp": 1.0, "end_timestamp": 2.0},
        source_url="https://example.test/transcript",
        local_path="transcript.json",
        extraction_method="test",
        extraction_confidence=0.9,
        created_at="2027-01-01T00:00:00+00:00",
        source_content_hash=f"hash-{claim_id}",
        extraction_version="qualitative-extraction-v8",
        provider_version="deterministic-rules-v8",
        rules_version="rules-v8",
        period_type=period_type,
        guidance_horizon=guidance_horizon,
    )


class TemporalComparisonTests(unittest.TestCase):
    def test_known_future_availability_is_excluded_without_assuming_unknown_dates(self):
        future = _claim("future")
        future.available_date = "2026-09-26"
        unknown = _claim("unknown", period="Q2 2027", quarter=2)
        unknown.available_date = None
        admitted = claims_available_as_of([future, unknown], "2026-09-25")
        self.assertEqual([claim.claim_id for claim in admitted], ["unknown"])
    def test_guidance_horizon_does_not_create_fy_sequential_period(self):
        claims = [
            _claim("q1", period="Q1 2026", year=2026, quarter=1, text="Revenue grew in Q1."),
            _claim("q2", period="Q2 2026", year=2026, quarter=2, text="Revenue grew in Q2.", guidance_horizon="FY 2026"),
            _claim("fy-guidance", period="FY 2026", year=2026, quarter=0, text="FY 2026 guidance was maintained.", period_type="GUIDANCE_HORIZON"),
        ]
        result = compare_latest_vs_previous("ZM", claims)
        self.assertEqual((result.from_period, result.to_period), ("Q1 2026", "Q2 2026"))
        self.assertNotEqual((result.from_period, result.to_period), ("Q2 2026", "FY 2026"))
    def test_topic_normalization_is_conservative(self):
        self.assertEqual(normalize_topic("Full-year revenue guidance", dimension="guidance"), "revenue")
        self.assertEqual(normalize_topic("Enterprise customer demand", dimension="demand"), "enterprise")
        self.assertNotEqual(normalize_topic("Operating margin", dimension="margins"), normalize_topic("Free cash flow margin", dimension="margins"))

    def test_numeric_guidance_raised_lowered_and_reiterated(self):
        previous = _claim("before", text="Full-year revenue guidance was $1.8 billion.", magnitude={"raw": "$1.8 billion", "value": 1.8, "unit": "USD_billion"})
        raised = _claim("after-raised", period="Q2 2027", quarter=2, text="Full-year revenue guidance is $1.9 billion.", magnitude={"raw": "$1.9 billion", "value": 1.9, "unit": "USD_billion"})
        lowered = _claim("after-lowered", period="Q2 2027", quarter=2, text="Full-year revenue guidance is $1.7 billion.", magnitude={"raw": "$1.7 billion", "value": 1.7, "unit": "USD_billion"})
        same = _claim("after-same", period="Q2 2027", quarter=2, text="Full-year revenue guidance remains $1.8 billion.", magnitude={"raw": "$1.8 billion", "value": 1.8, "unit": "USD_billion"})
        self.assertEqual(classify_matched(previous, raised)["change_type"], "raised")
        self.assertEqual(classify_matched(previous, lowered)["change_type"], "lowered")
        self.assertEqual(classify_matched(previous, same)["change_type"], "reiterated")

    def test_qualitative_direction_changes(self):
        previous = _claim("before", dimension="demand", topic="demand", text="Demand was stable.", direction="stable", certainty="unknown")
        current = _claim("after", period="Q2 2027", quarter=2, dimension="demand", topic="demand", text="Demand is improving.", direction="improving", certainty="unknown")
        self.assertEqual(classify_matched(previous, current)["change_type"], "improving")
        current.direction = "deteriorating"
        self.assertEqual(classify_matched(previous, current)["change_type"], "deteriorating")

    def test_newly_mentioned_differs_from_newly_occurred_and_silence_is_not_removed(self):
        current = _claim("new", period="Q2 2027", quarter=2, dimension="product", topic="product", text="Customers discussed a possible new product.", certainty="possibility")
        occurred = _claim("occurred", period="Q2 2027", quarter=2, dimension="product", topic="product", text="The product launched in June.", certainty="confirmed")
        provider = DeterministicTemporalComparisonProvider()
        result = provider.compare([], [current, occurred], "Q1 2027", "Q2 2027")
        types = {change.change_type for change in result.changes}
        self.assertIn("newly_mentioned", types)
        self.assertIn("newly_occurred", types)
        self.assertEqual(provider.compare([current], [], "Q1 2027", "Q2 2027").changes, [])

    def test_risk_new_and_explicit_removal(self):
        previous = _claim("risk-before", dimension="risks", topic="risk", text="Customer concentration risk remains.", direction="deteriorating", certainty="risk")
        current = _claim("risk-after", period="Q2 2027", quarter=2, dimension="risks", topic="risk", text="Customer concentration risk has been resolved.", direction="improving", certainty="confirmed")
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes[0].change_type, "risk_removed")

    def test_mixed_evidence_preserves_all_claim_ids(self):
        previous = [
            _claim("p-up", dimension="demand", topic="demand", text="Demand increased.", direction="increasing", certainty="actual"),
            _claim("p-down", dimension="demand", topic="demand", text="Demand decreased in another segment.", direction="decreasing", certainty="actual"),
        ]
        current = [_claim("c", period="Q2 2027", quarter=2, dimension="demand", topic="demand", text="Demand remains mixed.", direction="mixed", certainty="unknown")]
        result = DeterministicTemporalComparisonProvider().compare(previous, current, "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes[0].change_type, "mixed")
        self.assertEqual(set(result.changes[0].from_claim_ids), {"p-up", "p-down"})

    def test_incompatible_units_are_rejected(self):
        previous = _claim("before", text="Revenue guidance was $1.8 billion.", magnitude={"raw": "$1.8 billion", "value": 1.8, "unit": "USD_billion"})
        current = _claim("after", period="Q2 2027", quarter=2, text="Revenue guidance was up 4%.", magnitude={"raw": "4%", "value": 4, "unit": "percent"})
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.changes, [])

    def test_explicit_metric_mismatch_is_rejected_even_when_subject_normalizes_together(self):
        previous = _claim("before", topic="outlook", text="For the full year, we raised profitability guidance.", direction="raised")
        current = _claim("after", period="Q2 2027", quarter=2, topic="outlook", text="For the full year, we raised EPS guidance.", direction="raised")
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes, [])
        self.assertIn("incompatible explicit metric", result.rejected[0])

    def test_fiscal_horizon_mismatch_is_rejected(self):
        previous = _claim("before", topic="revenue", text="For Q2, deferred revenue is expected to grow 2 to 3%.", magnitude={"raw": "3%", "value": 3, "unit": "percent"})
        current = _claim("after", period="Q2 2027", quarter=2, topic="revenue", text="For Q3, deferred revenue is expected to grow 3 to 4%.", magnitude={"raw": "4%", "value": 4, "unit": "percent"})
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes, [])
        self.assertIn("incompatible fiscal horizon", result.rejected[0])

    def test_gaap_and_non_gaap_are_not_compared(self):
        previous = _claim("before", topic="outlook", text="GAAP operating income was $100 million.", magnitude={"raw": "$100 million", "value": 100, "unit": "USD_million"})
        current = _claim("after", period="Q2 2027", quarter=2, topic="outlook", text="Non-GAAP operating income was $110 million.", magnitude={"raw": "$110 million", "value": 110, "unit": "USD_million"})
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes, [])
        self.assertIn("incompatible reporting basis", result.rejected[0])

    def test_disaggregated_scope_changes_are_insufficient_evidence(self):
        previous = _claim("before", dimension="revenue", topic="revenue", text="Americas revenue grew 5% and EMEA revenue grew 5%.", direction="increasing", certainty="actual", magnitude={"values": [{"raw": "5%", "unit": "percent", "value": 5}, {"raw": "5%", "unit": "percent", "value": 5}]})
        current = _claim("after", period="Q2 2027", quarter=2, dimension="revenue", topic="revenue", text="Americas revenue grew 6% and EMEA revenue grew 2%.", direction="increasing", certainty="actual", magnitude={"values": [{"raw": "6%", "unit": "percent", "value": 6}, {"raw": "2%", "unit": "percent", "value": 2}]})
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes, [])
        self.assertIn("insufficient evidence", result.rejected[0])

    def test_same_subject_current_claims_are_consolidated_once(self):
        current = [
            _claim("new-a", period="Q2 2027", quarter=2, dimension="product", topic="product", text="We launched a new AI product.", certainty="confirmed"),
            _claim("new-b", period="Q2 2027", quarter=2, dimension="product", topic="product", text="The new AI product expanded to customers.", certainty="unknown"),
        ]
        result = DeterministicTemporalComparisonProvider().compare([], current, "Q1 2027", "Q2 2027")
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(set(result.changes[0].to_claim_ids), {"new-a", "new-b"})

    def test_generic_catalyst_without_a_concrete_fact_is_not_persisted(self):
        current = _claim("catalyst", period="Q2 2027", quarter=2, dimension="catalysts", topic="catalyst", text="This gives us an opportunity to land and expand.")
        result = DeterministicTemporalComparisonProvider().compare([], [current], "Q1 2027", "Q2 2027")
        self.assertEqual(result.changes, [])
        self.assertIn("low-value generic current-only mention", result.rejected[0])

    def test_actual_result_summary_does_not_claim_guidance_change(self):
        previous = _claim("before", text="The result was $14 million above the high end of guidance.", certainty="actual", magnitude={"raw": "$14 million", "value": 14, "unit": "USD_million"})
        current = _claim("after", period="Q2 2027", quarter=2, text="The result was $7 million above the high end of guidance.", certainty="actual", magnitude={"raw": "$7 million", "value": 7, "unit": "USD_million"})
        classification = classify_matched(previous, current)
        self.assertEqual(classification["change_type"], "insufficient_evidence")
        self.assertNotIn("raised", classification["summary"].lower())

    def test_period_ordering_and_one_period_insufficient(self):
        self.assertLess(period_sort_key("Q4 FY2025"), period_sort_key("Q1 FY2026"))
        self.assertEqual(compare_latest_vs_previous("ZM", [_claim("only")]).status, "INSUFFICIENT_PERIODS")

    def test_stable_ids_store_and_evidence_are_idempotent(self):
        previous = _claim("before", text="Revenue guidance was $1.8 billion.", magnitude={"raw": "$1.8 billion", "value": 1.8, "unit": "USD_billion"})
        current = _claim("after", period="Q2 2027", quarter=2, text="Revenue guidance was $1.9 billion.", magnitude={"raw": "$1.9 billion", "value": 1.9, "unit": "USD_billion"})
        result = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027")
        change = result.changes[0]
        self.assertTrue(change.from_evidence and change.to_evidence)
        self.assertEqual(change.comparison_version, "temporal-comparison-v2")
        self.assertEqual(change.rules_version, "temporal-rules-v2")
        self.assertEqual(change.change_id, stable_change_id("ZM", change.normalized_subject or "", "Q1 2027", "Q2 2027", ["before"], ["after"]))
        with tempfile.TemporaryDirectory() as temp:
            store = TemporalStore(Path(temp))
            first = store.upsert_many([change])
            second = store.upsert_many([change])
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(len(store.get_by_ticker("ZM")), 1)
            self.assertEqual(store.remove_scope("ZM", "Q1 2027", "Q2 2027"), 1)

    def test_comparison_version_invalidation_is_scoped(self):
        previous = _claim("before", text="Revenue guidance was $1.8 billion.", magnitude={"raw": "$1.8 billion", "value": 1.8, "unit": "USD_billion"})
        current = _claim("after", period="Q2 2027", quarter=2, text="Revenue guidance was $1.9 billion.", magnitude={"raw": "$1.9 billion", "value": 1.9, "unit": "USD_billion"})
        change = DeterministicTemporalComparisonProvider().compare([previous], [current], "Q1 2027", "Q2 2027").changes[0]
        change.comparison_version = "temporal-comparison-v0"
        with tempfile.TemporaryDirectory() as temp:
            store = TemporalStore(Path(temp))
            store.upsert_many([change])
            self.assertEqual(store.invalidate_scope("ZM", "Q1 2027", "Q2 2027", "temporal-comparison-v1"), 1)
            self.assertEqual(store.list_all(), [])


if __name__ == "__main__":
    unittest.main()
