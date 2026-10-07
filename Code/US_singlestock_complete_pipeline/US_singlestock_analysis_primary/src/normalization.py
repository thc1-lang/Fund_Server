"""Authoritative source-unit normalisation with per-row audit evidence."""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd


SUPPORTED_UNITS = {"percent", "ratio", "multiple", "currency", "currency_millions", "count", "boolean", "categorical"}


@dataclass(frozen=True)
class FieldUnit:
    unit: str
    normalized_name: str
    suspicious_min: float | None = None
    suspicious_max: float | None = None


# Exact names were verified against the authoritative Dataset tab on 2026-08-30.
FIELD_UNITS: dict[str, FieldUnit] = {
    "Debt/Equity Ratio": FieldUnit("ratio", "debt_equity_ratio", 0.0, 10.0),
    "Debt/Total Capital": FieldUnit("percent", "debt_total_capital_ratio", 0.0, 1.5),
    "Net Margin %": FieldUnit("percent", "net_margin_vendor_normalized", -5.0, 5.0),
    "Operating Margin 12 Mo %": FieldUnit("percent", "operating_margin_vendor_normalized", -5.0, 5.0),
    "Current ROE (TTM)": FieldUnit("percent", "roe_vendor_normalized", -10.0, 10.0),
    "Current ROA (TTM)": FieldUnit("percent", "roa_vendor_normalized", -10.0, 10.0),
    "Current ROI (TTM)": FieldUnit("percent", "roi_vendor_normalized", -10.0, 10.0),
    "Div. Yield %": FieldUnit("percent", "dividend_yield_normalized", -0.1, 1.0),
    "Current Ratio": FieldUnit("ratio", "current_ratio_normalized", 0.0, 50.0),
    "Quick Ratio": FieldUnit("ratio", "quick_ratio_normalized", 0.0, 50.0),
    "Cash Ratio": FieldUnit("ratio", "cash_ratio_normalized", 0.0, 50.0),
    "P/E (F1)": FieldUnit("multiple", "pe_f1_normalized", -1000.0, 1000.0),
    "P/E (F2)": FieldUnit("multiple", "pe_f2_normalized", -1000.0, 1000.0),
    "P/E (Trailing 12 Months)": FieldUnit("multiple", "pe_ttm_normalized", -1000.0, 1000.0),
    "PEG Ratio": FieldUnit("multiple", "peg_normalized", -1000.0, 1000.0),
}


def _number(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values.replace("", np.nan), errors="coerce").replace([np.inf, -np.inf], np.nan)


def normalize_series(values: pd.Series, unit: str) -> tuple[pd.Series, pd.Series]:
    """Return normalized values and an explicit deterministic transformation label."""
    if unit not in SUPPORTED_UNITS:
        raise ValueError(f"Unsupported configured unit {unit!r}")
    numeric = _number(values)
    if unit == "percent":
        return numeric / 100.0, pd.Series("DIVIDE_BY_100", index=values.index, dtype="object")
    if unit in {"ratio", "multiple", "currency", "currency_millions", "count"}:
        return numeric, pd.Series("IDENTITY", index=values.index, dtype="object")
    return values.copy(), pd.Series("IDENTITY", index=values.index, dtype="object")


def apply_unit_normalization(df: pd.DataFrame) -> pd.DataFrame:
    """Add normalized values and raw/unit/transformation/warning audit columns."""
    out = pd.DataFrame(index=df.index)
    warning_columns = []
    for source, spec in FIELD_UNITS.items():
        if source not in df:
            continue
        normalized, transformation = normalize_series(df[source], spec.unit)
        stem = spec.normalized_name
        out[f"{stem}__raw"] = df[source]
        # Debt ratios are assigned by the derived-feature registry from this
        # audited normalized value; avoid duplicate canonical columns.
        if stem not in {"debt_equity_ratio", "debt_total_capital_ratio"}:
            out[stem] = normalized
        out[f"{stem}__normalized"] = normalized
        out[f"{stem}__unit_source"] = spec.unit
        out[f"{stem}__normalization_applied"] = transformation
        warning = pd.Series(False, index=df.index, dtype=bool)
        if spec.suspicious_min is not None:
            warning |= normalized.notna() & normalized.lt(spec.suspicious_min)
        if spec.suspicious_max is not None:
            warning |= normalized.notna() & normalized.gt(spec.suspicious_max)
        out[f"{stem}__scaling_warning"] = warning
        warning_columns.append(f"{stem}__scaling_warning")
    out["margin_scaling_warning"] = out.get("net_margin_vendor_normalized__scaling_warning", False) | out.get("operating_margin_vendor_normalized__scaling_warning", False)
    out["yield_scaling_warning"] = out.get("dividend_yield_normalized__scaling_warning", False)
    any_warning = out[warning_columns].any(axis=1) if warning_columns else pd.Series(False, index=df.index)
    out["ratio_normalization_status"] = np.where(any_warning, "SCALING_WARNING", "CONFIGURED_UNITS_APPLIED")
    out["unit_scaling_status"] = out["ratio_normalization_status"]
    return out
