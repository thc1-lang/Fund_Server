"""Separate qualitative handoff from the scored research watchlist."""
from __future__ import annotations

from typing import Any

import pandas as pd

import config
from src.model import _reason
from src.shorts import _short_reason


COLUMNS = [
    "Category", "Ticker", "Company Name", "Primary Rank", "Primary Score",
    "Secondary Score", "Score Rank", "Data Review Complete", "Disposition",
    "Data Review Items", "Data Integrity Flags", "Primary PIT Status",
    "Provenance Complete", "Current TTM Verified", "Quantitative Reason",
]


def _true(value: Any) -> bool:
    return bool(value) if pd.notna(value) else False


def _text(value: Any) -> str:
    return str(value).strip() if pd.notna(value) else ""


def build_handoff(
    analyses: dict[str, pd.DataFrame], primary: dict[str, pd.DataFrame],
    short_analysis: pd.DataFrame, primary_shorts: pd.DataFrame,
    source_audit: pd.DataFrame, controls: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return score-ranked names separated only by external data-review status."""
    audit = {}
    for (category, ticker), group in source_audit.groupby(["Category", "Ticker"], sort=False):
        flags = "; ".join(dict.fromkeys(
            flag for cell in group["Integrity Flags"].fillna("") for flag in str(cell).split("; ") if flag
        ))
        audit[(category, ticker)] = (flags, bool(group["Provenance Complete"].all()))
    rows = []
    for category in (*config.CATEGORIES, "Short"):
        source = primary_shorts if category == "Short" else primary[category]
        scored = short_analysis if category == "Short" else analyses.get(category, pd.DataFrame())
        lookup = scored.set_index("Ticker", drop=False) if not scored.empty else pd.DataFrame()
        for _, primary_row in source.iterrows():
            ticker = str(primary_row["Ticker"]).strip().upper()
            if not ticker:
                continue
            found = not lookup.empty and ticker in lookup.index
            candidate = lookup.loc[ticker] if found else primary_row
            if isinstance(candidate, pd.DataFrame):
                raise ValueError(f"Duplicate candidate in {category}: {ticker}")
            short = category == "Short"
            history = pd.to_numeric(candidate.get("history_years"), errors="coerce")
            score = pd.to_numeric(candidate.get("short_score" if short else "final_score"), errors="coerce")
            rank_number = pd.to_numeric(candidate.get("short_rank" if short else "rank"), errors="coerce")
            primary_status = _text(candidate.get("Primary PIT Verification Status",
                                                 primary_row.get("Primary PIT Verification Status", "")))
            flags, provenance = audit.get((category, ticker), ("NO_SOURCE_AUDIT", False))
            ttm = _true(candidate.get("Current TTM Verified", False))
            blockers = []
            if pd.isna(rank_number) or rank_number <= 0:
                blockers.append("NO_RANKABLE_FOUR_YEAR_SCORE")
            if pd.isna(history) or history < 4:
                blockers.append("MISSING_OR_INCOMPLETE_SOURCE_HISTORY")
            if flags:
                blockers.append(flags)
            if not provenance:
                blockers.append("FIELD_PROVENANCE_INCOMPLETE")
            if primary_status != "VERIFIED_POINT_IN_TIME":
                blockers.append("PRIMARY_POINT_IN_TIME_UNVERIFIED")
            if not ttm:
                blockers.append("CURRENT_TTM_UNVERIFIED")
            if short:
                for signal in ("Primary 4-Week Price Change (%)", "Primary 12-Week Price Change (%)",
                               "Primary F1 Estimate Change 4-Week (%)", "Primary F2 Estimate Change 4-Week (%)"):
                    if pd.isna(pd.to_numeric(candidate.get(signal), errors="coerce")):
                        blockers.append("SHORT_PRIMARY_SIGNALS_INCOMPLETE")
                        break
            ready = not blockers
            disposition = "RESEARCH HANDOFF" if ready else "DATA REVIEW"
            reason = ""
            if pd.notna(score) and pd.notna(rank_number) and rank_number > 0 and found:
                rank = int(rank_number)
                reason = (_short_reason(candidate, controls, rank) if short else
                          _reason(candidate, category, controls, rank))
            rows.append({
                "Category": category, "Ticker": ticker,
                "Company Name": candidate.get("Company Name", candidate.get("Primary Company Name", primary_row.get("Company Name", ""))),
                "Primary Rank": candidate.get("Primary Short Rank" if short else "Primary Universe Rank",
                                              primary_row.get("Primary Short Rank" if short else "Primary Universe Rank")),
                "Primary Score": candidate.get("Primary Short Score" if short else "Primary Final Strategy Score",
                                               primary_row.get("Primary Short Score" if short else "Primary Final Strategy Score")),
                "Secondary Score": score, "Score Rank": rank_number, "Data Review Complete": ready,
                "Disposition": disposition, "Data Review Items": "; ".join(blockers),
                "Data Integrity Flags": flags, "Primary PIT Status": primary_status,
                "Provenance Complete": provenance, "Current TTM Verified": ttm,
                "Quantitative Reason": reason,
            })
    all_candidates = pd.DataFrame(rows, columns=COLUMNS)
    if all_candidates.duplicated(["Category", "Ticker"]).any():
        raise ValueError("Handoff contains duplicate category/ticker candidates")
    ready = all_candidates.loc[all_candidates["Data Review Complete"]].copy()
    watchlist = all_candidates.loc[~all_candidates["Data Review Complete"]].copy()
    for frame in (ready, watchlist):
        frame.sort_values(["Category", "Secondary Score", "Ticker"], ascending=[True, False, True],
                          kind="stable", na_position="last", inplace=True)
        frame.reset_index(drop=True, inplace=True)
    return ready, watchlist


def build_research_candidates(candidates: pd.DataFrame, source_audit: pd.DataFrame,
                              controls: dict[str, Any]) -> pd.DataFrame:
    """Expose displayed score leaders for current-data research."""
    candidates = candidates.copy()
    ranks = pd.to_numeric(candidates["Score Rank"], errors="coerce")
    caps = candidates["Category"].map({
        **{category: int(controls[f"TOP_N_{cfg['slug']}"]) for category, cfg in config.CATEGORIES.items()},
        "Short": int(controls["SHORT_TOP_N"]),
    })
    candidates = candidates.loc[ranks.ge(1) & ranks.le(caps)].copy()
    statuses = []
    blockers_by_row = []
    for _, row in candidates.iterrows():
        history = source_audit.loc[source_audit["Category"].eq(row["Category"])
                                   & source_audit["Ticker"].eq(row["Ticker"])].copy()
        blockers = []
        if history.empty or not history["Integrity Pass"].all():
            blockers.append("SOURCE_INTEGRITY_REVIEW")
        if history.empty or "Research Traceable" not in history or not history["Research Traceable"].all():
            blockers.append("SEC_FIELD_LINEAGE_INCOMPLETE")
        if not history.empty:
            history["_period"] = pd.to_datetime(history["Fiscal Period End"], errors="coerce")
            latest = history.sort_values("_period", na_position="first").iloc[-1]
            if str(latest["Missing Essential Fields"]).strip():
                blockers.append("LATEST_ESSENTIAL_FIELDS_MISSING: " + str(latest["Missing Essential Fields"]))
        statuses.append("CURRENT-DATA RESEARCH" if not blockers else "SOURCE REVIEW")
        blockers_by_row.append("; ".join(blockers))
    candidates.insert(candidates.columns.get_loc("Disposition") + 1, "Research Status", statuses)
    candidates.insert(candidates.columns.get_loc("Research Status") + 1, "Research Blockers", blockers_by_row)
    candidates.sort_values(["Research Status", "Category", "Secondary Score", "Ticker"],
                           ascending=[True, True, False, True], kind="stable", inplace=True)
    candidates.reset_index(drop=True, inplace=True)
    return candidates
