"""Signed changes over valid observations, standardised against prior changes."""

import numpy as np
from spread import rolling_statistics


def observation_change(values, lookback):
    if (
        isinstance(lookback, bool)
        or not isinstance(lookback, (int, np.integer))
        or lookback < 1
    ):
        raise ValueError("Momentum Lookback must be an integer >= 1")
    values = np.asarray(values, dtype=float)
    result = np.full(values.shape, np.nan)
    positions = np.flatnonzero(np.isfinite(values))
    if len(positions) > lookback:
        with np.errstate(over="ignore", invalid="ignore"):
            result[positions[lookback:]] = (
                values[positions[lookback:]] - values[positions[:-lookback]]
            )
    result[~np.isfinite(result)] = np.nan
    return result


def momentum_zscore(values, lookback, window):
    change = observation_change(values, lookback)
    mean, std = rolling_statistics(change, window)
    result = np.full(len(change), np.nan)
    valid = np.isfinite(change) & np.isfinite(mean) & np.isfinite(std) & (std > 0)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        result[valid] = (change[valid] - mean[valid]) / std[valid]
    result[~np.isfinite(result)] = np.nan
    return result
