from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from management_intelligence.governance_cli import build_governance
from management_intelligence.governance_sources import GovernanceSourceCatalog
from management_intelligence.main import ManagementIntelligenceProvider
from management_intelligence.normalized_sources import NormalizedSECSourceCatalog
from management_intelligence.sec_sources import SECManagementSourceProvider
from management_intelligence.store import ManagementStore
from management_intelligence.models import OfficialSource
from insider_intelligence.sec_ingestion import SECSourceProvider


PROXY_HTML = """
<html><body>
<table><tr><th>Name</th><th>Position</th></tr>
<tr><td>Alice Example</td><td>Chief Executive Officer</td></tr></table>
<table><tr><th>Name</th><th>Director Since</th><th>Occupation</th></tr>
<tr><td>Bob Smith</td><td>2020</td><td>Director</td></tr></table>
<p>All directors are independent, except for our CEO.</p>
</body></html>
"""


def _row(*, form: str, accession: str, filing_date: str, title: str, html: str = "<html><body>official filing</body></html>") -> dict:
    url = f"https://www.sec.gov/Archives/edgar/data/1045810/{accession.replace('-', '')}/primary.htm"
    return {
        "document_id": f"doc-{accession}",
        "ticker": "NVDA",
        "company_name": "NVIDIA Corporation",
        "title": title,
        "source_url": url,
        "filing_date": filing_date,
        "available_date": filing_date,
        "text": "official filing",
        "content_hash": accession,
        "metadata": {
            "source_family": "SEC_EDGAR",
            "sec_form": form,
            "issuer_cik": "0001045810",
            "accession_number": accession,
            "content_html": html,
        },
    }


class FiscalAndSECRoutingTests(unittest.TestCase):
    def test_normalized_sec_catalog_routes_proxy_and_supporting_filings_with_cutoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                _row(form="DEF 14A", accession="0001045810-26-000036", filing_date="2026-05-12", title="NVIDIA DEF 14A", html=PROXY_HTML),
                _row(form="DEF 14A", accession="0001045810-26-000036", filing_date="2026-05-12", title="NVIDIA DEF 14A duplicate", html=PROXY_HTML),
                _row(form="10-K", accession="0001045810-26-000010", filing_date="2026-02-20", title="NVIDIA 10-K"),
                _row(form="8-K", accession="0001045810-26-000080", filing_date="2026-08-01", title="NVIDIA 8-K"),
                _row(form="DEF 14A", accession="0001045810-26-000099", filing_date="2026-10-01", title="Future DEF 14A", html=PROXY_HTML),
            ]
            (root / "documents.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            catalog = NormalizedSECSourceCatalog(root)
            sources = catalog.sources("NVDA", as_of_date="2026-09-25")
            self.assertEqual(len(sources), 3)
            self.assertEqual({source.form_type for source in sources}, {"DEF 14A", "10-K", "8-K"})
            latest = catalog.latest_proxy("NVDA", as_of_date="2026-09-25")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.accession_number, "0001045810-26-000036")
            self.assertLessEqual(latest.filing_date, "2026-09-25")

    def test_component_6a_and_6c_consume_the_same_normalized_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            normalized = root / "normalized"
            normalized.mkdir()
            rows = [
                _row(form="DEF 14A", accession="0001045810-26-000036", filing_date="2026-05-12", title="NVIDIA DEF 14A", html=PROXY_HTML),
                _row(form="10-K", accession="0001045810-26-000010", filing_date="2026-02-20", title="NVIDIA 10-K"),
                _row(form="8-K", accession="0001045810-26-000080", filing_date="2026-08-01", title="NVIDIA 8-K"),
            ]
            (normalized / "documents.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            store_root = root / "management"
            sec = SECManagementSourceProvider(
                SECSourceProvider(cache_root=root / "sec-cache"), normalized_root=normalized,
            )
            result = ManagementIntelligenceProvider(
                store=ManagementStore(store_root), sec=sec,
            ).acquire("NVDA", as_of_date="2026-09-25")
            self.assertTrue(result.people)
            self.assertTrue(any(person.full_name == "Alice Example" for person in result.people))
            self.assertTrue(result.boards)
            self.assertIn("10-K", {item.form_type for item in sec.supporting_filings("NVDA", as_of_date="2026-09-25")})
            self.assertIn("8-K", {item.form_type for item in sec.supporting_filings("NVDA", as_of_date="2026-09-25")})
            self.assertIn("DEF 14A", {item.source_type for item in ManagementStore(store_root).sources.list_all()})
            governance = build_governance(
                "NVDA", as_of_date="2026-09-25", store_root=store_root,
                sec_cache_root=root / "sec-cache", normalized_root=normalized,
            )
            self.assertEqual(governance["proxy"].accession_number, "0001045810-26-000036")
            self.assertGreaterEqual(governance["snapshot"].board_size, 1)
            self.assertEqual(governance["snapshot"].as_of_date, "2026-09-25")

    def test_proxy_exists_but_parse_failure_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            normalized = root / "normalized"
            normalized.mkdir()
            row = _row(form="DEF 14A", accession="0001045810-26-000036", filing_date="2026-05-12", title="NVIDIA DEF 14A", html="<html><body>unstructured filing</body></html>")
            support = _row(form="8-K", accession="0001045810-26-000080", filing_date="2026-08-01", title="NVIDIA 8-K")
            (normalized / "documents.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(support) + "\n", encoding="utf-8")
            governance = build_governance("NVDA", as_of_date="2026-09-25", store_root=root / "management", sec_cache_root=root / "sec-cache", normalized_root=normalized)
            self.assertNotIn("no cached DEF 14A proxy available", governance["warnings"])
            self.assertIn("PROXY_PARSE_FAILED", governance["warnings"])

    def test_normalized_proxy_text_wins_over_binary_cached_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            normalized = root / "normalized"
            normalized.mkdir()
            local = root / "NVDA" / "proxy.pdf"
            local.parent.mkdir()
            local.write_bytes(b"%PDF-binary-cache")
            store_root = root / "management"
            ManagementStore(store_root).sources.upsert_many([OfficialSource(
                source_id="sec:0001045810:0001045810-26-000036",
                source_authority="sec_official_filing", source_type="DEF 14A",
                url="https://www.sec.gov/Archives/edgar/data/1045810/proxy.htm",
                local_path=str(local), issuer_cik="0001045810",
                accession_number="0001045810-26-000036", filing_date="2026-05-12",
            )])
            row = _row(form="DEF 14A", accession="0001045810-26-000036", filing_date="2026-05-12", title="NVIDIA DEF 14A")
            row["text"] = "Normalized proxy text with independent board evidence"
            row["metadata"]["content_html"] = ""
            (normalized / "documents.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            proxy = GovernanceSourceCatalog(store_root=store_root, sec_cache_root=root / "sec-cache", normalized_root=normalized, as_of_date="2026-09-25").latest_proxy("NVDA")
            self.assertIsNotNone(proxy)
            self.assertIn("Normalized proxy text", proxy.text)


if __name__ == "__main__":
    unittest.main()
