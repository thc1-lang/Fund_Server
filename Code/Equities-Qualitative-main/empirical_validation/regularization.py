from __future__ import annotations


def constrained_weights(factors, weights=None, max_factor_weight=1.0):
    weights=weights or {f:1.0/len(factors) for f in factors} if factors else {}
    clipped={f:min(max_factor_weight,max(0.0,float(weights.get(f,0.0)))) for f in factors}; total=sum(clipped.values())
    return {f:(v/total if total else 0.0) for f,v in clipped.items()}
