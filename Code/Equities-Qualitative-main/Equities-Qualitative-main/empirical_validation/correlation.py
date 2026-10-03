from __future__ import annotations

from .information_coefficient import pearson_ic


def correlation_matrix(matrix: dict[str, list[float | None]]) -> dict:
    names=sorted(matrix); out={}
    for a in names:
        out[a]={}
        for b in names: out[a][b]=pearson_ic(matrix[a],matrix[b])
    return {"columns":names,"matrix":out}
