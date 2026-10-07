"""Dated, current-only SEC and market evidence for model-qualified Long names.

This is an operational research check. It does not reconstruct an old Primary
vendor snapshot, establish predictive validity, or certify an executable quote.
"""
from __future__ import annotations

import json
import math
import hashlib
from datetime import datetime, time, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from src.sec_importer import (ANNUAL_FORMS, CONCEPTS, ImportSettings, _request_json,
                              _ticker_key, acceptance_map, annual_facts, companyfacts,
                              load_ticker_map, parse_sec_acceptance, revenue_concepts_for_issuer,
                              statement_currency,
                              submissions)


FLOW_METRICS = ("revenue", "net_income", "ocf", "capex")
QUARTER_FORMS = {"10-Q", "10-Q/A"}
EVIDENCE_COLUMNS = [
    "Category", "Ticker", "Company Name", "Secondary Score", "Quantitative Reason", "Selected in Summary", "Current Evidence Status",
    "Current Evidence Blockers", "Decision Captured At UTC", "SEC CIK",
    "Latest Filing Form", "Latest Filing Period End", "Latest Filing Accepted At UTC",
    "TTM Revenue", "TTM Net Income", "TTM Operating Cash Flow", "TTM Capex Outflow",
    "TTM Free Cash Flow", "TTM Source Currency", "TTM Provenance JSON",
    "Completed Close Date", "Completed Close", "Close Currency", "20D Average Dollar Volume",
    "Price Source", "Shares Outstanding", "Shares Period End", "Shares Accepted At UTC",
    "Estimated Current Market Cap", "Estimated TTM FCF Yield", "Share Basis Note",
    "Evidence Snapshot SHA256",
]


def _available(record: dict[str, Any], accepted: dict[str, str], decision_at: datetime) -> bool:
    timestamp = accepted.get(str(record.get("accn", "")), "")
    if not timestamp:
        return False
    parsed = pd.to_datetime(timestamp, errors="coerce", utc=True)
    return pd.notna(parsed) and parsed.to_pydatetime() <= decision_at


def _quarter_rows(payload: dict[str, Any], metric: str, unit: str, concept: str,
                  decision_at: datetime) -> list[dict[str, Any]]:
    rows = []
    for namespace, name in CONCEPTS[metric][2]:
        if name != concept:
            continue
        for fact in payload.get("facts", {}).get(namespace, {}).get(name, {}).get("units", {}).get(unit, []):
            if fact.get("form") not in QUARTER_FORMS or not fact.get("start") or not fact.get("end"):
                continue
            start, end = pd.to_datetime(fact["start"], errors="coerce"), pd.to_datetime(fact["end"], errors="coerce")
            filed = pd.to_datetime(fact.get("filed"), errors="coerce", utc=True)
            if pd.isna(start) or pd.isna(end) or pd.isna(filed) or filed.to_pydatetime() > decision_at:
                continue
            if 60 <= (end - start).days <= 320 and fact.get("val") is not None:
                rows.append({**fact, "namespace": namespace, "concept": concept, "unit": unit})
    return rows


def ttm_metric(payload: dict[str, Any], metric: str, currency: str,
               accepted: dict[str, str], decision_at: datetime,
               latest_report_end: str) -> tuple[float | None, str, list[dict[str, Any]]]:
    """Use annual plus matched current/prior YTD; fail on gaps or concept changes."""
    # Pick the annual value from information accepted by the decision time.
    # annual_facts by itself chooses a current-vintage comparative/restatement.
    available_facts = {}
    for namespace, concept in CONCEPTS[metric][2]:
        units = payload.get("facts", {}).get(namespace, {}).get(concept, {}).get("units", {})
        kept = {unit: [fact for fact in facts if fact.get("form") in ANNUAL_FORMS
                       and _available(fact, accepted, decision_at)]
                for unit, facts in units.items()}
        available_facts.setdefault(namespace, {})[concept] = {"units": kept}
    available_payload = {"facts": available_facts}
    preferred = revenue_concepts_for_issuer(available_payload, currency) if metric == "revenue" else None
    annual = annual_facts(available_payload, metric, currency, concepts_override=preferred)
    if annual.empty:
        return None, "ANNUAL_FACT_MISSING", []
    base = annual.iloc[-1].to_dict()
    base_end = pd.Timestamp(base["end"])
    if pd.Timestamp(latest_report_end) < base_end:
        return None, "REPORT_PERIOD_CONFLICT", [base]
    if pd.Timestamp(latest_report_end) == base_end:
        return float(base["value"]), "ANNUAL_AS_TTM", [base]
    quarter = _quarter_rows(payload, metric, str(base["unit"]), str(base["concept"]), decision_at)
    current = [f for f in quarter if pd.Timestamp(f["start"]) > base_end
               and pd.Timestamp(f["start"]) - base_end <= pd.Timedelta(days=7)
               and f["end"] == latest_report_end and _available(f, accepted, decision_at)]
    if not current:
        return None, "CURRENT_YTD_MISSING_OR_CONCEPT_CHANGED", [base]
    now = max(current, key=lambda f: (f.get("filed", ""), f.get("accn", "")))
    prior = [f for f in quarter
             if 330 <= (pd.Timestamp(now["start"]) - pd.Timestamp(f["start"])).days <= 400
             and 330 <= (pd.Timestamp(now["end"]) - pd.Timestamp(f["end"])).days <= 400
             and abs((pd.Timestamp(now["end"]) - pd.Timestamp(now["start"])).days
                     - (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).days) <= 14
             and _available(f, accepted, decision_at)]
    if not prior:
        return None, "PRIOR_YTD_MISSING", [base, now]
    before = max(prior, key=lambda f: (f.get("filed", ""), f.get("accn", "")))
    value = float(base["value"]) + float(now["val"]) - float(before["val"])
    return value, "ANNUAL_PLUS_YTD_MINUS_PRIOR_YTD", [base, now, before]


def _latest_report(submission: dict[str, Any], decision_at: datetime) -> dict[str, str] | None:
    recent = submission.get("filings", {}).get("recent", {})
    rows = [{key: values[i] for key, values in recent.items() if i < len(values)}
            for i in range(len(recent.get("accessionNumber", [])))]
    eligible = []
    for row in rows:
        if row.get("form") not in (ANNUAL_FORMS | QUARTER_FORMS) or not row.get("reportDate"):
            continue
        accepted_at = parse_sec_acceptance(row.get("acceptanceDateTime"))
        if accepted_at is not None and accepted_at <= decision_at:
            eligible.append(row)
    return max(eligible, key=lambda row: (row["reportDate"], row["acceptanceDateTime"])) if eligible else None


def _latest_shares(payload: dict[str, Any], accepted: dict[str, str], decision_at: datetime) -> dict[str, Any] | None:
    facts = payload.get("facts", {}).get("us-gaap", {}).get("CommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    eligible = [fact for fact in facts if fact.get("form") in (ANNUAL_FORMS | QUARTER_FORMS)
                and fact.get("end") and fact.get("val") and float(fact["val"]) > 0
                and pd.Timestamp(fact["end"]).date() <= decision_at.date()
                and _available(fact, accepted, decision_at)]
    return max(eligible, key=lambda fact: (fact["end"], fact.get("filed", ""))) if eligible else None


def completed_daily_market(ticker: str, session: requests.Session, decision_at: datetime,
                           timeout_seconds: int, lookback_days: int = 370) -> dict[str, Any]:
    """Exclude the current US session until after 16:30 New York time."""
    ny = decision_at.astimezone(ZoneInfo("America/New_York"))
    cutoff = ny.date() if ny.time() >= time(16, 30) else ny.date() - timedelta(days=1)
    # Keep enough corporate-action history to cover the permitted share-count age.
    start = cutoff - timedelta(days=lookback_days)
    params = {"period1": int(datetime.combine(start, time.min, tzinfo=timezone.utc).timestamp()),
              "period2": int(datetime.combine(cutoff + timedelta(days=2), time.min, tzinfo=timezone.utc).timestamp()),
              "interval": "1d", "events": "splits"}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{_ticker_key(ticker)}"
    payload = _request_json(session, url, {"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                            params, timeout_seconds)
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"No Yahoo chart result for {ticker}")
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    offset = int(result.get("meta", {}).get("gmtoffset") or 0)
    bars = []
    for stamp, close, volume in zip(result.get("timestamp") or [], quote.get("close") or [], quote.get("volume") or []):
        day = datetime.fromtimestamp(stamp + offset, timezone.utc).date()
        if day <= cutoff and close is not None and volume is not None and close > 0 and volume >= 0:
            bars.append((day, float(close), int(volume)))
    if not bars:
        raise ValueError(f"No completed daily price/volume bars for {ticker}")
    bars.sort()
    day, close, _ = bars[-1]
    splits = [datetime.fromtimestamp(int(stamp) + offset, timezone.utc).date().isoformat()
              for stamp in result.get("events", {}).get("splits", {})]
    return {"date": day.isoformat(), "close": close,
            "currency": str(result.get("meta", {}).get("currency") or "").upper(),
            "average_dollar_volume_20d": sum(p * v for _, p, v in bars[-20:]) / min(20, len(bars)),
            "completed_sessions": len(bars[-20:]), "bars_20d": bars[-20:], "splits": splits,
            "source": "Yahoo chart API daily close and volume"}


def current_evidence_row(ticker: str, cik: str, payload: dict[str, Any],
                         submission: dict[str, Any], market: dict[str, Any],
                         decision_at: datetime, controls: dict[str, Any], security_type: str = "") -> dict[str, Any]:
    accepted = acceptance_map(submission)
    report = _latest_report(submission, decision_at)
    blockers = []
    report_end = str(report.get("reportDate", "")) if report else ""
    if not report_end:
        blockers.append("LATEST_SEC_REPORT_UNAVAILABLE")
    currency = statement_currency(payload) or ""
    lineage = {}
    values = {}
    if report_end and currency:
        for metric in FLOW_METRICS:
            value, method, facts = ttm_metric(payload, metric, currency, accepted, decision_at, report_end)
            values[metric] = value
            lineage[metric] = {"method": method, "facts": [
                {key: fact.get(key, "") for key in ("namespace", "concept", "unit", "start", "end", "filed", "accn", "form")}
                | {"reported_value": fact.get("value", fact.get("val")),
                   "accepted_at_utc": accepted.get(str(fact.get("accn", "")), "")}
                for fact in facts]}
            if value is None:
                blockers.append(f"TTM_{metric.upper()}_{method}")
    else:
        blockers.append("STATEMENT_CURRENCY_UNAVAILABLE")
    shares = _latest_shares(payload, accepted, decision_at)
    if shares is None:
        blockers.append("CURRENT_SHARE_COUNT_UNVERIFIED")
    if "ADR" in security_type.upper():
        blockers.append("ADR_CONVERSION_UNVERIFIED")
    close_date = pd.Timestamp(market["date"]).date()
    if (decision_at.date() - close_date).days > int(controls["CURRENT_MAX_CLOSE_AGE_DAYS"]):
        blockers.append("CLOSE_STALE")
    if market["currency"] != currency:
        blockers.append("PRICE_STATEMENT_CURRENCY_MISMATCH")
    if market["completed_sessions"] < 15:
        blockers.append("INSUFFICIENT_COMPLETED_PRICE_SESSIONS")
    if market["average_dollar_volume_20d"] < float(controls["CURRENT_MIN_DOLLAR_VOLUME_20D"]):
        blockers.append("DOLLAR_LIQUIDITY_BELOW_FLOOR")
    if market["close"] < float(controls["CURRENT_MIN_PRICE"]):
        blockers.append("SHARE_PRICE_BELOW_FLOOR")
    if report_end and (decision_at.date() - pd.Timestamp(report_end).date()).days > int(controls["CURRENT_MAX_REPORT_AGE_DAYS"]):
        blockers.append("FINANCIAL_REPORT_STALE")
    if shares:
        share_date = pd.Timestamp(shares["end"]).date()
        if (decision_at.date() - share_date).days > int(controls["CURRENT_MAX_SHARE_AGE_DAYS"]):
            blockers.append("SHARE_COUNT_STALE")
        if any(day > share_date.isoformat() for day in market["splits"]):
            blockers.append("SPLIT_AFTER_SHARE_COUNT")
    cap = (float(shares["val"]) * market["close"] if shares and "ADR" not in security_type.upper()
           and market["currency"] == currency else None)
    fcf = (values["ocf"] - abs(values["capex"]) if values.get("ocf") is not None
           and values.get("capex") is not None else None)
    return {
        "Current Evidence Status": "CURRENT_REVIEW_EVIDENCE_COMPLETE" if not blockers else "REVIEW_REQUIRED",
        "Current Evidence Blockers": "; ".join(dict.fromkeys(blockers)),
        "Decision Captured At UTC": decision_at.isoformat(), "SEC CIK": cik,
        "Latest Filing Form": report.get("form", "") if report else "",
        "Latest Filing Period End": report_end,
        "Latest Filing Accepted At UTC": (parse_sec_acceptance(report.get("acceptanceDateTime")).isoformat()
                                           if report else ""),
        "TTM Revenue": values.get("revenue"), "TTM Net Income": values.get("net_income"),
        "TTM Operating Cash Flow": values.get("ocf"), "TTM Capex Outflow": abs(values["capex"]) if values.get("capex") is not None else None,
        "TTM Free Cash Flow": fcf, "TTM Source Currency": currency,
        "TTM Provenance JSON": json.dumps(lineage, sort_keys=True),
        "Completed Close Date": market["date"], "Completed Close": market["close"],
        "Close Currency": market["currency"], "20D Average Dollar Volume": market["average_dollar_volume_20d"],
        "Price Source": market["source"], "Shares Outstanding": shares.get("val") if shares else None,
        "Shares Period End": shares.get("end", "") if shares else "",
        "Shares Accepted At UTC": accepted.get(str(shares.get("accn", "")), "") if shares else "",
        "Estimated Current Market Cap": cap, "Estimated TTM FCF Yield": fcf / cap if fcf is not None and cap and cap > 0 else None,
        "Share Basis Note": "SEC latest reported common shares; review corporate actions and broker quote before trading",
    }


def build_current_evidence(candidates: pd.DataFrame, security_types: dict[str, str],
                           settings: ImportSettings, controls: dict[str, Any],
                           decision_at: datetime | None = None,
                           archive_dir: Path | None = None) -> pd.DataFrame:
    decision_at = decision_at or datetime.now(timezone.utc)
    if decision_at.tzinfo is None:
        raise ValueError("Current evidence decision time must include a timezone")
    if decision_at.date() != datetime.now(timezone.utc).date():
        raise ValueError("Current evidence can only be captured for today's UTC date")
    for key in ("CURRENT_MAX_CLOSE_AGE_DAYS", "CURRENT_MIN_DOLLAR_VOLUME_20D",
                "CURRENT_MIN_PRICE", "CURRENT_MAX_REPORT_AGE_DAYS", "CURRENT_MAX_SHARE_AGE_DAYS"):
        value = float(controls[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
    if candidates.empty:
        return pd.DataFrame(columns=EVIDENCE_COLUMNS)
    sec_session, price_session = requests.Session(), requests.Session()
    mapping = load_ticker_map(sec_session, settings)
    fresh_settings = replace(settings, cache_hours=0)
    evidence = {}
    for ticker in candidates["Ticker"].astype(str).str.upper().unique():
        cik = mapping.get(_ticker_key(ticker), "")
        try:
            if not cik:
                raise ValueError("SEC CIK mapping unavailable")
            payload = companyfacts(cik, sec_session, fresh_settings)
            filing = submissions(cik, sec_session, fresh_settings)
            market = completed_daily_market(ticker, price_session, decision_at, settings.timeout_seconds,
                                            max(370, int(controls["CURRENT_MAX_SHARE_AGE_DAYS"]) + 30))
            evidence[ticker] = current_evidence_row(ticker, cik, payload, filing, market, decision_at,
                                                    controls, security_types.get(ticker, ""))
            if archive_dir is not None:
                archive_dir.mkdir(parents=True, exist_ok=True)
                snapshot = json.dumps({"captured_at_utc": decision_at.isoformat(),
                                       "ticker": ticker, "cik": cik, "companyfacts": payload,
                                       "submissions": filing, "market": market},
                                      sort_keys=True, separators=(",", ":"), default=str)
                digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
                path = archive_dir / f"{ticker}_{decision_at.strftime('%Y%m%dT%H%M%SZ')}_{digest[:12]}.json"
                path.write_text(snapshot, encoding="utf-8")
                evidence[ticker]["Evidence Snapshot SHA256"] = digest
        except Exception as exc:
            evidence[ticker] = {"Current Evidence Status": "REVIEW_REQUIRED",
                                "Current Evidence Blockers": f"FETCH_OR_VALIDATION_ERROR: {type(exc).__name__}: {exc}",
                                "Decision Captured At UTC": decision_at.isoformat(), "SEC CIK": cik}
    rows = []
    for _, row in candidates.iterrows():
        values = {"Category": row["Category"], "Ticker": row["Ticker"],
                  "Company Name": row.get("Company Name", ""), "Secondary Score": row["Secondary Score"],
                  "Quantitative Reason": row.get("Quantitative Reason", ""),
                  "Selected in Summary": row.get("Selected in Summary", "NO")}
        values.update(evidence[str(row["Ticker"]).upper()])
        current_fcf = values.get("TTM Free Cash Flow")
        current_income = values.get("TTM Net Income")
        category_blockers = []
        if row["Category"] in {"Safe", "High Growth Potential"} and current_fcf is not None and current_fcf <= 0:
            category_blockers.append("CURRENT_TTM_FREE_CASH_FLOW_NOT_POSITIVE")
        if row["Category"] == "Safe" and current_income is not None and current_income <= 0:
            category_blockers.append("CURRENT_TTM_NET_INCOME_NOT_POSITIVE")
        if category_blockers:
            values["Current Evidence Blockers"] = "; ".join(filter(None,
                (values.get("Current Evidence Blockers", ""), *category_blockers)))
            values["Current Evidence Status"] = "REVIEW_REQUIRED"
        rows.append(values)
    return pd.DataFrame(rows, columns=EVIDENCE_COLUMNS)
