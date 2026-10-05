from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_output.assembler import assemble_dossier
from research_output.renderer_markdown import render_markdown
from research_output.store import output_paths, write_dossier


ROOT = Path(__file__).resolve().parents[1]


class ResearchOutputRealArtifactTests(unittest.TestCase):
    @staticmethod
    def _fixture_root(root: Path) -> Path:
        """Create immutable test inputs rather than selecting the live latest row."""
        for sub in ["qualitative_analysis", "insider_intelligence", "management_intelligence", "scoring_engine"]:
            (root / sub).mkdir(parents=True, exist_ok=True)

        def put(sub: str, name: str, rows: list[dict]) -> None:
            path = root / sub / name
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")

        score_rows = [
            {"ticker": "ZM", "profile": "core_v1", "profile_version": "core-v1.2", "scoring_version": "qualitative-scoring-v1.2", "coverage_version": "coverage-v3", "as_of_date": "2026-09-24", "created_at": "2026-09-24T12:00:00+00:00", "overall_score": None, "score_status": "INSUFFICIENT_SCORING_COVERAGE", "overall_evidence_coverage": 0.5702, "overall_scoring_coverage": 0.2510, "overall_confidence": 0.56, "coverage_limitations": []},
            {"ticker": "PLTR", "profile": "core_v1", "profile_version": "core-v1.2", "as_of_date": "2026-09-24", "created_at": "2026-09-24T12:00:00+00:00", "overall_score": None, "score_status": "INSUFFICIENT_SCORING_COVERAGE", "overall_evidence_coverage": 0.0, "overall_scoring_coverage": 0.0, "overall_confidence": 0.0},
            {"ticker": "EXEL", "profile": "core_v1", "profile_version": "core-v1.2", "as_of_date": "2026-09-24", "created_at": "2026-09-24T12:00:00+00:00", "overall_score": None, "score_status": "INSUFFICIENT_SCORING_COVERAGE"},
            {"ticker": "INCY", "profile": "core_v1", "profile_version": "core-v1.2", "as_of_date": "2026-09-24", "created_at": "2026-09-24T12:00:00+00:00", "overall_score": None, "score_status": "INSUFFICIENT_SCORING_COVERAGE"},
        ]
        put("scoring_engine", "company_scores.jsonl", score_rows)
        put("scoring_engine", "pillar_scores.jsonl", [
            {"ticker": "ZM", "pillar": "BUSINESS_QUALITY", "pillar_score_id": "pillar:ZM:core-v1.2:business-quality", "score": None, "calculated_score": None, "coverage_status": "INSUFFICIENT_SCORING_COVERAGE", "evidence_coverage": 0.0, "scoring_coverage": 0.0, "confidence": 0.0},
            {"ticker": "ZM", "pillar": "BUSINESS_TRAJECTORY", "pillar_score_id": "pillar:ZM:core-v1.2:business-trajectory", "score": 58.333333, "calculated_score": 58.333333, "coverage_status": "INSUFFICIENT_SCORING_COVERAGE", "evidence_coverage": 0.5, "scoring_coverage": 0.5, "confidence": 0.56},
            {"ticker": "ZM", "pillar": "GOVERNANCE", "pillar_score_id": "pillar:ZM:core-v1.2:governance", "score": None, "calculated_score": 78.8889, "coverage_status": "INSUFFICIENT_SCORING_COVERAGE", "evidence_coverage": 0.5, "scoring_coverage": 0.5, "confidence": 0.56},
        ])
        put("qualitative_analysis", "documents.jsonl", [{"ticker": "EXEL", "document_id": f"EXEL-doc-{i}"} for i in range(720)])
        put("qualitative_analysis", "claims.jsonl", [{"ticker": "INCY", "claim_id": f"INCY-claim-{i}"} for i in range(78)])
        for name in ("qualitative_states.jsonl", "temporal_changes.jsonl"):
            put("qualitative_analysis", name, [])
        for sub, names in {
            "insider_intelligence": ("alignment_snapshots.jsonl", "ownership.jsonl", "transactions.jsonl", "filings.jsonl"),
            "management_intelligence": ("people.jsonl", "roles.jsonl", "role_changes.jsonl", "guidance_coverage_diagnostics.jsonl", "track_record_snapshots.jsonl", "guidance_commitments.jsonl", "guidance_outcomes.jsonl", "strategic_commitments.jsonl", "capital_allocation_events.jsonl", "governance_snapshots.jsonl", "governance_committees.jsonl", "governance_shareholder_rights.jsonl", "governance_voting_control.jsonl", "governance_expertise.jsonl"),
        }.items():
            for name in names:
                put(sub, name, [])
        return root

    def _assemble(self, root: Path, ticker: str):
        return assemble_dossier(ticker, "2026-09-24", artifacts_root=root)

    def test_zm_reproduces_frozen_scoring_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._assemble(self._fixture_root(Path(tmp)), "ZM")
        self.assertIsNone(d.scoring["overall_score"])
        self.assertEqual(d.scoring["status"], "INSUFFICIENT_SCORING_COVERAGE")
        self.assertAlmostEqual(d.scoring["evidence_coverage"], 0.5702, places=4)
        self.assertAlmostEqual(d.scoring["scoring_coverage"], 0.2510, places=4)
        self.assertAlmostEqual(d.scoring["confidence"], 0.56, places=4)
        self.assertEqual(d.scoring["pillars"]["BUSINESS_QUALITY"]["score"], None)
        self.assertAlmostEqual(d.scoring["pillars"]["BUSINESS_TRAJECTORY"]["score"], 58.333333, places=4)
        self.assertEqual(d.scoring["pillars"]["GOVERNANCE"]["score"], None)
        self.assertAlmostEqual(d.scoring["pillars"]["GOVERNANCE"]["calculated_score"], 78.8889, places=3)

    def test_zm_report_is_evidence_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._assemble(self._fixture_root(Path(tmp)), "ZM")
            text = render_markdown(d)
        self.assertIn("Overall score: Not issued", text)
        self.assertIn("**Evidence coverage:** 57.0%", text)
        self.assertIn("**Scoring coverage:** 25.1%", text)
        self.assertIn("not yet been empirically calibrated", text)
        self.assertIn("No discretionary insider trading signal identified", text)
        self.assertIn("Company-level outcomes with TENURE_OVERLAP", text)
        self.assertNotIn("Buy", text)
        self.assertNotIn("Sell", text)

    def test_suppressed_scores_are_explain_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._assemble(self._fixture_root(Path(tmp)), "ZM")
            normal = render_markdown(d)
            explain = render_markdown(d, explain_scores=True)
        self.assertIn("GOVERNANCE:** displayed score Not issued", normal)
        self.assertNotIn("Internal calculated score (explain mode)", normal)
        self.assertIn("Internal calculated score (explain mode): 78.89", explain)

    def test_exel_document_claim_gap_is_neutral(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._assemble(self._fixture_root(Path(tmp)), "EXEL")
        self.assertEqual(d.metadata["documents"], 720)
        self.assertEqual(d.metadata["claims"], 0)
        text = render_markdown(d)
        self.assertIn("720 source documents are present but no qualitative claims", text)
        self.assertNotIn("weak business quality", text.lower())

    def test_pltr_critical_business_gap_and_incy_missing_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_root(Path(tmp))
            p = self._assemble(root, "PLTR")
            self.assertEqual(p.metadata["claims"], 0)
            self.assertEqual(p.scoring["status"], "INSUFFICIENT_SCORING_COVERAGE")
            i = self._assemble(root, "INCY")
            self.assertEqual(i.metadata["claims"], 78)
            self.assertFalse(i.governance.get("available"))
            self.assertEqual(i.management["people_count"], 0)


class ResearchOutputFixtureTests(unittest.TestCase):
    def _fixture(self):
        root = Path(tempfile.mkdtemp())
        for sub in ["qualitative_analysis", "insider_intelligence", "management_intelligence", "scoring_engine"]:
            (root / sub).mkdir()
        def put(sub, name, rows):
            (root / sub / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf8")
        put("qualitative_analysis", "qualitative_states.jsonl", [{"state_id":"s1","ticker":"ZZ","dimension":"revenue","current_state":"strong","trend":"improving","confidence":.9,"summary":"Revenue improved.","source_urls":["https://example.test/source"],"topic":"","subtopic":""}])
        put("qualitative_analysis", "temporal_changes.jsonl", [])
        put("qualitative_analysis", "claims.jsonl", [])
        put("qualitative_analysis", "documents.jsonl", [])
        put("scoring_engine", "company_scores.jsonl", [{"ticker":"ZZ","profile":"core_v1","profile_version":"core-v1.2","scoring_version":"qualitative-scoring-v1.2","coverage_version":"coverage-v3","as_of_date":"2026-09-24","overall_score":None,"score_status":"INSUFFICIENT_SCORING_COVERAGE","overall_evidence_coverage":.5,"overall_scoring_coverage":.1,"overall_confidence":.4,"coverage_limitations":[]}])
        put("scoring_engine", "pillar_scores.jsonl", [])
        return root

    def test_source_deduplication_and_stable_fingerprint(self):
        root = self._fixture()
        a = assemble_dossier("ZZ", "2026-09-24", artifacts_root=root)
        b = assemble_dossier("ZZ", "2026-09-24", artifacts_root=root)
        self.assertEqual(a.input_fingerprint, b.input_fingerprint)
        self.assertEqual(len([r for r in a.source_index if r.url]), 1)
        self.assertEqual(min(r.number for r in a.source_index), 1)
        self.assertEqual(a.dossier_id, b.dossier_id)

    def test_store_upsert_is_idempotent(self):
        root = self._fixture(); output = root / "out"; d = assemble_dossier("ZZ", "2026-09-24", artifacts_root=root)
        first = write_dossier(d, output, fmt="both")
        second = write_dossier(d, output, fmt="both")
        rows = (output / "dossiers.jsonl").read_text(encoding="utf8").splitlines()
        self.assertFalse(first["unchanged"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(len(rows), 1)

    def test_report_filenames_describe_qualitative_analysis(self):
        root = self._fixture()
        dossier = assemble_dossier("ZZ", "2026-09-24", artifacts_root=root)
        markdown, json_output = output_paths(dossier, root / "out")
        self.assertEqual(markdown.name, "ZZ_2026-09-24_core-v1.2_qualitative_analysis.md")
        self.assertEqual(json_output.name, "ZZ_2026-09-24_core-v1.2_qualitative_analysis.json")

    def test_unknown_governance_right_is_not_absence_and_no_section_padding(self):
        root = self._fixture(); d = assemble_dossier("ZZ", "2026-09-24", artifacts_root=root)
        self.assertFalse(d.governance.get("available"))
        text = render_markdown(d)
        self.assertIn("Not established from available official-source coverage", text)
        self.assertLessEqual(text.count("No supported records in the current store"), 2)


if __name__ == "__main__":
    unittest.main()
