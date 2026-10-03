"""Directional spreads and sample statistics over prior valid observations."""

import numpy as np
from correlation import prior_windows


def spread_series(x, y):
    return np.asarray(x, dtype=float) - np.asarray(y, dtype=float)


def rolling_statistics(values, window):
    history = prior_windows(values, window)
    valid = np.isfinite(history).all(axis=1)
    mean = np.full(len(history), np.nan)
    std = np.full(len(history), np.nan)
    mean[valid] = history[valid].mean(axis=1)
    std[valid] = history[valid].std(axis=1, ddof=1)
    return mean, std


def spread_zscore(x, y, window):
    spread = spread_series(x, y)
    mean, std = rolling_statistics(spread, window)
    result = np.full(len(spread), np.nan)
    valid = np.isfinite(spread) & np.isfinite(mean) & np.isfinite(std) & (std > 0)
    result[valid] = (spread[valid] - mean[valid]) / std[valid]
    result[~np.isfinite(result)] = np.nan
    return result
