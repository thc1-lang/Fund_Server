from __future__ import annotations

import random
from typing import Callable, Iterable


def bootstrap(values: Iterable[float], statistic: Callable[[list[float]], float], iterations: int = 1000, seed: int = 20260924) -> dict:
    vals=list(values)
    if not vals:return {"status":"INSUFFICIENT_SAMPLE","iterations":0,"seed":seed}
    rng=random.Random(seed); samples=[]
    for _ in range(iterations): samples.append(statistic([vals[rng.randrange(len(vals))] for _ in vals]))
    samples.sort(); lo=samples[max(0,int(iterations*.025)-1)]; hi=samples[min(len(samples)-1,int(iterations*.975))]
    return {"status":"AVAILABLE","iterations":iterations,"seed":seed,"method":"iid_percentile_bootstrap","lower":lo,"upper":hi,"mean":sum(samples)/len(samples)}
