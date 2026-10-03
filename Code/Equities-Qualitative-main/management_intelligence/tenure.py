"""Build execution tenures from the frozen Component 6A role records."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Iterable

from .models import CareerEntry, ManagementPerson, PersonCompanyRelationship, RoleAssertion, RoleChangeEvent
from .store import ManagementStore
from .track_record_models import ManagementTenure


EXECUTION_CATEGORIES = {"CEO", "CFO", "COO", "CTO", "president", "general_counsel", "executive", "founder"}
ROLE_PRIORITY = ("CEO", "CFO", "COO", "CTO", "president", "general_counsel", "executive", "founder")


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _canonical_role(assertion: RoleAssertion) -> str | None:
    title = f"{assertion.role} {assertion.title_as_reported or ''}".lower()
    if assertion.role_category == "CEO" or "chief executive officer" in title:
        return "CEO"
    if assertion.role_category == "CFO" or "chief financial officer" in title:
        return "CFO"
    if assertion.role_category == "COO" or "chief operating officer" in title:
        return "COO"
    if assertion.role_category == "CTO" or "chief technology officer" in title:
        return "CTO"
    if assertion.role_category == "general_counsel" or "general counsel" in title:
        return "general_counsel"
    if assertion.role_category == "president" and "chief executive officer" not in title and "executive vice president" not in title:
        return "president"
    if assertion.role_category == "executive" and not any(word in title for word in ("director", "chair", "chief executive", "chief financial", "chief operating", "chief technology")):
        return "executive"
    if assertion.role_category == "founder" and any(word in title for word in ("president", "chief", "executive")):
        return "founder"
    return None


def _precision(start: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if not start:
        return None
    if len(start) == 4:
        return "year"
    if len(start) == 7:
        return "month"
    if len(start) == 10:
        return "day"
    return "reported"


def _person_map(store: ManagementStore, ticker: str) -> dict[str, ManagementPerson]:
    return {item.person_id: item for item in store.list_people() if item.ticker.upper() == ticker.upper()}


def build_tenures(
    store: ManagementStore,
    ticker: str,
    *,
    as_of_date: str | None = None,
    include_prior_companies: bool = False,
) -> list[ManagementTenure]:
    """Create distinct role tenures; future departures remain current as-of date."""
    as_of = as_of_date or date.today().isoformat()
    people = _person_map(store, ticker)
    assertions = store.list_roles(ticker)
    relationships = {item.person_company_id: item for item in store.list_relationships(ticker)}
    changes = store.list_role_changes(ticker)
    scheduled: dict[tuple[str, str], str] = {}
    for change in changes:
        if change.event_type == "departure" and change.person_id and change.effective_date and change.effective_date > as_of:
            scheduled[(change.person_id, (change.role or "director").lower())] = change.effective_date
    values: dict[str, ManagementTenure] = {}
    for assertion in assertions:
        category = _canonical_role(assertion)
        if category is None or assertion.person_id not in people:
            continue
        role = assertion.title_as_reported or assertion.role
        start = assertion.start_date or people[assertion.person_id].current_since
        end = assertion.end_date
        current = bool(assertion.is_current and (not end or end > as_of))
        if current:
            end = None
        scheduled_end = next((value for (person_id, role_text), value in scheduled.items() if person_id == assertion.person_id and (role_text in role.lower() or (category == "CEO" and "chief executive" in role.lower()))), None)
        relationship_id = f"relationship:{assertion.person_id}:{assertion.issuer_cik}" if assertion.issuer_cik else ""
        relationship = relationships.get(relationship_id)
        source_ids = sorted(set(assertion.source_ids + (relationship.source_ids if relationship else [])))
        # Proxy and filing extraction can emit the same real-world tenure more
        # than once with slightly different title text.  A tenure is defined by
        # person, canonical role, and its date boundary; retain the most
        # informative title and merge source evidence for that boundary.
        group_key = (assertion.person_id, category, start, end, scheduled_end)
        tenure_id = f"tenure:{_stable(*(str(item or '') for item in group_key))}"
        existing = values.get(tenure_id)
        if existing is not None:
            merged_evidence = sorted(set(existing.source_evidence_ids + source_ids))
            merged_urls = sorted(set(existing.source_urls + assertion.source_urls + (relationship.source_urls if relationship else [])))
            merged_paths = sorted(set(existing.source_local_paths + assertion.source_local_paths + (relationship.source_local_paths if relationship else [])))
            merged_accns = sorted(set(existing.source_accession_numbers + assertion.source_accession_numbers + (relationship.source_accession_numbers if relationship else [])))
            if len(role) >= len(existing.role):
                existing.role = role
                existing.evidence_text = assertion.evidence_text or existing.evidence_text
                existing.date_precision = _precision(start, assertion.date_precision) or existing.date_precision
            existing.source_evidence_ids = merged_evidence
            existing.source_urls = merged_urls
            existing.source_local_paths = merged_paths
            existing.source_accession_numbers = merged_accns
            continue
        values[tenure_id] = ManagementTenure(
            tenure_id=tenure_id,
            person_id=assertion.person_id,
            company_name=people[assertion.person_id].company_name,
            ticker=assertion.ticker.upper(),
            issuer_cik=assertion.issuer_cik or people[assertion.person_id].issuer_cik,
            role=role,
            role_category=category,
            start_date=start,
            end_date=end,
            date_precision=_precision(start, assertion.date_precision),
            current_as_of_date=as_of if current else None,
            scheduled_end_date=scheduled_end,
            prior_company_status="CURRENT_COMPANY",
            source_relationship_ids=[relationship_id] if relationship else [],
            source_evidence_ids=source_ids,
            source_urls=sorted(set(assertion.source_urls + (relationship.source_urls if relationship else []))),
            source_local_paths=sorted(set(assertion.source_local_paths + (relationship.source_local_paths if relationship else []))),
            source_accession_numbers=sorted(set(assertion.source_accession_numbers + (relationship.source_accession_numbers if relationship else []))),
            evidence_text=assertion.evidence_text,
            created_at=assertion.source_ids[0] if assertion.source_ids else (start or ""),
        )
    if include_prior_companies:
        for career in store.list_career():
            if career.ticker.upper() != ticker.upper() or career.is_current or career.employer_classification != "PUBLIC_COMPANY_CONFIRMED" or not career.employer_ticker:
                continue
            category = _career_category(career)
            if category is None:
                continue
            key = (career.person_id, category, career.employer_ticker, career.employer, career.start_date, career.end_date)
            tenure_id = f"tenure:{_stable(*(str(item or '') for item in key))}"
            values.setdefault(tenure_id, ManagementTenure(
                tenure_id=tenure_id,
                person_id=career.person_id,
                company_name=career.employer,
                ticker=career.employer_ticker,
                issuer_cik=career.employer_cik,
                role=career.title or "executive",
                role_category=category,
                start_date=career.start_date,
                end_date=career.end_date,
                date_precision=career.date_precision,
                source_relationship_ids=[],
                source_evidence_ids=list(career.source_ids),
                source_urls=list(career.source_urls),
                source_local_paths=list(career.source_local_paths),
                source_accession_numbers=list(career.source_accession_numbers),
                evidence_text=career.evidence_text,
                prior_company_status=("IDENTITY_CONFIRMED_DATES_CONFIRMED" if career.start_date and career.end_date else "IDENTITY_CONFIRMED_DATES_PARTIAL" if career.start_date or career.end_date else "IDENTITY_CONFIRMED_DATES_UNKNOWN"),
                created_at=career.source_ids[0] if career.source_ids else (career.start_date or ""),
            ))
    # A generic executive assertion is often emitted alongside a more
    # specific canonical role for the same dated appointment. Keep the
    # specific role and avoid double-counting one tenure in snapshots.
    grouped: dict[tuple[str, str | None, str | None, str | None], list[ManagementTenure]] = {}
    for item in values.values():
        grouped.setdefault((item.person_id, item.start_date, item.end_date, item.scheduled_end_date), []).append(item)
    filtered: list[ManagementTenure] = []
    specific = {"CEO", "CFO", "COO", "CTO", "president", "general_counsel", "founder"}
    for group in grouped.values():
        has_specific = {item.role_category for item in group} & specific
        filtered.extend(item for item in group if not (item.role_category == "executive" and has_specific))
    return sorted(filtered, key=lambda item: (item.ticker, item.person_id, item.start_date or "", item.role_category, item.tenure_id))


def _career_category(career: CareerEntry) -> str | None:
    title = (career.title or "").lower()
    if "chief executive" in title or title == "ceo": return "CEO"
    if "chief financial" in title or title == "cfo": return "CFO"
    if "chief operating" in title or title == "coo": return "COO"
    if "chief technology" in title or title == "cto": return "CTO"
    if "president" in title: return "president"
    if "executive" in title or "officer" in title: return "executive"
    return None


__all__ = ["build_tenures", "EXECUTION_CATEGORIES"]
