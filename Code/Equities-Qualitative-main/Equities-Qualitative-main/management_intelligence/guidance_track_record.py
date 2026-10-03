"""Conservative guidance extraction and actual matching."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .attribution import classify_attribution
from .models import ManagementPerson
from .track_record_models import Attribution, FinancialFact, GuidanceCommitment, GuidanceOutcome, GuidanceCoverageDiagnostic, ManagementTenure


GUIDANCE_STATUSES = {"issued", "reiterated", "raised", "lowered", "withdrawn", "superseded"}
OUTCOMES = {"BEAT", "MET", "MISS", "WITHDRAWN", "SUPERSEDED", "UNRESOLVED", "NOT_COMPARABLE"}


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def load_qualitative_evidence(root: str | Path, ticker: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    root = Path(root)
    documents: dict[str, dict[str, Any]] = {}
    document_path = root / "documents.jsonl"
    if document_path.exists():
        for line in document_path.open(encoding="utf-8"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("ticker", "").upper() == ticker.upper():
                documents[str(value.get("document_id"))] = value
    claims: list[dict[str, Any]] = []
    claim_path = root / "claims.jsonl"
    if claim_path.exists():
        for line in claim_path.open(encoding="utf-8"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("ticker", "").upper() == ticker.upper():
                claims.append(value)
    return claims, documents


def _metric(text: str) -> str | None:
    lower = text.lower()
    for key, patterns in {
        "EPS": (r"\beps\b", r"earnings per share"),
        "revenue": (r"revenue", r"top[- ]line"),
        "gross_margin": (r"gross margin",),
        "operating_margin": (r"operating margin", r"income from operations"),
        "operating_income": (r"operating income",),
        "free_cash_flow": (r"free cash flow",),
        "capital_expenditure": (r"capex", r"capital expenditure"),
        "cash": (r"cash(?: balance| position)?",),
    }.items():
        if any(re.search(pattern, lower) for pattern in patterns):
            return key
    return None


def _metrics(text: str) -> list[str]:
    lower = text.lower()
    patterns = {
        "EPS": (r"\beps\b", r"earnings per share"),
        "revenue": (r"revenue", r"top[- ]line"),
        "gross_margin": (r"gross margin",),
        "operating_margin": (r"operating margin", r"income from operations"),
        "operating_income": (r"operating income",),
        "free_cash_flow": (r"free cash flow",),
        "capital_expenditure": (r"capex", r"capital expenditure"),
        "cash": (r"cash(?: balance| position)?",),
    }
    return [key for key, values in patterns.items() if any(re.search(pattern, lower) for pattern in values)]


def _horizon(text: str, period_label: str | None) -> str | None:
    match = re.search(r"\bFY\s*['’]?\s*(20\d{2}|\d{2})\b", text, re.I)
    if match:
        year = match.group(1)
        year = f"20{year}" if len(year) == 2 else year
        return f"FY{year}"
    match = re.search(r"\bQ([1-4])\s*(20\d{2})?\b", text, re.I)
    if match:
        year = match.group(2) or (re.search(r"(20\d{2})", period_label or "") or [None, None])[1]
        return f"Q{match.group(1)} {year}" if year else f"Q{match.group(1)}"
    if period_label and re.search(r"FY\s*20\d{2}", period_label, re.I):
        return "FY" + re.search(r"20\d{2}", period_label).group(0)
    return None


def _number_value(value: str, unit: str | None) -> float:
    number = float(value.replace(",", ""))
    unit_lower = (unit or "").lower()
    if unit_lower in {"billion", "bn"}: return number * 1_000_000_000
    if unit_lower in {"million", "mm", "m"}: return number * 1_000_000
    if unit_lower in {"thousand", "k"}: return number * 1_000
    return number


def _values(text: str) -> tuple[float | None, float | None, str | None]:
    range_match = re.search(r"(?:\$\s*)?(\d+(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|m|k|%)?\s*(?:to|-)\s*(?:\$\s*)?(\d+(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|m|k|%)?", text, re.I)
    if range_match:
        unit = range_match.group(2) or range_match.group(4)
        return _number_value(range_match.group(1), unit), _number_value(range_match.group(3), range_match.group(4) or unit), unit
    point_match = re.search(r"(?:guidance|outlook|forecast|target|guide)\D{0,50}(?:\$\s*)?(\d+(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|m|k|%)?", text, re.I)
    if point_match:
        return None, None, point_match.group(2)
    return None, None, None


def _status(text: str) -> str:
    lower = text.lower()
    if re.search(r"withdrawn|withdraw", lower): return "withdrawn"
    if re.search(r"supersed|replaced", lower): return "superseded"
    if re.search(r"raise|raised|increase|increased|higher", lower): return "raised"
    if re.search(r"lower|lowered|cut|reduced", lower): return "lowered"
    if re.search(r"reiterate|maintain|unchanged|continue to expect", lower): return "reiterated"
    return "issued"


def _date(document: dict[str, Any] | None, claim: dict[str, Any]) -> str | None:
    if document:
        return document.get("publication_date") or document.get("event_date")
    return claim.get("created_at", "")[:10] or None


def _speaker(evidence: str, people: Iterable[ManagementPerson]) -> tuple[ManagementPerson | None, str | None]:
    for person in people:
        if _name_in_text(person, evidence):
            return person, person.full_name
    return None, None


def _name_in_text(person: ManagementPerson, text: str) -> bool:
    tokens = [token for token in re.findall(r"[A-Za-z]+", person.full_name) if len(token) > 2]
    return len(tokens) >= 2 and all(re.search(rf"\b{re.escape(token)}\b", text, re.I) for token in tokens[-2:])


def extract_guidance_commitments(claims: Iterable[dict[str, Any]], documents: dict[str, dict[str, Any]], people: Iterable[ManagementPerson], tenures: Iterable[ManagementTenure], ticker: str) -> list[GuidanceCommitment]:
    people = list(people)
    tenures = list(tenures)
    values: list[GuidanceCommitment] = []
    for claim in claims:
        text = " ".join(str(claim.get(key) or "") for key in ("claim_text", "evidence_text"))
        lower = text.lower()
        if not re.search(r"guidance|outlook|forecast|target|expect", lower):
            continue
        horizon = _horizon(text, claim.get("period_label"))
        metrics = _metrics(text)
        if not metrics or not horizon:
            continue
        low, high, unit = _values(text)
        if low is None and not re.search(r"raise|raised|lower|lowered|maintain|reiterate|withdraw|supersed", lower):
            continue
        document = documents.get(str(claim.get("document_id")))
        issued = _date(document, claim)
        person, speaker = _speaker(text, people)
        matching_tenure = next((item for item in tenures if person and item.person_id == person.person_id and item.ticker.upper() == ticker.upper() and (not issued or not item.start_date or item.start_date <= issued) and (not item.end_date or not issued or issued <= item.end_date)), None)
        attribution = classify_attribution(text, person, role_category=matching_tenure.role_category if matching_tenure else None)
        for metric in metrics:
            guidance_id = "guidance:" + _stable(str(claim.get("claim_id")), ticker, metric, horizon)
            values.append(GuidanceCommitment(
                guidance_id=guidance_id,
                ticker=ticker.upper(),
                person_id=person.person_id if person else None,
                tenure_id=matching_tenure.tenure_id if matching_tenure else None,
                issued_date=issued,
                fiscal_horizon=horizon,
                metric=metric,
                scope="company",
                low_value=low,
                high_value=high,
                point_value=None,
                unit="percent" if unit == "%" else ("USD" if unit else None),
                accounting_basis="non-GAAP" if "non-gaap" in lower else "GAAP" if "gaap" in lower else None,
                guidance_status=_status(text),
                speaker=speaker,
                attribution_level=attribution,
                source_claim_id=claim.get("claim_id"),
                source_document_id=claim.get("document_id"),
                evidence_text=str(claim.get("evidence_text") or claim.get("claim_text") or ""),
                source_url=claim.get("source_url") or (document or {}).get("source_url"),
                source_local_path=claim.get("local_path") or (document or {}).get("local_path"),
                created_at=issued or "",
            ))
    unique = list({item.guidance_id: item for item in values}.values())
    series: dict[tuple[str, str, str, str, str | None], list[GuidanceCommitment]] = {}
    for item in unique:
        series.setdefault((item.ticker, item.metric, item.fiscal_horizon, item.scope, item.unit), []).append(item)
    for members in series.values():
        members.sort(key=lambda item: (item.issued_date or "", item.guidance_id))
        for index, item in enumerate(members, start=1):
            item.revision_number = index
            item.revision_type = "initial" if index == 1 else item.guidance_status
    return unique


def _horizon_matches(guidance: GuidanceCommitment, fact: FinancialFact) -> bool:
    if guidance.fiscal_horizon.startswith("FY"):
        return fact.fiscal_year == int(guidance.fiscal_horizon[2:]) and fact.fiscal_period in {"FY", None}
    match = re.match(r"Q([1-4])\s*(20\d{2})?", guidance.fiscal_horizon)
    return bool(match and fact.fiscal_period == f"Q{match.group(1)}" and (not match.group(2) or fact.fiscal_year == int(match.group(2))))


def _unit_matches(guidance: GuidanceCommitment, fact: FinancialFact) -> bool:
    """Do not compare percentage guidance with dollar facts (or vice versa)."""
    if not guidance.unit:
        return True
    wanted = guidance.unit.upper()
    actual = fact.unit.upper()
    if wanted in {"PERCENT", "%"}:
        return actual in {"PERCENT", "%"}
    if wanted in {"USD", "DOLLAR", "DOLLARS"}:
        return actual in {"USD", "USD/SHARES", "DOLLAR"}
    return wanted == actual


def match_guidance_to_actuals(guidance: GuidanceCommitment, facts: Iterable[FinancialFact], *, outcome_id: str | None = None, as_of_date: str | None = None) -> GuidanceOutcome:
    if guidance.guidance_status == "withdrawn":
        outcome = "WITHDRAWN"
        return GuidanceOutcome(outcome_id or "guidance-outcome:" + _stable(guidance.guidance_id), guidance.guidance_id, guidance.ticker, None, None, outcome, None, None, {}, "guidance_status", created_at="")
    horizon_candidates = [fact for fact in facts if fact.metric == guidance.metric and _horizon_matches(guidance, fact) and (not as_of_date or fact.period_end <= as_of_date)]
    candidates = [fact for fact in horizon_candidates if _unit_matches(guidance, fact)]
    if horizon_candidates and not candidates:
        actual = sorted(horizon_candidates, key=lambda item: (item.period_end, item.source_fact.get("filed") or ""))[-1]
        return GuidanceOutcome(outcome_id or "guidance-outcome:" + _stable(guidance.guidance_id), guidance.guidance_id, guidance.ticker, None, actual.period_end, "NOT_COMPARABLE", None, None, actual.to_dict(), "unit_match_required", "guidance unit does not match actual fact unit", created_at="")
    if not candidates:
        return GuidanceOutcome(outcome_id or "guidance-outcome:" + _stable(guidance.guidance_id), guidance.guidance_id, guidance.ticker, None, None, "UNRESOLVED", None, None, {}, "same_metric_same_horizon_required", "no comparable actual found", created_at="")
    actual = sorted(candidates, key=lambda item: (item.period_end, item.source_fact.get("filed") or ""))[-1]
    if guidance.accounting_basis and guidance.accounting_basis != actual.accounting_basis and guidance.metric in {"EPS", "operating_income", "revenue"}:
        return GuidanceOutcome(outcome_id or "guidance-outcome:" + _stable(guidance.guidance_id), guidance.guidance_id, guidance.ticker, None, actual.period_end, "NOT_COMPARABLE", None, None, actual.to_dict(), "accounting_basis_match", "accounting basis mismatch", created_at="")
    if guidance.low_value is None and guidance.high_value is None:
        return GuidanceOutcome(outcome_id or "guidance-outcome:" + _stable(guidance.guidance_id), guidance.guidance_id, guidance.ticker, actual.value, actual.period_end, "UNRESOLVED", None, None, actual.to_dict(), "range_or_point_required", "guidance value was not numerically stated", created_at="")
    if guidance.low_value is not None and guidance.high_value is not None:
        outcome = "MET" if guidance.low_value <= actual.value <= guidance.high_value else "BEAT" if actual.value > guidance.high_value else "MISS"
        midpoint = (guidance.low_value + guidance.high_value) / 2
        error = actual.value - midpoint
    else:
        target = guidance.point_value or guidance.low_value or guidance.high_value
        error = actual.value - target if target is not None else None
        outcome = "MET" if target is not None and (abs(error) <= abs(target) * 0.01 if target else actual.value == target) else "BEAT" if error and error > 0 else "MISS"
    percentage_error = (error / abs(actual.value) * 100.0) if error is not None and actual.value else None
    return GuidanceOutcome(outcome_id or "guidance-outcome:" + _stable(guidance.guidance_id), guidance.guidance_id, guidance.ticker, actual.value, actual.period_end, outcome, error, percentage_error, actual.to_dict(), "same_metric_same_horizon_same_unit", created_at=actual.period_end)


def diagnose_guidance_coverage(ticker: str, claims: Iterable[dict[str, Any]], documents: dict[str, dict[str, Any]], captured: Iterable[GuidanceCommitment]) -> GuidanceCoverageDiagnostic:
    claims = list(claims)
    captured = list(captured)
    language = []
    numeric = []
    for claim in claims:
        text = " ".join(str(claim.get(key) or "") for key in ("claim_text", "evidence_text"))
        if not re.search(r"\b(?:guidance|outlook|forecast|target|expect|raise|lower|reiterate)\b", text, re.I):
            continue
        language.append(text)
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|percent|million|billion|thousand|m|bn|mm|k)|\$\s*\d", text, re.I):
            numeric.append(text)
    explicit_absence = any(re.search(r"\b(?:we|the company|management)\s+(?:do not|does not|never)\s+(?:provide|issue|give)\s+guidance\b|\bno guidance is provided\b", text, re.I) for text in language)
    if explicit_absence:
        reason = "NO_GUIDANCE_GIVEN_CONFIRMED"
    elif captured:
        reason = "GUIDANCE_AVAILABLE_AND_CAPTURED"
    elif language and numeric:
        reason = "GUIDANCE_PRESENT_BUT_NOT_COMPARABLE"
    elif language:
        reason = "GUIDANCE_PRESENT_BUT_NOT_NUMERIC"
    elif documents and not claims:
        reason = "GUIDANCE_DOCUMENTS_PRESENT_BUT_NOT_EXTRACTED"
    elif not claims and not documents:
        reason = "GUIDANCE_NOT_IN_CURRENT_DOCUMENT_STORE"
    else:
        reason = "GUIDANCE_EVIDENCE_NOT_FOUND"
    return GuidanceCoverageDiagnostic(
        ticker=ticker.upper(), reason=reason, document_count=len(documents), claim_count=len(claims),
        guidance_language_claims=len(language), numeric_guidance_claims=len(numeric), captured_commitments=len(captured),
        examples=[item[:240] for item in (numeric or language)[:3]],
        guidance_absence_confirmed=explicit_absence,
        coverage_neutral=not explicit_absence,
        created_at="diagnostic",
    )


__all__ = ["load_qualitative_evidence", "extract_guidance_commitments", "match_guidance_to_actuals", "diagnose_guidance_coverage", "GUIDANCE_STATUSES", "OUTCOMES"]
