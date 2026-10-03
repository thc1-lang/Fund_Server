"""Conservative education extraction from official biographies."""

from __future__ import annotations

import hashlib
import re

from .models import EducationEntry, OfficialSource


def extract_education(person_id: str, ticker: str, biography: str, source: OfficialSource) -> list[EducationEntry]:
    if not biography:
        return []
    entries: list[EducationEntry] = []
    # Preserve only explicit degree/institution statements.  The source text
    # remains attached so downstream users can review the extraction.
    pattern = re.compile(r"(?:holds?|earned|received|graduated with)\s+(?:an?\s+)?(?P<degree>[A-Z][A-Za-z. &'/-]{1,80}?)\s+(?:from|at)\s+(?P<institution>[A-Z][^.;,]{2,120})", re.I)
    for match in pattern.finditer(biography):
        degree = " ".join(match.group("degree").split()).strip(" ,")
        institution = " ".join(match.group("institution").split()).strip(" ,")
        # SEC biographies often continue with a second degree in the same
        # sentence ("University and an MBA from ..."). Keep the institution
        # tied to the degree we actually matched instead of storing the rest
        # of the sentence as part of its name.
        institution = re.split(r"\s+and\s+(?:an?|is|was)\b", institution, maxsplit=1, flags=re.I)[0].strip(" ,")
        if not degree or not institution:
            continue
        year_match = re.search(r"\b((?:19|20)\d{2})\b", match.group(0))
        education_id = "education:" + hashlib.sha256(f"{person_id}|{institution}|{degree}".encode()).hexdigest()[:24]
        entries.append(EducationEntry(
            education_id=education_id,
            person_id=person_id,
            ticker=ticker,
            institution=institution,
            degree=degree,
            year=year_match.group(1) if year_match else None,
            date_precision="year" if year_match else None,
            evidence_text=match.group(0),
            source_ids=[source.source_id],
            source_urls=[source.url],
            source_local_paths=[source.local_path] if source.local_path else [],
            source_accession_numbers=[source.accession_number] if source.accession_number else [],
            source_form_types=[source.source_type],
        ))
    return list({item.education_id: item for item in entries}.values())


__all__ = ["extract_education"]
