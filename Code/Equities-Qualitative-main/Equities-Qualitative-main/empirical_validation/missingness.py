from __future__ import annotations

from .factor_matrix import factor_matrix


def availability_by_company(observations):
    result={}
    for row in observations:
        result[row.ticker]={k:(v is not None) for k,v in row.factor_values.items()}
    return result


def missingness_report(observations):
    matrix=factor_matrix(observations)
    return {"availability":matrix["availability"],"by_company":availability_by_company(observations),"missingness_preserved":True}
