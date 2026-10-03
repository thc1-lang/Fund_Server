from __future__ import annotations

from .models import CalibrationCandidate
from .profile_builder import build_candidate


def calibration_refusal(observations, config):
    dates={o.as_of_date for o in observations}; companies={o.ticker for o in observations}
    reasons=[]
    if len(observations)<config.minimum_observations: reasons.append("MINIMUM_OBSERVATIONS_NOT_MET")
    if len(companies)<config.minimum_unique_companies: reasons.append("MINIMUM_UNIQUE_COMPANIES_NOT_MET")
    if len(dates)<config.minimum_unique_dates: reasons.append("MINIMUM_UNIQUE_DATES_NOT_MET")
    return {"status":"INSUFFICIENT_HISTORICAL_DATA" if reasons else "READY_FOR_EXPLORATORY_CALIBRATION","reasons":reasons,"candidate_created":False}
