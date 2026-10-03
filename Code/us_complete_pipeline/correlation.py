"""Pearson/Fisher scores over prior valid paired observations."""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def prior_windows(values, window):
    """At each row, return the last w finite observations strictly before it."""
    if window < 2:
        raise ValueError("Window must be >= 2")
    values = np.asarray(values, dtype=float)
    if not len(values):
        return np.empty((0, window))
    finite = np.isfinite(values)
    # Subtract the current-row flag so t never enters its own historical window.
    prior_counts = np.cumsum(finite) - finite
    compact = values[finite]
    windows = sliding_window_view(np.r_[np.full(window, np.nan), compact], window)
    return windows[prior_counts]


def fisher_transform(r):
    r = np.asarray(r, dtype=float)
    out = np.full(r.shape, np.nan)
    # Perfect correlations have an undefined finite Fisher score; never clip to artificial values.
    valid = np.isfinite(r) & (np.abs(r) < 1 - 1e-14)
    out[valid] = np.arctanh(r[valid])
    return out


def rolling_correlation(x, y, window):
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if x.shape != y.shape:
        raise ValueError("Series lengths differ")
    paired = np.isfinite(x) & np.isfinite(y)
    # Apply one shared mask: never join X and Y from different observation dates.
    xw = prior_windows(np.where(paired, x, np.nan), window)
    yw = prior_windows(np.where(paired, y, np.nan), window)
    if xw.shape != yw.shape:
        raise ValueError("Series lengths differ")
    # A prior window alone must not carry a score into a date without a pair.
    valid = paired & np.isfinite(xw).all(axis=1) & np.isfinite(yw).all(axis=1)
    result = np.full(len(xw), np.nan)
    a, b = xw[valid], yw[valid]
    a = a - a.mean(axis=1, keepdims=True)
    b = b - b.mean(axis=1, keepdims=True)
    den = np.sqrt(np.sum(a * a, axis=1) * np.sum(b * b, axis=1))
    corr = np.divide(
        np.sum(a * b, axis=1), den, out=np.full(len(a), np.nan), where=den > 0
    )
    result[valid] = fisher_transform(corr)
    return result
