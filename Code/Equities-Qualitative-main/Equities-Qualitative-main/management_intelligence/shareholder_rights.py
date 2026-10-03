"""Factual shareholder-rights extraction with explicit UNKNOWN defaults."""

from __future__ import annotations

import re

from .board_governance_models import ShareholderRights
from .governance_sources import CachedGovernanceSource


def _yes_no_unknown(text: str, positive: tuple[str, ...], negative: tuple[str, ...] = ()) -> str:
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in positive):
        return "YES"
    if any(re.search(pattern, lowered) for pattern in negative):
        return "NO"
    return "UNKNOWN"


def _evidence_sentence(text: str, patterns: tuple[str, ...]) -> str | None:
    """Return the smallest source sentence supporting a provision."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if any(re.search(pattern, sentence, re.IGNORECASE) for pattern in patterns):
            return re.sub(r"\s+", " ", sentence).strip()[:1200]
    return None


def _conditional_term(text: str, condition: str) -> str:
    """Find the election standard nearest a contested/uncontested qualifier."""
    for match in re.finditer(condition, text, re.IGNORECASE):
        before = text[max(0, match.start() - 140):match.start()]
        next_marker = re.search(r"\b(?:contested|uncontested)\b|[.!?]", text[match.end():], re.IGNORECASE)
        after = text[match.end():match.end() + (next_marker.start() if next_marker else 160)]
        prior_terms = list(re.finditer(r"\b(majority|plurality)\b", before, re.IGNORECASE))
        if prior_terms:
            return prior_terms[-1].group(1).lower()
        following = re.search(r"\b(majority|plurality)\b", after, re.IGNORECASE)
        if following:
            return following.group(1).lower()
    return "UNKNOWN"


def extract_shareholder_rights(ticker: str, proxy: CachedGovernanceSource | None) -> ShareholderRights:
    if proxy is None:
        return ShareholderRights(rights_id=f"governance-rights:{ticker.upper()}", ticker=ticker.upper())
    text = proxy.text
    lower = text.lower()
    if re.search(r"classified board|staggered board|(?:two|three|\d+)\s+staggered classes? of directors|three classes of directors|each class of directors", lower):
        classified = "CLASSIFIED"
    elif re.search(r"each director.*?elected.*?annual meeting|each director.*?elected annually|annual election|one-year term", lower, re.IGNORECASE):
        classified = "ANNUAL_ELECTION"
    else:
        classified = "UNKNOWN"
    classes = re.findall(r"(?:two|three|\d+)\s+(?:staggered\s+)?classes?", lower, re.IGNORECASE)
    number_of_classes = None
    if classes:
        number_match = re.search(r"(two|three|\d+)", classes[0], re.IGNORECASE)
        if number_match:
            number_of_classes = {"two": 2, "three": 3}.get(number_match.group(1).lower()) or int(number_match.group(1))
    special = _yes_no_unknown(
        text,
        (r"stockholders? holding[^.]{0,180}(?:may|can)\s+(?:call|request|demand)[^.]{0,80}special meeting", r"holders? of [^.]{0,120}(?:may|can)\s+(?:call|request|demand)[^.]{0,80}special meeting"),
        (r"stockholders? may not[^.]{0,80}special meeting",),
    )
    written = _yes_no_unknown(text, (r"stockholders?[^.]{0,160}(?:may|can)\s+act by[^.]{0,80}written consent", r"action by written consent is permitted"), (r"stockholders?[^.]{0,120}may not[^.]{0,80}written consent", r"written consent is not permitted"))
    proxy_access = _yes_no_unknown(text, (r"proxy access", r"include director nominees in (?:the )?proxy"), (r"no proxy access",))
    advance_notice = _yes_no_unknown(text, (r"advance notice (?:requirement|provision|procedure|bylaw)", r"(?:bylaws?|charter)[^.]{0,120}advance notice", r"notice of nominations"), ())
    cumulative = _yes_no_unknown(text, (r"cumulative voting is permitted", r"stockholders? may cumulate votes"), (r"not permitted to cumulate votes", r"may not cumulate votes", r"cumulative voting is not permitted"))
    supermajority = _yes_no_unknown(text, (r"supermajority", r"two-thirds vote", r"66 ?(?:1/3|%)"), (r"no supermajority", r"simple majority"))
    poison_pill = _yes_no_unknown(text, (r"shareholder rights plan", r"poison pill"), ())
    contested_sentence = _evidence_sentence(text, (r"contested[^.]{0,120}?(?:plurality|majority)", r"(?:plurality|majority)[^.]{0,120}?contested"))
    uncontested_sentence = _evidence_sentence(text, (r"uncontested[^.]{0,160}?(?:plurality|majority)", r"(?:plurality|majority)[^.]{0,160}?uncontested"))
    contested = _conditional_term(text, r"\bcontested\b")
    uncontested = _conditional_term(text, r"\buncontested\b")
    if contested != "UNKNOWN" and uncontested != "UNKNOWN":
        election = "CONDITIONAL"
    elif contested != "UNKNOWN":
        election = contested
    elif uncontested != "UNKNOWN":
        election = uncontested
    else:
        election = "plurality" if re.search(r"plurality", lower) else "majority" if re.search(r"majority vote|majority of votes", lower) else "UNKNOWN"
    resignation_sentence = _evidence_sentence(text, (r"fails? to receive a majority[^.]{0,180}resign", r"resignation policy", r"conditional resignation"))
    special_scope = _evidence_sentence(text, (r"stockholders? holding[^.]{0,180}(?:may|can)\s+(?:call|request|demand)[^.]{0,80}special meeting", r"holders? of [^.]{0,120}(?:may|can)\s+(?:call|request|demand)[^.]{0,80}special meeting"))
    advance_scope = _evidence_sentence(text, (r"advance notice", r"notice of nominations"))
    return ShareholderRights(
        rights_id=f"governance-rights:{ticker.upper()}", ticker=ticker.upper(), classified_board_status=classified,
        number_of_classes=number_of_classes, director_election_standard=election,
        contested_election_standard=contested, uncontested_election_standard=uncontested,
        resignation_policy="YES" if resignation_sentence else "UNKNOWN",
        special_meeting_right=special, special_meeting_scope=special_scope,
        written_consent_right=written, proxy_access=proxy_access, advance_notice_requirements=advance_notice,
        advance_notice_scope=advance_scope,
        cumulative_voting=cumulative, supermajority_requirements=supermajority, poison_pill=poison_pill,
        source_url=proxy.source_url, source_accession=proxy.accession_number, source_form=proxy.form_type,
        source_date=proxy.filing_date, local_source_path=proxy.local_path,
        source_coverage="PROXY_ONLY_CHARTER_BYLAWS_NOT_CACHED",
        evidence_text=(text[:10000] if classified != "UNKNOWN" else text[:4000]),
    )


__all__ = ["extract_shareholder_rights"]
