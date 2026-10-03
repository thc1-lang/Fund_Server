from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import FactorDefinition, ProfileDefinition


class RegistryError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RegistryError(f"Missing scoring configuration: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_factor_definitions(config_root: str | Path = "config/scoring") -> dict[str, FactorDefinition]:
    payload = _load_json(Path(config_root) / "factor_definitions.json")
    values = payload.get("factors", payload)
    result = {row["factor_key"]: FactorDefinition.from_dict(row) for row in values}
    if not result:
        raise RegistryError("factor_definitions.json contains no factors")
    return result


def load_mappings(config_root: str | Path = "config/scoring") -> dict[str, Any]:
    return _load_json(Path(config_root) / "factor_definitions.json").get("mappings", {})


def load_profile_definition(name: str, config_root: str | Path = "config/scoring") -> ProfileDefinition:
    path = Path(config_root) / f"{name}.json"
    if not path.exists():
        raise RegistryError(f"PROFILE_NOT_CONFIGURED: {name}")
    payload = _load_json(path)
    pillar_weights = payload.get("pillar_weights", {})
    if abs(sum(float(value) for value in pillar_weights.values()) - 1.0) > 1e-9:
        raise RegistryError(f"Profile {name} pillar weights must sum to 1.0")
    definitions = load_factor_definitions(config_root)
    requested = payload.get("factor_keys")
    factors = [definitions[key] for key in requested] if requested else list(definitions.values())
    for factor in factors:
        if factor.pillar not in pillar_weights:
            raise RegistryError(f"Factor {factor.factor_key} has no pillar weight")
    return ProfileDefinition(
        profile=payload["profile"],
        profile_version=payload["profile_version"],
        scoring_version=payload.get("scoring_version", "qualitative-scoring-v1"),
        factor_rules_version=payload["factor_rules_version"],
        coverage_version=payload.get("coverage_version", "coverage-v1"),
        pillar_weights=pillar_weights,
        factors=factors,
        minimum_overall_coverage=float(payload.get("minimum_overall_coverage", payload.get("minimum_overall_scoring_coverage", 0.0))),
        minimum_pillar_coverage=payload.get("minimum_pillar_coverage", payload.get("minimum_pillar_scoring_coverage", {})),
        critical_factor_keys=payload.get("critical_factor_keys", []),
        dependency_groups=payload.get("dependency_groups", {}),
        created_date=payload.get("created_date", ""),
        calibration_status=payload.get("calibration_status", ""),
        description=payload.get("description", ""),
        minimum_overall_scoring_coverage=float(payload.get("minimum_overall_scoring_coverage", payload.get("minimum_overall_coverage", 0.0))),
        minimum_pillar_scoring_coverage=payload.get("minimum_pillar_scoring_coverage", payload.get("minimum_pillar_coverage", {})),
    )
