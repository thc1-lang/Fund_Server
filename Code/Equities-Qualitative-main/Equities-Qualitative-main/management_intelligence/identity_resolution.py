"""Conservative global identity resolution for official person assertions.

Issuer relationships are separate from global people. A partial or ambiguous
same-name match is kept as a disambiguated person rather than silently joining
two individuals.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ManagementPerson
from .people import disambiguated_person_key, merge_person, name_token_key, normalize_name, person_key, resolve_person


@dataclass
class IdentityResolution:
    person: ManagementPerson
    matched_existing: bool
    status: str


class ManagementIdentityResolver:
    def resolve(self, name: str, ticker: str, existing: list[ManagementPerson] | tuple[ManagementPerson, ...] = ()) -> IdentityResolution:
        match = resolve_person(name, ticker, existing)
        if match is not None:
            return IdentityResolution(match, True, "resolved")
        normalized = normalize_name(name)
        person = ManagementPerson(
            person_id=person_key(ticker, name),
            ticker=ticker.upper(),
            company_name=ticker.upper(),
            full_name=name,
            normalized_name=normalized,
            identity_status="unresolved",
        )
        return IdentityResolution(person, False, "unresolved")

    def merge(self, existing: ManagementPerson | None, incoming: ManagementPerson) -> ManagementPerson:
        return merge_person(existing, incoming)

    def reconcile_incoming(self, incoming: ManagementPerson, existing: list[ManagementPerson] | tuple[ManagementPerson, ...]) -> IdentityResolution:
        """Resolve an issuer profile against global people conservatively.

        Same-name candidates from another issuer are not merged unless an SEC
        owner CIK, an alias, or explicit biography/company evidence supports
        the join.  Ambiguous candidates receive a deterministic disambiguated
        global ID and remain separately reviewable.
        """
        candidates = [item for item in existing if name_token_key(item.full_name) == name_token_key(incoming.full_name) or any(name_token_key(alias) == name_token_key(incoming.full_name) for alias in item.aliases)]
        if not candidates:
            incoming.person_id = person_key(incoming.ticker, incoming.full_name)
            incoming.identity_status = "resolved"
            return IdentityResolution(incoming, False, "resolved")
        if len(candidates) == 1:
            candidate = candidates[0]
            ciks = set(candidate.reporting_owner_ciks).intersection(incoming.reporting_owner_ciks)
            text = f"{incoming.biography or ''} {' '.join(incoming.current_roles)}".lower()
            prior_company = (candidate.company_name or "").lower()
            candidate_company = (incoming.company_name or "").lower()
            supported = bool(ciks) or (candidate.issuer_cik and incoming.issuer_cik and candidate.issuer_cik == incoming.issuer_cik) or (prior_company and prior_company in text) or (candidate_company and candidate_company in (candidate.biography or '').lower())
            same_issuer = bool(candidate.issuer_cik and incoming.issuer_cik and candidate.issuer_cik == incoming.issuer_cik)
            if supported:
                incoming.person_id = candidate.person_id
                incoming.identity_status = "resolved"
                if ciks:
                    incoming.identity_confidence = "strong_identifier"
                    incoming.identity_evidence.append("matching SEC reporting-owner CIK")
                elif not same_issuer:
                    incoming.identity_confidence = "cross_company_evidence"
                    incoming.identity_evidence.append("official same-person biography/company evidence")
                return IdentityResolution(incoming, True, incoming.identity_confidence)
        incoming.person_id = disambiguated_person_key(incoming.full_name, incoming.source_accession_numbers[0] if incoming.source_accession_numbers else incoming.ticker)
        incoming.identity_status = "ambiguous"
        incoming.identity_confidence = "ambiguous_same_name"
        return IdentityResolution(incoming, False, "ambiguous")


__all__ = ["IdentityResolution", "ManagementIdentityResolver"]
