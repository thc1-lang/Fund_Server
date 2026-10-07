"""Portable SEC CompanyFacts and fiscal-end-price importer for this screener.

The module writes annual source history only when explicitly requested through
``main.py --import-sec-data``.  It does not claim that current CompanyFacts
responses reproduce the information available on historical selection dates.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import config
from src.io import client, parse_primary_short_candidates


ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
DEFAULT_SEC_USER_AGENT = "Theo Cooper t.h.c.1@icloud.com"
MONEY_UNITS = ("USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "HKD")
OUTPUT_COLUMNS = [
    "Ticker", "Fiscal Year", "Fiscal Period End", "Revenue", "Net Income", "EBIT", "EBITDA",
    "Interest Expense", "Interest Coverage Ratio", "Current Assets", "Current Liabilities",
    "Total Assets", "Shareholders Equity", "Operating Cash Flow", "Free Cash Flow",
    "Year End Price", "Market Cap", "Cash", "Total Debt", "Enterprise Value",
    "Free Cash Flow Yield", "Current Ratio", "Quick Ratio", "Working Capital to Total Assets Ratio",
    "Sales to Assets Ratio", "Return on Assets Ratio", "Return on Equity Ratio", "EV / EBIT",
    "EV / EBITDA", "Gross Margin", "Net Margin", "Debt to Equity Ratio",
    "Price / Free Cash Flow Per Share", "Pre-Tax Profit Ratio", "Price Date", "Price Source",
    "Price Currency", "Shares Outstanding", "Market Cap Currency", "Revenue Source Concept", "Revenue Filed", "CIK", "Accession",
    "Form", "Accepted At", "Statement Currency", "ADR Ratio", "Fact Provenance JSON", "Audit Notes",
    "Missing Data Notes", "Data Source", "Error",
]

# Concept order is policy.  The importer keeps the selected concept and filing
# date in the row so an analyst can audit a fallback rather than infer it.
CONCEPTS: dict[str, tuple[bool, str, tuple[tuple[str, str], ...]]] = {
    "revenue": (True, "money", (("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"), ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"), ("us-gaap", "Revenues"), ("us-gaap", "SalesRevenueNet"), ("us-gaap", "RegulatedAndUnregulatedOperatingRevenue"), ("ifrs-full", "Revenue"))),
    "net_income": (True, "money", (("us-gaap", "NetIncomeLoss"), ("ifrs-full", "ProfitLoss"))),
    "ebit": (True, "money", (("us-gaap", "OperatingIncomeLoss"), ("ifrs-full", "OperatingProfitLoss"))),
    "da": (True, "money", (("us-gaap", "DepreciationDepletionAndAmortization"), ("us-gaap", "DepreciationAndAmortization"), ("ifrs-full", "DepreciationAndAmortisationExpense"))),
    "interest": (True, "money", (("us-gaap", "InterestExpenseNonOperating"), ("us-gaap", "InterestExpense"), ("ifrs-full", "FinanceCosts"))),
    "current_assets": (False, "money", (("us-gaap", "AssetsCurrent"), ("ifrs-full", "CurrentAssets"))),
    "current_liabilities": (False, "money", (("us-gaap", "LiabilitiesCurrent"), ("ifrs-full", "CurrentLiabilities"))),
    "assets": (False, "money", (("us-gaap", "Assets"), ("ifrs-full", "Assets"))),
    "equity": (False, "money", (("us-gaap", "StockholdersEquity"), ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), ("ifrs-full", "Equity"))),
    "ocf": (True, "money", (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"), ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"))),
    "capex": (True, "money", (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"), ("us-gaap", "PaymentsToAcquireProductiveAssets"), ("ifrs-full", "PaymentsToAcquirePropertyPlantAndEquipmentClassifiedAsInvestingActivities"))),
    "cash": (False, "money", (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"), ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), ("ifrs-full", "CashAndCashEquivalents"))),
    "debt": (False, "money", (("us-gaap", "LongTermDebtAndFinanceLeaseObligations"), ("us-gaap", "LongTermDebt"), ("ifrs-full", "Borrowings"))),
    # Only an instant share count at the fiscal end can support a fiscal-end
    # market cap. DEI filing-date shares and weighted-average shares cannot.
    "shares": (False, "shares", (("us-gaap", "CommonStockSharesOutstanding"), ("dei", "EntityCommonStockSharesOutstanding"))),
    "gross_profit": (True, "money", (("us-gaap", "GrossProfit"), ("ifrs-full", "GrossProfit"))),
    "cost_of_revenue": (True, "money", (("us-gaap", "CostOfRevenue"), ("us-gaap", "CostOfGoodsAndServicesSold"), ("ifrs-full", "CostOfSales"))),
    "pretax": (True, "money", (("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"), ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"), ("ifrs-full", "ProfitLossBeforeTax"))),
}


@dataclass(frozen=True)
class ImportSettings:
    user_agent: str
    cache_dir: Path
    cache_hours: float = 12.0
    timeout_seconds: int = 30
    sec_pause_seconds: float = 0.15
    years: int = 4


def default_settings(user_agent: str | None = None, cache_dir: str | Path | None = None) -> ImportSettings:
    agent = user_agent or os.getenv("SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT
    if not agent or "@example.com" in agent.lower():
        raise ValueError("Set SEC_USER_AGENT to an organisation and monitored contact email before importing.")
    root = Path(cache_dir or os.getenv("SEC_COMPANYFACTS_CACHE", "sec_companyfacts_cache"))
    return ImportSettings(user_agent=agent, cache_dir=root)


def _request_json(session: requests.Session, url: str, headers: dict[str, str], params: dict[str, Any] | None, timeout: int, attempts: int = 4) -> dict[str, Any]:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(url, headers=headers, params=params, timeout=timeout)
            if response.status_code in {403, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Request failed after {attempts} attempts: {url}: {error}")


def _sec_headers(settings: ImportSettings) -> dict[str, str]:
    return {"User-Agent": settings.user_agent, "Accept-Encoding": "gzip, deflate"}


def _ticker_key(ticker: str) -> str:
    return str(ticker).strip().upper().replace(".", "-")


def load_ticker_map(session: requests.Session, settings: ImportSettings) -> dict[str, str]:
    payload = _request_json(session, "https://www.sec.gov/files/company_tickers.json", _sec_headers(settings), None, settings.timeout_seconds)
    return {_ticker_key(item["ticker"]): str(item["cik_str"]).zfill(10) for item in payload.values()}


def companyfacts(cik: str, session: requests.Session, settings: ImportSettings) -> dict[str, Any]:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    path = settings.cache_dir / f"CIK{str(cik).zfill(10)}.json"
    if path.exists() and time.time() - path.stat().st_mtime <= settings.cache_hours * 3600:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    payload = _request_json(session, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json", _sec_headers(settings), None, settings.timeout_seconds)
    path.write_text(json.dumps(payload), encoding="utf-8")
    time.sleep(settings.sec_pause_seconds)
    return payload


def submissions(cik: str, session: requests.Session, settings: ImportSettings) -> dict[str, Any]:
    """Fetch the SEC filing index, retaining a bounded local cache."""
    root = settings.cache_dir / "submissions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"CIK{str(cik).zfill(10)}.json"
    if path.exists() and time.time() - path.stat().st_mtime <= settings.cache_hours * 3600:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    payload = _request_json(session, f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json",
                            _sec_headers(settings), None, settings.timeout_seconds)
    path.write_text(json.dumps(payload), encoding="utf-8")
    time.sleep(settings.sec_pause_seconds)
    return payload


def parse_sec_acceptance(value: Any) -> datetime | None:
    """SEC submission timestamps without an offset are Eastern local time."""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(ZoneInfo("America/New_York"))
    return parsed.tz_convert(timezone.utc).to_pydatetime()


def acceptance_map(submission: dict[str, Any]) -> dict[str, str]:
    recent = submission.get("filings", {}).get("recent", {})
    return {str(accn): accepted_at.isoformat() for accn, raw in
            zip(recent.get("accessionNumber", []), recent.get("acceptanceDateTime", []))
            if accn and (accepted_at := parse_sec_acceptance(raw)) is not None}


def _unit(units: dict[str, Any], kind: str, currency: str | None = None) -> str | None:
    preferred = ("shares",) if kind == "shares" else ((currency,) if currency else MONEY_UNITS)
    return next((unit for unit in preferred if unit in units), None)


def statement_currency(payload: dict[str, Any]) -> str | None:
    """Choose one currency with the broadest annual statement coverage."""
    counts = {unit: 0 for unit in MONEY_UNITS}
    facts = payload.get("facts", {})
    for _, kind, concepts in CONCEPTS.values():
        if kind != "money":
            continue
        for namespace, concept in concepts:
            units = facts.get(namespace, {}).get(concept, {}).get("units", {})
            for unit in counts:
                if any(f.get("form") in ANNUAL_FORMS and f.get("val") is not None for f in units.get(unit, [])):
                    counts[unit] += 1
    return max(MONEY_UNITS, key=lambda unit: counts[unit]) if any(counts.values()) else None


def annual_facts(payload: dict[str, Any], metric: str, currency: str | None = None,
                 concepts_override: tuple[tuple[str, str], ...] | None = None) -> pd.DataFrame:
    duration, kind, default_concepts = CONCEPTS[metric]
    concepts = concepts_override or default_concepts
    records: list[dict[str, Any]] = []
    facts = payload.get("facts", {})
    for priority, (namespace, concept) in enumerate(concepts):
        item = facts.get(namespace, {}).get(concept, {})
        unit = _unit(item.get("units", {}), kind, currency)
        if not unit:
            continue
        for fact in item.get("units", {}).get(unit, []):
            if fact.get("form") not in ANNUAL_FORMS or fact.get("fy") is None or fact.get("val") is None:
                continue
            start, end = pd.to_datetime(fact.get("start"), errors="coerce"), pd.to_datetime(fact.get("end"), errors="coerce")
            elapsed = (end - start).days if pd.notna(start) and pd.notna(end) else None
            records.append({"fy": int(fact["fy"]), "value": float(fact["val"]), "start": fact.get("start", ""), "end": fact.get("end", ""), "filed": fact.get("filed", ""), "accn": fact.get("accn", ""), "form": fact.get("form", ""), "unit": unit, "concept": concept, "namespace": namespace, "priority": priority, "annual": bool(elapsed is not None and 250 <= elapsed <= 450), "is_fy": fact.get("fp") == "FY"})
    if not records:
        return pd.DataFrame(columns=["fy", "value", "start", "end", "filed", "accn", "form", "unit", "concept", "namespace"])
    frame = pd.DataFrame(records)
    if duration:
        # A 10-K can contain quarterly or comparative facts. A fiscal-year
        # label and annual form alone do not make every contained fact annual.
        frame = frame.loc[frame["annual"]]
    frame = frame.loc[frame["end"].astype(str).ne("")].copy()
    chosen = []
    for _, group in frame.groupby("end", sort=True):
        # SEC fy belongs to the *filing*, so a later filing's comparative
        # value can carry the next fiscal-year label. Retain the first annual
        # filing's label for this period, while using the preferred concept's
        # latest reported value for a current-vintage research snapshot.
        first = group.sort_values(["filed", "priority"], ascending=[True, True]).iloc[0]
        selected = group.sort_values(["priority", "is_fy", "filed"], ascending=[True, False, False]).iloc[0].copy()
        selected["fy"] = int(first["fy"])
        chosen.append(selected)
    if not chosen:
        return pd.DataFrame(columns=["fy", "value", "start", "end", "filed", "accn", "form", "unit", "concept", "namespace"])
    return pd.DataFrame(chosen)[["fy", "value", "start", "end", "filed", "accn", "form", "unit", "concept", "namespace"]].sort_values("end").reset_index(drop=True)


def revenue_concepts_for_issuer(payload: dict[str, Any], currency: str | None) -> tuple[tuple[str, str], ...]:
    """Prefer consolidated Revenues when a higher-priority tag is a small component.

    Compare annual facts from the same filing and period, so an unrelated
    restatement cannot trigger a concept switch.
    """
    concepts = CONCEPTS["revenue"][2]
    broad = ("us-gaap", "Revenues")
    narrow = annual_facts(payload, "revenue", currency,
                          concepts_override=tuple(item for item in concepts if item != broad))
    total = annual_facts(payload, "revenue", currency, concepts_override=(broad,))
    if narrow.empty or total.empty:
        return concepts
    matched = narrow.merge(total, on=["end", "accn"], suffixes=("_narrow", "_total"))
    matched = matched.loc[matched["accn"].astype(str).str.strip().ne("")]
    if matched.empty:
        return concepts
    latest = matched.sort_values("end").iloc[-1]
    if float(latest["value_narrow"]) > 0 and float(latest["value_total"]) >= 2 * float(latest["value_narrow"]):
        return (broad,) + tuple(item for item in concepts if item != broad)
    return concepts


def _fact(facts: dict[str, pd.DataFrame], metric: str, fiscal_end: str) -> dict[str, Any] | None:
    data = facts[metric]
    rows = data.loc[data["end"].eq(fiscal_end)]
    return rows.iloc[0].to_dict() if not rows.empty else None


def _same_unit(*records: dict[str, Any] | None) -> bool:
    units = [str(r.get("unit", "")).upper() for r in records if r is not None]
    return bool(units) and len(units) == len(records) and len(set(units)) == 1


def _divide(numerator: float | None, denominator: float | None) -> float | None:
    return None if numerator is None or denominator in {None, 0} else numerator / denominator


def fiscal_end_price(ticker: str, fiscal_end: str, session: requests.Session, settings: ImportSettings) -> dict[str, Any]:
    empty = {"price": None, "date": "", "currency": "", "source": "Yahoo chart API", "note": ""}
    target = pd.to_datetime(fiscal_end, errors="coerce")
    if pd.isna(target) or target.date() > date.today():
        return {**empty, "note": "missing, invalid, or future fiscal period end"}
    target_date = target.date()
    start = target_date - timedelta(days=21)
    params = {"period1": int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()), "period2": int(datetime.combine(target_date + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc).timestamp()), "interval": "1d", "events": "history", "includeAdjustedClose": "true"}
    symbol = quote(_ticker_key(ticker), safe="")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    errors = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            payload = _request_json(session, f"https://{host}/v8/finance/chart/{symbol}", headers, params, settings.timeout_seconds, attempts=2)
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not result:
                raise RuntimeError(str(payload.get("chart", {}).get("error") or "no chart result"))
            closes = ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or [])
            offset = int((result.get("meta") or {}).get("gmtoffset") or 0)
            valid = [(datetime.fromtimestamp(stamp + offset, tz=timezone.utc).date(), close) for stamp, close in zip(result.get("timestamp") or [], closes) if close is not None and datetime.fromtimestamp(stamp + offset, tz=timezone.utc).date() <= target_date]
            if not valid:
                raise RuntimeError("no unadjusted close on or before fiscal end")
            chosen_date, chosen_price = max(valid)
            return {"price": float(chosen_price), "date": chosen_date.isoformat(), "currency": str((result.get("meta") or {}).get("currency") or "").upper(), "source": "Yahoo chart API unadjusted close", "note": "previous trading close" if chosen_date != target_date else ""}
        except Exception as exc:  # retries are exhausted inside _request_json
            errors.append(str(exc))
    return {**empty, "note": " | ".join(errors)}


def fiscal_end_prices(ticker: str, fiscal_ends: list[str], session: requests.Session,
                      settings: ImportSettings) -> dict[str, dict[str, Any]]:
    """Resolve all fiscal-end closes from one bounded chart window per ticker."""
    empty = {"price": None, "date": "", "currency": "", "source": "Yahoo chart API", "note": ""}
    targets = {end: pd.to_datetime(end, errors="coerce") for end in fiscal_ends}
    valid = {end: stamp.date() for end, stamp in targets.items()
             if pd.notna(stamp) and stamp.date() <= date.today()}
    output = {end: {**empty, "note": "missing, invalid, or future fiscal period end"}
              for end in fiscal_ends if end not in valid}
    if not valid:
        return output
    start = min(valid.values()) - timedelta(days=21)
    finish = max(valid.values()) + timedelta(days=2)
    params = {"period1": int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
              "period2": int(datetime.combine(finish, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
              "interval": "1d", "events": "history", "includeAdjustedClose": "true"}
    symbol = quote(_ticker_key(ticker), safe="")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    errors = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            payload = _request_json(session, f"https://{host}/v8/finance/chart/{symbol}",
                                    headers, params, min(settings.timeout_seconds, 10), attempts=2)
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not result:
                raise RuntimeError(str(payload.get("chart", {}).get("error") or "no chart result"))
            closes = ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or [])
            offset = int((result.get("meta") or {}).get("gmtoffset") or 0)
            bars = sorted((datetime.fromtimestamp(stamp + offset, tz=timezone.utc).date(), float(close))
                          for stamp, close in zip(result.get("timestamp") or [], closes) if close is not None)
            if not bars:
                raise RuntimeError("no chart closes in requested window")
            currency = str((result.get("meta") or {}).get("currency") or "").upper()
            for end, target in valid.items():
                matches = [(day, close) for day, close in bars
                           if target - timedelta(days=21) <= day <= target and close > 0]
                if matches:
                    chosen_day, chosen_close = max(matches)
                    output[end] = {"price": chosen_close, "date": chosen_day.isoformat(),
                                   "currency": currency, "source": "Yahoo chart API unadjusted close",
                                   "note": "previous trading close" if chosen_day != target else ""}
                else:
                    output[end] = {**empty, "note": "no unadjusted close on or before fiscal end"}
            return output
        except Exception as exc:
            errors.append(str(exc))
    for end in valid:
        output[end] = {**empty, "note": " | ".join(errors)}
    return output


def company_rows(ticker: str, payload: dict[str, Any], price_session: requests.Session,
                 settings: ImportSettings, accepted: dict[str, str] | None = None,
                 use_batch_prices: bool = False) -> list[dict[str, Any]]:
    accepted = accepted or {}
    currency = statement_currency(payload)
    revenue_concepts = revenue_concepts_for_issuer(payload, currency)
    facts = {metric: annual_facts(payload, metric, currency,
                                  concepts_override=revenue_concepts if metric == "revenue" else None)
             for metric in CONCEPTS}
    periods = sorted({str(end) for metric in ("revenue", "net_income", "ocf", "ebit")
                      for end in facts[metric].get("end", pd.Series(dtype=str)).tolist()})[-settings.years:]
    if not periods:
        return [error_row(ticker, "No SEC annual facts found in supported concepts")]
    prices = fiscal_end_prices(ticker, periods, price_session, settings) if use_batch_prices else {}
    anchor_years = []
    for fiscal_end in periods:
        anchor = next((_fact(facts, name, fiscal_end) for name in ("revenue", "net_income", "ocf", "ebit")
                       if _fact(facts, name, fiscal_end)), None)
        anchor_years.append(int(anchor["fy"]))
    # Comparative SEC facts sometimes inherit a later filing's FY label.
    # Reconcile labels to the newest fiscal year and the actual period gaps.
    years = [anchor_years[-1]]
    for newer, older in zip(reversed(periods[1:]), reversed(periods[:-1])):
        gap_days = (pd.Timestamp(newer) - pd.Timestamp(older)).days
        years.append(years[-1] - max(1, round(gap_days / 365.25)))
    years.reverse()
    rows = []
    for fiscal_end, year, source_fy in zip(periods, years, anchor_years):
        selected = {name: _fact(facts, name, fiscal_end) for name in CONCEPTS}
        rev, ni, ebit, da, interest = (selected[name] for name in ("revenue", "net_income", "ebit", "da", "interest"))
        ca, cl, assets, equity = (selected[name] for name in ("current_assets", "current_liabilities", "assets", "equity"))
        ocf, capex, cash, debt, shares = (selected[name] for name in ("ocf", "capex", "cash", "debt", "shares"))
        gp, cor, pretax = (selected[name] for name in ("gross_profit", "cost_of_revenue", "pretax"))
        price = prices[fiscal_end] if use_batch_prices else fiscal_end_price(ticker, fiscal_end, price_session, settings)
        capex_value = abs(capex["value"]) if capex else None
        fcf = ocf["value"] - capex_value if _same_unit(ocf, capex) else None
        ebitda = ebit["value"] + da["value"] if _same_unit(ebit, da) else None
        gross_profit = gp["value"] if gp else (rev["value"] - abs(cor["value"]) if _same_unit(rev, cor) else None)
        foreign_annual = any(record and str(record.get("form", "")).startswith(("20-F", "40-F"))
                             for record in (rev, ni, ocf))
        market_cap = price["price"] * shares["value"] if price["price"] is not None and shares and not foreign_annual else None
        ev = market_cap + debt["value"] - cash["value"] if market_cap is not None and _same_unit(cash, debt) and str(cash["unit"]).upper() == price["currency"] else None
        def unit_ratio(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
            return _divide(a["value"], b["value"]) if _same_unit(a, b) else None
        audit = []
        if price["note"]:
            audit.append(f"price: {price['note']}")
        if capex and capex["value"] < 0:
            audit.append("negative SEC capex sign normalised to positive outflow")
        if not shares:
            audit.append("no shares outstanding fact at fiscal period end; historical market cap unavailable")
        if foreign_annual and shares:
            audit.append("ADR/share conversion unverified; historical market cap unavailable")
        if source_fy != year:
            audit.append(f"SEC comparative FY {source_fy} relabelled to period-sequence FY {year}")
        statement_units = {str(record["unit"]).upper() for name, record in selected.items()
                           if record and name != "shares"}
        provenance = {name: {key: record.get(key, "") for key in ("namespace", "concept", "unit", "start", "end", "filed", "accn", "form")}
                      | {"accepted_at_utc": accepted.get(str(record.get("accn", "")), "")}
                      for name, record in selected.items() if record}
        def value(name: str) -> float | None:
            record = selected[name]
            return float(record["value"]) if record else None
        values = {
            "Ticker": ticker, "Fiscal Year": year, "Fiscal Period End": fiscal_end,
            "Revenue": value("revenue"), "Net Income": value("net_income"), "EBIT": value("ebit"), "EBITDA": ebitda,
            "Interest Expense": value("interest"), "Interest Coverage Ratio": _divide(ebit["value"], abs(interest["value"])) if _same_unit(ebit, interest) else None,
            "Current Assets": value("current_assets"), "Current Liabilities": value("current_liabilities"), "Total Assets": value("assets"), "Shareholders Equity": value("equity"),
            "Operating Cash Flow": value("ocf"), "Free Cash Flow": fcf, "Year End Price": price["price"], "Market Cap": market_cap, "Cash": value("cash"), "Total Debt": value("debt"), "Enterprise Value": ev,
            "Free Cash Flow Yield": _divide(fcf, market_cap) if fcf is not None and market_cap is not None and str(ocf["unit"]).upper() == price["currency"] else None,
            "Current Ratio": unit_ratio(ca, cl), "Quick Ratio": None, "Working Capital to Total Assets Ratio": _divide(ca["value"] - cl["value"], assets["value"]) if _same_unit(ca, cl, assets) else None,
            "Sales to Assets Ratio": unit_ratio(rev, assets), "Return on Assets Ratio": unit_ratio(ni, assets), "Return on Equity Ratio": unit_ratio(ni, equity),
            "EV / EBIT": _divide(ev, ebit["value"]) if ev is not None and ebit and str(ebit["unit"]).upper() == price["currency"] else None,
            "EV / EBITDA": _divide(ev, ebitda) if ev is not None and ebitda is not None and ebit and str(ebit["unit"]).upper() == price["currency"] else None,
            "Gross Margin": _divide(gross_profit, rev["value"]) if gross_profit is not None and rev else None, "Net Margin": unit_ratio(ni, rev), "Debt to Equity Ratio": unit_ratio(debt, equity),
            "Price / Free Cash Flow Per Share": _divide(price["price"], _divide(fcf, shares["value"])) if price["price"] is not None and fcf is not None and shares and str(ocf["unit"]).upper() == price["currency"] else None,
            "Pre-Tax Profit Ratio": unit_ratio(pretax, rev), "Price Date": price["date"], "Price Source": price["source"], "Price Currency": price["currency"],
            "Shares Outstanding": value("shares"), "Market Cap Currency": price["currency"] if market_cap is not None else "",
            "Revenue Source Concept": rev["concept"] if rev else "", "Revenue Filed": rev["filed"] if rev else "",
            "CIK": str(payload.get("cik", "")).zfill(10) if payload.get("cik") else "",
            "Accession": rev.get("accn", "") if rev else "", "Form": rev.get("form", "") if rev else "",
            "Accepted At": accepted.get(str(rev.get("accn", "")), "") if rev else "",
            "Statement Currency": next(iter(statement_units)) if len(statement_units) == 1 else "",
            "ADR Ratio": "", "Fact Provenance JSON": json.dumps(provenance, sort_keys=True),
            "Audit Notes": "; ".join(audit),
            "Data Source": "SEC CompanyFacts + Yahoo chart HTTP price", "Error": "MIXED_STATEMENT_CURRENCIES" if len(statement_units) > 1 else "",
        }
        values["Missing Data Notes"] = "; ".join(name for name, value in values.items() if name in OUTPUT_COLUMNS and value is None)
        rows.append({column: values.get(column, "") for column in OUTPUT_COLUMNS})
    by_period: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["Fiscal Period End"]:
            by_period.setdefault(row["Fiscal Period End"], []).append(row)
    for same_period in by_period.values():
        if len({row["Fiscal Year"] for row in same_period}) > 1:
            for row in same_period:
                row["Error"] = "; ".join(filter(None, (row["Error"], "DUPLICATE_PERIOD_END_ACROSS_FISCAL_YEARS")))
    by_year: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_year.setdefault(int(row["Fiscal Year"]), []).append(row)
    for same_year in by_year.values():
        if len({row["Fiscal Period End"] for row in same_year}) > 1:
            for row in same_year:
                row["Error"] = "; ".join(filter(None, (row["Error"], "FISCAL_YEAR_PERIOD_COLLISION")))
    return rows


def error_row(ticker: str, error: str) -> dict[str, Any]:
    row = {column: "" for column in OUTPUT_COLUMNS}
    row.update({"Ticker": ticker, "Data Source": "SEC CompanyFacts + Yahoo chart HTTP price", "Error": error})
    return row


def import_tickers(tickers: list[str], settings: ImportSettings, sec_session: requests.Session | None = None, price_session: requests.Session | None = None) -> pd.DataFrame:
    sec_session, price_session = sec_session or requests.Session(), price_session or requests.Session()
    ticker_to_cik = load_ticker_map(sec_session, settings)
    output, seen = [], {}
    for ticker in dict.fromkeys(_ticker_key(t) for t in tickers if str(t).strip()):
        cik = ticker_to_cik.get(ticker)
        if not cik:
            output.append(error_row(ticker, "No SEC CIK mapping found"))
            continue
        try:
            if cik not in seen:
                facts = companyfacts(cik, sec_session, settings)
                accepted = acceptance_map(submissions(cik, sec_session, settings))
                seen[cik] = company_rows(ticker, facts, price_session, settings, accepted,
                                         use_batch_prices=True)
            output.extend({**row, "Ticker": ticker} for row in seen[cik])
        except Exception as exc:
            output.append(error_row(ticker, f"{type(exc).__name__}: {exc}"))
    return pd.DataFrame(output, columns=OUTPUT_COLUMNS)


def _table_values(worksheet: Any) -> tuple[list[str], list[list[str]]]:
    values = worksheet.get_all_values()
    for index, row in enumerate(values[:5]):
        if "Ticker" in row:
            width = len(row)
            return row, [(item + [""] * width)[:width] for item in values[index + 1:] if any(str(v).strip() for v in item)]
    raise ValueError(f"{worksheet.title} does not contain a Ticker header in its first five rows")


def _summary_tickers(worksheet: Any) -> list[str]:
    headers, rows = _table_values(worksheet)
    return list(dict.fromkeys(_ticker_key(row[headers.index("Ticker")]) for row in rows if row[headers.index("Ticker")].strip() and row[headers.index("Ticker")].strip().upper() not in {"NO DATA", "NONE"}))


def _write_frame(worksheet: Any, frame: pd.DataFrame) -> None:
    rows, cols = max(len(frame) + 50, 1000), max(len(frame.columns) + 5, 80)
    if worksheet.row_count < rows or worksheet.col_count < cols:
        worksheet.resize(rows=max(rows, worksheet.row_count), cols=max(cols, worksheet.col_count))
    worksheet.clear()
    values = [frame.columns.tolist()] + frame.where(pd.notna(frame), "").values.tolist()
    worksheet.update(range_name="A1", values=values, value_input_option="RAW")
    from gspread.utils import rowcol_to_a1
    readback = worksheet.get(f"A1:{rowcol_to_a1(len(values), len(values[0]))}", value_render_option="UNFORMATTED_VALUE")
    width = len(values[0])
    observed = [(row + [""] * width)[:width] for row in readback]
    if len(observed) != len(values) or any(
        a != b for expected_row, actual_row in zip(values, observed)
        for a, b in zip(expected_row, actual_row)
    ):
        raise RuntimeError(f"Source-tab read-back mismatch in {worksheet.title}")


def import_to_workbook(credentials_file: str | None, settings: ImportSettings,
                       spreadsheet_id: str = config.SPREADSHEET_ID,
                       backup_dir: str | Path | None = None) -> dict[str, Any]:
    """Refresh three Long source tabs and the Short source tab from current APIs."""
    if spreadsheet_id != config.SPREADSHEET_ID:
        raise ValueError(f"Refusing unexpected spreadsheet ID {spreadsheet_id}")
    gc = client(credentials_file)
    book = gc.open_by_key(spreadsheet_id)
    if book.title != config.EXPECTED_WORKBOOK_TITLE:
        raise ValueError(f"Workbook title mismatch: {book.title!r}")
    primary = gc.open_by_key(config.PRIMARY_SHORT_SPREADSHEET_ID)
    if primary.title != config.PRIMARY_SHORT_WORKBOOK_TITLE:
        raise ValueError(f"Primary source workbook title mismatch: {primary.title!r}")
    sheets = {sheet.title: sheet for sheet in book.worksheets()}
    long_targets = {
        category: _summary_tickers(primary.worksheet(cfg["primary_summary_sheet"]))
        for category, cfg in config.CATEGORIES.items()
    }
    short_candidates = parse_primary_short_candidates(primary.worksheet(config.PRIMARY_SHORT_TAB).get_all_values())
    short_tickers = short_candidates["Ticker"].tolist()
    combined = list(dict.fromkeys([ticker for tickers in long_targets.values() for ticker in tickers] + short_tickers))
    all_rows = import_tickers(combined, settings)
    valid = all_rows.loc[pd.to_numeric(all_rows["Fiscal Year"], errors="coerce").notna()]
    covered = int(valid.groupby("Ticker")["Fiscal Period End"].nunique().ge(4).sum())
    if covered < 0.75 * len(combined):
        raise RuntimeError(f"SEC import coverage too low: {covered}/{len(combined)} tickers have four distinct annual periods; source tabs unchanged")
    if valid.duplicated(["Ticker", "Fiscal Period End"]).any():
        raise RuntimeError("SEC import has duplicate ticker/period rows; source tabs unchanged")
    summary: dict[str, Any] = {"imported_tickers": len(combined), "four_period_tickers": covered, "tabs": {}}
    targets = []
    for category, tickers in long_targets.items():
        frame = all_rows.loc[all_rows["Ticker"].isin(tickers)].copy()
        title = config.CATEGORIES[category]["data_sheet"]
        targets.append((title, frame))
        summary["tabs"][title] = {"tickers": len(tickers), "rows": len(frame)}
    short_frame = all_rows.loc[all_rows["Ticker"].isin(short_tickers)].copy()
    targets.append(("Short Data", short_frame))
    summary["tabs"]["Short Data"] = {"tickers": len(short_tickers), "rows": len(short_frame)}
    snapshots = {title: sheets[title].get_all_values(value_render_option="UNFORMATTED_VALUE")
                 for title, _ in targets}
    if backup_dir is not None:
        path = Path(backup_dir)
        path.mkdir(parents=True, exist_ok=True)
        for title, before in snapshots.items():
            (path / f"{title.replace(' ', '_')}_before.json").write_text(json.dumps(before), encoding="utf-8")
        all_rows.to_csv(path / "sec_import_staged.csv", index=False)
    modified = []
    try:
        for title, frame in targets:
            modified.append(title)
            _write_frame(sheets[title], frame)
    except Exception:
        for title in reversed(modified):
            before = snapshots[title]
            sheets[title].clear()
            if before:
                sheets[title].update(range_name="A1", values=before, value_input_option="RAW")
        raise
    return summary
