"""Extract official committee membership and committee leadership statements."""

from __future__ import annotations

import re
from typing import Iterable

from .board_governance_models import BoardCommittee, GovernanceBoardMembership
from .governance_sources import CachedGovernanceSource
from .models import ManagementPerson


_COMMITTEE_PATTERNS = (
    ("audit", r"Audit Committee"),
    ("compensation", r"Compensation Committee"),
    ("nominating_governance", r"Nominating(?: and| &) Corporate Governance Committee|Compensation, Nominating & Governance Committee|Nominating & Governance Committee"),
    ("nominating_governance", r"Governance Committee"),
    ("executive", r"Executive Committee"),
    ("finance", r"Finance Committee"),
    ("risk", r"(?:Enterprise )?Risk Committee"),
    ("risk", r"Cybersecurity Risk Management Committee"),
    ("technology", r"Technology Committee"),
    ("science", r"Science Committee"),
    ("science", r"Research & Development Committee"),
)


def _person_ids(fragment: str, people: list[ManagementPerson]) -> list[str]:
    found: list[str] = []
    lowered = fragment.lower()
    for person in people:
        tokens = [token.lower() for token in re.findall(r"[a-zA-Z]{3,}", person.full_name)]
        if not tokens:
            continue
        # A last name plus either first name or a unique occurrence is enough
        # for the terse committee tables used by SEC proxies.
        last = tokens[-1]
        last_present = re.search(r"\b" + re.escape(last) + r"\b", lowered)
        first_present = re.search(r"\b" + re.escape(tokens[0]) + r"\b", lowered)
        if last_present and (len(tokens) == 1 or first_present or sum(1 for p in people if re.search(r"\b" + re.escape(tokens[-1]) + r"\b", p.full_name.lower())) == 1):
            found.append(person.person_id)
    return sorted(set(found))


def _chair_id(fragment: str, people: list[ManagementPerson]) -> str | None:
    match = re.search(r"chair(?:man|person)?(?: of [^.]*)?\s+(?:is|:|was)\s+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s*([A-Z][A-Za-z-]+)", fragment, re.IGNORECASE)
    if not match:
        return None
    ids = _person_ids(match.group(1), people)
    return ids[0] if ids else None


def _committee_chair_id(name: str, text: str, people: list[ManagementPerson]) -> str | None:
    escaped = re.escape(name)
    match = re.search(rf"chair(?:man|person)?\s+of\s+(?:our\s+)?{escaped}\s+is\s+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)?\s*([A-Z][A-Za-z-]+)", text, re.IGNORECASE)
    if match:
        ids = _person_ids(match.group(1), people)
        if ids:
            return ids[0]
    explicit_reverse = re.search(rf"\b([A-Z][A-Za-z-]+)\s+is\s+the\s+chair(?:man|person)?\s+of\s+(?:our\s+)?{escaped}", text, re.IGNORECASE)
    if explicit_reverse:
        ids = _person_ids(explicit_reverse.group(1), people)
        if ids:
            return ids[0]
    reverse = re.search(rf"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s*([A-Z][A-Za-z-]+)[^.]{0,80}(?:is\s+the\s+)?chair(?:man|person)?\s+of\s+(?:our\s+)?{escaped}", text, re.IGNORECASE)
    if reverse:
        ids = _person_ids(reverse.group(1), people)
        if ids:
            return ids[0]
    tagged = re.search(r"([A-Z][A-Za-z.'-]{2,}(?:\s+[A-Z][A-Za-z.'-]{2,}){0,4})\s*\(Chair\)", text, re.IGNORECASE)
    if tagged:
        ids = _person_ids(tagged.group(1), people)
        if ids:
            return ids[0]
    return _chair_id(text, people) if name.lower() in text.lower() else None


def _type(name: str) -> str:
    lowered = name.lower()
    if "audit" in lowered:
        return "audit"
    if "compensation" in lowered and "nominating" not in lowered:
        return "compensation"
    if "nominating" in lowered or "governance" in lowered:
        return "nominating_governance"
    for value, pattern in _COMMITTEE_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return value
    return "other"


def extract_committees(
    ticker: str,
    proxy: CachedGovernanceSource | None,
    people: Iterable[ManagementPerson],
    memberships: Iterable[GovernanceBoardMembership],
) -> list[BoardCommittee]:
    if proxy is None:
        return []
    people_list = list(people)
    member_map = {item.person_id: item for item in memberships}
    text = proxy.text
    result: list[BoardCommittee] = []
    for _, pattern in _COMMITTEE_PATTERNS:
        for match in re.finditer(rf"(?P<name>{pattern})[^.]{0,260}?(?:consists of|is composed of)\s+(?P<members>.*?)(?=\.\s+(?:Our|The|During|Specific|Members|Each)\b)", text, re.IGNORECASE):
            name = re.sub(r"\s+", " ", match.group("name")).strip()
            fragment = match.group(0)
            ids = _person_ids(match.group("members"), people_list)
            if not ids:
                continue
            member_independence = {person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in ids}
            section_for_roles = text[match.start():match.end() + 1200]
            independent_words = section_for_roles.lower()
            required = "YES" if "each member" in independent_words and "independent" in independent_words else "UNKNOWN"
            financial_ids: list[str] = []
            if _type(name) == "audit":
                expert_match = re.search(r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s+([A-Z][A-Za-z-]+)\s+is an?\s+[^.]{0,80}audit committee financial expert", text, re.IGNORECASE)
                if expert_match:
                    financial_ids = _person_ids(expert_match.group(1), people_list)
                else:
                    expert_window = section_for_roles
                    if re.search(r"all audit committee financial experts", expert_window, re.IGNORECASE):
                        financial_ids = list(ids)
            result.append(BoardCommittee(
                committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
                ticker=ticker.upper(),
                committee_type=_type(name),
                committee_name=name,
                members=ids,
                chair_person_id=_committee_chair_id(name, section_for_roles, people_list),
                financial_expert_person_ids=financial_ids,
                financial_background={person_id: "explicit audit committee financial expert" for person_id in financial_ids},
                required_independence=required,
                member_independence=member_independence,
                source_url=proxy.source_url,
                source_accession=proxy.accession_number,
                source_form=proxy.form_type,
                source_date=proxy.filing_date,
                local_source_path=proxy.local_path,
                evidence_text=fragment,
            ))
    # Some proxy layouts use the phrase "members are" rather than "consists
    # of".  Preserve a committee record with an unknown membership rather than
    # manufacturing names.
    if not result:
        for _, pattern in _COMMITTEE_PATTERNS:
            match = re.search(rf"(?P<name>{pattern})", text, re.IGNORECASE)
            if match:
                name = match.group("name")
                result.append(BoardCommittee(
                    committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
                    ticker=ticker.upper(), committee_type=_type(name), committee_name=name,
                    source_url=proxy.source_url, source_accession=proxy.accession_number,
                    source_form=proxy.form_type, source_date=proxy.filing_date,
                    local_source_path=proxy.local_path, evidence_text=match.group(0),
                ))
    # Issuers such as Exelixis use a compact "Current Members:" table and
    # Palantir uses "current members ... are".  Search every occurrence so a
    # table-of-contents heading cannot hide the operative section.
    for member_match in re.finditer(r"(?:Current Members?\s*:\s*|current members? of [^.]{0,120}\s+(?:are|:|include)\s+)", text, re.IGNORECASE):
        prefix = text[max(0, member_match.start() - 260):member_match.start()]
        candidates = []
        for _, pattern in _COMMITTEE_PATTERNS:
            candidates.extend(re.finditer(rf"(?P<name>{pattern})", prefix, re.IGNORECASE))
        if not candidates:
            continue
        name = re.sub(r"\s+", " ", max(candidates, key=lambda item: (item.end(), item.end() - item.start())).group("name")).strip()
        committee_type = _type(name)
        body = text[member_match.end():member_match.end() + 700]
        body = re.split(r"\.\s+(?=(?:Mr|Ms|Mrs|Dr|Our|The|This)\b)", body, maxsplit=1)[0]
        ids = _person_ids(body, people_list)
        if not ids or any(item.committee_type == committee_type and item.members for item in result):
            continue
        financial_ids = ids if committee_type == "audit" and re.search(r"all audit committee financial experts", body, re.IGNORECASE) else []
        result.append(BoardCommittee(
            committee_id=f"governance-committee:{ticker.upper()}:{committee_type}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}", ticker=ticker.upper(), committee_type=committee_type, committee_name=name,
            members=ids, chair_person_id=_committee_chair_id(name, body + text[member_match.end():member_match.end() + 800], people_list),
            financial_expert_person_ids=financial_ids, financial_background={person_id: "explicit audit committee financial expert" for person_id in financial_ids},
            required_independence="YES" if "all independent" in body.lower() else "UNKNOWN", source_url=proxy.source_url,
            source_accession=proxy.accession_number, source_form=proxy.form_type, source_date=proxy.filing_date, local_source_path=proxy.local_path, evidence_text=body,
        ))
    # SEC/IR PDFs commonly flatten the committee overview tables into the
    # sequence "Chair <name> Other Members <names> Independent: 100%".  The
    # formal committee heading follows that sequence, so use the bounded
    # table text to recover current membership and chair evidence without
    # guessing from unrelated narrative mentions.
    table_pattern = re.compile(
        r"Board\s+and\s+Governance\s+Matters\s+Chair\s+"
        r"(?P<chair>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,3})\s+"
        r"Other\s+Members\s+(?P<members>.*?)\s+Independent:\s*(?P<ind>\d+)%",
        re.IGNORECASE,
    )
    committee_headings = (
        "Audit Committee",
        "Compensation and Leadership Development Committee",
        "Compliance, Privacy and Quality Committee",
        "Nominating and Governance Committee",
    )
    for match in table_pattern.finditer(text):
        heading_match = re.search(
            r"\b(" + "|".join(re.escape(item) for item in committee_headings) + r")\b",
            text[match.end():match.end() + 600],
            re.IGNORECASE,
        )
        if not heading_match:
            continue
        name = re.sub(r"\s+", " ", heading_match.group(1)).strip()
        fragment = match.group(0)
        members_text = f"{match.group('chair')} {match.group('members')}"
        ids = _person_ids(members_text, people_list)
        if not ids:
            continue
        member_independence = {person_id: member_map.get(person_id).is_independent if person_id in member_map else "UNKNOWN" for person_id in ids}
        section_after_heading = text[match.end() + heading_match.end(): match.end() + heading_match.end() + 900]
        financial_ids = ids if _type(name) == "audit" and re.search(r"identified\s+all\s+members.*?financial\s+experts", section_after_heading, re.IGNORECASE | re.DOTALL) else []
        result.append(BoardCommittee(
            committee_id=f"governance-committee:{ticker.upper()}:{_type(name)}:{re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')}",
            ticker=ticker.upper(), committee_type=_type(name), committee_name=name,
            members=ids, chair_person_id=(_person_ids(match.group("chair"), people_list) or [None])[0],
            financial_expert_person_ids=financial_ids,
            financial_background={person_id: "explicit audit committee financial expert" for person_id in financial_ids},
            required_independence="YES" if match.group("ind") == "100" else "UNKNOWN",
            member_independence=member_independence, source_url=proxy.source_url,
            source_accession=proxy.accession_number, source_form=proxy.form_type,
            source_date=proxy.filing_date, local_source_path=proxy.local_path,
            evidence_text=fragment,
        ))
    # Prefer an explicit membership record over a table-of-contents placeholder
    # for the same committee type.  Keep one empty record only when the source
    # names a committee but does not expose its membership.
    by_type: dict[str, list[BoardCommittee]] = {}
    for item in result:
        by_type.setdefault(item.committee_type, []).append(item)
    explicit_members_seen = bool(re.search(r"(?:Current Members?\s*:|current members? of [^.]{0,120}\s+(?:are|:|include)\s+)", text, re.IGNORECASE))
    chosen: list[BoardCommittee] = []
    for items in by_type.values():
        populated = [item for item in items if item.members]
        if populated:
            # Nested regex matches such as "Governance Committee" inside
            # "Nominating and Corporate Governance Committee" describe one
            # committee; preserve the issuer's longest formal name.
            chosen.append(max(populated, key=lambda item: len(item.committee_name)))
        elif not explicit_members_seen and not bool(table_pattern.search(text)):
            chosen.extend(items[:1])
    if any("compensation" in item.committee_name.lower() and "nominating" in item.committee_name.lower() for item in chosen):
        chosen = [item for item in chosen if item.members or "compensation committee" not in item.committee_name.lower()]
    for item in chosen:
        if item.committee_type != "audit" or not item.members:
            continue
        direct = re.search(r"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?)\s+([A-Z][A-Za-z-]+)\s+is an?\s+[^.]{0,80}audit committee financial expert", text, re.IGNORECASE)
        if direct:
            ids = [person_id for person_id in _person_ids(direct.group(1), people_list) if person_id in item.members]
            if ids:
                item.financial_expert_person_ids = ids
                item.financial_background = {person_id: "explicit audit committee financial expert" for person_id in ids}
        elif re.search(r"all audit committee financial experts", item.evidence_text, re.IGNORECASE):
            item.financial_expert_person_ids = list(item.members)
            item.financial_background = {person_id: "explicit audit committee financial expert" for person_id in item.members}
    return sorted({item.committee_id: item for item in chosen}.values(), key=lambda item: item.committee_id)


__all__ = ["extract_committees"]
