from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChronologicalSplit:
    train: list
    validation: list
    holdout: list
    status: str
    reason: str | None = None


def chronological_split(observations, train_end: str | None = None, validation_end: str | None = None, holdout_start: str | None = None) -> ChronologicalSplit:
    rows=sorted(observations,key=lambda x:x.as_of_date)
    if not rows:return ChronologicalSplit([],[],[],"INSUFFICIENT_DATA","no observations")
    dates=sorted({x.as_of_date for x in rows})
    if len(dates)<3:return ChronologicalSplit(rows,[],[],"INSUFFICIENT_HISTORY_FOR_CALIBRATION","fewer than three chronological dates")
    if train_end is None: train_end=dates[max(0,len(dates)//2-1)]
    if validation_end is None: validation_end=dates[-2]
    train=[x for x in rows if x.as_of_date<=train_end]
    validation=[x for x in rows if train_end<x.as_of_date<=validation_end]
    holdout=[x for x in rows if x.as_of_date>=(holdout_start or validation_end) and x not in validation]
    return ChronologicalSplit(train,validation,holdout,"AVAILABLE")
