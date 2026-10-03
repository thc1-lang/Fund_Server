from __future__ import annotations

from .walk_forward import chronological_split


def expanding_walk_forward(observations, minimum_train_dates=2):
    dates=sorted({o.as_of_date for o in observations}); windows=[]
    for i in range(minimum_train_dates,len(dates)-1):
        train_dates=set(dates[:i]); validation_date=dates[i]
        windows.append({"train":[o.observation_id for o in observations if o.as_of_date in train_dates],"validation":[o.observation_id for o in observations if o.as_of_date==validation_date],"status":"AVAILABLE"})
    return windows
