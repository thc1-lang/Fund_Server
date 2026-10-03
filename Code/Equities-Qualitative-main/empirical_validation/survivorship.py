from __future__ import annotations

from .models import DataAudit


def survivorship_status() -> str:
    return "SURVIVORSHIP_COVERAGE_INCOMPLETE"


def corporate_action_status() -> str:
    return "CORPORATE_ACTION_COVERAGE_INCOMPLETE"
