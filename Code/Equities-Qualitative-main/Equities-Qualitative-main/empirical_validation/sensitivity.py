from __future__ import annotations


def coverage_band(value: float | None) -> str:
    if value is None:return "MISSING"
    for threshold in (0.4,0.5,0.6,0.7,0.8):
        if value < threshold:return f"<{threshold:.1f}"
    return ">=0.8"


def coverage_sensitivity(observations):
    out={}
    for o in observations: out.setdefault(coverage_band(o.overall_scoring_coverage),0); out[coverage_band(o.overall_scoring_coverage)]+=1
    return out
