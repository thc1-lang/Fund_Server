from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .aggregation import aggregate
from .factor_registry import load_profile_definition
from .governance_scoring import score_governance
from .insider_scoring import score_insider
from .management_scoring import score_management
from .models import ScoreRun
from .qualitative_scoring import score_qualitative
from .sources import EvidenceSources
from .store import ScoreStore
from .utils import stable_id, make_factor


def score_company(ticker: str, as_of: str, profile_name: str = "core_v1", *, artifacts_root: str = "artifacts", config_root: str = "config/scoring"):
    profile = load_profile_definition(profile_name, config_root)
    sources = EvidenceSources(artifacts_root)
    ticker = ticker.upper()
    materialization_profile = f"{profile_name}@{profile.profile_version}"
    factors = []
    factors.extend(score_qualitative(ticker, as_of, materialization_profile, profile.factors, sources, config_root))
    factors.extend(score_management(ticker, as_of, materialization_profile, profile.factors, sources, config_root))
    factors.extend(score_insider(ticker, as_of, materialization_profile, profile.factors, sources, config_root))
    factors.extend(score_governance(ticker, as_of, materialization_profile, profile.factors, sources, config_root))
    observed = {item.factor_key for item in factors}
    for definition in profile.factors:
        if definition.factor_key not in observed:
            factors.append(make_factor(ticker=ticker, as_of=as_of, profile=materialization_profile, definition=definition,
                                       score=None, status="UNSCORED", coverage=0.0, confidence=0.0,
                                       coverage_reason="INPUT_SOURCE_HANDLER_NOT_CONFIGURED"))
    # Registry order is the audit order, regardless of module order.
    order = {item.factor_key: idx for idx, item in enumerate(profile.factors)}
    factors.sort(key=lambda item: order.get(item.factor_key, 999))
    pillars, snapshot = aggregate(ticker, as_of, profile, factors)
    return profile, factors, pillars, snapshot


def run_score(tickers: list[str], as_of: str, profile_name: str = "core_v1", *, artifacts_root: str = "artifacts",
              config_root: str = "config/scoring", output_root: str = "artifacts/scoring_engine", write: bool = True):
    profile = load_profile_definition(profile_name, config_root)
    all_factors, all_pillars, snapshots = [], [], []
    for ticker in dict.fromkeys(item.upper() for item in tickers):
        _, factors, pillars, snapshot = score_company(ticker, as_of, profile_name, artifacts_root=artifacts_root, config_root=config_root)
        all_factors.extend(factors); all_pillars.extend(pillars); snapshots.append(snapshot)
    run = ScoreRun(run_id=f"run:{profile_name}:{profile.profile_version}:{as_of}:{stable_id(profile_name, as_of, *sorted(set(tickers)))}", profile=profile_name,
                   as_of_date=as_of, tickers=[item.upper() for item in dict.fromkeys(tickers)], scoring_version=profile.scoring_version,
                   factor_rules_version=profile.factor_rules_version, profile_version=profile.profile_version,
                   coverage_version=profile.coverage_version, created_at=datetime.now(timezone.utc).isoformat())
    counts = {}
    if write:
        store = ScoreStore(output_root)
        counts = store.write_run(all_factors, all_pillars, snapshots, run)
    return profile, all_factors, all_pillars, snapshots, run, counts
