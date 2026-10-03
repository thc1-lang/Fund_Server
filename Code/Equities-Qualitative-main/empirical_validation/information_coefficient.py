from __future__ import annotations

import math
import random
from typing import Iterable


def _pairs(x: Iterable[float | None], y: Iterable[float | None]):
    return [(float(a), float(b)) for a,b in zip(x,y) if a is not None and b is not None and math.isfinite(float(a)) and math.isfinite(float(b))]


def pearson_ic(x: Iterable[float | None], y: Iterable[float | None]) -> float | None:
    pairs=_pairs(x,y)
    if len(pairs)<2:return None
    xs=[p[0] for p in pairs]; ys=[p[1] for p in pairs]; mx=sum(xs)/len(xs); my=sum(ys)/len(ys)
    den=math.sqrt(sum((a-mx)**2 for a in xs)*sum((b-my)**2 for b in ys))
    return None if den==0 else sum((a-mx)*(b-my) for a,b in pairs)/den


def _rank(values):
    order=sorted(range(len(values)),key=lambda i:values[i]); ranks=[0.0]*len(values); i=0
    while i<len(order):
        j=i
        while j+1<len(order) and values[order[j+1]]==values[order[i]]:j+=1
        rank=(i+j)/2+1
        for k in range(i,j+1):ranks[order[k]]=rank
        i=j+1
    return ranks


def spearman_ic(x,y):
    pairs=_pairs(x,y)
    if len(pairs)<2:return None
    return pearson_ic(_rank([a for a,b in pairs]),_rank([b for a,b in pairs]))


def ic_summary(x,y):
    p=_pairs(x,y); pear=pearson_ic(x,y); spear=spearman_ic(x,y)
    return {"sample_count":len(p),"pearson_ic":pear,"spearman_ic":spear,"available":bool(p)}
