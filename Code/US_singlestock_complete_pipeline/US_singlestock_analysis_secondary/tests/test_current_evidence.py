from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from src import current_evidence as evidence
from src.sec_importer import default_settings
import config


def _fact(start, end, value, form, accession, filed, fiscal_year, period):
    return {"start": start, "end": end, "val": value, "form": form,
            "accn": accession, "filed": filed, "fy": fiscal_year, "fp": period}


def test_ttm_uses_only_facts_accepted_before_decision_and_matching_ytd():
    annual = _fact("2025-01-01", "2025-12-31", 100, "10-K", "annual", "2026-02-01", 2025, "FY")
    future_revision = {**annual, "val": 200, "accn": "future", "filed": "2026-10-01"}
    current = _fact("2026-01-01", "2026-06-30", 60, "10-Q", "current", "2026-08-01", 2026, "Q2")
    prior = _fact("2025-01-01", "2025-06-30", 45, "10-Q", "prior", "2025-08-01", 2025, "Q2")
    payload = {"facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax":
                                      {"units": {"USD": [annual, future_revision, current, prior]}}}}}
    accepted = {key: value for key, value in {
        "annual": "2026-02-01T22:00:00+00:00", "future": "2026-10-01T22:00:00+00:00",
        "current": "2026-08-01T22:00:00+00:00", "prior": "2025-08-01T22:00:00+00:00",
    }.items()}
    decision = datetime(2026, 9, 29, 16, tzinfo=timezone.utc)
    value, method, facts = evidence.ttm_metric(payload, "revenue", "USD", accepted, decision, "2026-06-30")
    assert (value, method) == (115.0, "ANNUAL_PLUS_YTD_MINUS_PRIOR_YTD")
    assert [fact["accn"] for fact in facts] == ["annual", "current", "prior"]
    value, method, _ = evidence.ttm_metric(payload, "revenue", "USD", accepted, decision, "2026-09-30")
    assert value is None and method == "CURRENT_YTD_MISSING_OR_CONCEPT_CHANGED"


def test_sec_acceptance_uses_eastern_time_and_rejects_later_filing():
    submissions = {"filings": {"recent": {"accessionNumber": ["one"],
                                          "acceptanceDateTime": ["2026-09-29T12:30:00"]}}}
    accepted = evidence.acceptance_map(submissions)
    assert accepted["one"] == "2026-09-29T16:30:00+00:00"
    assert not evidence._available({"accn": "one"}, accepted,
                                   datetime(2026, 9, 29, 16, 15, tzinfo=timezone.utc))
    assert evidence._available({"accn": "one"}, accepted,
                               datetime(2026, 9, 29, 16, 45, tzinfo=timezone.utc))


def test_intraday_daily_bar_is_excluded_before_us_close(monkeypatch):
    ny = ZoneInfo("America/New_York")
    def stamp(day):
        return int(datetime(2026, 9, day, 16, tzinfo=ny).timestamp())
    payload = {"chart": {"result": [{"timestamp": [stamp(28), stamp(29)],
                                     "indicators": {"quote": [{"close": [10.0, 12.0],
                                                                "volume": [100, 1000]}]},
                                     "meta": {"gmtoffset": -4 * 3600, "currency": "USD"}}]}}
    monkeypatch.setattr(evidence, "_request_json", lambda *args, **kwargs: payload)
    row = evidence.completed_daily_market("AAA", None,
                                          datetime(2026, 9, 29, 16, 15, tzinfo=timezone.utc), 5)
    assert row["date"] == "2026-09-28"
    assert row["close"] == 10.0
    assert row["average_dollar_volume_20d"] == 1000.0


def test_latest_report_requires_valid_acceptance_time():
    submission = {"filings": {"recent": {"accessionNumber": ["one"], "form": ["10-Q"],
                                          "reportDate": ["2026-06-30"],
                                          "acceptanceDateTime": ["bad"]}}}
    assert evidence._latest_report(submission, datetime.now(timezone.utc)) is None


def test_negative_current_ttm_fcf_blocks_growth_research_status(monkeypatch):
    monkeypatch.setattr(evidence, "load_ticker_map", lambda *args: {"AAA": "0000000001"})
    monkeypatch.setattr(evidence, "companyfacts", lambda *args: {})
    monkeypatch.setattr(evidence, "submissions", lambda *args: {})
    monkeypatch.setattr(evidence, "completed_daily_market", lambda *args: {})
    monkeypatch.setattr(evidence, "current_evidence_row", lambda *args: {
        "Current Evidence Status": "CURRENT_REVIEW_EVIDENCE_COMPLETE",
        "Current Evidence Blockers": "", "TTM Free Cash Flow": -1.0, "TTM Net Income": 2.0,
    })
    candidates = pd.DataFrame([{"Category": "High Growth Potential", "Ticker": "AAA",
                                "Secondary Score": 70.0, "Quantitative Reason": "Dynamic reason"}])
    result = evidence.build_current_evidence(candidates, {}, default_settings(), config.GENERAL_DEFAULTS)
    assert result.loc[0, "Current Evidence Status"] == "REVIEW_REQUIRED"
    assert result.loc[0, "Current Evidence Blockers"] == "CURRENT_TTM_FREE_CASH_FLOW_NOT_POSITIVE"
    assert result.loc[0, "Quantitative Reason"] == "Dynamic reason"
