from __future__ import annotations
import pandas as pd
import numpy as np


def validate_results(
    df: pd.DataFrame,
    *,
    top_long: int | None = None,
    top_short: int | None = None,
    min_long: float | None = None,
    min_short: float | None = None,
    max_total_long: int | None = None,
    max_total_short: int | None = None,
    min_confidence: float | None = None,
    max_debt_equity: float | None = None,
    max_book_divergence: float | None = None,
    financial_sectors: tuple[str, ...] = (),
) -> None:
    for col in ["Expanded Long Score", "Expanded Short Score", "Score Confidence", "Short Score Confidence"]:
        if col not in df:
            continue
        bad = df[col].dropna().loc[lambda x: ~x.between(0, 100)]
        if not bad.empty:
            raise ValueError(f"{col} outside 0..100")
    overlap = df.groupby("Ticker")["Quantitative Candidate"].nunique().gt(1)
    if overlap.any():
        raise ValueError("A ticker appears in conflicting candidate directions")
    long_selected = df["Quantitative Candidate"].eq("QUANT LONG")
    short_selected = df["Quantitative Candidate"].eq("QUANT SHORT")
    if (long_selected & df["Data Status"].ne("SUFFICIENT")).any():
        raise ValueError("Quantitative Long candidate has insufficient data")
    if (short_selected & df.get("Short Data Status", df["Data Status"]).ne("SUFFICIENT")).any():
        raise ValueError("Quantitative Short candidate has insufficient data")
    short_numerators = [c for c in df if c.startswith("Quant Composite ") and c.endswith(" Short Weighted Numerator")]
    if short_numerators and "Short Applicable Weight" in df:
        rebuilt = df[short_numerators].sum(axis=1, min_count=1) / pd.to_numeric(df["Short Applicable Weight"], errors="coerce").replace(0, np.nan)
        reported = pd.to_numeric(df["Expanded Raw Short Score"], errors="coerce")
        comparable = rebuilt.notna() & reported.notna()
        if (rebuilt.loc[comparable] - reported.loc[comparable]).abs().gt(1e-10).any():
            raise ValueError("Independent Short weighted numerator/denominator reconciliation failed")
        confidence = pd.to_numeric(df.get("Short Score Confidence", df["Score Confidence"]), errors="coerce") / 100.0
        final = pd.to_numeric(df["Expanded Short Score"], errors="coerce")
        if (final.loc[comparable] - (50 + confidence.loc[comparable] * (reported.loc[comparable] - 50))).abs().gt(1e-10).any():
            raise ValueError("Independent Short reliability adjustment reconciliation failed")
    numerator_columns = [c for c in df if c.startswith("Quant Composite ") and c.endswith(" Long Weighted Numerator")]
    if numerator_columns:
        numerator = df[numerator_columns].sum(axis=1, min_count=1)
        denominator = pd.to_numeric(df["Quant Composite Applicable Weight"], errors="coerce")
        rebuilt_raw = numerator / denominator.replace(0, np.nan)
        published_raw = pd.to_numeric(df["Expanded Raw Long Score"], errors="coerce")
        comparable = rebuilt_raw.notna() & published_raw.notna()
        if (rebuilt_raw.loc[comparable] - published_raw.loc[comparable]).abs().gt(1e-10).any():
            raise ValueError("Quant composite weighted numerator/denominator reconciliation failed")
        confidence = pd.to_numeric(df["Score Confidence"], errors="coerce") / 100.0
        expected_adjusted = 50.0 + confidence * (published_raw - 50.0)
        published_adjusted = pd.to_numeric(df["Expanded Long Score"], errors="coerce")
        if (expected_adjusted.loc[comparable] - published_adjusted.loc[comparable]).abs().gt(1e-10).any():
            raise ValueError("Quant composite reliability adjustment reconciliation failed")
        contribution_columns = [c for c in df if c.startswith("Quant Composite ") and c.endswith(" Long Contribution Points")]
        contribution_sum = df[contribution_columns].sum(axis=1, min_count=1)
        if (contribution_sum.loc[comparable] - published_raw.loc[comparable]).abs().gt(1e-10).any():
            raise ValueError("Quant composite contribution-point reconciliation failed")
    candidate_specs = (
        ("QUANT LONG", "Long Selection Rank" if "Long Selection Rank" in df else "Expanded Long Rank", "Expanded Long Score", top_long, min_long, max_total_long),
        ("QUANT SHORT", "Short Selection Rank" if "Short Selection Rank" in df else "Expanded Short Rank", "Expanded Short Score", top_short, min_short, max_total_short),
    )
    for candidate, rank_col, score_col, maximum, threshold, total_maximum in candidate_specs:
        selected = df["Quantitative Candidate"].eq(candidate)
        if maximum is not None:
            counts = selected.groupby(df["Industry"]).sum()
            if counts.gt(maximum).any() or df.loc[selected, rank_col].gt(maximum).any():
                raise ValueError(f"{candidate} exceeds configured maximum {maximum}")
        if threshold is not None and df.loc[selected, score_col].lt(threshold).any():
            raise ValueError(f"{candidate} falls below configured threshold {threshold}")
        if total_maximum is not None and selected.sum() > total_maximum:
            raise ValueError(f"{candidate} exceeds universe-wide maximum {total_maximum}")
        confidence_col = "Short Score Confidence" if candidate == "QUANT SHORT" and "Short Score Confidence" in df else "Score Confidence"
        if min_confidence is not None and df.loc[selected, confidence_col].lt(min_confidence).any():
            raise ValueError(f"{candidate} falls below confidence threshold {min_confidence}")
    for direction, flag in [("QUANT LONG", "tradability_gate_pass"), ("QUANT LONG", "balance_sheet_gate_pass"), ("QUANT SHORT", "short_tradability_gate_pass")]:
        if flag in df and not df.loc[df["Quantitative Candidate"].eq(direction), flag].fillna(False).all():
            raise ValueError(f"{direction} fails {flag}")
    if "Short Thesis Gate Pass" in df and not df.loc[df["Quantitative Candidate"].eq("QUANT SHORT"), "Short Thesis Gate Pass"].fillna(False).all():
        raise ValueError("QUANT SHORT fails current-state weakness thesis gate")
    if "Short Business Type Gate Pass" in df and not df.loc[df["Quantitative Candidate"].eq("QUANT SHORT"), "Short Business Type Gate Pass"].fillna(False).all():
        raise ValueError("QUANT SHORT fails allowed business-type gate")
    if "Fundamental Business Type Gate Pass" in df and not df.loc[df["Quantitative Candidate"].eq("QUANT LONG"), "Fundamental Business Type Gate Pass"].fillna(False).all():
        raise ValueError("QUANT LONG fails allowed business-type gate")
    if "Short Selected" in df and not df["Short Selected"].equals(df["Quantitative Candidate"].eq("QUANT SHORT")):
        raise ValueError("Short display and industry selections disagree")
    selected = df["Quantitative Candidate"].eq("QUANT LONG")
    if max_debt_equity is not None:
        sector = df["Sector"] if "Sector" in df else pd.Series("", index=df.index)
        financial = sector.astype(str).str.casefold().isin({s.casefold() for s in financial_sectors})
        debt_equity = pd.to_numeric(df.loc[selected & ~financial, "debt_equity_ratio"], errors="coerce")
        if debt_equity.isna().any() or debt_equity.lt(0).any() or debt_equity.ge(max_debt_equity).any():
            raise ValueError(f"Quant candidate fails debt/equity hard gate below {max_debt_equity}")
    if max_book_divergence is not None:
        book_divergence = pd.to_numeric(df.loc[selected, "book_price_divergence"], errors="coerce")
        if book_divergence.isna().any() or book_divergence.gt(max_book_divergence).any():
            raise ValueError(f"Quant candidate fails book/price divergence hard gate {max_book_divergence}")


def reconciliation_flags(df: pd.DataFrame, pb_tolerance: float, mcps_tolerance: float) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    supplied_pb = pd.to_numeric(df["Price/Book"], errors="coerce")
    rebuilt_pb = pd.to_numeric(df["price_book_reconstructed"], errors="coerce")
    out["P/B Reconciliation Flag"] = ((rebuilt_pb / supplied_pb - 1).abs() > pb_tolerance).where(supplied_pb.gt(0) & rebuilt_pb.gt(0))
    close = pd.to_numeric(df["Last Close"], errors="coerce")
    mcps = pd.to_numeric(df["market_cap_per_share"], errors="coerce")
    out["Market Cap Per Share Flag"] = ((mcps / close - 1).abs() > mcps_tolerance).where(close.gt(0) & mcps.gt(0))
    rating_sum = pd.to_numeric(df["% Rating Strong Buy or Buy"], errors="coerce") + pd.to_numeric(df["% Rating Hold"], errors="coerce") + pd.to_numeric(df["% Rating Strong Sell or Sell"], errors="coerce")
    out["Rating Percent Reconciliation Flag"] = rating_sum.sub(100).abs().gt(2).where(rating_sum.notna())
    return out


def validate_strategy_results(df: pd.DataFrame, strategy_weights: dict[str, dict[str, float]]) -> None:
    """Reconcile every final strategy score to its visible helper components."""
    for strategy, weights in strategy_weights.items():
        score = pd.to_numeric(df[f"{strategy} Score"], errors="coerce")
        if not score.dropna().between(0, 100).all():
            raise ValueError(f"{strategy} score outside 0..100")
        numerator = pd.concat([
            pd.to_numeric(df[f"{strategy} {family} Weighted Numerator"], errors="coerce")
            for family in weights
        ], axis=1).sum(axis=1, min_count=1)
        denominator = pd.to_numeric(df[f"{strategy} Applicable Weight"], errors="coerce")
        rebuilt = numerator / denominator.replace(0, np.nan)
        published = pd.to_numeric(df[f"{strategy} Score Before Gates"], errors="coerce")
        comparable = rebuilt.notna() & published.notna()
        if (rebuilt.loc[comparable] - published.loc[comparable]).abs().gt(1e-10).any():
            raise ValueError(f"{strategy} numerator/denominator reconciliation failed")
        contribution_sum = pd.concat([
            pd.to_numeric(df[f"{strategy} {family} Contribution Points"], errors="coerce")
            for family in weights
        ], axis=1).sum(axis=1, min_count=1)
        if (contribution_sum.loc[comparable] - published.loc[comparable]).abs().gt(1e-10).any():
            raise ValueError(f"{strategy} contribution-point reconciliation failed")
        eligible = df[f"{strategy} Eligible"].fillna(False).astype(bool)
        if score.notna().ne(eligible).any():
            raise ValueError(f"{strategy} eligibility and published score availability disagree")
        ranked = df.loc[eligible, f"{strategy} Universe Rank"].sort_values().to_numpy()
        if not np.array_equal(ranked, np.arange(1, len(ranked) + 1, dtype=float)):
            raise ValueError(f"{strategy} universe ranks are not consecutive")
    turn = df["Turnaround Story Eligible"].fillna(False).astype(bool)
    if (~df.loc[turn, "Turnaround Setup Gate Pass"].fillna(False).astype(bool)).any():
        raise ValueError("Turnaround score published without passing the current-state setup gate")
