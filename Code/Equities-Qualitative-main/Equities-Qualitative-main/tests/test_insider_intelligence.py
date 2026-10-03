from __future__ import annotations

import gzip
import json
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path

from insider_intelligence.alignment import build_alignment_snapshot, detect_clusters
from insider_intelligence.main import build_parser, resolve_date_window
from insider_intelligence.models import InsiderTransaction, SECFiling
from insider_intelligence.ownership import avoid_double_counting_positions, reconcile_transaction
from insider_intelligence.sec_ingestion import SEC_ACCEPT, SEC_ACCEPT_ENCODING, SEC_ARCHIVES, SEC_DATA, SEC_TICKERS, SEC_USER_AGENT, SECResponse, SECSourceProvider, extract_ownership_xml, is_ownership_form
from insider_intelligence.sec_parser import parse_ownership_xml
from insider_intelligence.store import InsiderStore
from insider_intelligence.transaction_classification import classify_transaction
from insider_intelligence.transactions import transaction_counts


FIXTURES = Path(__file__).parent / "fixtures" / "sec"


class FakeTransport:
    def __init__(self, values: dict[str, bytes]):
        self.values = values
        self.calls: list[str] = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        if url not in self.values:
            return SECResponse(404, b"not found", {})
        return SECResponse(200, self.values[url], {"content-type": "application/xml" if url.endswith(".xml") else "application/json"})


class CaptureTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        response = self.responses.pop(0) if self.responses else SECResponse(200, b"{}", {})
        return response


def filing(form: str, accession: str = "0001585521-26-000001") -> SECFiling:
    return SECFiling(
        ticker="ZM", issuer_cik="0001585521", form_type=form, accession_number=accession,
        filing_date="2026-09-04", report_date="2026-09-03", primary_document=f"form{form}.xml",
        source_url=f"https://www.sec.gov/Archives/edgar/data/1585521/{accession.replace('-', '')}/form{form}.xml",
        local_source_path=str(FIXTURES / f"form{form}.xml"),
    )


class InsiderIntelligenceTests(unittest.TestCase):
    def test_cli_uses_bounded_365_day_default_and_explicit_full_history(self):
        args = build_parser().parse_args(["--ticker", "ZM"])
        self.assertEqual(resolve_date_window(args, today=__import__("datetime").date(2026, 9, 23)), ("2025-09-23", "2026-09-23", True, False))
        full = build_parser().parse_args(["--ticker", "ZM", "--full-history"])
        self.assertEqual(resolve_date_window(full, today=__import__("datetime").date(2026, 9, 23)), (None, None, False, True))
        explicit = build_parser().parse_args(["--ticker", "ZM", "--from-date", "2026-06-01", "--to-date", "2026-09-23"])
        self.assertEqual(resolve_date_window(explicit, today=__import__("datetime").date(2026, 9, 23)), ("2026-06-01", "2026-09-23", False, False))

    def test_form4_preserves_code_and_classifies_transactions(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        self.assertEqual(parsed.people[0].primary_role, "CFO")
        self.assertEqual(len(parsed.transactions), 3)
        purchase, sale, option = parsed.transactions
        self.assertEqual(purchase.transaction_code, "P")
        self.assertEqual(purchase.transaction_type, "open_market_purchase")
        self.assertEqual(purchase.transaction_value, 60000.0)
        self.assertEqual(sale.transaction_type, "automatic_sale")
        self.assertTrue(sale.is_10b5_1)
        self.assertEqual(option.transaction_type, "option_exercise")
        self.assertEqual(option.underlying_shares, 200.0)
        self.assertTrue(any("10b5-1" in note for note in sale.footnotes))

    def test_form4_post_transaction_holdings_create_latest_position(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        common = [item for item in parsed.ownership_positions if item.security_title == "Common Stock"]
        self.assertEqual(len(common), 2)
        self.assertEqual({item.ownership_method for item in common}, {"reported_post_transaction"})
        self.assertEqual(common[-1].shares_owned_direct, 10500.0)
        with tempfile.TemporaryDirectory() as temp:
            store = InsiderStore(temp)
            store.upsert_parsed(parsed)
            current = [item for item in store.get_current_holdings("ZM") if item.security_title == "Common Stock"]
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0].shares_owned_direct, 10500.0)
            self.assertEqual(len(store.get_transactions("ZM")), 3)

    def test_same_day_rows_use_xml_order_only_when_holdings_are_coherent(self):
        xml = (FIXTURES / "form4.xml").read_text(encoding="utf-8")
        xml = xml.replace("2026-09-01", "2026-09-08").replace("2026-09-02", "2026-09-08")
        xml = xml.replace("<transactionShares><value>500</value>", "<transactionShares><value>10000</value>")
        xml = xml.replace("<sharesOwnedFollowingTransaction><value>11000</value>", "<sharesOwnedFollowingTransaction><value>100000</value>")
        xml = xml.replace("<sharesOwnedFollowingTransaction><value>10500</value>", "<sharesOwnedFollowingTransaction><value>90000</value>")
        parsed = parse_ownership_xml(xml, filing("4"))
        common = [item for item in parsed.ownership_positions if item.security_title == "Common Stock"]
        self.assertEqual(common[-1].shares_owned_direct, 90000.0)
        self.assertTrue(all(item.pre_holdings_method == "derived" for item in parsed.transactions[:2]))
        self.assertEqual(parsed.warnings, [])

        unresolved = xml.replace("<sharesOwnedFollowingTransaction><value>90000</value>", "<sharesOwnedFollowingTransaction><value>95000</value>")
        uncertain = parse_ownership_xml(unresolved, filing("4"))
        self.assertTrue(any("same-day transaction sequence unresolved" in warning for warning in uncertain.warnings))
        self.assertTrue(all(item.pre_holdings_method == "unknown" for item in uncertain.transactions[:2]))
        reconciled = [reconcile_transaction(item) for item in uncertain.transactions]
        self.assertTrue(all(item.pre_holdings_method == "unknown" for item in reconciled[:2]))
        self.assertTrue(all(item.pre_transaction_shares is None for item in reconciled[:2]))

    def test_form3_is_position_not_open_market_transaction(self):
        parsed = parse_ownership_xml((FIXTURES / "form3.xml").read_bytes(), filing("3", "0001585521-26-000002"))
        self.assertEqual(parsed.transactions, [])
        self.assertEqual(len(parsed.ownership_positions), 1)
        self.assertEqual(parsed.ownership_positions[0].direct_indirect, "I")
        self.assertTrue(parsed.people[0].is_founder)
        self.assertEqual(parsed.people[0].filing_id, "0001585521-26-000002")
        self.assertEqual(parsed.people[0].form_type, "3")

    def test_indirect_ownership_resolves_linked_footnote_text(self):
        xml = (FIXTURES / "form3.xml").read_text(encoding="utf-8")
        xml = xml.replace(
            "<natureOfOwnership><value>By trust</value></natureOfOwnership>",
            "<natureOfOwnership><value>See footnote</value><footnoteId id=\"F1\" /></natureOfOwnership>",
        )
        xml = xml.replace(
            "</ownershipDocument>",
            "<footnotes><footnote id=\"F1\">The shares are held by a family trust and the Reporting Person disclaims beneficial ownership.</footnote></footnotes></ownershipDocument>",
        )
        parsed = parse_ownership_xml(xml, filing("3", "0001585521-26-000006"))
        nature = parsed.ownership_positions[0].nature_of_indirect_ownership or ""
        self.assertIn("family trust", nature)
        self.assertIn("disclaims", nature)

    def test_transaction_counts_separate_discretionary_and_automatic_sales(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        counts = transaction_counts(parsed.transactions)
        self.assertEqual(counts["open_market_sales"], 0)
        self.assertEqual(counts["automatic_sales"], 1)
        self.assertEqual(counts["conversions"], 0)

    def test_form5_preserves_transaction_date(self):
        parsed = parse_ownership_xml((FIXTURES / "form5.xml").read_bytes(), filing("5", "0001585521-26-000003"))
        self.assertEqual(parsed.transactions[0].transaction_date, "2025-12-15")
        self.assertEqual(parsed.transactions[0].filing_date, "2026-09-04")
        self.assertEqual(parsed.transactions[0].transaction_type, "tax_withholding")

    def test_ownership_form_detection_includes_amendments(self):
        self.assertTrue(is_ownership_form("3"))
        self.assertTrue(is_ownership_form("4/A"))
        self.assertTrue(is_ownership_form("5/A"))
        self.assertFalse(is_ownership_form("10-K"))

    def test_raw_sec_submission_xml_is_extracted_from_wrapper(self):
        xml = (FIXTURES / "form4.xml").read_bytes()
        wrapped = b"SEC HEADER\n<XML>\n" + xml + b"\n</XML>\n"
        extracted = extract_ownership_xml(wrapped)
        self.assertIsNotNone(extracted)
        self.assertTrue(extracted.startswith(b"<ownershipDocument>"))
        self.assertEqual(parse_ownership_xml(extracted, filing("4")).filing.form_type, "4")
        self.assertIsNone(extract_ownership_xml(b"<html>rendered filing</html>"))

    def test_classification_taxonomy_does_not_collapse_events(self):
        self.assertEqual(classify_transaction("P", "A").transaction_type, "open_market_purchase")
        self.assertEqual(classify_transaction("S", "D").transaction_type, "open_market_sale")
        self.assertEqual(classify_transaction("M", "A").transaction_type, "option_exercise")
        self.assertEqual(classify_transaction("A", "A", security_title="RSU vesting").transaction_type, "rsu_vesting")
        self.assertEqual(classify_transaction("F", "D").transaction_type, "tax_withholding")
        self.assertEqual(classify_transaction("G", "D").transaction_type, "gift")

    def test_ambiguous_automatic_sale_does_not_invent_10b5_1_status(self):
        automatic = classify_transaction("S", "D", footnotes=["Sale made under an automatic sale program."])
        self.assertEqual(automatic.transaction_type, "planned_sale")
        self.assertIsNone(automatic.is_10b5_1)
        self.assertFalse(classify_transaction("S", "D", footnotes=["The sale was not pursuant to Rule 10b5-1."]).is_10b5_1)

    def test_provider_discovers_downloads_and_reuses_cached_filings(self):
        ticker_json = (FIXTURES / "company_tickers.json").read_bytes()
        submissions_json = (FIXTURES / "submissions.json").read_bytes()
        values = {SEC_TICKERS: ticker_json, f"{SEC_DATA}/submissions/CIK0001585521.json": submissions_json}
        for form, accession in (("4", "0001585521-26-000001"), ("3", "0001585521-26-000002"), ("5", "0001585521-26-000003")):
            url = f"{SEC_ARCHIVES}/1585521/{accession.replace('-', '')}/form{form}.xml"
            values[url] = (FIXTURES / f"form{form}.xml").read_bytes()
        transport = FakeTransport(values)
        with tempfile.TemporaryDirectory() as temp:
            provider = SECSourceProvider(Path(temp) / "sec", transport=transport, min_interval=0, sleep=lambda _: None)
            first = provider.acquire("ZM", include_historical=False)
            self.assertEqual(first.filings_discovered, 3)
            self.assertEqual(first.filings_processed, 3)
            self.assertEqual(len(first.parsed_filings), 3)
            calls_after_first = len(transport.calls)
            second = provider.acquire("ZM", include_historical=False)
            self.assertEqual(second.filings_reused, 3)
            self.assertEqual(len(transport.calls), calls_after_first)
            self.assertTrue((Path(temp) / "sec" / "ZM" / "filings" / "0001585521-26-000001" / "ownership.xml").exists())
            self.assertTrue(any("submissions/CIK0001585521.json" in call for call in transport.calls))

    def test_legacy_amendment_xml_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sec"
            directory = root / "ZM" / "filings" / "0001585521-26-000004"
            directory.mkdir(parents=True)
            legacy = directory / "doc4a.xml"
            legacy.write_bytes((FIXTURES / "form4.xml").read_bytes())
            (directory / "filing_metadata.json").write_text(json.dumps({
                "accession_number": "0001585521-26-000004",
                "form_type": "4/A",
                "issuer_cik": "0001585521",
                "filing_date": "2026-09-04",
                "report_date": "2026-09-03",
                "primary_document": "doc4a.xml",
                "source_url": "https://www.sec.gov/Archives/edgar/data/1585521/000158552126000004/doc4a.xml",
            }), encoding="utf-8")
            transport = CaptureTransport([])
            provider = SECSourceProvider(root, transport=transport, min_interval=0, sleep=lambda _: None)
            body, cached = provider.download_filing(filing("4/A", "0001585521-26-000004"))
            self.assertTrue(cached.cache_hit)
            self.assertEqual(body, legacy.read_bytes())
            self.assertEqual(transport.calls, [])

    def test_raw_submission_fallback_skips_rendered_html(self):
        current = filing("4", "0001585521-26-000005")
        base = f"{SEC_ARCHIVES}/1585521/{current.accession_number.replace('-', '')}"
        raw_url = f"{base}/{current.accession_number}.txt"
        values = {
            current.source_url: b"<html><title>SEC FORM 4</title></html>",
            raw_url: b"SEC HEADER\n<XML>\n" + (FIXTURES / "form4.xml").read_bytes() + b"\n</XML>",
        }
        transport = FakeTransport(values)
        with tempfile.TemporaryDirectory() as temp:
            provider = SECSourceProvider(Path(temp) / "sec", transport=transport, min_interval=0, sleep=lambda _: None)
            body, downloaded = provider.download_filing(current)
            self.assertEqual(downloaded.source_url, raw_url)
            self.assertTrue(body.startswith(b"<ownershipDocument>"))
            self.assertTrue((Path(temp) / "sec" / "ZM" / "filings" / current.accession_number / "ownership.xml").exists())

    def test_sec_headers_and_official_endpoint_hosts(self):
        transport = CaptureTransport([SECResponse(200, b"{}", {}), SECResponse(200, b"{}", {})])
        provider = SECSourceProvider(tempfile.mkdtemp(), transport=transport, min_interval=0, sleep=lambda _: None)
        provider._request(SEC_TICKERS)
        provider._request(f"{SEC_DATA}/submissions/CIK0001585521.json")
        self.assertEqual(transport.calls[0][0], SEC_TICKERS)
        self.assertEqual(transport.calls[1][0], f"{SEC_DATA}/submissions/CIK0001585521.json")
        for _, headers in transport.calls:
            self.assertEqual(headers["User-Agent"], SEC_USER_AGENT)
            self.assertEqual(headers["Accept-Encoding"], SEC_ACCEPT_ENCODING)
            self.assertEqual(headers["Accept"], SEC_ACCEPT)
        self.assertEqual(provider.diagnostics[0]["host"], "www.sec.gov")
        self.assertEqual(provider.diagnostics[1]["host"], "data.sec.gov")

    def test_gzip_and_deflate_responses_are_decoded(self):
        for encoding, body in (("gzip", gzip.compress(b'{"ok": true}')), ("deflate", zlib.compress(b'{"ok": true}'))):
            transport = CaptureTransport([SECResponse(200, body, {"Content-Encoding": encoding})])
            provider = SECSourceProvider(tempfile.mkdtemp(), transport=transport, min_interval=0, sleep=lambda _: None)
            response = provider._request(SEC_DATA + "/compressed")
            self.assertEqual(response.body, b'{"ok": true}')
            self.assertNotIn("content-encoding", response.headers)

    def test_rate_limiter_defaults_to_five_requests_per_second(self):
        sleeps = []
        transport = CaptureTransport([SECResponse(200, b"{}", {}), SECResponse(200, b"{}", {})])
        provider = SECSourceProvider(tempfile.mkdtemp(), transport=transport, sleep=sleeps.append)
        self.assertEqual(provider.max_requests_per_second, 5.0)
        self.assertEqual(provider.min_interval, 0.2)
        provider._request(SEC_TICKERS)
        provider._request(SEC_TICKERS)
        self.assertTrue(any(delay >= 0.19 for delay in sleeps))

    def test_transient_statuses_retry_with_bounded_backoff(self):
        sleeps = []
        transport = CaptureTransport([
            SECResponse(429, b"", {}),
            SECResponse(503, b"", {}),
            SECResponse(200, b"{}", {}),
        ])
        provider = SECSourceProvider(tempfile.mkdtemp(), transport=transport, min_interval=0, retries=2, sleep=sleeps.append)
        response = provider._request(SEC_DATA + "/retry")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps[:2], [1.0, 2.0])

    def test_forbidden_status_is_bounded_and_explicit(self):
        sleeps = []
        transport = CaptureTransport([SECResponse(403, b"", {}), SECResponse(403, b"", {}), SECResponse(200, b"{}", {})])
        provider = SECSourceProvider(tempfile.mkdtemp(), transport=transport, min_interval=0, retries=3, forbidden_retries=1, forbidden_delay=0.5, sleep=sleeps.append)
        with self.assertRaisesRegex(Exception, "SEC_ACCESS_FORBIDDEN") as context:
            provider._request(SEC_TICKERS)
        self.assertEqual(context.exception.code, "SEC_ACCESS_FORBIDDEN")
        self.assertEqual(context.exception.status, 403)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [0.5])
        self.assertIn("declared_user_agent=yes", str(context.exception))

    def test_store_is_idempotent_and_requires_provenance(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        parsed.transactions = [reconcile_transaction(item) for item in parsed.transactions]
        with tempfile.TemporaryDirectory() as temp:
            store = InsiderStore(temp)
            first = store.upsert_parsed(parsed)
            second = store.upsert_parsed(parsed)
            self.assertEqual(first["people"]["added"], 1)
            self.assertEqual(second["transactions"]["unchanged"], 3)
            self.assertEqual(len(store.get_purchases("ZM")), 1)
            bad = replace(parsed.transactions[0], source_url="")
            with self.assertRaises(ValueError):
                store.upsert_transactions([bad])

    def test_official_sec_record_cannot_be_overwritten_by_lower_authority(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        official = reconcile_transaction(parsed.transactions[0])
        third_party = replace(official, source_authority="third_party", price=1.0)
        with tempfile.TemporaryDirectory() as temp:
            store = InsiderStore(temp)
            store.upsert_transactions([official])
            result = store.upsert_transactions([third_party])
            self.assertEqual(result["unchanged"], 1)
            self.assertEqual(store.list_transactions()[0].price, official.price)

    def test_reconciliation_and_transaction_significance(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        purchase = reconcile_transaction(parsed.transactions[0])
        self.assertEqual(purchase.pre_transaction_shares, 10000.0)
        self.assertEqual(purchase.percent_of_pre_transaction_holdings, 10.0)
        self.assertEqual(purchase.percent_of_post_transaction_holdings, 1000 / 11000 * 100)

    def test_stable_ids_null_holdings_and_overlap_warning(self):
        first = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        second = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        self.assertEqual(first.people[0].person_id, second.people[0].person_id)
        self.assertEqual([item.transaction_id for item in first.transactions], [item.transaction_id for item in second.transactions])
        unknown = reconcile_transaction(replace(first.transactions[0], shares_owned_after=None, pre_transaction_shares=None, pre_holdings_method="unknown"))
        self.assertIsNone(unknown.pre_transaction_shares)
        self.assertEqual(unknown.pre_holdings_method, "unknown")
        position = parse_ownership_xml((FIXTURES / "form3.xml").read_bytes(), filing("3", "0001585521-26-000002")).ownership_positions[0]
        overlap = replace(position, ownership_id="overlap", direct_indirect="I", nature_of_indirect_ownership="Family trust")
        retained, warnings = avoid_double_counting_positions([position, overlap])
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(warnings), 1)

    def test_clusters_require_distinct_discretionary_open_market_insiders(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        first = parsed.transactions[0]
        second = replace(first, transaction_id="second", person_id="sec:0001585521:0001000003", transaction_date="2026-09-10")
        vesting = replace(first, transaction_id="vesting", person_id="sec:0001585521:0001000004", transaction_type="rsu_vesting", is_open_market=False)
        clusters = detect_clusters([first, second, vesting])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(set(clusters[0].people), {first.person_id, second.person_id})
        self.assertNotIn("vesting", clusters[0].transaction_ids)

    def test_alignment_snapshot_is_factual_and_excludes_automatic_sales(self):
        parsed = parse_ownership_xml((FIXTURES / "form4.xml").read_bytes(), filing("4"))
        snapshot, clusters = build_alignment_snapshot("ZM", "2026-09-30", parsed.transactions, parsed.people, [])
        self.assertEqual(snapshot.open_market_purchases_30d, 1)
        self.assertEqual(snapshot.open_market_sales_30d, 0)
        self.assertEqual(snapshot.purchase_value_365d, 60000.0)
        self.assertEqual(snapshot.known_insider_percent, None)


if __name__ == "__main__":
    unittest.main()
