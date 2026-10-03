"""Conservative extraction of expressly disclosed related-party facts."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable

from .board_governance_models import RelatedPartyRelationship
from .governance_sources import CachedGovernanceSource
from .models import ManagementPerson


def _section(text: str, heading: str, limit: int = 18000) -> str:
    matches = list(re.finditer(re.escape(heading), text, re.IGNORECASE))
    if not matches:
        return ""
    match = matches[-1]
    return text[match.start():match.start() + limit]


def _person_id(sentence: str, people: list[ManagementPerson]) -> str | None:
    lowered = sentence.lower()
    for person in people:
        parts = [item.lower() for item in re.findall(r"[a-zA-Z]{3,}", person.full_name)]
        if parts and parts[-1] in lowered and (len(parts) == 1 or parts[0] in lowered):
            return person.person_id
    return None


def _stable_id(sentence: str) -> str:
    return hashlib.sha1(sentence.encode("utf-8", errors="ignore")).hexdigest()[:12]


def extract_related_parties(ticker: str, proxy: CachedGovernanceSource | None, people: Iterable[ManagementPerson]) -> tuple[list[RelatedPartyRelationship], dict[str, str]]:
    if proxy is None:
        return [], {"family": "UNKNOWN", "related_party": "UNKNOWN", "interlocks": "UNKNOWN"}
    people_list = list(people)
    text = proxy.text
    records: list[RelatedPartyRelationship] = []
    family_status = "UNKNOWN"
    if re.search(r"there are no family relationships among (?:any of )?our directors", text, re.IGNORECASE):
        family_status = "NO"
    elif re.search(r"family relationship", text, re.IGNORECASE) and not re.search(r"no family relationships", text, re.IGNORECASE):
        family_status = "YES"
        section = _section(text, "family relationship", 5000)
        for sentence in re.split(r"(?<=[.!?])\s+", section):
            person_id = _person_id(sentence, people_list)
            if person_id and re.search(r"brother|sister|spouse|parent|child|family", sentence, re.IGNORECASE):
                records.append(RelatedPartyRelationship(
                    relationship_id=f"governance-related:{ticker.upper()}:family:{person_id}:{_stable_id(sentence)}", ticker=ticker.upper(), person_id=person_id,
                    relationship_type="family_relationship", description=sentence[:1000], source_url=proxy.source_url,
                    source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date,
                    local_source_path=proxy.local_path, evidence_text=sentence[:2000], created_at=proxy.filing_date or "",
                ))

    related_section = _section(text, "CERTAIN RELATIONSHIPS AND RELATED PERSON TRANSACTIONS") or _section(text, "CERTAIN RELATED PERSON TRANSACTIONS")
    if related_section:
        related_status = "NO" if re.search(r"no (?:such )?transactions|none of (?:the )?(?:transactions|relationships)", related_section, re.IGNORECASE) else "YES"
        for sentence in re.split(r"(?<=[.!?])\s+", related_section):
            if not re.search(r"transaction|agreement|services|payments|relationship", sentence, re.IGNORECASE):
                continue
            person_id = _person_id(sentence, people_list)
            # Keep only sentences tied to a named director/officer/person, or
            # an explicit policy statement.  Headings alone are not records.
            if person_id is None:
                continue
            kind = "consulting_relationship" if "consult" in sentence.lower() else "related_party_transaction"
            records.append(RelatedPartyRelationship(
                relationship_id=f"governance-related:{ticker.upper()}:{kind}:{person_id}:{_stable_id(sentence)}", ticker=ticker.upper(), person_id=person_id,
                relationship_type=kind, description=sentence[:1000], source_url=proxy.source_url,
                source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date,
                local_source_path=proxy.local_path, evidence_text=sentence[:2000], created_at=proxy.filing_date or "",
            ))
    else:
        related_status = "UNKNOWN"

    interlock_section = _section(text, "Compensation Committee Interlocks and Insider Participation", 7000)
    if interlock_section:
        interlock_status = "NO" if re.search(r"none of|no other person|does not currently serve", interlock_section, re.IGNORECASE) else "YES"
        if interlock_status == "YES":
            records.append(RelatedPartyRelationship(
                relationship_id=f"governance-related:{ticker.upper()}:interlock:proxy", ticker=ticker.upper(), relationship_type="interlocking_relationship",
                description=interlock_section[:1200], source_url=proxy.source_url, source_accession=proxy.accession_number,
                source_form=proxy.form_type, source_date=proxy.filing_date, local_source_path=proxy.local_path,
                evidence_text=interlock_section[:2500], created_at=proxy.filing_date or "",
            ))
    else:
        interlock_status = "UNKNOWN"
    unique = {record.relationship_id: record for record in records}
    return sorted(unique.values(), key=lambda record: record.relationship_id), {"family": family_status, "related_party": related_status, "interlocks": interlock_status}


__all__ = ["extract_related_parties"]
