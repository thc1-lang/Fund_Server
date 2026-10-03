import tempfile
import unittest

from insider_intelligence.models import SECFiling
from management_intelligence.attribution import classify_attribution, validate_attribution
from management_intelligence.capital_allocation import extract_capital_events
from management_intelligence.guidance_track_record import diagnose_guidance_coverage, match_guidance_to_actuals
from management_intelligence.models import CareerEntry, ManagementPerson, PersonCompanyRelationship, RoleAssertion, RoleChangeEvent
from management_intelligence.operating_outcomes import build_operating_outcomes, normalize_company_facts, period_status
from management_intelligence.store import ManagementStore
from management_intelligence.strategic_commitments import extract_strategic_commitments, resolve_strategic_outcomes
from management_intelligence.tenure import build_tenures
from management_intelligence.track_record_models import Attribution, FinancialFact, GuidanceCommitment, ManagementTenure, StrategicCommitment
from management_intelligence.track_record_store import TrackRecordStore


class TrackRecordTests(unittest.TestCase):
    def _seed_store(self):
        store = ManagementStore(tempfile.mkdtemp())
        person = ManagementPerson(
            person_id="person:alice", ticker="TST", company_name="TestCo", full_name="Alice Example", normalized_name="alice example", issuer_cik="0000000001", current_roles=["Chief Executive Officer"],
            source_ids=["sec:proxy"], source_urls=["https://sec.test/proxy"], source_local_paths=["cache/proxy.htm"],
        )
        role = RoleAssertion(
            assertion_id="role:alice", person_id=person.person_id, ticker="TST", issuer_cik="0000000001", role="Chief Executive Officer", role_category="CEO", is_current=True, start_date="2024-07", date_precision="month", source_ids=["sec:proxy"], source_urls=["https://sec.test/proxy"], source_local_paths=["cache/proxy.htm"], evidence_text="Alice Example has served as Chief Executive Officer since July 2024.",
        )
        relationship = PersonCompanyRelationship(
            person_company_id="relationship:person:alice:0000000001", person_id=person.person_id, issuer_cik="0000000001", ticker="TST", company_name="TestCo", current_roles=["Chief Executive Officer"], source_ids=["sec:proxy"], source_urls=["https://sec.test/proxy"], source_local_paths=["cache/proxy.htm"],
        )
        store.upsert_people([person]); store.upsert_roles([role]); store.upsert_relationships([relationship])
        store.upsert_role_changes([RoleChangeEvent(event_id="change:future", person_id=person.person_id, ticker="TST", issuer_cik="0000000001", event_type="departure", person_name=person.full_name, role="CEO", effective_date="2026-11-19", source_id="sec:8k", source_url="https://sec.test/8k", local_source_path="cache/8k.htm")])
        return store, person

    def test_future_effective_departure_keeps_tenure_current(self):
        store, person = self._seed_store()
        tenures = build_tenures(store, "TST", as_of_date="2026-09-24")
        self.assertEqual(len(tenures), 1)
        self.assertIsNone(tenures[0].end_date)
        self.assertEqual(tenures[0].scheduled_end_date, "2026-11-19")
        self.assertEqual(tenures[0].current_as_of_date, "2026-09-24")

    def test_period_status_rejects_pre_and_post_tenure_boundaries(self):
        tenure = ManagementTenure("tenure:1", "person:1", "TestCo", "TST", "1", "CEO", "CEO", "2024-07", None, "month", "2026-09-24")
        before = FinancialFact("fact:before", "TST", "revenue", "2023-01-01", "2023-12-31", 10, "USD", "GAAP")
        partial = FinancialFact("fact:partial", "TST", "revenue", "2024-01-01", "2024-12-31", 20, "USD", "GAAP")
        after = FinancialFact("fact:after", "TST", "revenue", "2027-01-01", "2027-12-31", 30, "USD", "GAAP")
        self.assertEqual(period_status(before, tenure, as_of_date="2026-09-24"), "PRE_TENURE")
        self.assertEqual(period_status(partial, tenure, as_of_date="2026-09-24"), "PARTIAL_PERIOD")
        self.assertEqual(period_status(after, tenure, as_of_date="2026-09-24"), "POST_TENURE")

    def test_operating_outcome_is_tenure_overlap(self):
        tenure = ManagementTenure("tenure:1", "person:1", "TestCo", "TST", "1", "CEO", "CEO", "2020-01-01", None, "day", "2026-09-24")
        facts = [FinancialFact("fact:1", "TST", "revenue", "2020-01-01", "2020-12-31", 100, "USD", "GAAP", fiscal_year=2020, fiscal_period="FY", source_url="https://data.sec.gov", source_fact={"filed": "2021-02-01"}), FinancialFact("fact:2", "TST", "revenue", "2025-01-01", "2025-12-31", 150, "USD", "GAAP", fiscal_year=2025, fiscal_period="FY", source_url="https://data.sec.gov", source_fact={"filed": "2026-02-01"})]
        outcomes = build_operating_outcomes(tenure, facts, as_of_date="2026-09-24")
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].attribution_level, Attribution.TENURE_OVERLAP)
        self.assertEqual(outcomes[0].percent_change, 50.0)

    def test_operating_outcome_does_not_mix_annual_and_quarterly_periods(self):
        tenure = ManagementTenure("tenure:mixed", "person:1", "TestCo", "TST", "1", "CEO", "CEO", "2020-01-01", None, "day", "2026-09-24")
        facts = [
            FinancialFact("fact:fy", "TST", "revenue", "2024-01-01", "2024-12-31", 100, "USD", "GAAP", fiscal_year=2024, fiscal_period="FY", source_url="https://data.sec.gov"),
            FinancialFact("fact:q", "TST", "revenue", "2025-04-01", "2025-06-30", 30, "USD", "GAAP", fiscal_year=2025, fiscal_period="Q2", source_url="https://data.sec.gov"),
        ]
        self.assertEqual(build_operating_outcomes(tenure, facts, as_of_date="2026-09-24"), [])

    def test_attribution_levels_do_not_turn_tenure_into_causation(self):
        person = ManagementPerson("person:alice", "TST", "TestCo", "Alice Example", "alice example")
        self.assertEqual(classify_attribution("Revenue increased during the period.", person, role_category="CEO"), Attribution.ROLE_BASED)
        self.assertEqual(classify_attribution("Management approved the acquisition.", person, role_category="CEO"), Attribution.TEAM)
        self.assertEqual(classify_attribution("Alice Example directed the launch.", person, role_category="CEO"), Attribution.DIRECT)
        self.assertEqual(validate_attribution(Attribution.DIRECT, "The company launched it.", person)[0], False)

    def _guidance(self, low, high, metric="revenue", status="issued", basis="GAAP"):
        return GuidanceCommitment("guidance:1", "TST", "person:1", "tenure:1", "2025-01-01", "FY2025", metric, "company", low, high, None, "USD", basis, status, None, Attribution.TEAM, "claim:1", "doc:1", "FY2025 revenue guidance", "https://sec.test", "cache/doc", created_at="2025-01-01")

    def test_guidance_range_beat_met_miss(self):
        fact = FinancialFact("fact:actual", "TST", "revenue", "2025-01-01", "2025-12-31", 110, "USD", "GAAP", fiscal_year=2025, fiscal_period="FY", source_url="https://data.sec.gov")
        self.assertEqual(match_guidance_to_actuals(self._guidance(90, 100), [fact]).outcome, "BEAT")
        fact.value = 95
        self.assertEqual(match_guidance_to_actuals(self._guidance(90, 100), [fact]).outcome, "MET")
        fact.value = 80
        self.assertEqual(match_guidance_to_actuals(self._guidance(90, 100), [fact]).outcome, "MISS")

    def test_guidance_withdrawal_and_basis_or_horizon_rejection(self):
        fact = FinancialFact("fact:actual", "TST", "revenue", "2025-01-01", "2025-12-31", 100, "USD", "GAAP", fiscal_year=2025, fiscal_period="FY", source_url="https://data.sec.gov")
        self.assertEqual(match_guidance_to_actuals(self._guidance(90, 100, status="withdrawn"), [fact]).outcome, "WITHDRAWN")
        wrong = self._guidance(90, 100, basis="non-GAAP")
        self.assertEqual(match_guidance_to_actuals(wrong, [fact]).outcome, "NOT_COMPARABLE")
        wrong_horizon = self._guidance(90, 100); wrong_horizon.fiscal_horizon = "Q3 2025"
        self.assertEqual(match_guidance_to_actuals(wrong_horizon, [fact]).outcome, "UNRESOLVED")

    def test_guidance_unit_mismatch_is_not_comparable(self):
        guidance = GuidanceCommitment(
            "guidance:unit", "TST", None, None, "2025-01-01", "Q2 2025", "revenue", "company",
            2, 3, None, "percent", None, "issued", None, Attribution.TEAM,
            "claim:unit", "doc:unit", "revenue up 2 to 3%", "https://sec.test", "cache/unit",
            created_at="2025-01-01",
        )
        fact = FinancialFact("fact:unit", "TST", "revenue", "2025-04-01", "2025-06-30", 100, "USD", "GAAP", fiscal_year=2025, fiscal_period="Q2", source_url="https://data.sec.gov")
        outcome = match_guidance_to_actuals(guidance, [fact])
        self.assertEqual(outcome.outcome, "NOT_COMPARABLE")
        self.assertEqual(outcome.comparison_method, "unit_match_required")

    def test_guidance_does_not_use_future_actual_at_as_of_date(self):
        guidance = self._guidance(90, 100)
        future = FinancialFact("fact:future", "TST", "revenue", "2025-01-01", "2025-12-31", 95, "USD", "GAAP", fiscal_year=2025, fiscal_period="FY", source_url="https://data.sec.gov")
        outcome = match_guidance_to_actuals(guidance, [future], as_of_date="2025-06-30")
        self.assertEqual(outcome.outcome, "UNRESOLVED")

    def test_strategic_silence_is_unresolved_and_completion_is_achieved(self):
        commitment = StrategicCommitment("commitment:1", "person:1", "TST", "tenure:1", "2024-01-01", "product_launch", "launch product", "We will launch product by 2025.", "2025", None, None, None, Attribution.TEAM, "claim:1", "doc:1", "We will launch product by 2025.", "https://sec.test", "cache/1", created_at="2024-01-01")
        silence = resolve_strategic_outcomes([commitment], [], {})[0]
        self.assertEqual(silence.outcome, "UNRESOLVED")
        achieved = resolve_strategic_outcomes([commitment], [{"claim_id": "claim:2", "document_id": "doc:2", "created_at": "2025-02-01", "evidence_text": "We launched product in January."}], {})[0]
        self.assertEqual(achieved.outcome, "ACHIEVED")

    def test_fact_normalization_distinguishes_share_metrics(self):
        payload = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 100, "fy": 2024, "fp": "FY", "form": "10-K", "accn": "a"}]}},
                    "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [{"start": "2024-01-01", "end": "2024-12-31", "val": 10, "fy": 2024, "fp": "FY", "form": "10-K", "accn": "b"}]}},
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {"units": {"shares": [{"end": "2024-12-31", "val": 9, "fy": 2024, "fp": "FY", "form": "10-K", "accn": "c"}]}},
                },
            },
        }
        facts = normalize_company_facts(payload, "TST", "https://data.sec.gov/facts", "cache/facts.json")
        self.assertEqual({fact.metric for fact in facts}, {"revenue", "diluted_weighted_average_shares", "shares_outstanding"})
        self.assertNotEqual(next(f for f in facts if f.metric == "shares_outstanding").value, next(f for f in facts if f.metric == "diluted_weighted_average_shares").value)

    def test_fact_normalization_keeps_latest_revision_provenance(self):
        payload = {"facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01", "accn": "old"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 105, "fy": 2024, "fp": "FY", "form": "10-K/A", "filed": "2025-03-01", "accn": "new"},
        ]}}}}}
        facts = normalize_company_facts(payload, "TST", "https://data.sec.gov/facts", "cache/facts.json")
        revenue = next(f for f in facts if f.metric == "revenue")
        self.assertEqual(revenue.value, 105)
        self.assertEqual(revenue.accession_number, "new")
        self.assertEqual(revenue.revision_provenance[0]["value"], 100)

    def test_fact_normalization_keeps_annual_and_ytd_periods_distinct(self):
        payload = {"facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "fy": 2024, "fp": "FY", "form": "10-K", "accn": "fy"},
            {"start": "2024-01-01", "end": "2024-09-30", "val": 75, "fy": 2024, "fp": "Q3", "form": "10-Q", "accn": "ytd"},
        ]}}}}}
        facts = [f for f in normalize_company_facts(payload, "TST", "https://data.sec.gov/facts") if f.metric == "revenue"]
        self.assertEqual({f.period_end for f in facts}, {"2024-09-30", "2024-12-31"})

    def test_prior_company_without_dates_is_not_eligible_for_performance(self):
        store, person = self._seed_store()
        store.upsert_career([CareerEntry("career:prior", person.person_id, "TST", "PriorCo", employer_ticker="PRIOR", employer_cik="0000000002", employer_classification="PUBLIC_COMPANY_CONFIRMED", title="Chief Executive Officer", source_ids=["sec:prior"], source_urls=["https://sec.test/prior"], source_local_paths=["cache/prior.htm"])])
        prior = [item for item in build_tenures(store, "TST", as_of_date="2026-09-24", include_prior_companies=True) if item.ticker == "PRIOR"][0]
        self.assertEqual(prior.prior_company_status, "IDENTITY_CONFIRMED_DATES_UNKNOWN")

    def test_fact_normalization_derives_fcf_and_gaap_margins_with_provenance(self):
        entries = {
            "NetCashProvidedByUsedInOperatingActivities": 80,
            "PaymentsToAcquirePropertyPlantAndEquipment": 20,
            "RevenueFromContractWithCustomerExcludingAssessedTax": 200,
            "GrossProfit": 100,
            "OperatingIncomeLoss": 40,
        }
        us_gaap = {tag: {"units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": value, "fy": 2024, "fp": "FY", "form": "10-K", "accn": tag}]}} for tag, value in entries.items()}
        facts = normalize_company_facts({"facts": {"us-gaap": us_gaap}}, "TST", "https://data.sec.gov/facts", "cache/facts.json")
        by_metric = {f.metric: f for f in facts}
        self.assertEqual(by_metric["free_cash_flow"].value, 60)
        self.assertEqual(by_metric["free_cash_flow"].value_method, "derived")
        self.assertEqual(by_metric["gross_margin"].value, 50)
        self.assertEqual(by_metric["operating_margin"].value, 20)
        self.assertEqual(len(by_metric["free_cash_flow"].derivation["source_fact_ids"]), 2)

    def test_guidance_coverage_diagnostic_distinguishes_missing_store(self):
        diagnostic = diagnose_guidance_coverage("PLTR", [], {}, [])
        self.assertEqual(diagnostic.reason, "GUIDANCE_NOT_IN_CURRENT_DOCUMENT_STORE")
        self.assertTrue(diagnostic.coverage_neutral)

    def test_guidance_coverage_documents_without_claims_is_not_no_guidance(self):
        diagnostic = diagnose_guidance_coverage("EXEL", [], {"doc": {"document_id": "doc"}}, [])
        self.assertEqual(diagnostic.reason, "GUIDANCE_DOCUMENTS_PRESENT_BUT_NOT_EXTRACTED")
        self.assertFalse(diagnostic.guidance_absence_confirmed)
        self.assertTrue(diagnostic.coverage_neutral)

    def test_guidance_coverage_no_evidence_is_neutral_for_downstream_scoring(self):
        diagnostic = diagnose_guidance_coverage("TST", [{"claim_id": "c", "evidence_text": "The business delivered strong execution."}], {"doc": {}}, [])
        self.assertEqual(diagnostic.reason, "GUIDANCE_EVIDENCE_NOT_FOUND")
        self.assertTrue(diagnostic.coverage_neutral)

    def test_guidance_absence_requires_explicit_source_statement(self):
        diagnostic = diagnose_guidance_coverage("TST", [{"claim_id": "c", "evidence_text": "We do not provide guidance."}], {"doc": {}}, [])
        self.assertEqual(diagnostic.reason, "NO_GUIDANCE_GIVEN_CONFIRMED")
        self.assertTrue(diagnostic.guidance_absence_confirmed)
        self.assertFalse(diagnostic.coverage_neutral)

    def test_capital_events_keep_authorization_separate_from_execution(self):
        claims = [
            {"claim_id": "c1", "document_id": "d1", "created_at": "2026-01-01", "evidence_text": "The board authorized an incremental $1 billion share repurchase."},
            {"claim_id": "c2", "document_id": "d2", "created_at": "2026-02-01", "evidence_text": "The company repurchased $100 million of shares."},
            {"claim_id": "c3", "document_id": "d3", "created_at": "2026-03-01", "evidence_text": "We closed the acquisition of ExampleAI for $250 million."},
        ]
        events = extract_capital_events(claims, {}, [], [], "TST", "TestCo")
        self.assertEqual({event.event_type for event in events}, {"buyback_authorization", "buyback_execution", "acquisition"})
        self.assertEqual(next(event for event in events if event.event_type == "buyback_authorization").event_status, "authorized")
        self.assertEqual(next(event for event in events if event.event_type == "acquisition").event_status, "completed")

    def test_capital_acquisition_history_is_completed_and_program_linked(self):
        claims = [{"claim_id": "c-history", "document_id": "d1", "created_at": "2026-01-01", "evidence_text": "Common Room was our largest acquisition at $250 million."}]
        event = extract_capital_events(claims, {}, [], [], "TST", "TestCo")[0]
        self.assertEqual(event.event_status, "completed")
        self.assertEqual(event.target_or_counterparty, "Common Room")
        self.assertTrue(event.capital_allocation_program_id)

    def test_strategic_extraction_rejects_guidance_and_historical_prose(self):
        claims = [
            {"claim_id": "g", "document_id": "d", "created_at": "2026-01-01", "evidence_text": "We expect revenue to grow 3% next quarter."},
            {"claim_id": "h", "document_id": "d", "created_at": "2026-01-01", "evidence_text": "We will launch the product by 2027."},
            {"claim_id": "x", "document_id": "d", "created_at": "2026-01-01", "evidence_text": "We launched the product last quarter."},
        ]
        commitments = extract_strategic_commitments(claims, {}, [], [], "TST")
        self.assertEqual(len(commitments), 1)
        self.assertIn("launch", commitments[0].commitment_text.lower())

    def test_track_store_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TrackRecordStore(directory)
            tenure = ManagementTenure("tenure:1", "person:1", "TestCo", "TST", "1", "CEO", "CEO")
            self.assertEqual(store.upsert_tenures([tenure])["added"], 1)
            self.assertEqual(store.upsert_tenures([tenure])["unchanged"], 1)
            self.assertEqual(len(store.list_tenures("TST")), 1)


if __name__ == "__main__":
    unittest.main()
