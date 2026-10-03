"""Evidence-backed director expertise derived from Component 6A career facts."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from .board_governance_models import BoardExpertise, GovernanceBoardMembership
from .models import CareerEntry


_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "finance": ("finance", "financial", "cfo", "accounting", "audit", "treasurer", "investment banker"),
    "accounting": ("accounting", "auditor", "audit committee", "certified public accountant", "cpa"),
    "capital_markets": ("capital markets", "investment banking", "private equity", "venture capital", "investor"),
    "operations": ("operations", "operating officer", "chief operating", "supply chain", "manufacturing"),
    "technology": ("technology", "software", "information technology", "digital", "computer", "engineering", "artificial intelligence", " ai "),
    "cybersecurity": ("cybersecurity", "cyber security", "network security", "information security"),
    "biotechnology": ("biotech", "biotechnology", "life sciences"),
    "pharmaceuticals": ("pharmaceutical", "pharma", "drug development", "medicinal chemistry"),
    "clinical": ("clinical", "clinical-stage", "clinical trials"),
    "regulatory": ("regulatory", "government", "public policy", "federal", "compliance"),
    "legal": ("legal", "law", "attorney", "general counsel"),
    "sales": ("sales", "revenue", "commercial"),
    "marketing": ("marketing", "brand", "communications"),
    "international": ("international", "global", "europe", "asia"),
    "M&A": ("merger", "acquisition", "m&a", "corporate development"),
    "consumer": ("consumer", "retail", "e-commerce"),
    "government": ("government", "defense", "military", "public sector"),
}


def _categories(text: str) -> list[str]:
    lowered = " " + re.sub(r"\s+", " ", text.lower()) + " "
    return sorted(category for category, terms in _CATEGORY_TERMS.items() if any(term in lowered for term in terms))


def build_expertise(memberships: Iterable[GovernanceBoardMembership], career: Iterable[CareerEntry]) -> list[BoardExpertise]:
    by_person: dict[str, list[CareerEntry]] = defaultdict(list)
    for entry in career:
        by_person[entry.person_id].append(entry)
    result: list[BoardExpertise] = []
    for member in memberships:
        entries = by_person.get(member.person_id, [])
        evidence = " ".join(
            [entry.evidence_text for entry in entries]
            + [entry.title or "" for entry in entries]
            + [" ".join(entry.functional_areas) for entry in entries]
            + [" ".join(entry.industry_categories) for entry in entries]
        ).strip()
        categories = _categories(evidence) if evidence else []
        result.append(BoardExpertise(
            expertise_id=f"governance-expertise:{member.ticker}:{member.person_id}", ticker=member.ticker,
            person_id=member.person_id, categories=categories,
            career_ids=sorted({entry.career_id for entry in entries if entry.career_id}),
            evidence_text=evidence[:4000],
            source_urls=sorted({url for entry in entries for url in entry.source_urls}),
            source_accessions=sorted({acc for entry in entries for acc in entry.source_accession_numbers}),
        ))
    return sorted(result, key=lambda item: item.person_id)


def expertise_counts(records: Iterable[BoardExpertise]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        for category in record.categories:
            counts[category] += 1
    return dict(sorted(counts.items()))


__all__ = ["build_expertise", "expertise_counts"]
