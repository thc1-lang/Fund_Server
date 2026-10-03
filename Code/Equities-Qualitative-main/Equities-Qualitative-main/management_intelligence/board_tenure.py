"""Conservative tenure and factual refreshment calculations."""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean, median
from typing import Iterable

from .board_governance_models import BoardRefreshment, BoardTenure, GovernanceBoardMembership
from .models import RoleChangeEvent


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            if fmt == "%Y":
                return date(int(value), 1, 1)
            if fmt == "%Y-%m":
                return date.fromisoformat(value + "-01")
            return date.fromisoformat(value)
        except ValueError:
            continue
    return None


def _bucket(years: float | None) -> str:
    if years is None:
        return "unknown"
    if years < 3:
        return "<3 years"
    if years < 6:
        return "3-6 years"
    if years < 9:
        return "6-9 years"
    return ">9 years"


def calculate_tenure(memberships: Iterable[GovernanceBoardMembership], *, as_of_date: str) -> list[BoardTenure]:
    as_of = _parse_date(as_of_date) or date.today()
    result: list[BoardTenure] = []
    for member in memberships:
        start = _parse_date(member.start_date)
        days = (as_of - start).days if start else None
        years = round(days / 365.2425, 2) if days is not None else None
        precision = (member.date_precision or "").lower()
        if member.start_date and len(member.start_date) == 10 and precision not in {"year", "month"}:
            precision = "day"
        if precision not in {"day", "month", "year"}:
            precision = "unknown"
        exact_years = years if precision == "day" else None
        estimate_years = years if precision in {"day", "month", "year"} else None
        result.append(BoardTenure(
            tenure_id=f"governance-tenure:{member.ticker}:{member.person_id}",
            ticker=member.ticker, person_id=member.person_id, start_date=member.start_date,
            as_of_date=as_of_date, date_precision=member.date_precision,
            tenure_days=days, tenure_years=years, tenure_years_exact=exact_years,
            tenure_years_estimate=estimate_years, tenure_precision=precision.upper(),
            tenure_bucket=_bucket(years), age=member.age,
            source_url=member.source_url, source_accession=member.source_accession,
            evidence_text=member.evidence_text,
        ))
    return sorted(result, key=lambda item: item.person_id)


def summarize_tenure(records: Iterable[BoardTenure]) -> dict[str, object]:
    records = list(records)
    values = [item.tenure_years for item in records if item.tenure_years is not None]
    buckets = {key: 0 for key in ("<3 years", "3-6 years", "6-9 years", ">9 years", "unknown")}
    for item in records:
        buckets[item.tenure_bucket] = buckets.get(item.tenure_bucket, 0) + 1
    precisions = {item.tenure_precision for item in records if item.tenure_years is not None}
    if not values:
        statistics_precision = "UNKNOWN"
    elif precisions == {"DAY"}:
        statistics_precision = "EXACT"
    elif precisions and precisions <= {"DAY", "MONTH", "YEAR"}:
        statistics_precision = "ESTIMATED"
    else:
        statistics_precision = "PARTIAL"
    return {
        "average": round(mean(values), 2) if values else None,
        "median": round(median(values), 2) if values else None,
        "buckets": buckets,
        "statistics_precision": statistics_precision,
        "value_count": len(values),
    }


def refreshment(
    ticker: str,
    memberships: Iterable[GovernanceBoardMembership],
    events: Iterable[RoleChangeEvent],
    *,
    as_of_date: str,
) -> BoardRefreshment:
    as_of = _parse_date(as_of_date) or date.today()
    added: dict[str, str] = {}
    departed: dict[str, str] = {}
    for member in memberships:
        start = _parse_date(member.start_date)
        if start and start <= as_of:
            added.setdefault(member.person_id, member.start_date or "")
        if member.end_date and member.end_date <= as_of:
            departed.setdefault(member.person_id, member.end_date)
    source_ids: list[str] = []
    for event in events:
        effective = _parse_date(event.effective_date)
        if not effective or effective > as_of:
            continue
        target = added if event.event_type == "appointment" else departed if event.event_type == "departure" else None
        if target is not None and event.person_id:
            target.setdefault(event.person_id, event.effective_date or "")
        if event.event_id:
            source_ids.append(event.event_id)
    one_year = as_of.toordinal() - 366
    three_year = as_of.toordinal() - 1096
    def count(values: dict[str, str], days: int) -> tuple[int, list[str]]:
        ids = sorted(person_id for person_id, value in values.items() if (_parse_date(value) and _parse_date(value).toordinal() >= as_of.toordinal() - days))
        return len(ids), ids
    a1, a1_ids = count(added, 366); a3, a3_ids = count(added, 1096)
    d1, d1_ids = count(departed, 366); d3, d3_ids = count(departed, 1096)
    return BoardRefreshment(
        refreshment_id=f"governance-refreshment:{ticker.upper()}:{as_of_date}", ticker=ticker.upper(), as_of_date=as_of_date,
        directors_added_1y=a1, directors_added_3y=a3, directors_departed_1y=d1, directors_departed_3y=d3,
        added_person_ids=a3_ids, departed_person_ids=d3_ids, source_ids=sorted(set(source_ids)),
    )


__all__ = ["calculate_tenure", "summarize_tenure", "refreshment"]
