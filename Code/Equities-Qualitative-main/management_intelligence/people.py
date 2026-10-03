"""Identity normalization and conservative matching for management records."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

from .models import ManagementPerson


def normalize_name(name: str) -> str:
    value = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\b(?:dr|mr|mrs|ms|miss|prof|phd|jd|md)\.?\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    tokens = [token for token in value.split() if token not in {"ph", "d", "md", "jd", "dvm", "frs"}]
    return " ".join(tokens)


def name_token_key(name: str) -> tuple[str, ...]:
    """Order-independent identity tokens used for surname-first SEC names."""
    return tuple(sorted(normalize_name(name).split()))


def person_key(ticker: str, name: str, *, strong_identifier: str | None = None) -> str:
    """Return a global human identity key.

    ``ticker`` remains a compatibility argument for callers from v1, but is
    intentionally excluded from the canonical key.  Ambiguous same-name
    people are disambiguated by the identity resolver rather than silently
    merged.
    """
    normalized = normalize_name(name)
    seed = strong_identifier or " ".join(name_token_key(normalized))
    digest = hashlib.sha256(seed.encode()).hexdigest()[:20]
    return f"person:{digest}"


def disambiguated_person_key(name: str, issuer_cik: str | None) -> str:
    seed = f"{issuer_cik or 'unknown'}|{' '.join(name_token_key(name))}"
    return f"person:{hashlib.sha256(seed.encode()).hexdigest()[:20]}"


def resolve_person(name: str, ticker: str, existing: Iterable[ManagementPerson] = ()) -> ManagementPerson | None:
    key = name_token_key(name)
    exact = [item for item in existing if name_token_key(item.full_name) == key or any(name_token_key(alias) == key for alias in item.aliases)]
    return exact[0] if len(exact) == 1 else None


def merge_person(existing: ManagementPerson | None, incoming: ManagementPerson) -> ManagementPerson:
    if existing is None:
        return incoming
    existing.current_roles = sorted(set(existing.current_roles + incoming.current_roles))
    existing.role_categories = sorted(set(existing.role_categories + incoming.role_categories))
    existing.role_start_dates.update(incoming.role_start_dates)
    existing.source_ids = sorted(set(existing.source_ids + incoming.source_ids))
    existing.aliases = sorted(set(existing.aliases + incoming.aliases))
    existing.relevant_industries = sorted(set(existing.relevant_industries + incoming.relevant_industries))
    existing.relevant_responsibilities = sorted(set(existing.relevant_responsibilities + incoming.relevant_responsibilities))
    existing.is_founder = existing.is_founder or incoming.is_founder
    if incoming.founder_scope != "unknown":
        existing.founder_scope = incoming.founder_scope
    existing.founder_context = existing.founder_context or incoming.founder_context
    existing.biography = existing.biography or incoming.biography
    existing.current_since = existing.current_since or incoming.current_since
    existing.issuer_relationship_ids = sorted(set(existing.issuer_relationship_ids + incoming.issuer_relationship_ids))
    existing.insider_person_ids = sorted(set(existing.insider_person_ids + incoming.insider_person_ids))
    existing.reporting_owner_ciks = sorted(set(existing.reporting_owner_ciks + incoming.reporting_owner_ciks))
    existing.identity_evidence = sorted(set(existing.identity_evidence + incoming.identity_evidence))
    if incoming.identity_confidence != "name_only":
        existing.identity_confidence = incoming.identity_confidence
    if incoming.identity_status != "resolved":
        existing.identity_status = incoming.identity_status
    existing.updated_at = incoming.updated_at or existing.updated_at
    return existing


__all__ = ["normalize_name", "name_token_key", "person_key", "disambiguated_person_key", "resolve_person", "merge_person"]
