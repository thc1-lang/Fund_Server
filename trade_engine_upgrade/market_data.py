"""Provider-neutral market-data loading, provenance, coverage and reconciliation.

This module deliberately contains no provider credentials or scraping logic. A
source may be a local CSV/Parquet/Feather file, a DataFrame supplied by a
connector, or a callable provider adapter. The research engine receives a
canonical frame plus auditable metadata and does not know how it was fetched.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from research_core import frame_hash, standardise_raw_data


class MarketDataError(ValueError):
    """Raised when a market-data source cannot be loaded or identified."""


@dataclass(frozen=True)
class DataProvenance:
    instrument: str = "UNSPECIFIED"
    ticker: str = "UNSPECIFIED"
    asset_class: str = "unknown"
    provider: str = "UNSPECIFIED"
    provider_symbol: str = "UNSPECIFIED"
    frequency: str = "1D"
    trading_calendar: str = "UNSPECIFIED"
    timezone: str = "UNSPECIFIED"
    currency: str = "UNSPECIFIED"
    price_type: str = "close"
    ohlc_adjustment: str = "raw"
    adjustment_method: str = "UNSPECIFIED"
    retrieval_timestamp: str = ""
    first_date: str = ""
    last_date: str = ""
    row_count: int = 0
    source_identifier: str = "UNSPECIFIED"
    source_hash: str = ""
    data_hash: str = ""
    requested_start: str = ""
    requested_end: str = ""

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MarketDataBundle:
    frame: pd.DataFrame
    provenance: DataProvenance
    mapping: dict


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_source(source: Any) -> tuple[pd.DataFrame, str, str]:
    if isinstance(source, pd.DataFrame):
        return source.copy(), "dataframe", "in_memory_dataframe"
    if callable(source):
        frame = source()
        if not isinstance(frame, pd.DataFrame):
            raise MarketDataError("Provider adapter must return a pandas DataFrame")
        return frame.copy(), "callable", getattr(source, "__name__", "provider_callable")
    path = Path(str(source))
    if not path.exists():
        raise MarketDataError(f"Market-data source does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in (".parquet", ".pq"):
        try:
            frame = pd.read_parquet(path)
        except ImportError as exc:
            raise MarketDataError("Parquet loading requires pyarrow or fastparquet") from exc
    elif suffix in (".feather", ".ft"):
        try:
            frame = pd.read_feather(path)
        except ImportError as exc:
            raise MarketDataError("Feather loading requires pyarrow") from exc
    else:
        raise MarketDataError(f"Unsupported market-data format {suffix!r}; use CSV, Parquet or Feather")
    return frame, "file", str(path.resolve())


def _date_text(value) -> str:
    return "" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def load_market_data(
    instrument: str,
    source: Any,
    start=None,
    end=None,
    price_type: str = "close",
    adjustment: str = "raw",
    asset_class: str = "unknown",
    provider: str = "UNSPECIFIED",
    ticker: str = "UNSPECIFIED",
    provider_symbol: str = "UNSPECIFIED",
    frequency: str = "1D",
    trading_calendar: str = "UNSPECIFIED",
    timezone_name: str = "UNSPECIFIED",
    currency: str = "UNSPECIFIED",
    adjustment_method: str = "UNSPECIFIED",
    source_identifier: str | None = None,
    metadata: dict | None = None,
) -> MarketDataBundle:
    """Load and canonicalise a source while retaining the complete source range.

    `start` and `end` are recorded as requested bounds; the frame is not
    trimmed before the coverage gate runs, so an early or late source shortfall
    remains observable.
    """
    if source is None:
        raise MarketDataError("No approved market-data provider configured")
    frame, source_kind, source_name = _read_source(source)
    if frame.empty:
        raise MarketDataError("Market-data source is empty")
    metadata = dict(metadata or {})
    if source_kind == "file":
        source_hash = _sha256_file(Path(source_name))
    else:
        source_hash = frame_hash(frame)
    canonical, mapping = standardise_raw_data(frame, price_type=price_type, ohlc_adjustment=adjustment)
    dates = pd.to_datetime(canonical["date"], errors="coerce")
    if dates.notna().any():
        first_date, last_date = dates.min(), dates.max()
    else:
        first_date = last_date = None
    provenance = DataProvenance(
        instrument=str(metadata.get("instrument", instrument)),
        ticker=str(metadata.get("ticker", ticker)),
        asset_class=str(metadata.get("asset_class", asset_class)),
        provider=str(metadata.get("provider", provider)),
        provider_symbol=str(metadata.get("provider_symbol", provider_symbol)),
        frequency=str(metadata.get("frequency", frequency)),
        trading_calendar=str(metadata.get("trading_calendar", trading_calendar)),
        timezone=str(metadata.get("timezone", timezone_name)),
        currency=str(metadata.get("currency", currency)),
        price_type=str(metadata.get("price_type", price_type)),
        ohlc_adjustment=str(metadata.get("ohlc_adjustment", adjustment)),
        adjustment_method=str(metadata.get("adjustment_method", adjustment_method)),
        retrieval_timestamp=str(metadata.get("retrieval_timestamp", datetime.now(timezone.utc).isoformat())),
        first_date=_date_text(first_date), last_date=_date_text(last_date), row_count=len(canonical),
        source_identifier=str(metadata.get("source_identifier", source_identifier or source_name)),
        source_hash=source_hash, data_hash=frame_hash(canonical),
        requested_start=_date_text(start), requested_end=_date_text(end),
    )
    canonical.attrs["raw_data_mapping"] = asdict(mapping)
    canonical.attrs["market_data_provenance"] = provenance.as_dict()
    canonical.attrs["market_data_source_kind"] = source_kind
    return MarketDataBundle(canonical, provenance, asdict(mapping))


def cache_market_data(bundle: MarketDataBundle, cache_dir: str | Path, key: str | None = None) -> dict:
    """Persist a validated source locally, preferring Parquet when available."""
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cache_key = key or f"{bundle.provenance.instrument}_{bundle.provenance.frequency}".replace(" ", "_")
    try:
        import pyarrow  # noqa: F401
        path = directory / f"{cache_key}.parquet"
        bundle.frame.to_parquet(path, index=False)
        storage_format = "parquet"
    except (ImportError, ModuleNotFoundError):
        path = directory / f"{cache_key}.csv"
        bundle.frame.to_csv(path, index=False)
        storage_format = "csv_fallback_no_parquet_engine"
    metadata_path = directory / f"{cache_key}.metadata.json"
    payload = {"provenance": bundle.provenance.as_dict(), "mapping": bundle.mapping,
               "storage_format": storage_format, "path": str(path),
               "data_hash": bundle.provenance.data_hash}
    metadata_path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    return {"path": str(path), "metadata_path": str(metadata_path), "storage_format": storage_format,
            "data_hash": bundle.provenance.data_hash}


def load_cached_market_data(cache_path: str | Path, metadata_path: str | Path | None = None) -> MarketDataBundle:
    path = Path(cache_path)
    metadata_file = Path(metadata_path) if metadata_path else path.with_suffix(".metadata.json")
    if not metadata_file.exists():
        raise MarketDataError(f"Cache metadata is missing: {metadata_file}")
    payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    bundle = load_market_data(**{
        "instrument": payload["provenance"].get("instrument", "UNSPECIFIED"),
        "source": path,
        "price_type": payload["provenance"].get("price_type", "close"),
        "adjustment": payload["provenance"].get("ohlc_adjustment", "raw"),
        "metadata": payload["provenance"],
    })
    if bundle.provenance.data_hash != payload.get("data_hash"):
        raise MarketDataError("Cached market-data hash does not match its metadata")
    return bundle


def _canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not {"date", "open", "high", "low", "close"}.issubset(frame.columns):
        frame, _ = standardise_raw_data(frame)
    out = frame[["date", "open", "high", "low", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    return out


def reconcile_sources(primary: pd.DataFrame, secondary: pd.DataFrame, tolerance_bps: float = 5.0,
                      absolute_tolerance: float = 1e-8) -> tuple[pd.DataFrame, dict]:
    """Compare overlapping canonical bars and classify every date."""
    p, s = _canonical_frame(primary), _canonical_frame(secondary)
    p = p.rename(columns={c: f"primary_{c}" for c in ("open", "high", "low", "close")})
    s = s.rename(columns={c: f"secondary_{c}" for c in ("open", "high", "low", "close")})
    merged = p.merge(s, on="date", how="outer", indicator=True).sort_values("date", kind="stable")
    records = []
    for _, row in merged.iterrows():
        if row["_merge"] == "left_only":
            records.append({"date": row["date"], "classification": "SECONDARY_MISSING"})
            continue
        if row["_merge"] == "right_only":
            records.append({"date": row["date"], "classification": "PRIMARY_MISSING"})
            continue
        diffs = {field: abs(row[f"primary_{field}"] - row[f"secondary_{field}"])
                 for field in ("open", "high", "low", "close")}
        bps = {field: 10000 * diffs[field] / max(abs(row[f"primary_{field}"]), absolute_tolerance)
               for field in diffs}
        material = {field: bps[field] > tolerance_bps and diffs[field] > absolute_tolerance for field in diffs}
        if not any(material.values()):
            label = "MATCH" if all(diffs[field] <= absolute_tolerance for field in diffs) else "ROUNDING_DIFFERENCE"
        elif material["close"] and not any(material[f] for f in ("open", "high", "low")):
            label = "CLOSE_MISMATCH"
        elif material["open"] and not any(material[f] for f in ("high", "low", "close")):
            label = "OPEN_MISMATCH"
        elif material["high"] and not any(material[f] for f in ("open", "low", "close")):
            label = "HIGH_MISMATCH"
        elif material["low"] and not any(material[f] for f in ("open", "high", "close")):
            label = "LOW_MISMATCH"
        else:
            label = "UNRESOLVED"
        record = {"date": row["date"], "classification": label}
        for field in diffs:
            record[f"{field}_difference"] = diffs[field]
            record[f"{field}_difference_bps"] = bps[field]
        records.append(record)
    diagnostics = pd.DataFrame(records)
    counts = diagnostics["classification"].value_counts().to_dict() if len(diagnostics) else {}
    summary = {"exact_matches": int(counts.get("MATCH", 0)),
               "near_matches": int(counts.get("ROUNDING_DIFFERENCE", 0)),
               "material_discrepancies": int(len(diagnostics) - counts.get("MATCH", 0) - counts.get("ROUNDING_DIFFERENCE", 0)),
               "missing_primary_bars": int(counts.get("PRIMARY_MISSING", 0)),
               "missing_secondary_bars": int(counts.get("SECONDARY_MISSING", 0)),
               "classification_counts": counts, "tolerance_bps": tolerance_bps,
               "absolute_tolerance": absolute_tolerance}
    return diagnostics, summary


def _longest_true_run(mask) -> int:
    longest = current = 0
    for value in np.asarray(mask, dtype=bool):
        current = current + 1 if value else 0
        longest = max(longest, current)
    return int(longest)


_SPECIAL_CLOSURE_REASONS = (
    (pd.Timestamp("1985-09-27"), pd.Timestamp("1985-09-27"),
     "NYSE closure for Hurricane Gloria"),
    (pd.Timestamp("2001-09-11"), pd.Timestamp("2001-09-14"),
     "NYSE closure following the September 11 attacks"),
    (pd.Timestamp("2007-01-02"), pd.Timestamp("2007-01-02"),
     "National day of mourning for former President Gerald Ford"),
    (pd.Timestamp("2012-10-29"), pd.Timestamp("2012-10-30"),
     "NYSE closure for Hurricane Sandy"),
)


def _known_closure_reason(value: pd.Timestamp) -> str | None:
    for start, end, reason in _SPECIAL_CLOSURE_REASONS:
        if start <= value <= end:
            return reason
    if value.month == 1 and value.day == 1:
        return "New Year's Day market holiday"
    if value in (pd.Timestamp("1980-02-18"),):
        return "Presidents Day market holiday"
    return None


def _normalise_calendar_name(value: str | None) -> str:
    name = str(value or "").strip().upper()
    return {"XNYS": "NYSE", "US_EQUITIES": "NYSE", "NYSE_ARCA": "NYSE"}.get(name, name)


def _calendar_sessions(calendar_name: str, start: pd.Timestamp, end: pd.Timestamp):
    """Return local exchange sessions without fetching a remote calendar."""
    name = _normalise_calendar_name(calendar_name)
    if not name or name in ("UNSPECIFIED", "UNKNOWN"):
        return pd.DatetimeIndex([]), "NOT_CONFIGURED", "Trading calendar is not configured"
    start_text, end_text = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    if name == "NYSE":
        try:
            import pandas_market_calendars as mcal
            calendar = mcal.get_calendar("NYSE")
            sessions = calendar.schedule(start_text, end_text).index
            return pd.DatetimeIndex(sessions).tz_localize(None).normalize(), "pandas_market_calendars", ""
        except Exception as exc:
            pmc_error = str(exc)
        try:
            import exchange_calendars as xcals
            calendar = xcals.get_calendar("XNYS")
            sessions = calendar.sessions_in_range(start_text, end_text)
            return pd.DatetimeIndex(sessions).tz_localize(None).normalize(), "exchange_calendars", ""
        except Exception as exc:
            return pd.DatetimeIndex([]), "UNAVAILABLE", f"NYSE calendar unavailable: {pmc_error}; {exc}"
    return pd.DatetimeIndex([]), "UNAVAILABLE", f"Unsupported trading calendar {calendar_name!r}"


def _classify_closed_interval(previous: pd.Timestamp, following: pd.Timestamp, calendar_name: str) -> tuple[str, str]:
    between = pd.date_range(previous + pd.Timedelta(days=1), following - pd.Timedelta(days=1), freq="D")
    if len(between) == 0 or all(day.weekday() >= 5 for day in between):
        return "WEEKEND", "No weekday occurred between the available sessions"
    for start, end, reason in _SPECIAL_CLOSURE_REASONS:
        if any(start <= day <= end for day in between):
            return "SPECIAL MARKET CLOSURE", reason
    return "NORMAL MARKET HOLIDAY", f"{calendar_name} calendar contains no sessions in the interval"


def _gap_details(dates: pd.DatetimeIndex, calendar_name: str, sessions: pd.DatetimeIndex) -> list[dict]:
    details = []
    ordered = pd.Series(dates.sort_values().drop_duplicates()).reset_index(drop=True)
    diffs = ordered.diff().dt.days
    for index in diffs[diffs > 4].index:
        previous, following = ordered.iloc[index - 1], ordered.iloc[index]
        expected_between = sessions[(sessions > previous) & (sessions < following)]
        if len(expected_between):
            classification = "GENUINE MISSING SESSION"
            reason = f"{calendar_name} expects {len(expected_between)} session(s) in this interval"
        elif calendar_name not in ("UNSPECIFIED", "UNKNOWN", ""):
            classification, reason = _classify_closed_interval(previous, following, calendar_name)
        else:
            classification, reason = "UNCLASSIFIED CALENDAR GAP", "Trading calendar is not configured"
        details.append({
            "previous_available_session": str(previous.date()),
            "next_available_session": str(following.date()),
            "calendar_classification": classification,
            "missing_expected_sessions": [str(value.date()) for value in expected_between],
            "reason": reason,
            "calendar": calendar_name,
        })
    return details


def _normalise_session_dates(values) -> pd.DatetimeIndex:
    """Normalize mixed date, naive timestamp and timezone-aware inputs to dates."""
    parsed = pd.to_datetime(pd.Series(values), errors="coerce", utc=True).dropna()
    if parsed.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(parsed.dt.tz_convert(None).dt.normalize()).sort_values()


def coverage_audit(frame: pd.DataFrame, cfg) -> dict:
    """Audit range coverage, required-session completeness and session consistency separately."""
    dates = _normalise_session_dates(frame["date"])
    first, last = (dates.min(), dates.max()) if len(dates) else (None, None)
    max_horizon = max(int(x) for x in cfg.horizons)
    indicator = max(int(cfg.feature_lookback), int(getattr(cfg, "regime_lookback", 0)))
    volatility = int(getattr(cfg, "volatility_lookback", cfg.feature_lookback))
    safety = int(getattr(cfg, "coverage_safety_buffer_sessions", 5))
    # Keep independent lookbacks additive. A regime/feature window and a
    # volatility/ATR window may both need their own history before TRAIN.
    warmup_sessions = indicator + volatility + safety
    required_start = (pd.Timestamp(cfg.train_start) - pd.offsets.BDay(warmup_sessions)
                      if getattr(cfg, "require_warmup_coverage", False) else pd.Timestamp(cfg.train_start))
    required_end = (pd.Timestamp(cfg.test_end) + pd.offsets.BDay(max_horizon)
                    if getattr(cfg, "require_full_horizon_coverage", False) else pd.Timestamp(cfg.test_end))
    required_start = pd.Timestamp(required_start).tz_localize(None).normalize()
    required_end = pd.Timestamp(required_end).tz_localize(None).normalize()
    duplicate_count = int(dates.duplicated().sum())
    required_dates = dates[(dates >= required_start) & (dates <= required_end)]
    weekend_count = int(sum(d.weekday() >= 5 for d in dates))
    required_weekend_count = int(sum(d.weekday() >= 5 for d in required_dates))
    stale = frame[["open", "high", "low", "close"]].eq(frame[["open", "high", "low", "close"]].shift()).all(axis=1) if len(frame) else pd.Series(dtype=bool)
    calendar_name = _normalise_calendar_name(getattr(cfg, "trading_calendar", "UNSPECIFIED"))
    calendar_requested = calendar_name not in ("", "UNSPECIFIED", "UNKNOWN")
    if calendar_requested:
        full_start, full_end = (first, last) if first is not None and last is not None else (required_start, required_end)
        full_sessions, calendar_provider, calendar_error = _calendar_sessions(calendar_name, full_start, full_end)
        required_sessions, required_calendar_provider, required_calendar_error = _calendar_sessions(calendar_name, required_start, required_end)
        calendar_error = calendar_error or required_calendar_error
    else:
        full_sessions = required_sessions = pd.DatetimeIndex([])
        calendar_provider = required_calendar_provider = "NOT_CONFIGURED"
        calendar_error = "Trading calendar is not configured"
    calendar_available = bool(calendar_requested
                              and calendar_provider not in ("UNAVAILABLE", "NOT_CONFIGURED")
                              and (not calendar_requested or required_calendar_provider not in ("UNAVAILABLE", "NOT_CONFIGURED")))

    observed_required = set(required_dates.drop_duplicates())
    observed_full = set(dates.drop_duplicates())
    if calendar_available:
        genuine_missing_required = [value for value in required_sessions if value not in observed_required]
        genuine_missing_full = [value for value in full_sessions if value not in observed_full]
        unexpected_required = sorted(observed_required - set(required_sessions))
        unexpected_full = sorted(observed_full - set(full_sessions))
    else:
        genuine_missing_required = genuine_missing_full = []
        unexpected_required = unexpected_full = []

    # Long calendar gaps are diagnostics only. Missing expected exchange
    # sessions, calculated above, are the authoritative completeness test.
    gap_details = _gap_details(dates, calendar_name, full_sessions)
    session_classes = pd.Series([item["calendar_classification"] for item in gap_details]).value_counts().to_dict() if gap_details else {}
    start_ok = first is not None and first <= required_start
    end_ok = last is not None and last >= required_end
    range_coverage_pass = bool(start_ok and end_ok)
    session_completeness_pass = bool(not calendar_requested or (calendar_available and not genuine_missing_required))
    session_consistency_pass = bool(not calendar_requested or (calendar_available and not unexpected_required and required_weekend_count == 0))
    duplicate_ok = duplicate_count == 0
    coverage_pass = bool(range_coverage_pass and session_completeness_pass and session_consistency_pass and duplicate_ok)
    if not calendar_requested:
        calendar_status = "NOT_CONFIGURED"
        session_completeness_status = "NOT_CONFIGURED"
        session_consistency_status = "NOT_CONFIGURED"
    elif not calendar_available:
        calendar_status = "FAIL"
        session_completeness_status = "FAIL"
        session_consistency_status = "FAIL"
    else:
        calendar_status = "PASS"
        session_completeness_status = "PASS" if not genuine_missing_required else "FAIL"
        session_consistency_status = "PASS" if not unexpected_required and required_weekend_count == 0 else "FAIL"
    return {
        "configured_train_start": str(pd.Timestamp(cfg.train_start).date()),
        "required_market_data_start": str(required_start.date()),
        "actual_market_data_start": _date_text(first),
        "configured_test_end": str(pd.Timestamp(cfg.test_end).date()),
        "required_market_data_end": str(required_end.date()),
        "actual_market_data_end": _date_text(last),
        "coverage_buffer_days_at_start": int((required_start - first).days) if first is not None else None,
        "coverage_buffer_days_at_end": int((last - required_end).days) if last is not None else None,
        "indicator_lookback_sessions": indicator, "volatility_lookback_sessions": volatility,
        "safety_buffer_sessions": safety, "warmup_sessions": warmup_sessions,
        "warmup_coverage_required": bool(getattr(cfg, "require_warmup_coverage", False)),
        "full_horizon_coverage_required": bool(getattr(cfg, "require_full_horizon_coverage", False)),
        "maximum_horizon_sessions": max_horizon,
        "status": "PASS" if coverage_pass else "FAIL",
        "range_coverage_pass": range_coverage_pass,
        "range_coverage_status": "PASS" if range_coverage_pass else "FAIL",
        "duplicate_sessions": duplicate_count, "weekend_rows": weekend_count,
        "required_period_weekend_rows": required_weekend_count,
        "trading_calendar": calendar_name or "UNSPECIFIED",
        "calendar_provider": calendar_provider,
        "calendar_status": calendar_status,
        "calendar_error": calendar_error,
        "calendar_expected_sessions": int(len(full_sessions)),
        "required_period_expected_sessions": int(len(required_sessions)),
        "required_period_observed_sessions": int(len(observed_required)),
        "genuine_missing_session_dates": [str(value.date()) for value in genuine_missing_required],
        "genuine_missing_session_count": int(len(genuine_missing_required)),
        "full_history_missing_session_dates": [str(value.date()) for value in genuine_missing_full],
        "full_history_missing_session_count": int(len(genuine_missing_full)),
        "session_completeness_pass": session_completeness_pass,
        "session_completeness_status": session_completeness_status,
        "unexpected_source_session_dates": [str(value.date()) for value in unexpected_required],
        "unexpected_source_session_count": int(len(unexpected_required)),
        "full_history_unexpected_source_session_dates": [str(value.date()) for value in unexpected_full],
        "full_history_unexpected_source_session_count": int(len(unexpected_full)),
        "unexpected_source_session_details": [
            {"date": str(value.date()), "classification": "UNEXPECTED SOURCE SESSION",
             "reason": _known_closure_reason(value) or f"{calendar_name} calendar contains no session"}
            for value in unexpected_required
        ],
        "full_history_unexpected_source_session_details": [
            {"date": str(value.date()), "classification": "UNEXPECTED SOURCE SESSION",
             "reason": _known_closure_reason(value) or f"{calendar_name} calendar contains no session"}
            for value in unexpected_full
        ],
        "session_consistency_pass": session_consistency_pass,
        "session_consistency_status": session_consistency_status,
        "unexplained_gap_count": int(len(genuine_missing_required)),
        "long_gap_count": int(len(gap_details)),
        "long_gap_details": gap_details,
        "session_classification_counts": session_classes,
        "special_closure_gap_count": int(session_classes.get("SPECIAL MARKET CLOSURE", 0)),
        "normal_market_holiday_gap_count": int(session_classes.get("NORMAL MARKET HOLIDAY", 0)),
        "weekend_gap_count": int(session_classes.get("WEEKEND", 0)),
        "largest_unexplained_gap_days": int(max(((pd.Timestamp(item["next_available_session"]) - pd.Timestamp(item["previous_available_session"])).days for item in gap_details if item["calendar_classification"] == "GENUINE MISSING SESSION"), default=0)),
        "longest_stale_run": _longest_true_run(stale.to_numpy()) if len(stale) else 0,
        "missing_start_days": int((first - required_start).days) if first is not None and first > required_start else 0,
        "missing_end_days": int((required_end - last).days) if last is not None and last < required_end else 0,
        "missing_start_sessions_estimate": int(np.busday_count(first.date(), required_start.date())) if first is not None and first > required_start else 0,
        "required_period_complete": coverage_pass,
        "full_history_session_completeness_status": "PASS" if calendar_available and not genuine_missing_full else "FAIL" if calendar_available else "NOT_CONFIGURED" if not calendar_requested else "UNAVAILABLE",
    }


def provenance_audit(provenance: dict | None, cfg) -> dict:
    required = ("instrument", "ticker", "asset_class", "provider", "provider_symbol", "frequency",
                "trading_calendar", "timezone", "currency", "price_type", "ohlc_adjustment", "adjustment_method",
                "retrieval_timestamp", "first_date", "last_date", "row_count", "source_identifier",
                "data_hash")
    provenance = dict(provenance or {})
    missing = [field for field in required
               if provenance.get(field) in (None, "", "UNSPECIFIED", "unknown", "UNKNOWN")]
    status = "FAIL" if missing and getattr(cfg, "require_provenance", False) else "PASS"
    return {"status": status, "missing_fields": missing, "record": provenance}


def market_data_quality_score(ohlc_status: str, coverage: dict, provenance: dict,
                              reconciliation: dict | None = None, adjustment_consistent: bool = True) -> dict:
    range_pass = coverage.get("range_coverage_pass", coverage.get("status") == "PASS")
    completeness_pass = coverage.get("session_completeness_pass", coverage.get("unexplained_gap_count", 0) == 0)
    consistency_pass = coverage.get("session_consistency_pass", coverage.get("session_consistency_status", "PASS") == "PASS")
    components = {
        "ohlc_integrity": 100 if ohlc_status == "PASS" else 0,
        "range_coverage": 100 if range_pass else 0,
        "session_completeness": 100 if completeness_pass and coverage.get("duplicate_sessions", 0) == 0 else 0,
        "session_consistency": 100 if consistency_pass else 75,
        "source_provenance": 100 if provenance.get("status") == "PASS" else 0,
        "adjustment_consistency": 100 if adjustment_consistent else 0,
        # An unperformed secondary-source check is a configuration state, not
        # evidence that two providers agreed.  It is excluded from the score.
        "cross_provider_agreement": (100 if reconciliation.get("status") == "PASS" else 0)
            if reconciliation and reconciliation.get("status") not in ("NOT_CONFIGURED", "UNAVAILABLE") else None,
    }
    # Keep the pre-separation names in the artifact for consumers that read
    # older reports, while scoring the distinct readiness dimensions once.
    components["coverage_completeness"] = components["range_coverage"]
    components["gap_completeness"] = 100 if completeness_pass and coverage.get("duplicate_sessions", 0) == 0 else 50
    components["timestamp_session_consistency"] = components["session_consistency"]
    scored_names=("ohlc_integrity", "range_coverage", "session_completeness",
                  "session_consistency", "source_provenance", "adjustment_consistency",
                  "cross_provider_agreement")
    included=[name for name in scored_names if components[name] is not None]
    excluded=[name for name in scored_names if components[name] is None]
    score = float(np.mean([components[name] for name in included])) if included else 0.
    caps = []
    if ohlc_status != "PASS": caps.append("material OHLC integrity failure")
    if not range_pass: caps.append("required date-range coverage is incomplete")
    if not completeness_pass or coverage.get("duplicate_sessions", 0): caps.append("required-period session completeness failed")
    if not consistency_pass: caps.append("required-period session consistency failed")
    if coverage.get("calendar_status") == "FAIL": caps.append("configured trading calendar is unavailable")
    if provenance.get("status") == "FAIL": caps.append("required provenance is incomplete")
    if not adjustment_consistent: caps.append("adjustment consistency is unresolved")
    if reconciliation and reconciliation.get("status") == "FAIL": caps.append("required secondary-source reconciliation is unavailable or failed")
    if caps: score = 0.0
    status = "FAIL" if caps else "PASS" if score >= 90 else "PASS WITH WARNINGS" if score >= 75 else "REVIEW"
    return {"score": score, "components": components, "included_components": included,
            "excluded_components": excluded, "hard_caps": caps, "status": status}


def readiness_report(frame: pd.DataFrame, cfg, ohlc_summary: dict | None = None,
                     provenance: dict | None = None, reconciliation: dict | None = None) -> dict:
    coverage = coverage_audit(frame, cfg)
    prov = provenance_audit(provenance, cfg)
    ohlc_status = "FAIL" if ohlc_summary and ohlc_summary.get("material_rows", 0) else "PASS"
    quality = market_data_quality_score(ohlc_status, coverage, prov, reconciliation,
                                        adjustment_consistent=not bool(ohlc_summary and ohlc_summary.get("adjusted_close_mismatch_rows", 0)))
    hard_gate_pass = bool(
        ohlc_status == "PASS"
        and coverage.get("range_coverage_pass", False)
        and coverage.get("session_completeness_pass", False)
        and coverage.get("session_consistency_pass", False)
        and prov.get("status") == "PASS"
        and not bool(ohlc_summary and ohlc_summary.get("adjusted_close_mismatch_rows", 0))
    )
    return {"instrument": cfg.instrument, "asset_class": cfg.asset_class,
            "ticker": cfg.ticker, "trading_calendar": getattr(cfg, "trading_calendar", "UNSPECIFIED"),
            "price_type": cfg.price_type, "ohlc_adjustment": cfg.ohlc_adjustment,
            "provenance": prov, "coverage": coverage,
            "ohlc_integrity": ohlc_summary or {"status": ohlc_status},
            "reconciliation": reconciliation or {"status": "NOT_CONFIGURED"},
            "quality": quality, "hard_gate_pass": hard_gate_pass, "train_authorised": hard_gate_pass}
