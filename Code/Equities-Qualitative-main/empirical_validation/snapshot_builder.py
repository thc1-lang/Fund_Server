"""Compatibility facade for point-in-time snapshot construction."""

from .observation_builder import build_observations, deduplicate_observations

__all__ = ["build_observations", "deduplicate_observations"]
