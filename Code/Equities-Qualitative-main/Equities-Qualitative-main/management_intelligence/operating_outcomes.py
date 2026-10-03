"""SEC Company Facts retrieval, normalization, comparability, and outcomes."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from insider_intelligence.sec_ingestion import SECSourceProvider, SEC_DATA

from .track_record_models import Attribution, FinancialFact, ManagementTenure, OperatingOutcome


METRIC_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "free_cash_flow": ("FreeCashFlow",),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "current_debt": ("LongTermDebtCurrent", "DebtCurrent"),
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
    "diluted_weighted_average_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "EPS": ("EarningsPerShareDiluted",),
    "stock_based_compensation": ("ShareBasedCompensation",),
}


class CompanyFactsProvider:
    """Small adapter over the frozen SEC transport/cache implementation."""

    def __init__(self, sec: SECSourceProvider | None = None):
        self.sec = sec or SECSourceProvider()

    def fetch(self, ticker: str, *, force: bool = False) -> tuple[dict[str, Any], str, str | None]:
        identity = self.sec.resolve_issuer(ticker, force=force)
        url = f"{SEC_DATA}/api/xbrl/companyfacts/CIK{identity.issuer_cik}.json"
        cache = self.sec.cache_root / identity.ticker / "companyfacts.json"
        payload = self.sec._json(url, cache_path=cache, force=force)
        return payload, url, str(cache)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _basis(namespace: str, tag: str) -> str:
    return "GAAP" if namespace == "us-gaap" or namespace == "dei" else "UNKNOWN"


def normalize_company_facts(payload: dict[str, Any], ticker: str, source_url: str, source_local_path: str | None = None) -> list[FinancialFact]:
    """Normalize annual and quarterly SEC facts without inventing missing values."""
    values: list[FinancialFact] = []
    facts = payload.get("facts", {}) if isinstance(payload, dict) else {}
    for metric, tags in METRIC_TAGS.items():
        for namespace, namespace_facts in facts.items():
            if not isinstance(namespace_facts, dict):
                continue
            for tag in tags:
                definition = namespace_facts.get(tag)
                if not isinstance(definition, dict):
                    continue
                units = definition.get("units", {})
                for unit, entries in units.items():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict) or not entry.get("end"):
                            continue
                        value = _number(entry.get("val"))
                        if value is None:
                            continue
                        form = str(entry.get("form") or "")
                        if form and form.upper() not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
                            continue
                        period_end = str(entry["end"])
                        period_start = str(entry["start"]) if entry.get("start") else None
                        fy = entry.get("fy")
                        try:
                            fiscal_year = int(fy) if fy is not None else int(period_end[:4])
                        except (TypeError, ValueError):
                            fiscal_year = None
                        fiscal_period = str(entry.get("fp") or "") or None
                        source_fact = {"namespace": namespace, "tag": tag, "unit": unit, "frame": entry.get("frame"), "fy": fiscal_year, "fp": fiscal_period, "form": form, "filed": entry.get("filed"), "accn": entry.get("accn"), "start": period_start, "end": period_end, "val": value}
                        fact_id = "fact:" + hashlib.sha256(f"{ticker}|{metric}|{namespace}|{tag}|{unit}|{period_start}|{period_end}|{entry.get('frame')}|{entry.get('accn')}|{value}".encode()).hexdigest()[:24]
                        values.append(FinancialFact(
                            fact_id=fact_id,
                            ticker=ticker.upper(),
                            metric=metric,
                            period_start=period_start,
                            period_end=period_end,
                            value=value,
                            unit=unit,
                            accounting_basis=_basis(namespace, tag),
                            fiscal_year=fiscal_year,
                            fiscal_period=fiscal_period,
                            form=form or None,
                            accession_number=entry.get("accn"),
                            source_url=source_url,
                            source_local_path=source_local_path,
                            source_fact=source_fact,
                            created_at=str(entry.get("filed") or period_end),
                        ))
    # First collapse amendments/comparative disclosures for the same taxonomy
    # concept. Preserve every discarded candidate as revision provenance.
    concept_dedup: dict[tuple[str, str, str | None, str, int | None, str | None, str | None], FinancialFact] = {}
    for fact in values:
        source = fact.source_fact
        key = (fact.metric, str(source.get("tag") or ""), fact.period_start, fact.period_end, fact.fiscal_year, fact.fiscal_period, str(source.get("frame") or ""))
        old = concept_dedup.get(key)
        if old is None:
            concept_dedup[key] = fact
        else:
            old_filed = str(old.source_fact.get("filed") or "")
            new_filed = str(source.get("filed") or "")
            if new_filed >= old_filed:
                fact.revision_provenance = old.revision_provenance + [old.to_dict()]
                concept_dedup[key] = fact
            else:
                old.revision_provenance.append(fact.to_dict())

    # Select one canonical concept for each economic metric/period. Tag order
    # is an explicit, deterministic taxonomy preference; no values are averaged.
    canonical: dict[tuple[str, str, str | None, str], FinancialFact] = {}
    rank = {tag: index for metric_tags in METRIC_TAGS.values() for index, tag in enumerate(metric_tags)}
    for fact in concept_dedup.values():
        # Fiscal labels and frames are tie-breaking provenance for an
        # observation; exact concept/unit/start/end still represent one
        # economic observation and must not be counted repeatedly.
        key = (fact.metric, fact.unit, fact.period_start, fact.period_end)
        old = canonical.get(key)
        tag = str(fact.source_fact.get("tag") or "")
        if old is None:
            canonical[key] = fact
            continue
        old_tag = str(old.source_fact.get("tag") or "")
        old_rank = rank.get(old_tag, 999)
        new_rank = rank.get(tag, 999)
        if new_rank < old_rank or (new_rank == old_rank and str(fact.source_fact.get("filed") or "") >= str(old.source_fact.get("filed") or "")):
            fact.revision_provenance = old.revision_provenance + [old.to_dict()]
            canonical[key] = fact
        else:
            old.revision_provenance.append(fact.to_dict())

    result = list(canonical.values())
    # Derive only same-period, same-unit, same-basis FCF and GAAP margins when
    # the filing reports the required components. Derived values retain the
    # exact source fact IDs and formula for auditability.
    for operating_cash in [x for x in result if x.metric == "operating_cash_flow"]:
        key = (operating_cash.period_start, operating_cash.period_end, operating_cash.fiscal_year, operating_cash.fiscal_period, operating_cash.unit, operating_cash.accounting_basis)
        capex = next((x for x in result if x.metric == "capital_expenditure" and (x.period_start, x.period_end, x.fiscal_year, x.fiscal_period, x.unit, x.accounting_basis) == key), None)
        if capex is None:
            continue
        if any(x.metric == "free_cash_flow" and (x.period_start, x.period_end, x.fiscal_year, x.fiscal_period, x.unit, x.accounting_basis) == key and x.value_method == "reported" for x in result):
            continue
        result.append(FinancialFact(
            fact_id="fact:" + hashlib.sha256(f"{ticker}|free_cash_flow|{key}".encode()).hexdigest()[:24],
            ticker=ticker.upper(), metric="free_cash_flow", period_start=operating_cash.period_start, period_end=operating_cash.period_end,
            value=operating_cash.value - capex.value, unit=operating_cash.unit, accounting_basis=operating_cash.accounting_basis,
            fiscal_year=operating_cash.fiscal_year, fiscal_period=operating_cash.fiscal_period, form=operating_cash.form,
            accession_number=operating_cash.accession_number, source_url=source_url, source_local_path=source_local_path,
            source_fact={"formula": "operating_cash_flow - capital_expenditure", "operating_cash_flow_fact_id": operating_cash.fact_id, "capital_expenditure_fact_id": capex.fact_id, "start": operating_cash.period_start, "end": operating_cash.period_end},
            value_method="derived", derivation={"formula": "operating_cash_flow - capital_expenditure", "source_fact_ids": [operating_cash.fact_id, capex.fact_id]}, created_at=max(operating_cash.created_at, capex.created_at),
        ))
    revenue_by_period = {(x.period_start, x.period_end, x.fiscal_year, x.fiscal_period, x.accounting_basis): x for x in result if x.metric == "revenue" and x.unit == "USD"}
    for key, revenue in revenue_by_period.items():
        for profit_metric, derived_metric in (("gross_profit", "gross_margin"), ("operating_income", "operating_margin")):
            profit = next((x for x in result if x.metric == profit_metric and x.unit == revenue.unit and (x.period_start, x.period_end, x.fiscal_year, x.fiscal_period, x.accounting_basis) == key), None)
            if profit is None or revenue.value == 0 or any(x.metric == derived_metric and (x.period_start, x.period_end, x.fiscal_year, x.fiscal_period, x.accounting_basis) == key for x in result):
                continue
            result.append(FinancialFact(
                fact_id="fact:" + hashlib.sha256(f"{ticker}|{derived_metric}|{key}".encode()).hexdigest()[:24], ticker=ticker.upper(), metric=derived_metric,
                period_start=revenue.period_start, period_end=revenue.period_end, value=profit.value / revenue.value * 100.0, unit="percent", accounting_basis=revenue.accounting_basis,
                fiscal_year=revenue.fiscal_year, fiscal_period=revenue.fiscal_period, form=revenue.form, accession_number=revenue.accession_number,
                source_url=source_url, source_local_path=source_local_path,
                source_fact={"formula": f"{profit_metric} / revenue * 100", "profit_fact_id": profit.fact_id, "revenue_fact_id": revenue.fact_id},
                value_method="derived", derivation={"formula": f"{profit_metric} / revenue * 100", "source_fact_ids": [profit.fact_id, revenue.fact_id]}, created_at=max(profit.created_at, revenue.created_at),
            ))
    return sorted(result, key=lambda item: (item.metric, item.period_end, item.unit, item.fact_id))


def period_status(fact: FinancialFact, tenure: ManagementTenure, *, as_of_date: str) -> str:
    """Classify a financial period relative to a tenure boundary."""
    period_start = fact.period_start or f"{fact.period_end[:4]}-01-01"
    period_end = fact.period_end
    start = tenure.start_date or "0000-01-01"
    end = tenure.end_date or tenure.scheduled_end_date or as_of_date
    if period_end < start:
        return "PRE_TENURE"
    if period_start > end:
        return "POST_TENURE"
    if period_start >= start and period_end <= end:
        return "FULL_PERIOD"
    return "PARTIAL_PERIOD"


def _comparable(a: FinancialFact, b: FinancialFact) -> bool:
    return a.metric == b.metric and a.unit == b.unit and a.accounting_basis == b.accounting_basis


def _boundary_date(value: str, *, end: bool = False) -> date:
    if len(value) == 4:
        return date.fromisoformat(f"{value}-12-31" if end else f"{value}-01-01")
    if len(value) == 7:
        year, month = (int(part) for part in value.split("-"))
        if end:
            next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
            return next_month.fromordinal(next_month.toordinal() - 1)
        return date(year, month, 1)
    return date.fromisoformat(value)


def build_operating_outcomes(tenure: ManagementTenure, facts: Iterable[FinancialFact], *, as_of_date: str) -> list[OperatingOutcome]:
    """Compare the first and latest comparable periods; attribution stays overlap."""
    by_metric: dict[str, list[tuple[FinancialFact, str]]] = {}
    for fact in facts:
        if fact.ticker.upper() != tenure.ticker.upper():
            continue
        if fact.period_end > as_of_date:
            continue
        status = period_status(fact, tenure, as_of_date=as_of_date)
        if status in {"FULL_PERIOD", "PARTIAL_PERIOD"}:
            by_metric.setdefault(fact.metric, []).append((fact, status))
    outcomes: list[OperatingOutcome] = []
    for metric, candidates in by_metric.items():
        candidates.sort(key=lambda item: (item[0].period_end, item[0].source_fact.get("filed") or ""))
        full = [item for item in candidates if item[1] == "FULL_PERIOD"]
        eligible = full if len(full) >= 2 else candidates
        # Keep period definitions comparable. Prefer annual facts; otherwise
        # use the same fiscal period (for example, Q2 to Q2). Never silently
        # compare an annual fact with a quarter-to-date fact.
        annual = [item for item in eligible if (item[0].fiscal_period or "").upper() in {"FY", ""}]
        if len(annual) >= 2:
            selected = annual
        else:
            by_fp: dict[str, list[tuple[FinancialFact, str]]] = {}
            for item in eligible:
                by_fp.setdefault((item[0].fiscal_period or "").upper(), []).append(item)
            same_period = max(by_fp.values(), key=lambda group: (len(group), group[-1][0].period_end), default=[])
            selected = same_period if len(same_period) >= 2 else []
        if len(selected) < 2:
            continue
        start_fact, start_status = selected[0]
        end_fact, end_status = selected[-1]
        if not _comparable(start_fact, end_fact) or start_fact.period_end == end_fact.period_end:
            continue
        absolute = end_fact.value - start_fact.value
        percent = (absolute / abs(start_fact.value) * 100.0) if start_fact.value else None
        years = (date.fromisoformat(end_fact.period_end) - date.fromisoformat(start_fact.period_end)).days / 365.25
        cagr = None
        if years > 0 and start_fact.value > 0 and end_fact.value >= 0:
            cagr = ((end_fact.value / start_fact.value) ** (1.0 / years) - 1.0) * 100.0
        status = "FULL_PERIOD" if start_status == end_status == "FULL_PERIOD" else "PARTIAL_PERIOD"
        source_fact = {"start": start_fact.to_dict(), "end": end_fact.to_dict(), "comparability": {"same_metric": True, "same_unit": True, "same_accounting_basis": True}}
        outcome_id = "outcome:" + hashlib.sha256(f"{tenure.tenure_id}|{metric}|{start_fact.period_end}|{end_fact.period_end}".encode()).hexdigest()[:24]
        outcomes.append(OperatingOutcome(
            outcome_id=outcome_id,
            person_id=tenure.person_id,
            tenure_id=tenure.tenure_id,
            ticker=tenure.ticker,
            company_name=tenure.company_name,
            metric=metric,
            period_start=start_fact.period_end,
            period_end=end_fact.period_end,
            starting_value=start_fact.value,
            ending_value=end_fact.value,
            absolute_change=absolute,
            percent_change=percent,
            cagr=cagr,
            unit=end_fact.unit,
            accounting_basis=end_fact.accounting_basis,
            period_status=status,
            source_fact=source_fact,
            source_url=end_fact.source_url,
            source_accession=end_fact.accession_number,
            source_local_path=end_fact.source_local_path,
            attribution_level=Attribution.TENURE_OVERLAP,
            evidence_text=f"Comparable SEC Company Facts periods {start_fact.period_end} to {end_fact.period_end}; no person-level causation asserted.",
            created_at=str(end_fact.source_fact.get("filed") or end_fact.period_end),
            tenure_duration_days=(_boundary_date(tenure.end_date or as_of_date, end=True) - _boundary_date(tenure.start_date)).days if tenure.start_date and (tenure.end_date or as_of_date) else None,
            observation_duration_days=(date.fromisoformat(end_fact.period_end) - date.fromisoformat(start_fact.period_end)).days,
        ))
    return outcomes


__all__ = ["CompanyFactsProvider", "normalize_company_facts", "period_status", "build_operating_outcomes", "METRIC_TAGS"]
