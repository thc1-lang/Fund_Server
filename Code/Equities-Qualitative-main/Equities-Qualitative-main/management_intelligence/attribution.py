"""Controlled person-level attribution; no causal inference from tenure alone."""

from __future__ import annotations

import re

from .models import ManagementPerson
from .track_record_models import Attribution


def classify_attribution(evidence_text: str, person: ManagementPerson | None, *, role_category: str | None = None, direct: bool = False) -> str:
    text = str(evidence_text or "")
    lower = text.lower()
    if direct and person and _person_named(text, person):
        return Attribution.DIRECT
    if person and _person_named(text, person) and re.search(r"(?:decided|directed|led|committed|announced|said|stated|approved|signed|initiated|delivered)", lower):
        return Attribution.DIRECT
    if re.search(r"\b(?:board|management|management team|executive team|company|we|our)\b", lower):
        return Attribution.TEAM
    if role_category in {"CEO", "CFO", "COO", "CTO", "president", "general_counsel", "executive"}:
        return Attribution.ROLE_BASED
    if person:
        return Attribution.TENURE_OVERLAP
    return Attribution.UNATTRIBUTED


def _person_named(text: str, person: ManagementPerson) -> bool:
    names = [person.full_name, *person.aliases]
    for name in names:
        tokens = [token for token in re.findall(r"[A-Za-z]+", name) if len(token) > 2]
        if len(tokens) >= 2 and all(re.search(rf"\b{re.escape(token)}\b", text, re.I) for token in tokens[-2:]):
            return True
    return False


def validate_attribution(level: str, evidence_text: str, person: ManagementPerson | None = None) -> tuple[bool, str | None]:
    allowed = {Attribution.DIRECT, Attribution.ROLE_BASED, Attribution.TEAM, Attribution.TENURE_OVERLAP, Attribution.UNATTRIBUTED}
    if level not in allowed:
        return False, "unsupported attribution level"
    if level == Attribution.DIRECT and person is not None and not _person_named(evidence_text, person):
        return False, "DIRECT requires the named person in evidence"
    return True, None


__all__ = ["classify_attribution", "validate_attribution"]
