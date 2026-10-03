from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .models import HistoricalObservation, LeakageAudit, LeakageFinding, ForwardOutcome
from .point_in_time import parse_date


class LeakageAuditor:
    def audit(self, observations: Iterable[HistoricalObservation], outcomes: Iterable[ForwardOutcome] = ()) -> LeakageAudit:
        obs = list(observations); outcome_by_id = {}
        for outcome in outcomes: outcome_by_id.setdefault(outcome.observation_id, []).append(outcome)
        findings: list[LeakageFinding] = []
        for row in obs:
            if not row.point_in_time_valid:
                findings.append(LeakageFinding(row.observation_id, "FAIL", "observation_valid", row.exclusion_reason or "point-in-time observation is invalid"))
            if row.as_of_date != row.information_cutoff:
                findings.append(LeakageFinding(row.observation_id, "FAIL", "cutoff_alignment", "observation date and information cutoff differ"))
            if row.source_availability_unknown:
                findings.append(LeakageFinding(row.observation_id, "UNKNOWN", "source_timestamp", "one or more source timestamps are unavailable"))
            for outcome in outcome_by_id.get(row.observation_id, []):
                if outcome.execution_date and outcome.execution_date <= row.information_cutoff:
                    findings.append(LeakageFinding(row.observation_id, "FAIL", "outcome_after_cutoff", "outcome execution begins on or before the information cutoff", outcome.execution_date))
        failed = sum(1 for f in findings if f.status == "FAIL")
        unknown = sum(1 for f in findings if f.status == "UNKNOWN")
        status = "FAIL" if failed else "UNKNOWN" if unknown else "PASS"
        return LeakageAudit(status, findings, len(obs), failed, unknown, synthetic_detection_passed=False)


def audit_rows(rows: Iterable[dict[str, Any]], cutoff: str) -> list[LeakageFinding]:
    """Low-level helper used by synthetic publication-date tests."""
    out=[]; cutoff_date=parse_date(cutoff)
    for i,row in enumerate(rows):
        for key in ("publication_date", "filing_date", "event_date"):
            value=parse_date(row.get(key))
            if value and cutoff_date and value > cutoff_date:
                out.append(LeakageFinding(str(row.get("id", i)), "FAIL", key, f"{key} is after cutoff", str(value)))
    return out
