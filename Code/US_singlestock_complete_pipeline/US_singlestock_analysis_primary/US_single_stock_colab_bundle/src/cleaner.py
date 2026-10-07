from __future__ import annotations
import numpy as np
import pandas as pd
from .schema import IDENTITY_FIELDS


def clean_source(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c) for c in out.columns]
    for c in out.columns:
        if c not in IDENTITY_FIELDS:
            converted = pd.to_numeric(out[c].replace("", np.nan), errors="coerce").replace([np.inf, -np.inf], np.nan)
            if converted.notna().sum() > 0:
                out[c] = converted
        else:
            out[c] = out[c].where(out[c].notna(), "").astype(str).str.strip()
    return pd.concat([pd.Series(np.arange(2, len(out) + 2), index=out.index, name="Source Row"), out], axis=1).copy()
