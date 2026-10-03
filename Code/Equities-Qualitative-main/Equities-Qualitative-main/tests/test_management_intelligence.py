import tempfile
import unittest
from pathlib import Path

from insider_intelligence.models import SECFiling
from insider_intelligence.models import InsiderPerson
from management_intelligence.career_history import parse_proxy_people
from management_intelligence.audit import audit_store
from management_intelligence.identity_resolution import ManagementIdentityResolver
from management_intelligence.insider_links import link_management_to_insiders
from management_intelligence.models import ManagementPerson, OfficialSource
from management_intelligence.organization_resolution import resolve_organization
from management_intelligence.people import normalize_name, person_key
from management_intelligence.role_changes import parse_role_changes
from management_intelligence.store import ManagementStore


FIXTURE = """
<html><body>
<table><tr><th>Director Nominees</th><th>Age</th><th>Position</th><th>Director Since</th></tr>
<tr><td>Alex Example</td><td>55</td><td>Independent Director</td><td>2020</td></tr>
<tr><td>Jordan Founder</td><td>52</td><td>Co-Founder, Chief Executive Officer, and Chair</td><td>2010</td></tr>
</table>
<table><tr><th>Name</th><th>Age</th><th>Position</th></tr>
<tr><td>Alex Example</td><td>55</td><td>Independent Director</td></tr>
<tr><td>Jordan Founder</td><td>52</td><td>Co-Founder, Chief Executive Officer, and Chair</td></tr>
<tr><td>Casey Finance</td><td>48</td><td>Chief Financial Officer</td></tr>
</table>
<div>Alex Example. Ms. Example has served as a director since 2020. Previously, she was a senior executive at Example Systems.</div>
<div>Jordan Founder. Mr. Founder co-founded ExampleCo and has served as Chief Executive Officer since 2010. He currently serves on the board of directors of OutsideCo.</div>
<div>Casey Finance. Ms. Finance has served as Chief Financial Officer since 2022. Prior to joining ExampleCo, she served as Vice President at Finance Systems.</div>
<table><tr><th>Name and Principal Position</th><th>Salary</th></tr><tr><td>Compensation Discussion and Analysis</td><td>12</td></tr></table>
</body></html>
"""


class ManagementIntelligenceTests(unittest.TestCase):
    def filing(self):
        return SECFiling(
            ticker="EXM", issuer_cik="0000000001", form_type="DEF 14A",
            accession_number="0000000001-26-000001", filing_date="2026-04-01",
            report_date=None, primary_document="proxy.htm",
            source_url="https://www.sec.gov/Archives/edgar/data/1/proxy.htm",
            local_source_path="artifacts/management_intelligence/sec/EXM/proxy.htm",
        )

    def test_proxy_extracts_roles_career_and_board_provenance(self):
        result = parse_proxy_people(FIXTURE, self.filing(), "ExampleCo")
        names = {person.full_name for person in result.people}
        self.assertEqual(names, {"Alex Example", "Jordan Founder", "Casey Finance"})
        founder = next(person for person in result.people if person.full_name == "Jordan Founder")
        self.assertTrue(founder.is_founder)
        self.assertEqual(founder.current_since, "2010")
        self.assertTrue(any(entry.employer == "Finance Systems" for entry in result.career))
        self.assertTrue(any(board.board_company == "ExampleCo" for board in result.boards))
        self.assertTrue(any(board.board_company == "OutsideCo" and board.board_ticker is None for board in result.boards))
        self.assertTrue(all(source_id.startswith("sec:") for person in result.people for source_id in person.source_ids))
        self.assertTrue(all(person.source_urls and person.source_local_paths for person in result.people))
        self.assertTrue(all(assertion.source_ids for assertion in result.roles))

    def test_plain_text_pdf_proxy_roster_is_parsed_without_html_tables(self):
        text = (
            "Elect the following 2 nominees as directors: Alex Example and Jordan Founder. FOR each nominee. "
            "Our named executive officers for fiscal year 2025 were: Alex Example Chairman of the Board and Chief Executive Officer "
            "Casey Finance Chief Financial Officer. This section also discusses executive compensation."
        )
        result = parse_proxy_people(text, self.filing(), "ExampleCo")
        names = {person.full_name for person in result.people}
        self.assertTrue({"Alex Example", "Jordan Founder", "Casey Finance"}.issubset(names))
        alex = next(person for person in result.people if person.full_name == "Alex Example")
        self.assertTrue(any("Chief Executive Officer" in role for role in alex.current_roles))
        self.assertTrue(any(board.person_id == alex.person_id for board in result.boards))

    def test_identity_key_is_deterministic_and_normalized(self):
        self.assertEqual(normalize_name("Dr. J. Example, Ph.D."), "j example")
        self.assertEqual(person_key("exm", "Alex Example"), person_key("EXM", "Alex Example"))

    def test_store_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ManagementStore(directory)
            person = ManagementPerson(
                person_id="mgmt:EXM:1", ticker="EXM", company_name="ExampleCo",
                full_name="Alex Example", normalized_name="alex example",
                current_roles=["Independent Director"], source_ids=["sec:1:2"],
            )
            self.assertEqual(store.upsert_people([person])["added"], 1)
            self.assertEqual(store.upsert_people([person])["unchanged"], 1)
            self.assertEqual(len(store.list_people("EXM")), 1)

    def test_global_identity_key_is_separate_from_issuer_relationship(self):
        self.assertEqual(person_key("ZM", "Jane Doe"), person_key("EXEL", "Jane Doe"))

    def test_unsupported_same_name_stays_disambiguated(self):
        existing = ManagementPerson(
            person_id=person_key("AAA", "Alex Example"), ticker="AAA", company_name="Alpha",
            full_name="Alex Example", normalized_name="alex example", issuer_cik="0000000001",
        )
        incoming = ManagementPerson(
            person_id=person_key("BBB", "Alex Example"), ticker="BBB", company_name="Beta",
            full_name="Alex Example", normalized_name="alex example", issuer_cik="0000000002",
        )
        outcome = ManagementIdentityResolver().reconcile_incoming(incoming, [existing])
        self.assertEqual(outcome.status, "ambiguous")
        self.assertNotEqual(outcome.person.person_id, existing.person_id)

    def test_same_issuer_rerun_is_idempotent_identity(self):
        existing = ManagementPerson(
            person_id=person_key("AAA", "Alex Example"), ticker="AAA", company_name="Alpha",
            full_name="Alex Example", normalized_name="alex example", issuer_cik="0000000001",
        )
        incoming = ManagementPerson(
            person_id=person_key("AAA", "Alex Example"), ticker="AAA", company_name="Alpha",
            full_name="Alex Example", normalized_name="alex example", issuer_cik="0000000001",
        )
        outcome = ManagementIdentityResolver().reconcile_incoming(incoming, [existing])
        self.assertTrue(outcome.matched_existing)
        self.assertEqual(outcome.person.person_id, existing.person_id)
        self.assertEqual(outcome.person.identity_evidence, [])

    def test_insider_link_preserves_two_canonical_ids(self):
        management = ManagementPerson(
            person_id="person:management", ticker="EXM", company_name="ExampleCo",
            full_name="Alex Example", normalized_name="alex example", issuer_cik="0000000001",
        )
        insider = InsiderPerson(
            person_id="insider:owner", ticker="EXM", company_name="ExampleCo",
            full_name="Example, Alex", reporting_owner_cik="000000000000001",
        )
        links = link_management_to_insiders([management], [insider], issuer_cik="0000000001")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].confidence, "high")
        self.assertEqual(management.insider_person_ids, ["insider:owner"])
        self.assertNotEqual(management.person_id, insider.person_id)

    def test_item_502_parser_distinguishes_appointment_and_departure(self):
        html = """
        <html><body><p>Item 5.02 Departure of Directors or Certain Officers; Appointment of Certain Officers.</p>
        <p>On July 27, 2026, William R. McDermott, a director, notified the Company of his decision to resign from the board, effective immediately.</p>
        <p>On and effective as of August 31, 2026, the Board appointed Jeff Epstein to the Board as a director.</p>
        <p>Item 5.03 Amendments to Articles.</p></body></html>
        """
        filing = SECFiling(
            ticker="EXM", issuer_cik="0000000001", form_type="8-K",
            accession_number="0000000001-26-000002", filing_date="2026-09-01",
            report_date=None, primary_document="change.htm",
            source_url="https://www.sec.gov/change.htm", local_source_path="cache/change.htm",
        )
        person = ManagementPerson(
            person_id="person:william", ticker="EXM", company_name="ExampleCo",
            full_name="William R. McDermott", normalized_name="william r mcdermott",
        )
        events = parse_role_changes(html, filing, [person])
        self.assertEqual({event.person_name for event in events}, {"William R. McDermott", "Jeff Epstein"})
        self.assertEqual({event.event_type for event in events}, {"departure", "appointment"})
        self.assertEqual({event.effective_date for event in events}, {"2026-07-27", "2026-08-31"})

    def test_item_507_vote_is_not_role_change(self):
        filing = SECFiling(
            ticker="EXM", issuer_cik="0000000001", form_type="8-K",
            accession_number="0000000001-26-000003", filing_date="2026-06-15",
            report_date=None, primary_document="vote.htm", source_url="https://www.sec.gov/vote.htm",
        )
        html = "<p>Item 5.07 Submission of Matters to a Vote. Stockholders approved the appointment of the auditor.</p>"
        self.assertEqual(parse_role_changes(html, filing, []), [])

    def test_date_precision_and_board_employment_dates_are_distinct(self):
        result = parse_proxy_people(FIXTURE, self.filing(), "ExampleCo")
        jordan = next(person for person in result.people if person.full_name == "Jordan Founder")
        role = next(item for item in result.roles if item.person_id == jordan.person_id and item.is_current)
        board = next(item for item in result.boards if item.person_id == jordan.person_id and item.board_company == "ExampleCo")
        self.assertEqual(role.date_precision, "year")
        self.assertEqual(board.date_precision, "year")
        self.assertEqual(role.start_date, "2010")
        self.assertEqual(board.start_date, "2010")

    def test_public_company_mapping_requires_exact_sec_cache_match(self):
        path = Path(__file__).parent / "fixtures" / "sec" / "company_tickers.json"
        reference = resolve_organization("Zoom", ticker_cache=path)
        self.assertEqual(reference.classification, "PUBLIC_COMPANY_CONFIRMED")
        self.assertEqual(reference.ticker, "ZM")
        unresolved = resolve_organization("Imaginary Holdings", ticker_cache=path)
        self.assertEqual(unresolved.classification, "UNRESOLVED")

    def test_audit_reports_no_duplicate_or_chronology_errors_for_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ManagementStore(directory)
            result = parse_proxy_people(FIXTURE, self.filing(), "ExampleCo")
            store.upsert_people(result.people)
            store.upsert_roles(result.roles)
            store.upsert_career(result.career)
            store.upsert_boards(result.boards)
            store.upsert_relationships(result.relationships)
            store.upsert_education(result.education)
            report = audit_store(store, "EXM", as_of="2026-09-24")
            self.assertEqual(report["duplicate_people"], [])
            self.assertEqual(report["duplicate_career"], [])
            self.assertEqual(report["chronology_errors"], [])


if __name__ == "__main__":
    unittest.main()
