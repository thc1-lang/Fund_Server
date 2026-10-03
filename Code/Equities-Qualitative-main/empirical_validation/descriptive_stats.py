from __future__ import annotations

import math


def describe(values):
    vals=[float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:return {"count":0,"mean":None,"median":None,"std":None}
    s=sorted(vals); mean=sum(vals)/len(vals); med=s[len(s)//2] if len(s)%2 else (s[len(s)//2-1]+s[len(s)//2])/2
    std=(sum((v-mean)**2 for v in vals)/(len(vals)-1))**.5 if len(vals)>1 else 0.0
    return {"count":len(vals),"mean":mean,"median":med,"std":std,"min":min(vals),"max":max(vals)}
