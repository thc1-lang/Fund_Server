"""Compatibility facade for the Component 5B ownership-baseline API."""

from .proxy_baseline import (
    ProxyBaselineProvider,
    ProxyBaselineResult,
    enrich_people_from_baselines,
    parse_proxy_html,
    reconcile_baseline,
)

__all__ = [
    "ProxyBaselineProvider",
    "ProxyBaselineResult",
    "parse_proxy_html",
    "enrich_people_from_baselines",
    "reconcile_baseline",
]
