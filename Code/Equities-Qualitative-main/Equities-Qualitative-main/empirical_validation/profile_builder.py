from __future__ import annotations

import hashlib, json
from .models import CalibrationCandidate


def build_candidate(parent_profile: str, target: str, horizon_days: int | None, factor_set: list[str], weights: dict[str,float], metrics: dict, dataset_version: str) -> CalibrationCandidate:
    payload={"parent_profile":parent_profile,"target":target,"horizon_days":horizon_days,"factor_set":factor_set,"weights":weights,"dataset_version":dataset_version}
    candidate_id="candidate:"+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:20]
    return CalibrationCandidate(candidate_id,parent_profile,parent_profile,target,horizon_days,{"start":None,"end":None},{"start":None,"end":None},factor_set,weights,{"non_negative":True,"sum_to_one":True},metrics,{}, {"minimum_observations":30},"not_fitted", "CANDIDATE")
