from __future__ import annotations
import numpy as np
import pandas as pd


def _rank_with_tiebreak(df: pd.DataFrame, score_column: str, sufficient: pd.Series) -> pd.Series:
    """Rank deterministically so ties never select more than the configured maximum."""
    score = pd.to_numeric(df[score_column], errors="coerce")
    ranked = df.loc[sufficient & score.notna(), ["Industry", "Ticker"]].copy()
    ranked["_score"] = score.loc[ranked.index]
    ranked["_ticker"] = ranked["Ticker"].fillna("").astype(str).str.casefold()
    if "Source Row" in df:
        ranked["_source_row"] = pd.to_numeric(df.loc[ranked.index, "Source Row"], errors="coerce")
    else:
        ranked["_source_row"] = np.arange(len(ranked))
    ranked = ranked.sort_values(
        ["Industry", "_score", "_ticker", "_source_row"],
        ascending=[True, False, True, True],
        kind="stable",
    )
    result = pd.Series(np.nan, index=df.index, dtype=float)
    result.loc[ranked.index] = ranked.groupby("Industry", sort=False).cumcount().add(1).astype(float)
    return result


def _apply_global_cap(
    df: pd.DataFrame, eligible: pd.Series, score_column: str, maximum: int | None
) -> pd.Series:
    """Keep the strongest candidates universe-wide with deterministic tie-breaks."""
    if maximum is None or int(eligible.sum()) <= maximum:
        return eligible
    ranked = df.loc[eligible, [score_column, "Score Confidence", "Ticker"]].copy()
    ranked["_ticker"] = ranked["Ticker"].fillna("").astype(str).str.casefold()
    if "Source Row" in df:
        ranked["_source_row"] = pd.to_numeric(df.loc[ranked.index, "Source Row"], errors="coerce")
    else:
        ranked["_source_row"] = np.arange(len(ranked))
    keep = ranked.sort_values(
        [score_column, "Score Confidence", "_ticker", "_source_row"],
        ascending=[False, False, True, True],
        kind="stable",
    ).head(maximum).index
    return eligible & df.index.to_series().isin(keep)


def rank_quant_candidates(
    df: pd.DataFrame,
    top_long: int,
    top_short: int,
    min_long: float,
    min_short: float,
    max_total_long: int | None = None,
    max_total_short: int | None = None,
    min_confidence: float = 0.0,
) -> pd.DataFrame:
    out = df.copy()
    sufficient = out["Data Status"].eq("SUFFICIENT")
    out["Expanded Long Rank"] = _rank_with_tiebreak(out, "Expanded Long Score", sufficient)
    out["Expanded Short Rank"] = _rank_with_tiebreak(out, "Expanded Short Score", sufficient)
    confidence = pd.to_numeric(
        out["Score Confidence"] if "Score Confidence" in out else pd.Series(100.0, index=out.index),
        errors="coerce",
    )
    confident = confidence.ge(min_confidence)
    long = sufficient & confident & out["Expanded Long Rank"].le(top_long) & out["Expanded Long Score"].ge(min_long)
    short = sufficient & confident & out["Expanded Short Rank"].le(top_short) & out["Expanded Short Score"].ge(min_short)
    overlap = long & short
    long.loc[overlap] = out.loc[overlap, "Expanded Long Score"] >= out.loc[overlap, "Expanded Short Score"]
    short.loc[overlap] = ~long.loc[overlap]
    long = _apply_global_cap(out, long, "Expanded Long Score", max_total_long)
    short = _apply_global_cap(out, short, "Expanded Short Score", max_total_short)
    out["Quantitative Candidate"] = np.select([long, short], ["QUANT LONG", "QUANT SHORT"], default="")
    out["Candidate Conflict Resolved"] = overlap
    long_trade = out["tradability_gate_pass"] if "tradability_gate_pass" in out else pd.Series(True, index=out.index)
    short_trade = out["short_tradability_gate_pass"] if "short_tradability_gate_pass" in out else pd.Series(True, index=out.index)
    balance = out["balance_sheet_gate_pass"] if "balance_sheet_gate_pass" in out else pd.Series(True, index=out.index)
    short_validation = out["short_implementation_validation_required"] if "short_implementation_validation_required" in out else pd.Series(False, index=out.index)
    long_impl = out["Quantitative Candidate"].eq("QUANT LONG") & long_trade & balance
    short_impl = out["Quantitative Candidate"].eq("QUANT SHORT") & short_trade & ~short_validation
    out["Implementation Candidate"] = np.select([long_impl, short_impl], ["IMPLEMENTABLE LONG", "IMPLEMENTABLE SHORT"], default="")
    return out
