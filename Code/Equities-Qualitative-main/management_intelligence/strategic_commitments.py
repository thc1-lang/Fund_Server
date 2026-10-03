"""Evidence-preserving strategic commitments and later outcomes."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Iterable

from .attribution import classify_attribution
from .models import ManagementPerson
from .track_record_models import ManagementTenure, StrategicCommitment, StrategicOutcome


OUTCOME_VALUES = {"ACHIEVED", "PARTIALLY_ACHIEVED", "DELAYED", "MISSED", "REVISED", "ABANDONED", "UNRESOLVED", "NOT_COMPARABLE"}
MATCH_STOPWORDS = {"this", "that", "will", "plan", "expect", "intended", "intend", "company", "continue", "important", "measure", "terms", "think", "meaning", "putting", "additional", "cost", "bring", "interest", "customers", "drive", "future", "more", "also", "really", "some", "very", "their", "our", "into", "from", "with", "have", "been"}


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _category(text: str) -> str:
    lower = text.lower()
    mapping = (
        ("product_launch", r"launch|introduc|release"),
        ("capacity", r"capacity|manufactur|supply chain"),
        ("cost_reduction", r"cost|expense|efficien"),
        ("margin_target", r"margin"),
        ("revenue_target", r"revenue|growth target"),
        ("international_expansion", r"international|europe|asia|global expansion"),
        ("customer_target", r"customer|user|seat"),
        ("regulatory_milestone", r"regulatory|approval|FDA|IND"),
        ("clinical_milestone", r"clinical|trial|phase [1-3]"),
        ("acquisition_integration", r"acquisition|integration|synerg"),
        ("debt_reduction", r"debt|leverage"),
        ("capital_return", r"buyback|repurchase|dividend|capital return"),
    )
    for value, pattern in mapping:
        if re.search(pattern, lower, re.I):
            return value
    return "other"


def _subject(text: str) -> str:
    value = " ".join(text.split())
    value = re.sub(r"^(?:we|our|the company|management)\s+", "", value, flags=re.I)
    return value[:160].strip(" .,:;")


def extract_strategic_commitments(claims: Iterable[dict[str, Any]], documents: dict[str, dict[str, Any]], people: Iterable[ManagementPerson], tenures: Iterable[ManagementTenure], ticker: str) -> list[StrategicCommitment]:
    people = list(people)
    tenures = list(tenures)
    values: list[StrategicCommitment] = []
    for claim in claims:
        evidence = str(claim.get("evidence_text") or claim.get("claim_text") or "")
        lower = evidence.lower()
        # Future, measurable, or explicitly action-oriented commitments only.
        # Guidance language is handled by guidance_track_record and must not be
        # duplicated here as a strategic commitment.
        if re.search(r"\b(?:guidance|outlook|forecast|deferred revenue|eps|revenue|margin|cash flow)\b", lower) and re.search(r"\b(?:expect|raise|lower|reiterate|up|down)\b", lower):
            continue
        action = re.search(r"\b(?:will|plan to|intend to|intended to|committed to|aim to|target(?:ed|ing)?|goal is to|objective is to)\b.{0,140}\b(?:launch|complete|expand|reduce|invest|integrat|achiev|reach|deliver|develop|build|enter|open|increase|decrease|add|create)\w*\b", lower)
        if not action:
            continue
        if re.search(r"\b(?:could|may|might|risk|possibility|i think|opportunity)\b", lower) and not re.search(r"\b(?:target|goal|objective|committed|intend|intended|plan)\b", lower):
            continue
        document = documents.get(str(claim.get("document_id")))
        commitment_date = (document or {}).get("publication_date") or (document or {}).get("event_date") or str(claim.get("created_at") or "")[:10] or None
        person = next((item for item in people if _name_in_text(item, evidence)), None)
        tenure = next((item for item in tenures if person and item.person_id == person.person_id and item.ticker.upper() == ticker.upper()), None)
        target_match = re.search(r"\bby\s+((?:Q[1-4]\s+)?20\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2})", evidence, re.I)
        metric = "revenue" if re.search(r"revenue|growth", lower) else "margin" if "margin" in lower else None
        value_match = re.search(r"(?:\$\s*)?(\d+(?:\.\d+)?)\s*(million|billion|%)", evidence, re.I)
        value = float(value_match.group(1)) * (1_000_000_000 if value_match and value_match.group(2).lower() == "billion" else 1_000_000 if value_match and value_match.group(2).lower() == "million" else 1) if value_match else None
        unit = value_match.group(2) if value_match else None
        values.append(StrategicCommitment(
            commitment_id="commitment:" + _stable(str(claim.get("claim_id")), ticker),
            person_id=person.person_id if person else None,
            ticker=ticker.upper(),
            tenure_id=tenure.tenure_id if tenure else None,
            commitment_date=commitment_date,
            category=_category(evidence),
            subject=_subject(str(claim.get("claim_text") or evidence)),
            commitment_text=evidence,
            target_date=target_match.group(1) if target_match else None,
            metric=metric,
            value=value,
            unit=unit,
            attribution_level=classify_attribution(evidence, person, role_category=tenure.role_category if tenure else None),
            source_claim_id=claim.get("claim_id"),
            source_document_id=claim.get("document_id"),
            evidence_text=evidence,
            source_url=claim.get("source_url") or (document or {}).get("source_url"),
            source_local_path=claim.get("local_path") or (document or {}).get("local_path"),
            created_at=commitment_date or "",
        ))
    return list({item.commitment_id: item for item in values}.values())


def _name_in_text(person: ManagementPerson, text: str) -> bool:
    tokens = [token for token in re.findall(r"[A-Za-z]+", person.full_name) if len(token) > 2]
    return len(tokens) >= 2 and all(re.search(rf"\b{re.escape(token)}\b", text, re.I) for token in tokens[-2:])


def resolve_strategic_outcomes(commitments: Iterable[StrategicCommitment], claims: Iterable[dict[str, Any]], documents: dict[str, dict[str, Any]]) -> list[StrategicOutcome]:
    claims = list(claims)
    outcomes: list[StrategicOutcome] = []
    for commitment in commitments:
        later = []
        for claim in claims:
            evidence = str(claim.get("evidence_text") or claim.get("claim_text") or "")
            if commitment.source_claim_id and claim.get("claim_id") == commitment.source_claim_id:
                continue
            if commitment.commitment_date and str(claim.get("created_at") or "")[:10] <= commitment.commitment_date:
                continue
            tokens = [token.lower() for token in re.findall(r"[A-Za-z]{4,}", commitment.commitment_text) if token.lower() not in MATCH_STOPWORDS]
            overlap = sum(token in evidence.lower() for token in set(tokens[:12]))
            if overlap >= 2:
                later.append((claim, evidence))
        later.sort(key=lambda pair: str(pair[0].get("created_at") or ""))
        if not later:
            outcomes.append(StrategicOutcome(
                outcome_id="strategic-outcome:" + _stable(commitment.commitment_id),
                commitment_id=commitment.commitment_id,
                outcome="UNRESOLVED",
                outcome_date=None,
                evidence_text="No later source evidence located; absence of mention is not treated as failure.",
                source_claim_id=None,
                source_document_id=None,
                source_url=None,
                source_local_path=None,
                created_at="",
            ))
            continue
        claim, evidence = later[-1]
        if re.search(r"\b(?:launched|completed|closed|achieved|delivered|approved|initiated|implemented)\b", evidence, re.I):
            status = "ACHIEVED"
        elif re.search(r"delay|later|rescheduled", evidence, re.I):
            status = "DELAYED"
        elif re.search(r"revised|changed", evidence, re.I):
            status = "REVISED"
        elif re.search(r"abandon|discontinued|cancelled", evidence, re.I):
            status = "ABANDONED"
        else:
            status = "UNRESOLVED"
        document = documents.get(str(claim.get("document_id"))) or {}
        outcomes.append(StrategicOutcome(
            outcome_id="strategic-outcome:" + _stable(commitment.commitment_id),
            commitment_id=commitment.commitment_id,
            outcome=status,
            outcome_date=document.get("publication_date") or document.get("event_date") or str(claim.get("created_at") or "")[:10] or None,
            evidence_text=evidence,
            source_claim_id=claim.get("claim_id"),
            source_document_id=claim.get("document_id"),
            source_url=claim.get("source_url") or document.get("source_url"),
            source_local_path=claim.get("local_path") or document.get("local_path"),
            created_at=str(claim.get("created_at") or "")[:10],
        ))
    return outcomes


__all__ = ["extract_strategic_commitments", "resolve_strategic_outcomes", "OUTCOME_VALUES"]
