from __future__ import annotations

from .descriptive_stats import describe
from .information_coefficient import ic_summary
from .quantile_analysis import quantile_outcomes


def diagnose_factor(scores, outcomes, groups=3, minimum_group_size=10, seed=20260924):
    return {"score_distribution":describe(scores),"outcome_distribution":describe(outcomes),"information_coefficient":ic_summary(scores,outcomes),"quantiles":quantile_outcomes(scores,outcomes,groups,minimum_group_size),"status":"EXPLORATORY"}
