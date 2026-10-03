"""Conservative Item 5.02 role-change extraction from official 8-K filings.

Only the formal Item 5.02 section is considered. This avoids treating annual
meeting vote text or historical biography language as current role changes.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from bs4 import BeautifulSoup
from insider_intelligence.models import SECFiling

from .models import ManagementPerson, RoleChangeEvent
from .people import name_token_key, normalize_name


ROLE_WORDS = r"CEO|chief executive officer|CFO|chief financial officer|COO|chief operating officer|president|chair(?:man|person)?|director|general counsel|executive officer|chief legal officer"


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return value


def _event_type(context: str) -> str | None:
    if re.search(r"resign|retir|depart|ceased", context, re.I):
        return "departure"
    if re.search(r"interim", context, re.I):
        return "interim_appointment"
    if re.search(r"appoint|appointment", context, re.I):
        return "appointment"
    return None


def _event_date(context: str, filing_date: str | None) -> str | None:
    patterns = (
        r"effective\s+(?:as\s+of\s+)?([A-Z][a-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        r"on\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        r"as\s+of\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, context, re.I)
        if match:
            return _iso_date(match.group(1))
    return filing_date


def _role(context: str) -> str | None:
    match = re.search(rf"\b({ROLE_WORDS})\b", context, re.I)
    if not match:
        return "director" if re.search(r"\bboard(?: of directors)?\b", context, re.I) else None
    value = match.group(1)
    return value.upper() if value.lower() in {"ceo", "cfo", "coo"} else value


def _stable_event(filing: SECFiling, person_id: str | None, person_name: str, event_type: str, role: str | None, date: str | None) -> str:
    seed = "|".join((filing.accession_number, person_id or "", normalize_name(person_name), event_type, role or "", date or ""))
    return "role-change:" + hashlib.sha256(seed.encode()).hexdigest()[:24]


def _candidate_known(text: str, person: ManagementPerson) -> list[tuple[str, str]]:
    names = [person.full_name, *person.aliases]
    found: list[tuple[str, str]] = []
    for name in names:
        if not name or not name.strip():
            continue
        match = re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text, re.I)
        if match:
            start_candidates = [text.rfind(mark, 0, match.start()) for mark in ".?!"]
            start = max(start_candidates, default=-1) + 1
            end_candidates = [value for value in (text.find(mark, match.end()) for mark in ".?!") if value >= 0]
            end = min(end_candidates, default=len(text)) + 1
            found.append((name, text[start:end]))
    if not found:
        tokens = name_token_key(person.full_name)
        if len(tokens) >= 2:
            surname = max(tokens, key=len)
            given = [token for token in tokens if token != surname]
            pattern = rf"(?<![A-Za-z])(?:{'|'.join(map(re.escape, given))})\s+(?:[A-Z]\.\s+)?{re.escape(surname)}(?![A-Za-z])"
            match = re.search(pattern, text, re.I)
            if match:
                start = max(text.rfind(".", 0, match.start()), text.rfind("?", 0, match.start()), text.rfind("!", 0, match.start())) + 1
                end_candidates = [value for value in (text.find(mark, match.end()) for mark in ".?!") if value >= 0]
                end = min(end_candidates, default=len(text)) + 1
                found.append((match.group(0), text[start:end]))
    return found


def _unknown_candidates(text: str) -> list[tuple[str, str, str]]:
    patterns = (
        r"\bappointed\s+(?P<name>[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,4})\s+(?:to|as)\b",
        r"\b(?P<name>[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,4}),?\s+(?:a\s+[^.]{0,120}\s+)?(?:notified|resigned|retired|departed|ceased)\b",
    )
    found: list[tuple[str, str, str]] = []
    for pattern, explicit_type in zip(patterns, ("appointment", "departure")):
        for match in re.finditer(pattern, text):
            name = " ".join(match.group("name").split()).strip(" ,")
            if name.lower() in {"the board", "the company", "the company board"}:
                continue
            context = text[max(0, match.start() - 350):min(len(text), match.end() + 500)]
            found.append((name, context, explicit_type))
    return found


def parse_role_changes(html: str | bytes, filing: SECFiling, people: list[ManagementPerson]) -> list[RoleChangeEvent]:
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())
    # Item 5.07 annual-vote filings often contain the word appointment but do
    # not change the roster, so require the formal Item 5.02 heading.
    if not re.search(r"\bitem\s+5\.02\b", text, re.I):
        return []
    section_match = re.search(
        r"\bitem\s+5\.02\b(?P<body>.*?)(?=\bitem\s+5\.(?:03|04|05|06|07)\b|\bsignatures\b|$)",
        text,
        re.I,
    )
    text = section_match.group("body") if section_match else text
    events: list[RoleChangeEvent] = []
    for person in people:
        for display_name, context in _candidate_known(text, person):
            event_type = _event_type(context)
            if not event_type:
                continue
            date = _event_date(context, filing.filing_date)
            role = _role(context)
            events.append(RoleChangeEvent(
                event_id=_stable_event(filing, person.person_id, display_name, event_type, role, date),
                person_id=person.person_id,
                ticker=filing.ticker.upper(),
                issuer_cik=filing.issuer_cik,
                person_name=person.full_name,
                event_type=event_type,
                role=role,
                effective_date=date,
                source_id=f"sec:{filing.issuer_cik}:{filing.accession_number}",
                source_url=filing.source_url,
                local_source_path=filing.local_source_path,
                accession_number=filing.accession_number,
                filing_date=filing.filing_date,
                evidence_text=context,
            ))
            break
    for display_name, context, explicit_type in _unknown_candidates(text):
        if any(name_token_key(display_name) == name_token_key(person.full_name) for person in people):
            continue
        event_type = explicit_type
        date = _event_date(context, filing.filing_date)
        role = _role(context)
        events.append(RoleChangeEvent(
            event_id=_stable_event(filing, None, display_name, event_type, role, date),
            person_id=None,
            ticker=filing.ticker.upper(),
            issuer_cik=filing.issuer_cik,
            person_name=display_name,
            event_type=event_type,
            role=role,
            effective_date=date,
            source_id=f"sec:{filing.issuer_cik}:{filing.accession_number}",
            source_url=filing.source_url,
            local_source_path=filing.local_source_path,
            accession_number=filing.accession_number,
            filing_date=filing.filing_date,
            evidence_text=context,
        ))
    return list({item.event_id: item for item in events}.values())


__all__ = ["parse_role_changes"]
