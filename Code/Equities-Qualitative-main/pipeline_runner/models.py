from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StageStatus(str, Enum):
    SUCCESS = "SUCCESS"
    REUSED = "REUSED"
    SKIPPED = "SKIPPED"
    NOT_RUN = "NOT_RUN"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass
class AcquisitionResult:
    """Explicit handoff from acquisition to the rest of one pipeline run."""

    ticker: str
    status: str
    download_root: str = ""
    company_acquisition_root: str = ""
    manifest_paths: list[str] = field(default_factory=list)
    provenance_paths: list[str] = field(default_factory=list)
    news_paths: list[str] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)
    event_paths: list[str] = field(default_factory=list)
    transcript_paths: list[str] = field(default_factory=list)
    record_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def eligible_artifacts(self) -> int:
        return int(self.record_counts.get("eligible_source_artifacts", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "status": self.status,
            "download_root": self.download_root,
            "company_acquisition_root": self.company_acquisition_root,
            "manifest_paths": list(self.manifest_paths),
            "provenance_paths": list(self.provenance_paths),
            "news_paths": list(self.news_paths),
            "report_paths": list(self.report_paths),
            "event_paths": list(self.event_paths),
            "transcript_paths": list(self.transcript_paths),
            "record_counts": dict(self.record_counts),
            "eligible_source_artifacts": self.eligible_artifacts,
            "warnings": list(self.warnings),
        }


@dataclass
class StageResult:
    name: str
    status: StageStatus
    record_counts: dict[str, int] = field(default_factory=dict)
    input_paths: list[str] = field(default_factory=list)
    output_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0
    message: str = ""
    change: str = "REUSED"
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "record_counts": dict(self.record_counts),
            "input_paths": list(self.input_paths),
            "output_paths": list(self.output_paths),
            "warnings": list(self.warnings),
            "versions": dict(self.versions),
            "duration_seconds": round(float(self.duration_seconds), 3),
            "message": self.message,
            "change": self.change,
            "provenance": dict(self.provenance),
        }


@dataclass
class PipelineRunResult:
    ticker: str
    as_of_date: str
    profile: str
    profile_version: str
    mode: str
    stages: list[StageResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest_path: str = ""
    dossier_json: str = ""
    dossier_markdown: str = ""
    final_status: StageStatus = StageStatus.SUCCESS
    duration_seconds: float = 0.0
    production_root: str = ""
    batch_manifest_path: str = ""
    exit_code: int = 0
    failure_stage: str = ""
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of_date": self.as_of_date,
            "profile": self.profile,
            "profile_version": self.profile_version,
            "mode": self.mode,
            "final_status": self.final_status.value,
            "stages": [stage.to_dict() for stage in self.stages],
            "warnings": list(self.warnings),
            "manifest_path": self.manifest_path,
            "dossier_paths": {"json": self.dossier_json or None, "markdown": self.dossier_markdown or None},
            "duration_seconds": round(float(self.duration_seconds), 3),
            "production_root": self.production_root,
            "batch_manifest_path": self.batch_manifest_path,
            "exit_code": self.exit_code,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
        }
