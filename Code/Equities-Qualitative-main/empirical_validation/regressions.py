from __future__ import annotations

from .information_coefficient import pearson_ic


def univariate_regression(x, y):
    pairs=[(a,b) for a,b in zip(x,y) if a is not None and b is not None]
    if len(pairs)<3:return {"status":"INSUFFICIENT_SAMPLE","sample_count":len(pairs)}
    mx=sum(a for a,b in pairs)/len(pairs); my=sum(b for a,b in pairs)/len(pairs)
    den=sum((a-mx)**2 for a,b in pairs)
    slope=sum((a-mx)*(b-my) for a,b in pairs)/den if den else None
    return {"status":"EXPLORATORY","sample_count":len(pairs),"slope":slope,"intercept":my-slope*mx if slope is not None else None,"correlation":pearson_ic(x,y),"standard_errors":"NOT_ESTIMATED_FOR_SMALL_PILOT"}
