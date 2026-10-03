"""Repeatable quality checks for the Component 6A store."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from .store import ManagementStore


EXECUTIVE_CATEGORIES = {"CEO", "CFO", "COO", "CTO", "president", "general_counsel", "executive"}


def audit_store(store: ManagementStore, ticker: str, *, as_of: str | None = None) -> dict[str, Any]:
    """Return deterministic coverage, chronology, provenance and quality metrics."""
    ticker = ticker.upper()
    as_of = as_of or date.today().isoformat()
    people = store.list_people(ticker)
    roles = store.list_roles(ticker)
    career = store.list_career(ticker)
    boards = store.list_boards(ticker)
    relationships = store.list_relationships(ticker)
    education = store.list_education(ticker)
    links = store.list_insider_links(ticker)
    changes = store.list_role_changes(ticker)
    issuer_cik = next((item.issuer_cik for item in people if item.issuer_cik), None)

    duplicate_people: list[str] = []
    by_name: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for item in people:
        by_name[tuple(sorted(item.normalized_name.split()))].append(item.person_id)
    for ids in by_name.values():
        if len(ids) > 1:
            duplicate_people.extend(sorted(ids))

    chronology_errors: list[str] = []
    duplicate_career: list[str] = []
    career_keys: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    for item in career:
        if item.start_date and item.end_date and item.end_date < item.start_date:
            chronology_errors.append(item.career_id)
        career_keys[(item.person_id, item.employer.lower(), (item.title or "").lower(), item.start_date, item.end_date, item.event_type)].append(item.career_id)
    duplicate_career = sorted(value[0] for value in career_keys.values() if len(value) > 1)
    for item in boards:
        if item.start_date and item.end_date and item.end_date < item.start_date:
            chronology_errors.append(item.board_id)
    for item in roles:
        if item.start_date and item.end_date and item.end_date < item.start_date:
            chronology_errors.append(item.assertion_id)

    def missing_provenance(values) -> int:
        return sum(not (getattr(item, "source_ids", []) and getattr(item, "source_urls", []) and getattr(item, "source_local_paths", [])) for item in values)

    current_executives = {item.person_id for item in roles if item.is_current and item.role_category in EXECUTIVE_CATEGORIES}
    current_directors = {item.person_id for item in boards if item.is_current and item.board_cik == issuer_cik}
    future_departures = [item.person_name for item in changes if item.event_type == "departure" and item.effective_date and item.effective_date > as_of]
    return {
        "ticker": ticker,
        "issuer_cik": issuer_cik,
        "people": len(people),
        "current_executives": len(current_executives),
        "current_directors": len(current_directors),
        "roles": len(roles),
        "career": len(career),
        "prior_career": sum(not item.is_current for item in career),
        "public_prior_employers": sum(not item.is_current and item.employer_classification == "PUBLIC_COMPANY_CONFIRMED" for item in career),
        "unresolved_employers": sum(item.employer_classification == "UNRESOLVED" for item in career),
        "boards": len(boards),
        "education": len(education),
        "insider_links": len(links),
        "role_changes": len(changes),
        "identity_ambiguous": sum(item.identity_status == "ambiguous" for item in people),
        "duplicate_people": sorted(set(duplicate_people)),
        "duplicate_career": duplicate_career,
        "chronology_errors": sorted(set(chronology_errors)),
        "missing_provenance": {
            "people": missing_provenance(people),
            "roles": missing_provenance(roles),
            "career": missing_provenance(career),
            "boards": missing_provenance(boards),
            "relationships": missing_provenance(relationships),
            "education": missing_provenance(education),
        },
        "future_departures": future_departures,
    }


__all__ = ["audit_store", "EXECUTIVE_CATEGORIES"]
