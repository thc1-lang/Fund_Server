from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qualitative_analysis.evidence import EvidenceValidationError, assert_valid_claim, validate_claim
from qualitative_analysis.extraction_models import QualitativeClaim, register_dimension
from qualitative_analysis.extraction_store import ExtractionStore
from qualitative_analysis.models import NormalizedDocument
from qualitative_analysis.qualitative_extraction import chunk_document, extract_document
from qualitative_analysis.extraction_rules import is_material_sentence


def _document(text: str, kind: str = "earnings_transcript", segments=None, metadata=None) -> NormalizedDocument:
    return NormalizedDocument(
        document_id=f"doc-{abs(hash((text, kind))) % 100000}", ticker="INCY", company_name="Incyte",
        document_type=kind, document_subtype=None, title="Q2 2026 results", publication_date="2026-07-28",
        event_date="2026-07-28", fiscal_year=2026, fiscal_quarter=2, period_label="Q2 FY2026",
        source_url="https://example.test/doc", local_path="doc.txt", source_method="test", text=text,
        text_length=len(text), content_hash=f"hash-{len(text)}", metadata={"segments": segments or [], **(metadata or {})},
    )


class QualitativeExtractionTests(unittest.TestCase):
    def test_transcript_claims_have_exact_evidence_and_timestamp_provenance(self):
        text = "Revenue increased 12% year over year. Management raised full-year guidance."
        segments = [
            {"start": 1.0, "end": 4.0, "text": "Revenue increased 12% year over year."},
            {"start": 4.1, "end": 8.0, "text": "Management raised full-year guidance."},
        ]
        doc = _document(text, segments=segments)
        result = extract_document(doc)
        self.assertGreaterEqual(len(result.claims), 2)
        for claim in result.claims:
            self.assertEqual(doc.text[claim.source_location["start_char"]:claim.source_location["end_char"]], claim.evidence_text)
            self.assertTrue(claim.source_location["segment_ids"])
            self.assertLessEqual(claim.source_location["start_timestamp"], claim.source_location["end_timestamp"])
            self.assertEqual(validate_claim(claim, doc).valid, True)

    def test_news_and_pdf_locations_use_character_offsets(self):
        text = "Demand improved 8% in the quarter."
        doc = _document(text, "news_release", metadata={"page_offsets": [{"page": 1, "start_char": 0, "end_char": len(text)}]})
        result = extract_document(doc)
        self.assertTrue(result.claims)
        claim = next(c for c in result.claims if c.dimension == "demand")
        self.assertEqual(claim.source_location["page_start"], 1)
        self.assertEqual(text[claim.source_location["start_char"]:claim.source_location["end_char"]], claim.evidence_text)

    def test_actual_beat_against_guidance_is_not_guidance_raised(self):
        doc = _document("This result was 8 cents above the high end of our guidance.", "news_release")
        claim = extract_document(doc).claims[0]
        self.assertEqual(claim.direction, "increasing")
        self.assertEqual(claim.certainty, "actual")

    def test_completed_actions_are_confirmed_even_with_planning_context(self):
        doc = _document("The product was approved in the quarter, representing the first of two launches planned this year.", "news_release")
        claim = extract_document(doc).claims[0]
        self.assertEqual(claim.certainty, "confirmed")
        conditional = extract_document(_document("If approved, the product would become available next year.", "news_release")).claims[0]
        self.assertEqual(conditional.certainty, "possibility")

    def test_invalid_evidence_is_rejected(self):
        doc = _document("Revenue increased.")
        claim = QualitativeClaim(
            claim_id="bad", ticker="INCY", company_name="Incyte", document_id=doc.document_id,
            document_type=doc.document_type, fiscal_year=2026, fiscal_quarter=2, period_label="Q2 FY2026",
            dimension="revenue", topic="revenue", subtopic=None, claim_text="made up", direction="increasing",
            magnitude=None, certainty="actual", evidence_text="not in source", source_location={"start_char": 0, "end_char": 12},
            source_url=doc.source_url, local_path=doc.local_path, extraction_method="test", extraction_confidence=1.0,
            created_at="now", source_content_hash=doc.content_hash,
        )
        self.assertFalse(validate_claim(claim, doc).valid)
        with self.assertRaises(EvidenceValidationError):
            assert_valid_claim(claim, doc)

    def test_materiality_filters_noise_and_questions(self):
        self.assertFalse(is_material_sentence("Revenue was discussed on the call."))
        self.assertFalse(is_material_sentence("What are some of the features or use cases?"))
        self.assertFalse(is_material_sentence("Thank you for joining today's call."))
        self.assertFalse(is_material_sentence("Operator instructions are now complete. Please go ahead."))
        self.assertFalse(is_material_sentence("Turning to the guidance."))
        doc = _document("Revenue was discussed on the call. What are some of the features? Revenue increased 15% year over year.", "news_release")
        result = extract_document(doc)
        self.assertEqual(len(result.claims), 1)
        self.assertEqual(result.claims[0].direction, "increasing")

    def test_guidance_and_certainty_are_not_promoted_to_actual(self):
        doc = _document("We expect revenue of $500 million. Revenue increased 15% year over year. Revenue may decline if demand weakens.", "news_release")
        claims = extract_document(doc).claims
        guidance = next(c for c in claims if c.dimension == "guidance")
        actual = next(c for c in claims if c.evidence_text.startswith("Revenue increased"))
        possibility = next(c for c in claims if c.evidence_text.startswith("Revenue may"))
        self.assertEqual((guidance.direction, guidance.certainty), ("unknown", "guidance"))
        self.assertEqual((actual.direction, actual.certainty), ("increasing", "actual"))
        self.assertEqual(possibility.certainty, "possibility")

    def test_one_sentence_has_one_primary_dimension_and_exact_repeats_deduplicate(self):
        sentence = "We raised revenue guidance to $500 million."
        doc = _document(sentence + " " + sentence, "news_release")
        claims = extract_document(doc).claims
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].dimension, "guidance")

    def test_conservative_semantic_duplicate_uses_same_magnitude_and_overlap(self):
        doc = _document(
            "Excluding this benefit, TotalNet sales increased 17%. "
            "Excluding the one-time benefit, total net sales increased 17% versus the prior year.",
            "news_release",
        )
        claims = extract_document(doc).claims
        self.assertEqual(len(claims), 1)

    def test_taxonomy_direction_and_certainty_are_controlled_but_extensible(self):
        doc = _document("Revenue increased 12% year over year.", "news_release")
        claim = extract_document(doc).claims[0]
        claim.dimension = "unsupported_dimension"
        self.assertFalse(validate_claim(claim, doc).valid)
        claim.dimension = register_dimension("custom_topic")
        claim.direction = "not-a-direction"
        self.assertFalse(validate_claim(claim, doc).valid)
        claim.direction = "increasing"
        claim.certainty = "not-a-certainty"
        self.assertFalse(validate_claim(claim, doc).valid)

    def test_store_is_idempotent_and_queries_by_document_and_dimension(self):
        doc = _document("Revenue increased 12% year over year.", "news_release")
        result = extract_document(doc)
        self.assertTrue(result.claims)
        with tempfile.TemporaryDirectory() as temp:
            store = ExtractionStore(Path(temp))
            first = store.upsert_many(result.claims)
            second = store.upsert_many(result.claims)
            self.assertEqual(first["added"], len(result.claims))
            self.assertEqual(second["unchanged"], len(result.claims))
            self.assertEqual(len(store.get_by_document_id(doc.document_id)), len(result.claims))
            self.assertTrue(store.get_by_dimension("revenue"))
            state = store.get_document_state(doc.document_id)
            self.assertEqual(state["source_content_hash"], doc.content_hash)

    def test_store_retries_transient_windows_replace_lock(self):
        doc = _document("Revenue increased 12% year over year.")
        claims = extract_document(doc).claims
        with tempfile.TemporaryDirectory() as temp:
            store = ExtractionStore(Path(temp))
            original_replace = __import__("qualitative_analysis.extraction_store", fromlist=["os"]).os.replace
            calls = 0

            def locked_once(source, destination):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("file is temporarily locked")
                return original_replace(source, destination)

            with patch("qualitative_analysis.extraction_store.os.replace", side_effect=locked_once), patch("qualitative_analysis.extraction_store.time.sleep"):
                store.upsert_many(claims)
            self.assertGreaterEqual(calls, 2)
            self.assertEqual(len(store.get_by_document_id(doc.document_id)), len(claims))

    def test_content_hash_and_extraction_version_change_require_replacement(self):
        original = _document("Revenue increased 12% year over year.", "news_release")
        original.document_id = "stable-document-id"
        first = extract_document(original).claims
        with tempfile.TemporaryDirectory() as temp:
            store = ExtractionStore(Path(temp))
            store.upsert_many(first)
            changed = _document("Revenue decreased 4% year over year.", "news_release")
            changed.document_id = original.document_id
            changed.content_hash = "changed-content"
            state = store.get_document_state(changed.document_id)
            self.assertNotEqual(state["source_content_hash"], changed.content_hash)
            store.remove_for_document(changed.document_id)
            second = extract_document(changed).claims
            for claim in second:
                claim.extraction_version = "qualitative-extraction-v9"
            store.upsert_many(second)
            self.assertEqual(store.get_document_state(changed.document_id)["extraction_version"], "qualitative-extraction-v9")

    def test_chunk_offsets_are_monotonic_and_overlap_does_not_duplicate_claims(self):
        text = " ".join(["Demand improved in the quarter."] * 80)
        doc = _document(text, "news_release")
        chunks = chunk_document(doc, max_chars=300, overlap_chars=60)
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].start_char, 0)
        self.assertTrue(all(a.start_char < a.end_char for a in chunks))
        result = extract_document(doc)
        identities = {(c.dimension, c.evidence_text, c.source_location["start_char"]) for c in result.claims}
        self.assertEqual(len(identities), len(result.claims))
        self.assertEqual(len([c for c in result.claims if c.dimension == "demand"]), 1)

