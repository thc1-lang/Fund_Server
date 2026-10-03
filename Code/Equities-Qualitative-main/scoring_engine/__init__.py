"""Evidence- and coverage-aware qualitative scoring (Component 7)."""

from .models import (
    CompanyScoreSnapshot,
    FactorScore,
    PillarScore,
    ScoreRun,
)
from .profiles import ProfileNotConfigured, load_profile

__all__ = [
    "CompanyScoreSnapshot",
    "FactorScore",
    "PillarScore",
    "ScoreRun",
    "ProfileNotConfigured",
    "load_profile",
]

SCORING_VERSION = "qualitative-scoring-v1.2"
