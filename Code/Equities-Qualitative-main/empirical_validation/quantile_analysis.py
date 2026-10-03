from __future__ import annotations

import math


def assign_quantiles(values: list[float | None], groups: int = 3, minimum_group_size: int = 10) -> dict:
    pairs=[(i,v) for i,v in enumerate(values) if v is not None]
    if len(pairs)<groups*minimum_group_size:return {"status":"INSUFFICIENT_SAMPLE","groups":groups,"assignments":{}}
    ordered=sorted(pairs,key=lambda x:x[1]); assignments={}; n=len(ordered)
    for rank,(idx,_) in enumerate(ordered): assignments[str(idx)]=min(groups-1,rank*groups//n+1)
    return {"status":"AVAILABLE","groups":groups,"assignments":assignments,"minimum_group_size":minimum_group_size}


def quantile_outcomes(scores: list[float | None], outcomes: list[float | None], groups: int = 3, minimum_group_size: int = 10) -> dict:
    valid=[(s,o) for s,o in zip(scores,outcomes) if s is not None and o is not None]
    if len(valid)<groups*minimum_group_size:return {"status":"INSUFFICIENT_SAMPLE","groups":groups}
    valid.sort(key=lambda p:p[0]); n=len(valid); result={}
    for g in range(groups):
        chunk=valid[g*n//groups:(g+1)*n//groups]; vals=[o for _,o in chunk]
        result[str(g+1)]={"count":len(vals),"mean":sum(vals)/len(vals),"median":sorted(vals)[len(vals)//2]}
    result["diagnostic_spread_top_minus_bottom"]=result[str(groups)]["mean"]-result["1"]["mean"]
    result["status"]="AVAILABLE"; return result
