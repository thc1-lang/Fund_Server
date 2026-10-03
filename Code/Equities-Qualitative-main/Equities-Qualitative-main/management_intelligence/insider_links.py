"""Non-destructive links between Component 6A people and SEC insider people."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Iterable

from insider_intelligence.models import InsiderPerson

from .models import InsiderIdentityLink, ManagementPerson
from .people import name_token_key, normalize_name


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _compatible_name(management: ManagementPerson, insider: InsiderPerson) -> tuple[bool, str]:
    a = name_token_key(management.full_name)
    b = name_token_key(insider.full_name)
    if a == b:
        return True, "issuer_exact_name"
    if not a or not b:
        return False, ""
    common = set(a).intersection(b)
    if len(common) < 1:
        return False, ""
    surname = max(common, key=len)
    if len(surname) < 4:
        return False, ""
    a_given = [item for item in a if item != surname]
    b_given = [item for item in b if item != surname]
    if a_given and b_given and all(any(x.startswith(y[:3]) or y.startswith(x[:3]) for y in b_given) for x in a_given):
        return True, "issuer_name_variant"
    return False, ""


def link_management_to_insiders(
    management_people: Iterable[ManagementPerson],
    insider_people: Iterable[InsiderPerson],
    *,
    issuer_cik: str | None = None,
    source_ids: list[str] | None = None,
) -> list[InsiderIdentityLink]:
    """Create reviewable links without replacing either canonical identity."""
    management_people = list(management_people)
    insider_people = list(insider_people)
    grouped: dict[str, list[InsiderPerson]] = defaultdict(list)
    for insider in insider_people:
        grouped[insider.ticker.upper()].append(insider)
    result: list[InsiderIdentityLink] = []
    for person in management_people:
        candidates = grouped.get(person.ticker.upper(), [])
        matched: list[tuple[InsiderPerson, str]] = []
        for insider in candidates:
            ok, method = _compatible_name(person, insider)
            if ok:
                matched.append((insider, method))
        if not matched:
            continue
        # A unique reporting-owner CIK is the strongest available bridge. If
        # only a name match exists, preserve it as probable rather than
        # rewriting the global person ID.
        unique = len(matched) == 1
        for insider, method in matched:
            confidence = "high" if unique and insider.reporting_owner_cik else ("probable" if unique else "ambiguous")
            link = InsiderIdentityLink(
                link_id=f"insider-link:{_stable(person.person_id, insider.person_id)}",
                management_person_id=person.person_id,
                insider_person_id=insider.person_id,
                ticker=person.ticker.upper(),
                issuer_cik=issuer_cik or person.issuer_cik,
                reporting_owner_cik=insider.reporting_owner_cik,
                match_method=method + ("+owner_cik" if insider.reporting_owner_cik else ""),
                confidence=confidence,
                evidence_text=f"Official proxy identity {person.full_name!r} matched SEC insider record {insider.full_name!r}; canonical identities remain separate.",
                source_ids=source_ids or [],
                source_urls=list(person.source_urls),
                source_local_paths=list(person.source_local_paths),
                source_accession_numbers=list(person.source_accession_numbers),
            )
            result.append(link)
            if confidence in {"high", "probable"}:
                person.insider_person_ids = sorted(set(person.insider_person_ids + [insider.person_id]))
                if insider.reporting_owner_cik:
                    person.reporting_owner_ciks = sorted(set(person.reporting_owner_ciks + [insider.reporting_owner_cik]))
                person.identity_evidence = sorted(set(person.identity_evidence + ["SEC Form 3/4/5 identity link"]))
                person.identity_confidence = "strong_identifier" if insider.reporting_owner_cik else "official_cross_source"
    return list({item.link_id: item for item in result}.values())


__all__ = ["link_management_to_insiders"]
