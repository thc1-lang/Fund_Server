from __future__ import annotations


def score_stability(observations):
    rows=sorted(observations,key=lambda x:(x.ticker,x.as_of_date)); changes=[]
    for a,b in zip(rows,rows[1:]):
        if a.ticker != b.ticker or a.overall_score is None or b.overall_score is None: continue
        changes.append(abs(b.overall_score-a.overall_score))
    return {"status":"AVAILABLE" if changes else "INSUFFICIENT_SAMPLE","count":len(changes),"mean_absolute_change":sum(changes)/len(changes) if changes else None,"largest_change":max(changes) if changes else None}
