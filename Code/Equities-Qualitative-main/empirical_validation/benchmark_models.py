from __future__ import annotations

from .information_coefficient import pearson_ic


def baseline_definitions():
    return ["design_core_v1.2", "equal_weight_eligible_factors", "business_trajectory_only", "governance_only", "random_no_signal"]


def random_signal(n: int, seed: int = 20260924) -> list[float]:
    import random
    rng=random.Random(seed); return [rng.random() for _ in range(n)]
