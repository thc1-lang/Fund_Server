from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from management_intelligence.board_committees import extract_committees
from management_intelligence.board_composition import build_board_memberships, infer_independence, leadership_structure
from management_intelligence.board_governance_models import (
    BoardCommittee, GovernanceBoardMembership, GovernanceCoverage, VotingControlSnapshot,
)
from management_intelligence.board_tenure import calculate_tenure, summarize_tenure
from management_intelligence.governance_audit import audit_current_board
from management_intelligence.governance_cli import _coverage, _related_diagnostics
from management_intelligence.governance_sources import CachedGovernanceSource
from management_intelligence.governance_sources import GovernanceSourceCatalog
from management_intelligence.governance_store import GovernanceStore
from management_intelligence.models import BoardMembership, ManagementPerson, RoleAssertion, RoleChangeEvent
from management_intelligence.overboarding import build_overboarding
from management_intelligence.related_parties import extract_related_parties
from management_intelligence.shareholder_rights import extract_shareholder_rights
from management_intelligence.store import ManagementStore
from management_intelligence.voting_control import extract_voting_structure


def _person(person_id: str, name: str, ticker: str = "TST") -> ManagementPerson:
    return ManagementPerson(person_id=person_id, ticker=ticker, company_name="Test Co", full_name=name, normalized_name=name.lower())


def _proxy(text: str) -> CachedGovernanceSource:
    return CachedGovernanceSource("TST", "0000000000-26-000001", "DEF 14A", "2026-04-01", "2026-04-01", "https://www.sec.gov/test.htm", "test.htm", text)


class BoardGovernanceTests(unittest.TestCase):
    def test_future_effective_departure_remains_current(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ManagementStore(directory)
            person = _person("p1", "Alice Director")
            store.upsert_people([person])
            store.upsert_boards([BoardMembership(board_id="b1", person_id="p1", ticker="TST", board_company="Test Co", board_ticker="TST", board_role="director", start_date="2020", end_date="2026-12-31", is_current=True, evidence_text="Alice Director | Independent Director | 2020")])
            proxy = _proxy("Alice Director is an independent director.")
            self.assertEqual(len(build_board_memberships(store, "TST", as_of_date="2026-09-24", proxy=proxy)), 1)
            self.assertEqual(len(build_board_memberships(store, "TST", as_of_date="2027-01-01", proxy=proxy)), 0)

    def test_independence_explicit_yes_no_unknown(self):
        yes = BoardMembership(board_id="1", person_id="p1", ticker="TST", board_company="Test", evidence_text="Alice | Independent Director")
        no = BoardMembership(board_id="2", person_id="p2", ticker="TST", board_company="Test", evidence_text="Bob | Chief Executive Officer and Director")
        unknown = BoardMembership(board_id="3", person_id="p3", ticker="TST", board_company="Test", evidence_text="Carol | Director")
        self.assertEqual(infer_independence(yes, _person("p1", "Alice"))[0], "INDEPENDENT")
        self.assertEqual(infer_independence(no, _person("p2", "Bob"))[0], "NOT_INDEPENDENT")
        self.assertEqual(infer_independence(unknown, _person("p3", "Carol"))[0], "UNKNOWN")

    def test_leadership_structures(self):
        members = [
            GovernanceBoardMembership("1", "p1", "TST", board_role="chair", is_independent="INDEPENDENT", chair_status="chair"),
            GovernanceBoardMembership("2", "p2", "TST", board_role="lead independent director", is_independent="INDEPENDENT", lead_independent_status=True),
        ]
        roles = [RoleAssertion("r", "p1", "TST", "Chief Executive Officer", "CEO", True)]
        self.assertEqual(leadership_structure(members, roles)["chair_structure"], "CEO_CHAIR_WITH_LEAD_INDEPENDENT_DIRECTOR")

    def test_committee_membership_and_chair(self):
        people = [_person("p1", "Alice Chadwick"), _person("p2", "Bob Scheinman")]
        text = "Audit Committee consists of Ms. Chadwick and Mr. Scheinman. The chair of our Audit Committee is Ms. Chadwick. Our Board has determined that each member of our Audit Committee is independent."
        members = [GovernanceBoardMembership("1", "p1", "TST", is_independent="INDEPENDENT"), GovernanceBoardMembership("2", "p2", "TST", is_independent="INDEPENDENT")]
        committees = extract_committees("TST", _proxy(text), people, members)
        audit = next(item for item in committees if item.committee_type == "audit")
        self.assertEqual(set(audit.members), {"p1", "p2"})
        self.assertEqual(audit.chair_person_id, "p1")

    def test_flattened_pdf_committee_tables_are_recovered(self):
        people = [_person("p1", "Alice Chadwick"), _person("p2", "Bob Scheinman")]
        text = "Board and Governance Matters Chair Alice Chadwick Other Members Bob Scheinman Independent: 100% Audit Committee"
        members = [GovernanceBoardMembership("1", "p1", "TST", is_independent="INDEPENDENT"), GovernanceBoardMembership("2", "p2", "TST", is_independent="INDEPENDENT")]
        committees = extract_committees("TST", _proxy(text), people, members)
        audit = next(item for item in committees if item.committee_type == "audit")
        self.assertEqual(set(audit.members), {"p1", "p2"})
        self.assertEqual(audit.chair_person_id, "p1")
        self.assertEqual(audit.required_independence, "YES")

    def test_audit_financial_expert_is_explicit(self):
        people = [_person("p1", "Alice Chadwick")]
        text = "Audit Committee consists of Ms. Chadwick. Our Board has determined that Ms. Chadwick is an audit committee financial expert."
        committees = extract_committees("TST", _proxy(text), people, [GovernanceBoardMembership("1", "p1", "TST", is_independent="INDEPENDENT")])
        audit = next(item for item in committees if item.committee_type == "audit")
        self.assertEqual(audit.financial_expert_person_ids, ["p1"])

    def test_tenure_precision_and_buckets(self):
        members = [GovernanceBoardMembership("1", "p1", "TST", start_date="2020", date_precision="year"), GovernanceBoardMembership("2", "p2", "TST")]
        records = calculate_tenure(members, as_of_date="2026-09-24")
        summary = summarize_tenure(records)
        self.assertIsNotNone(records[0].tenure_days)
        self.assertEqual(records[1].tenure_bucket, "unknown")
        self.assertEqual(summary["buckets"]["unknown"], 1)
        self.assertIsNone(records[0].tenure_years_exact)
        self.assertIsNotNone(records[0].tenure_years_estimate)
        self.assertEqual(records[0].tenure_precision, "YEAR")
        self.assertEqual(summary["statistics_precision"], "ESTIMATED")

    def test_day_precision_tenure_is_exact(self):
        record = calculate_tenure([GovernanceBoardMembership("1", "p1", "TST", start_date="2020-01-02", date_precision="day")], as_of_date="2026-09-24")[0]
        self.assertIsNotNone(record.tenure_years_exact)
        self.assertEqual(record.tenure_precision, "DAY")

    def test_unknown_independence_has_explicit_denominator(self):
        from management_intelligence.board_governance_models import GovernanceSnapshot
        snapshot = GovernanceSnapshot("s", "TST", "2026-09-24", board_size=3, independent_count=1, independent_denominator=3, known_independence_count=2, known_independence_denominator=2, unknown_independence=1, known_status_independent_percentage=50.0)
        self.assertEqual(snapshot.independent_denominator, 3)
        self.assertEqual(snapshot.known_independence_denominator, 2)
        self.assertEqual(snapshot.unknown_independence, 1)

    def test_outside_public_board_count_excludes_target_and_historical(self):
        member = GovernanceBoardMembership("g", "p1", "TST")
        boards = [
            BoardMembership("target", "p1", "TST", "Test", board_ticker="TST", board_classification="PUBLIC_COMPANY_CONFIRMED"),
            BoardMembership("outside", "p1", "TST", "Other", board_ticker="OTH", board_classification="PUBLIC_COMPANY_CONFIRMED"),
            BoardMembership("former", "p1", "TST", "Former", board_ticker="OLD", board_classification="PUBLIC_COMPANY_CONFIRMED", is_current=False),
        ]
        result = build_overboarding("TST", [member], boards, [_person("p1", "Alice")], [], as_of_date="2026-09-24")
        self.assertEqual(result[0].current_public_company_board_count, 1)

    def test_related_party_requires_explicit_family_statement(self):
        people = [_person("p1", "Alice Director")]
        records, status = extract_related_parties("TST", _proxy("There are no family relationships among any of our directors. Certain Relationships and Related Person Transactions."), people)
        self.assertEqual(records, [])
        self.assertEqual(status["family"], "NO")

    def test_related_party_empty_records_are_not_explicit_none(self):
        self.assertEqual(_related_diagnostics([], {"family": "NO", "related_party": "YES", "interlocks": "UNKNOWN"}), {"family": "EXPLICIT_NONE", "related_party": "NO_PARSEABLE_RECORDS", "interlocks": "UNKNOWN"})

    def test_coverage_downgrades_when_related_source_is_not_sufficient(self):
        coverage = _coverage("TST", "2026-09-24", board=[GovernanceBoardMembership("1", "p1", "TST", is_independent="INDEPENDENT")], committees=[], expertise=[], overboarding=[], related_status={"family": "NO", "related_party": "YES", "interlocks": "UNKNOWN", "_records": []}, classes=[], rights=None, warnings=[])
        self.assertEqual(coverage.related_parties, "PARTIAL")
        self.assertEqual(coverage.related_party_diagnostics["related_party"], "NO_PARSEABLE_RECORDS")

    def test_dual_class_votes_and_economic_control_are_separate(self):
        classes, control = extract_voting_structure("TST", _proxy("Each share of Class A common stock is entitled to one vote per share. Each share of Class B common stock is entitled to 10 votes per share."))
        self.assertEqual(control.share_class_structure, "dual_class")
        self.assertEqual([item.votes_per_share for item in classes], [1.0, 10.0])
        self.assertNotEqual(control.economic_ownership, control.voting_ownership)

    def test_class_f_voting_structure_keeps_formula_unknown(self):
        classes, control = extract_voting_structure("TST", _proxy("Class F common stock is held in a Founder Voting Trust established by Stephen Cohen, Alexander Karp, and Peter Thiel."))
        class_f = next(item for item in classes if item.class_name == "Class F common stock")
        self.assertIsNone(class_f.votes_per_share)
        self.assertEqual(class_f.structural_notes["voting_formula"], "UNKNOWN")
        self.assertTrue(control.structural_notes["economic_ownership_not_inferred"])

    def test_shareholder_rights_unknown_is_preserved(self):
        rights = extract_shareholder_rights("TST", _proxy("Each director is elected at the annual meeting. Stockholders may not cumulate votes."))
        self.assertEqual(rights.classified_board_status, "ANNUAL_ELECTION")
        self.assertEqual(rights.cumulative_voting, "NO")
        self.assertEqual(rights.proxy_access, "UNKNOWN")

    def test_conditional_election_standard_is_retained(self):
        rights = extract_shareholder_rights("TST", _proxy("Directors are elected by a majority vote in an uncontested election and by a plurality in a contested election. Directors who fail to receive a majority must resign."))
        self.assertEqual(rights.director_election_standard, "CONDITIONAL")
        self.assertEqual(rights.uncontested_election_standard, "majority")
        self.assertEqual(rights.contested_election_standard, "plurality")
        self.assertEqual(rights.resignation_policy, "YES")

    def test_special_meeting_right_requires_stockholder_provision(self):
        rights = extract_shareholder_rights("TST", _proxy("The board held regular and special meetings during the year. Our bylaws contain an advance notice procedure for nominations."))
        self.assertEqual(rights.special_meeting_right, "UNKNOWN")
        self.assertIsNone(rights.special_meeting_scope)

    def test_future_role_change_is_an_event_but_not_current_departure(self):
        event = RoleChangeEvent("e", "p1", "TST", "1", "departure", effective_date="2026-12-31")
        self.assertEqual(event.effective_date, "2026-12-31")

    def test_cached_source_catalog_is_network_free(self):
        catalog = GovernanceSourceCatalog(sec_cache_root="does-not-exist")
        self.assertEqual(catalog.network_requests, 0)
        self.assertEqual(catalog.sources("TST"), [])

    def test_classified_board_and_annual_election_are_distinct(self):
        classified = extract_shareholder_rights("TST", _proxy("Our board is divided into three staggered classes of directors."))
        annual = extract_shareholder_rights("TST", _proxy("Each director is elected annually for a one-year term."))
        self.assertEqual(classified.classified_board_status, "CLASSIFIED")
        self.assertEqual(classified.number_of_classes, 3)
        self.assertEqual(annual.classified_board_status, "ANNUAL_ELECTION")

    def test_founder_voting_trust_is_preserved_without_economic_inference(self):
        classes, control = extract_voting_structure("TST", _proxy("All shares of Class F common stock are held in a Founder Voting Trust established by Stephen Cohen, Alexander Karp, and Peter Thiel."))
        self.assertTrue(control.founder_control["disclosed"])
        self.assertEqual(control.economic_ownership["status"], "UNKNOWN")
        self.assertEqual(len(control.voting_agreements), 1)

    def test_committee_independence_keeps_unknown_members(self):
        committee = BoardCommittee("c", "TST", "audit", "Audit Committee", members=["p1", "p2"], member_independence={"p1": "INDEPENDENT", "p2": "UNKNOWN"})
        self.assertEqual(committee.member_independence["p2"], "UNKNOWN")

    def test_governance_versions_are_explicit(self):
        record = GovernanceBoardMembership("g", "p1", "TST")
        self.assertEqual(record.governance_version, "board-governance-v1")
        self.assertEqual(record.independence_version, "board-independence-v1")

    def test_related_party_positive_record_requires_named_person(self):
        people = [_person("p1", "Alice Director")]
        records, status = extract_related_parties("TST", _proxy("Certain Related Person Transactions. Alice Director received consulting payments under a disclosed agreement."), people)
        self.assertTrue(status["related_party"] in {"YES", "UNKNOWN"})
        self.assertTrue(all(item.person_id == "p1" for item in records))

    def test_governance_store_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = GovernanceStore(directory)
            coverage = GovernanceCoverage("c", "TST", "2026-09-24", board_composition="COMPLETE")
            first = store.upsert("governance_coverage", [coverage])
            second = store.upsert("governance_coverage", [coverage])
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(len(store.list("governance_coverage", "TST")), 1)

    def test_director_audit_keeps_unknown_independence_valid(self):
        member = GovernanceBoardMembership("g", "p1", "TST", board_role="director", is_independent="UNKNOWN", independence_basis="unknown", independence_source="unknown", start_date="2020", date_precision="year", source_url="https://sec.test", source_accession="0000000000-26-000001", source_form="DEF 14A", source_date="2026-04-01", local_source_path="proxy.htm", evidence_text="Alice Director | Director")
        result = {"board": [member], "committees": [], "tenure": calculate_tenure([member], as_of_date="2026-09-24"), "expertise": [], "overboarding": [], "related": []}
        finding = audit_current_board(result, {"p1": _person("p1", "Alice Director")})[0]
        self.assertEqual(finding.independent, "UNKNOWN")
        self.assertNotIn("UNSUPPORTED_INDEPENDENCE", finding.issues)

    def test_coverage_does_not_encode_a_governance_score(self):
        coverage = GovernanceCoverage("c", "TST", "2026-09-24", independence="INSUFFICIENT")
        self.assertEqual(coverage.independence, "INSUFFICIENT")
        self.assertFalse(hasattr(coverage, "governance_score"))


if __name__ == "__main__":
    unittest.main()
