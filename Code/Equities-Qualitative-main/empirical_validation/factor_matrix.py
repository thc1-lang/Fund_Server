from __future__ import annotations

from .models import HistoricalObservation


def factor_matrix(observations: list[HistoricalObservation]) -> dict:
    factors=sorted({key for row in observations for key in row.factor_values})
    rows=[]
    for obs in observations:
        rows.append({"observation_id":obs.observation_id,"ticker":obs.ticker,"as_of_date":obs.as_of_date,**{f:obs.factor_values.get(f) for f in factors},"overall_scoring_coverage":obs.overall_scoring_coverage})
    availability={f:sum(1 for r in rows if r.get(f) is not None)/len(rows) if rows else 0.0 for f in factors}
    return {"factors":factors,"rows":rows,"availability":availability,"missingness_preserved":True}
