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
    df: pd.DataFrame, eligible: pd.Series, score_column: str, maximum: int | None,
    confidence_column: str = "Score Confidence",
) -> pd.Series:
    """Keep the strongest candidates universe-wide with deterministic tie-breaks."""
    if maximum is None or int(eligible.sum()) <= maximum:
        return eligible
    ranked = df.loc[eligible, [score_column, "Ticker"]].copy()
    ranked[confidence_column] = df.loc[eligible, confidence_column] if confidence_column in df else 100.0
    ranked["_ticker"] = ranked["Ticker"].fillna("").astype(str).str.casefold()
    if "Source Row" in df:
        ranked["_source_row"] = pd.to_numeric(df.loc[ranked.index, "Source Row"], errors="coerce")
    else:
        ranked["_source_row"] = np.arange(len(ranked))
    keep = ranked.sort_values(
        [score_column, confidence_column, "_ticker", "_source_row"],
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
    max_book_divergence: float | None = None,
) -> pd.DataFrame:
    out = df.copy()
    sufficient = out["Data Status"].eq("SUFFICIENT")
    short_sufficient = out.get("Short Data Status", out["Data Status"]).eq("SUFFICIENT")
    out["Expanded Long Rank"] = _rank_with_tiebreak(out, "Expanded Long Score", sufficient)
    out["Expanded Short Rank"] = _rank_with_tiebreak(out, "Expanded Short Score", short_sufficient)
    confidence = pd.to_numeric(
        out["Score Confidence"] if "Score Confidence" in out else pd.Series(100.0, index=out.index),
        errors="coerce",
    )
    confident = confidence.ge(min_confidence)
    short_confidence_column = "Short Score Confidence" if "Short Score Confidence" in out else "Score Confidence"
    short_confidence = pd.to_numeric(out[short_confidence_column], errors="coerce") if short_confidence_column in out else confidence
    short_confident = short_confidence.ge(min_confidence)
    def flag(name, default=True):
        return out.get(name, pd.Series(default, index=out.index)).fillna(False).astype(bool)
    long_trade, short_trade = flag("tradability_gate_pass"), flag("short_tradability_gate_pass")
    balance = flag("balance_sheet_gate_pass")
    classified = flag("economic_classification_verified")
    short_validation = flag("short_implementation_validation_required", False)
    book_pass = pd.Series(True, index=out.index)
    if max_book_divergence is not None:
        book = pd.to_numeric(out.get("book_price_divergence", pd.Series(np.nan, index=out.index)), errors="coerce")
        book_pass = book.notna() & book.le(max_book_divergence)
    out["Book Price Gate Pass"] = book_pass
    long_pool = sufficient & confident & long_trade & balance & classified & flag("Fundamental Business Type Gate Pass") & book_pass & out["Expanded Long Score"].ge(min_long)
    short_pool = short_sufficient & short_confident & short_trade & classified & flag("Short Thesis Gate Pass", False) & flag("Short Business Type Gate Pass", False) & out["Expanded Short Score"].ge(min_short)
    overlap = long_pool & short_pool
    prefer_long = out["Expanded Long Score"].ge(out["Expanded Short Score"])
    long_pool &= ~overlap | prefer_long
    short_pool &= ~overlap | ~prefer_long
    # Filter before per-industry limits, so an ineligible stock cannot use a slot.
    out["Long Selection Rank"] = _rank_with_tiebreak(out, "Expanded Long Score", long_pool)
    out["Short Selection Rank"] = _rank_with_tiebreak(out, "Expanded Short Score", short_pool)
    long = long_pool & out["Long Selection Rank"].le(top_long)
    short = short_pool & out["Short Selection Rank"].le(top_short)
    out["Long Candidate Eligible"] = long
    out["Short Candidate Eligible"] = short
    long = _apply_global_cap(out, long, "Expanded Long Score", max_total_long)
    short = _apply_global_cap(out, short, "Expanded Short Score", max_total_short, short_confidence_column)
    out["Quantitative Candidate"] = np.select([long, short], ["QUANT LONG", "QUANT SHORT"], default="")
    out["Candidate Conflict Resolved"] = overlap
    long_impl = out["Quantitative Candidate"].eq("QUANT LONG") & long_trade & balance
    short_impl = out["Quantitative Candidate"].eq("QUANT SHORT") & short_trade & ~short_validation
    out["Implementation Candidate"] = np.select([long_impl, short_impl], ["IMPLEMENTABLE LONG", "IMPLEMENTABLE SHORT"], default="")
    return out
