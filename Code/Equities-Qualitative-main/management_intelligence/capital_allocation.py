"""Factual capital-allocation event extraction from source-bound claims."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from .attribution import classify_attribution
from .models import ManagementPerson
from .track_record_models import CapitalAllocationEvent, ManagementTenure


EVENT_TYPES = {"acquisition", "divestiture", "buyback_authorization", "buyback_execution", "share_issuance", "equity_compensation", "debt_issuance", "debt_repayment", "dividend", "capex_program", "major_investment"}


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _amount(text: str) -> tuple[float | None, str | None]:
    match = re.search(r"(?:\$\s*)?(\d+(?:\.\d+)?)\s*(million|billion|thousand|m|bn|k)\b", text, re.I)
    if not match:
        return None, None
    scale = {"billion": 1_000_000_000, "bn": 1_000_000_000, "million": 1_000_000, "m": 1_000_000, "thousand": 1_000, "k": 1_000}.get(match.group(2).lower(), 1)
    return float(match.group(1)) * scale, "USD"


def _classify(text: str) -> tuple[str, str] | None:
    lower = text.lower()
    if re.search(r"closed (?:the )?acquisition|completed (?:the )?acquisition|was .*largest acquisition|largest acquisition .*at", lower): return "acquisition", "completed"
    if re.search(r"acquisition of|acquired\b|entered into an agreement", lower) or ("acquisition" in lower and "largest acquisition" in lower): return "acquisition", "announced"
    if re.search(r"divest|sale of (?:the )?(?:business|subsidiary|asset)", lower): return "divestiture", "completed" if re.search(r"sold|completed", lower) else "announced"
    if re.search(r"authorized .*repurchase|repurchase authorization|authorized .*buyback", lower): return "buyback_authorization", "authorized"
    if re.search(r"repurchased|buyback program|share repurchase program", lower) and re.search(r"completed|repurchased|bought back", lower): return "buyback_execution", "executed"
    if re.search(r"issued .*shares|equity issuance|offering", lower): return "share_issuance", "executed"
    if re.search(r"stock[- ]based compensation|equity compensation", lower): return "equity_compensation", "reported"
    if re.search(r"issued .*debt|debt issuance|notes offering", lower): return "debt_issuance", "executed"
    if re.search(r"repaid .*debt|debt repayment|maturity", lower): return "debt_repayment", "executed"
    if re.search(r"dividend", lower): return "dividend", "declared" if "declared" in lower else "executed"
    if re.search(r"capex|capital expenditure", lower) and re.search(r"program|plan|spend", lower): return "capex_program", "planned"
    if re.search(r"invested|investment", lower): return "major_investment", "reported"
    return None


def _target(text: str, event_type: str) -> str | None:
    if event_type == "acquisition":
        match = re.search(r"(?:acquisition of|acquired)\s+([A-Za-z0-9][A-Za-z0-9 .&'-]{1,70}?)(?=\s+(?:for|at|in|valued|worth)\b|[,.;]|$)", text, re.I)
        if match:
            return match.group(1).strip(" .,;")
        largest = re.search(r"(?:while|and|;|^)\s*([A-Za-z][A-Za-z0-9&.'-]*(?:\s+[A-Za-z][A-Za-z0-9&.'-]*){0,3})\s+was\s+(?:[A-Za-z][A-Za-z0-9&.'-]*\s+)?largest acquisition\b", text, re.I)
        return largest.group(1).strip(" .,;") if largest else None
    return None


def extract_capital_events(claims: Iterable[dict[str, Any]], documents: dict[str, dict[str, Any]], people: Iterable[ManagementPerson], tenures: Iterable[ManagementTenure], ticker: str, company_name: str) -> list[CapitalAllocationEvent]:
    people = list(people)
    tenures = list(tenures)
    result: list[CapitalAllocationEvent] = []
    for claim in claims:
        text = str(claim.get("evidence_text") or claim.get("claim_text") or "")
        classified = _classify(text)
        if not classified:
            continue
        event_type, status = classified
        document = documents.get(str(claim.get("document_id"))) or {}
        event_date = document.get("publication_date") or document.get("event_date") or str(claim.get("created_at") or "")[:10] or None
        person = next((item for item in people if _name_in_text(item, text)), None)
        tenure = next((item for item in tenures if person and item.person_id == person.person_id and item.ticker.upper() == ticker.upper()), None)
        value, currency = _amount(text)
        target = _target(text, event_type)
        program_id = "capital-program:" + _stable(ticker, event_type, target.lower() if target else str(claim.get("claim_id")))
        result.append(CapitalAllocationEvent(
            event_id="capital:" + _stable(str(claim.get("claim_id")), ticker, event_type),
            ticker=ticker.upper(),
            company_name=company_name,
            person_id=person.person_id if person else None,
            tenure_id=tenure.tenure_id if tenure else None,
            event_date=event_date,
            event_type=event_type,
            event_status=status,
            value=value,
            currency=currency,
            shares=None,
            target_or_counterparty=target,
            stated_rationale=text if event_type in {"acquisition", "divestiture", "major_investment", "capex_program"} else None,
            source_claim_id=claim.get("claim_id"),
            source_document_id=claim.get("document_id"),
            evidence_text=text,
            source_url=claim.get("source_url") or document.get("source_url"),
            source_local_path=claim.get("local_path") or document.get("local_path"),
            attribution_level=classify_attribution(text, person, role_category=tenure.role_category if tenure else None),
            capital_allocation_program_id=program_id,
            created_at=event_date or "",
        ))
    return list({item.event_id: item for item in result}.values())


def _name_in_text(person: ManagementPerson, text: str) -> bool:
    tokens = [token for token in re.findall(r"[A-Za-z]+", person.full_name) if len(token) > 2]
    return len(tokens) >= 2 and all(re.search(rf"\b{re.escape(token)}\b", text, re.I) for token in tokens[-2:])


__all__ = ["extract_capital_events", "EVENT_TYPES"]
