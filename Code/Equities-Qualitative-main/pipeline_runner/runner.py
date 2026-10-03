from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from .models import AcquisitionResult, PipelineRunResult, StageResult, StageStatus
from .issuer_resolution import IssuerMetadata, IssuerResolutionError, resolve_issuer_metadata


MODULES = {
    "acquisition": "qualitative_ir_downloader.main",
    "qualitative": "qualitative_analysis.main",
    "insider": "insider_intelligence.main",
    "management": "management_intelligence.main",
    "track_record": "management_intelligence.track_record_cli",
    "governance": "management_intelligence.governance_cli",
    "scoring": "scoring_engine.main",
    "dossier": "research_output.main",
    "validation": "empirical_validation.main",
}
STAGE_ORDER = (
    "acquisition", "normalization", "qualitative_extraction", "temporal_comparison",
    "state_synthesis", "insider_intelligence", "management_intelligence",
    "management_track_record", "governance_intelligence", "scoring", "dossier_generation",
)
EXIT_SUCCESS = 0
EXIT_UNEXPECTED_FAILURE = 1
EXIT_ACQUISITION_FAILURE = 2
EXIT_NORMALIZATION_FAILURE = 3
EXIT_INVALID_CONFIGURATION = 4
STAGE_NUMBERS = {name: index for index, name in enumerate(STAGE_ORDER, start=1)}
DEFAULT_DATA_ROOT = Path(r"C:\Fund_Server\Data\Qual_Data")
DEFAULT_ACQUISITION_CACHE = DEFAULT_DATA_ROOT / "_Acquisition_Cache"
DEFAULT_ARTIFACTS_ROOT = DEFAULT_DATA_ROOT / "_Pipeline_Artifacts"
DEFAULT_OUTPUT_ROOT = DEFAULT_DATA_ROOT / "_Pipeline_Output"
FOLDER_NAMES = {
    "acquisition": "01_Acquisition",
    "documents": "02_Normalized_Documents",
    "claims": "03_Claims",
    "temporal": "04_Temporal_Analysis",
    "states": "05_Qualitative_State",
    "insider": "06_Insider_Intelligence",
    "management": "07_Management_Intelligence",
    "governance": "08_Board_Governance",
    "scoring": "09_Scoring",
    "dossier": "10_Research_Dossier",
    "summaries": "11_Run_Summaries",
}


def _run_module(module: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a component while draining both pipes so long acquisitions cannot deadlock."""
    command = [sys.executable, "-m", module, *args]
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout: list[str] = []
    stderr: list[str] = []

    def drain(stream, target: list[str]) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            target.append(line)
        stream.close()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    return_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    return subprocess.CompletedProcess(command, return_code, "".join(stdout), "".join(stderr))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _ticker_match(row: dict[str, Any], ticker: str) -> bool:
    ticker = ticker.upper()
    for key in ("ticker", "issuer_ticker", "board_ticker", "employer_ticker", "target_ticker"):
        if str(row.get(key) or "").upper() == ticker:
            return True
    # Some historical stores identify an issuer only through a nested company object.
    for value in row.values():
        if isinstance(value, dict) and _ticker_match(value, ticker):
            return True
    return False


def _rows(root: Path, relative: str, ticker: str) -> list[dict[str, Any]]:
    return [row for row in _read_jsonl(root / relative) if _ticker_match(row, ticker)]


def _count(root: Path, relative: str, ticker: str) -> int:
    return len(_rows(root, relative, ticker))


def _git_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], text=True, capture_output=True, check=False)
        return result.stdout.strip() or "UNKNOWN"
    except OSError:
        return "UNKNOWN"


class PipelineRunner:
    """Run existing component entry points without owning their analytical logic."""

    def __init__(
        self,
        ticker: str,
        as_of_date: str,
        profile: str = "core_v1",
        mode: str = "existing",
        skip_acquisition: bool = False,
        run_validation_audit: bool = False,
        artifacts_root: str | Path | None = None,
        download_root: str | Path | None = None,
        config_root: str | Path = "config/scoring",
        output_root: str | Path | None = None,
        validation_config: str | Path = "config/calibration/validation_v1.json",
        data_root: str | Path = DEFAULT_DATA_ROOT,
        clean_output: bool = False,
        executor: Callable[[str, list[str]], Any] | None = None,
        verbose: bool = False,
        manual_ticker: bool = False,
    ) -> None:
        if mode not in {"existing", "fresh"}:
            raise ValueError("mode must be 'existing' or 'fresh'")
        self.ticker = ticker.upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,15}", self.ticker):
            raise ValueError("ticker must be a simple symbol without path separators")
        self.as_of_date = as_of_date
        self.profile = profile
        self.mode = mode
        self.skip_acquisition = skip_acquisition
        self.run_validation_audit = run_validation_audit
        self.data_root = Path(data_root)
        self.artifacts_root = Path(artifacts_root) if artifacts_root else self.data_root / "_Pipeline_Artifacts"
        self.download_root = Path(download_root) if download_root else self.data_root / "_Acquisition_Cache"
        self.config_root = Path(config_root)
        self.output_root = Path(output_root) if output_root else self.data_root / "_Pipeline_Output"
        self.validation_config = Path(validation_config)
        self.clean_output = clean_output
        self.verbose = verbose
        # The CLI sets this for an explicit --ticker. Batch --tickers runs
        # retain worksheet selection semantics in the acquisition component.
        self.manual_ticker = bool(manual_ticker)
        self.ticker_root = self.data_root / self.ticker
        self.production_dossier_root = self.ticker_root / FOLDER_NAMES["dossier"]
        self.production_pipeline_root = self.ticker_root / FOLDER_NAMES["summaries"] / "Pipeline"
        self.production_download_root = self.ticker_root / FOLDER_NAMES["acquisition"]
        if self.mode == "fresh":
            resolved_cache = self.download_root.expanduser().resolve(strict=False)
            resolved_data_root = self.data_root.expanduser().resolve(strict=False)
            dedicated_cache = (resolved_data_root / "_Acquisition_Cache").resolve(strict=False)
            if resolved_cache == self.production_download_root.resolve(strict=False) or (resolved_data_root in resolved_cache.parents and resolved_cache != dedicated_cache):
                raise ValueError("Fresh-mode acquisition cache must be outside ticker output or in _Acquisition_Cache")
        self.executor = executor or _run_module
        self._default_executor = executor is None
        self._stages: list[StageResult] = []
        self._warnings: list[str] = []
        self._current_stage = ""
        self._active_stage = ""
        self._manifest_ready = False
        self._clean_output_status = "NOT_RUN"
        self._failure_stage = ""
        self._failure_reason = ""
        self._exit_code = EXIT_SUCCESS
        self._run_started = time.perf_counter()
        self._git_commit_value = _git_commit()
        self._acquisition_result: AcquisitionResult | None = None
        self._issuer_metadata: IssuerMetadata | None = None

    def _safe_clean_target(self) -> None:
        root = self.data_root.expanduser().resolve(strict=False)
        target = self.ticker_root.expanduser().resolve(strict=False)
        unsafe = {
            Path("C:\\").resolve(strict=False),
            Path(r"C:\NAllenAI").resolve(strict=False),
            Path(r"C:\NAllenAI\Data").resolve(strict=False),
            root,
        }
        if target in unsafe or target.parent != root:
            raise ValueError(f"Refusing to clean unsafe production path: {target}")
        if self.ticker_root.is_symlink():
            raise ValueError(f"Refusing to clean symlinked production path: {self.ticker_root}")
        if self.ticker_root.exists():
            shutil.rmtree(self.ticker_root)

    def acquisition_invocation(self, issuer: IssuerMetadata | None = None) -> tuple[str, list[str]]:
        """Return the frozen acquisition CLI invocation without executing it."""
        args = [
            "--ticker", self.ticker,
            "--download-root", str(self.download_root),
        ]
        if issuer is not None:
            args.extend(["--company-name", issuer.company_name])
            if issuer.cik:
                args.extend(["--issuer-cik", issuer.cik])
        return MODULES["acquisition"], args

    def _resolve_manual_issuer(self) -> IssuerMetadata:
        if self._issuer_metadata is None:
            self._issuer_metadata = resolve_issuer_metadata(
                self.ticker,
                download_root=self.download_root,
                data_root=self.data_root,
            )
        return self._issuer_metadata

    def _stage_label(self, name: str) -> str:
        return name.replace("_", " ").upper()

    def _announce_start(self, name: str) -> None:
        self._current_stage = name
        self._active_stage = name
        position = STAGE_NUMBERS.get(name)
        if position is None:
            return
        print(f"[{position}/{len(STAGE_ORDER)}] {self._stage_label(name)} — STARTED", flush=True)
        if name == "acquisition":
            print(f"Ticker: {self.ticker}", flush=True)
            print(f"Mode: {self.mode}", flush=True)
        elif name == "normalization":
            handoff = self._acquisition_result
            if handoff is not None:
                print(f"Ticker: {self.ticker}", flush=True)
                print(f"Source root: {handoff.download_root}", flush=True)
                print(f"Manifests: {len(handoff.manifest_paths)}", flush=True)
                print(f"Eligible source artifacts: {handoff.eligible_artifacts}", flush=True)
            print(f"Production output: {self.ticker_root / FOLDER_NAMES['documents']}", flush=True)
        if self.verbose:
            if name == "normalization" and self._acquisition_result is not None:
                print(f"Input artifacts: {self._acquisition_result.download_root}")
            else:
                print(f"Input artifacts: {self.artifacts_root}")
            print(f"Production root: {self.ticker_root}")
        self._write_manifest_snapshot()

    def _announce_heartbeat(self, name: str, started: float, stop: threading.Event) -> None:
        position = STAGE_NUMBERS.get(name, 0)
        while not stop.wait(60):
            elapsed = int(time.perf_counter() - started)
            print(f"[{position}/{len(STAGE_ORDER)}] {self._stage_label(name)} still running ({elapsed}s)", flush=True)

    def _manifest_path(self) -> Path:
        return self.production_pipeline_root / f"{self.ticker}_{self.as_of_date}_pipeline_run.json"

    def _reported_dossier_paths(self, profile_version: str) -> tuple[str | None, str | None]:
        """Return only dossier files produced/reused by the current run."""
        dossier_stage = next((stage for stage in self._stages if stage.name == "dossier_generation"), None)
        if dossier_stage is None or dossier_stage.status not in {StageStatus.SUCCESS, StageStatus.REUSED}:
            return None, None
        dossier_json, dossier_md = self._dossier_paths(profile_version)
        return (
            str(dossier_json) if dossier_json.exists() else None,
            str(dossier_md) if dossier_md.exists() else None,
        )

    def _manifest_payload(self, *, final_status: StageStatus | str = "RUNNING", exit_code: int | None = None) -> dict[str, Any]:
        profile_version = self._profile_version()
        dossier_json_value, dossier_md_value = self._reported_dossier_paths(profile_version)
        payload: dict[str, Any] = {
            "ticker": self.ticker,
            "as_of_date": self.as_of_date,
            "profile": self.profile,
            "profile_version": profile_version,
            "mode": self.mode,
            "final_status": final_status.value if isinstance(final_status, StageStatus) else final_status,
            "stages": [stage.to_dict() for stage in self._stages],
            "warnings": list(dict.fromkeys(self._warnings)),
            "manifest_path": str(self._manifest_path()),
            "dossier_paths": {"json": dossier_json_value, "markdown": dossier_md_value},
            "production_root": str(self.ticker_root),
            "source_artifacts_root": str(self.artifacts_root),
            "acquisition_cache_root": str(self.download_root),
            "clean_output": self._clean_output_status,
            "validation_audit_requested": self.run_validation_audit,
            "production_layout": {key: str(self.ticker_root / folder) for key, folder in FOLDER_NAMES.items()},
            "exit_code": self._exit_code if exit_code is None else exit_code,
            "failure_stage": self._failure_stage,
            "failure_reason": self._failure_reason,
            "active_stage": self._active_stage,
            "acquisition_result": self._acquisition_result.to_dict() if self._acquisition_result else None,
            "issuer_resolution": self._issuer_metadata.to_dict() if self._issuer_metadata else None,
        }
        return payload

    def _write_manifest_snapshot(self, *, final_status: StageStatus | str = "RUNNING", exit_code: int | None = None) -> None:
        if not self._manifest_ready:
            return
        manifest = self._manifest_path()
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = self._manifest_payload(final_status=final_status, exit_code=exit_code)
        payload["git_commit"] = self._git_commit_value
        payload["versions"] = {"pipeline_runner": "pipeline-runner-v1"}
        manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _append_not_run_stages(self, after: str) -> None:
        try:
            start = STAGE_ORDER.index(after) + 1
        except ValueError:
            start = 0
        for name in STAGE_ORDER[start:]:
            if any(stage.name == name for stage in self._stages):
                continue
            self._stages.append(StageResult(name=name, status=StageStatus.NOT_RUN,
                                             warnings=[f"Not run because {after} failed"], change="REUSED"))

    def _build_result(self, final_status: StageStatus, *, exit_code: int | None = None) -> PipelineRunResult:
        profile_version = self._profile_version()
        dossier_json_value, dossier_md_value = self._reported_dossier_paths(profile_version)
        return PipelineRunResult(
            self.ticker, self.as_of_date, self.profile, profile_version, self.mode,
            list(self._stages), list(dict.fromkeys(self._warnings)), str(self._manifest_path()),
            dossier_json_value or "", dossier_md_value or "", final_status,
            time.perf_counter() - self._run_started, str(self.ticker_root), "",
            self._exit_code if exit_code is None else exit_code,
            self._failure_stage, self._failure_reason,
        )

    def _finish_failure(self, stage: str, reason: str, exit_code: int) -> PipelineRunResult:
        self._failure_stage = stage
        self._failure_reason = reason
        self._exit_code = exit_code
        self._append_not_run_stages(stage)
        if self._manifest_ready:
            try:
                self._materialize_acquisition()
                self._materialize_acquisition_summaries()
            except OSError as exc:
                self._warnings.append(f"Failed to materialize partial acquisition output: {exc}")
        self._write_manifest_snapshot(final_status=StageStatus.FAILED, exit_code=exit_code)
        return self._build_result(StageStatus.FAILED, exit_code=exit_code)

    def _report_unexpected_exception(self, exc: BaseException) -> None:
        print("PIPELINE FAILED")
        print(f"Stage: {self._current_stage or 'orchestration'}")
        print(f"Exception type: {type(exc).__name__}")
        print(f"Message: {exc}")
        if self.verbose:
            traceback.print_exc()

    def _ensure_production_layout(self) -> None:
        if self.clean_output:
            print("Cleaning production output:", flush=True)
            print(str(self.ticker_root.resolve(strict=False)), flush=True)
            print("\nPreserving acquisition/cache data outside target output:", flush=True)
            print("YES", flush=True)
            self._safe_clean_target()
            self.clean_output = False
            self._clean_output_status = "SUCCESS"
        elif self._clean_output_status == "NOT_RUN":
            self._clean_output_status = "NOT_REQUESTED"
        for key in FOLDER_NAMES:
            (self.ticker_root / FOLDER_NAMES[key]).mkdir(parents=True, exist_ok=True)
        for child in ("News_Releases", "Reports", "Events", "Transcripts"):
            (self.production_download_root / child).mkdir(parents=True, exist_ok=True)
        for child in ("Pipeline", "Production", "Excel_Analysis"):
            (self.ticker_root / FOLDER_NAMES["summaries"] / child).mkdir(parents=True, exist_ok=True)
        if self.verbose:
            print(f"Resolved acquisition cache: {self.download_root}")
            print(f"Resolved production root: {self.ticker_root}")

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
        path.write_text(content, encoding="utf-8")
        return True

    def _materialize_jsonl(self, relative: str, destination: Path) -> int:
        rows = _rows(self.artifacts_root, relative, self.ticker)
        if not rows:
            return 0
        content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        self._write_if_changed(destination, content)
        return len(rows)

    def _materialize_folder_jsonl(self, source_folder: str, destination: Path, names: tuple[str, ...]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in names:
            counts[name[:-6]] = self._materialize_jsonl(f"{source_folder}/{name}", destination / name)
        return counts

    def _materialize_acquisition(self) -> None:
        if not self.download_root.exists():
            return
        same_root = self.download_root.resolve(strict=False) == self.production_download_root.resolve(strict=False)
        category_names = {
            "News Releases": "News_Releases",
            "News_Releases": "News_Releases",
            "Reports": "Reports",
            "Events": "Events",
            "Transcripts": "Transcripts",
        }
        for candidate in self.download_root.iterdir():
            if not candidate.is_dir() or not candidate.name.upper().startswith(self.ticker + "_"):
                continue
            manifest_path = candidate / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                manifest = {}
            manifest_ticker = str(manifest.get("ticker") or "").upper()
            if manifest_ticker and manifest_ticker != self.ticker:
                continue
            target = self.production_download_root / candidate.name
            if not same_root:
                if target.exists():
                    shutil.copytree(candidate, target, dirs_exist_ok=True)
                else:
                    shutil.copytree(candidate, target)
            # Keep the downloader's resumable company folder intact, and also
            # expose canonical category views at the production root.  The
            # views are namespaced by company folder so files from separate
            # runs cannot collide.
            source = target if same_root else candidate
            for source_name, category in category_names.items():
                source_dir = source / source_name
                if source_dir.exists() and source_dir.is_dir():
                    category_target = self.production_download_root / category / candidate.name
                    shutil.copytree(source_dir, category_target, dirs_exist_ok=True)

    def _materialize_acquisition_summaries(self) -> None:
        """Keep downloader run summaries out of ``01_Acquisition`` itself."""
        destination = self.ticker_root / FOLDER_NAMES["summaries"] / "Production"
        patterns = (
            "qualitative_analysis_run_summary_*.json",
            "qualitative_analysis_run_summary_*.csv",
            "production_run_summary_*.json",
            "generated_search_summary_*.json",
        )
        for pattern in patterns:
            for source in self.production_download_root.glob(pattern):
                target = destination / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.suffix.lower() == ".json":
                    self._write_if_changed(target, source.read_text(encoding="utf-8"))
                else:
                    shutil.copy2(source, target)
                source.unlink()

    def _materialize_outputs(self) -> dict[str, Any]:
        self._ensure_production_layout()
        self._materialize_acquisition()
        self._materialize_acquisition_summaries()
        qa = self.ticker_root / FOLDER_NAMES["documents"]
        qb = self.ticker_root / FOLDER_NAMES["claims"]
        qt = self.ticker_root / FOLDER_NAMES["temporal"]
        qs = self.ticker_root / FOLDER_NAMES["states"]
        counts = {
            "documents": self._materialize_jsonl("qualitative_analysis/documents.jsonl", qa / "documents.jsonl"),
            "claims": self._materialize_jsonl("qualitative_analysis/claims.jsonl", qb / "claims.jsonl"),
            "temporal_changes": self._materialize_jsonl("qualitative_analysis/temporal_changes.jsonl", qt / "temporal_changes.jsonl"),
            "states": self._materialize_jsonl("qualitative_analysis/qualitative_states.jsonl", qs / "qualitative_states.jsonl"),
        }
        for key, folder, names in (
            ("insider", FOLDER_NAMES["insider"], ("people.jsonl", "transactions.jsonl", "ownership.jsonl", "filings.jsonl", "ownership_baselines.jsonl", "ownership_reconciliations.jsonl", "alignment_snapshots.jsonl", "clusters.jsonl", "insider_links.jsonl")),
            ("management", FOLDER_NAMES["management"], ("people.jsonl", "roles.jsonl", "career_history.jsonl", "education.jsonl", "sources.jsonl", "relationships.jsonl", "role_changes.jsonl", "runs.jsonl", "tenures.jsonl", "financial_facts.jsonl", "guidance_commitments.jsonl", "guidance_outcomes.jsonl", "guidance_coverage_diagnostics.jsonl", "strategic_commitments.jsonl", "strategic_outcomes.jsonl", "capital_allocation_events.jsonl", "operating_outcomes.jsonl", "track_record_snapshots.jsonl", "track_record_runs.jsonl")),
            ("governance", FOLDER_NAMES["governance"], ("board_history.jsonl", "governance_board_memberships.jsonl", "governance_committees.jsonl", "governance_snapshots.jsonl", "governance_coverage.jsonl", "governance_events.jsonl", "governance_expertise.jsonl", "governance_overboarding.jsonl", "governance_refreshment.jsonl", "governance_related_parties.jsonl", "governance_shareholder_rights.jsonl", "governance_voting_classes.jsonl", "governance_voting_control.jsonl", "governance_runs.jsonl", "governance_tenure.jsonl")),
        ):
            counts[key] = self._materialize_folder_jsonl("insider_intelligence" if key == "insider" else "management_intelligence", self.ticker_root / folder, names)
        scoring_dir = self.ticker_root / FOLDER_NAMES["scoring"]
        counts["scoring"] = {}
        for name in ("factor_scores.jsonl", "pillar_scores.jsonl", "company_scores.jsonl", "score_runs.jsonl"):
            counts["scoring"][name[:-6]] = self._materialize_jsonl(f"scoring_engine/{name}", scoring_dir / name)
        source_json, source_md = self._source_dossier_paths(self._profile_version())
        target_json = self.production_dossier_root / source_json.name
        target_md = self.production_dossier_root / source_md.name
        for source, target in ((source_json, target_json), (source_md, target_md)):
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for key, filename in (("documents", "document_index.json"), ("claims", "claim_index.json"), ("temporal_changes", "temporal_index.json"), ("states", "state_index.json")):
            folder = {"documents": qa, "claims": qb, "temporal_changes": qt, "states": qs}[key]
            source_name = {"documents": "documents.jsonl", "claims": "claims.jsonl", "temporal_changes": "temporal_changes.jsonl", "states": "qualitative_states.jsonl"}[key]
            index = {"ticker": self.ticker, "count": counts[key], "source": str(folder / source_name)}
            (folder / filename).write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        return {"counts": counts, "dossier_json": str(target_json), "dossier_markdown": str(target_md)}

    @staticmethod
    def _record_artifact_path(company_root: Path, record: dict[str, Any]) -> Path | None:
        """Resolve a downloader record without searching outside its company folder."""
        for key in ("local_filename", "local_pdf", "local_json", "retained_media"):
            value = record.get(key)
            if not value:
                continue
            path = Path(str(value))
            if not path.is_absolute():
                path = company_root / path
            if path.exists() and path.is_file():
                return path
        return None

    def _company_manifest_dirs(self) -> list[Path]:
        root = self.download_root
        if not root.exists() or not root.is_dir():
            return []
        candidates = [root] if (root / "manifest.json").exists() else [
            path for path in root.iterdir() if path.is_dir() and (path / "manifest.json").exists()
        ]
        selected: list[tuple[Path, dict[str, Any]]] = []
        for path in candidates:
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            manifest_ticker = str(manifest.get("ticker") or "").upper()
            if manifest_ticker == self.ticker:
                selected.append((path, manifest))
        selected.sort(key=lambda item: (str(item[1].get("run_timestamp") or ""), item[0].stat().st_mtime, item[0].name), reverse=True)
        return [path for path, _ in selected]

    def _resolve_acquisition_result(self, *, allow_local_fallback: bool | None = None) -> AcquisitionResult:
        """Resolve the current ticker's acquisition handoff from manifests.

        The downloader owns cache layout and semantics.  The runner only reads
        its manifests and uses the parent cache root with an explicit ticker
        selector for normalization; it never recursively ingests the whole
        external download directory.
        """
        if allow_local_fallback is None:
            # Fresh acquisition must be grounded in the cache/manifests just
            # resolved by acquisition.  Existing mode and an explicit
            # acquisition skip may intentionally operate on an established
            # local store.
            allow_local_fallback = self.mode != "fresh" or self.skip_acquisition
        company_dirs = self._company_manifest_dirs()
        if not company_dirs:
            local_counts = self._qual_counts()
            if allow_local_fallback and local_counts.get("documents", 0):
                return AcquisitionResult(
                    ticker=self.ticker,
                    status="LOCAL_ARTIFACTS",
                    download_root=str(self.artifacts_root),
                    company_acquisition_root=str(self.artifacts_root),
                    record_counts={**local_counts, "eligible_source_artifacts": local_counts["documents"]},
                    warnings=["No downloader manifest was found; using explicitly supplied local artifacts"],
                )
            return AcquisitionResult(
                ticker=self.ticker,
                status="NO_SOURCE",
                download_root=str(self.download_root),
                record_counts={"eligible_source_artifacts": 0},
            )

        manifest_paths = [path / "manifest.json" for path in company_dirs]
        news_paths: list[str] = []
        report_paths: list[str] = []
        event_paths: list[str] = []
        transcript_paths: list[str] = []
        news_records = reports_records = event_records = 0
        reusable = 0
        saved = 0
        warnings: list[str] = []
        seen_records = {"news": set(), "reports": set(), "events": set()}
        for company_dir in company_dirs:
            try:
                manifest = json.loads((company_dir / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            for kind, target in (("news", news_paths), ("reports", report_paths), ("events", event_paths)):
                records = manifest.get(kind) or []
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    record_key = str(record.get("source_url") or record.get("event_url") or record.get("official_ir_url") or "")
                    if not record_key:
                        record_key = str(record.get("local_filename") or record.get("local_json") or record.get("title") or "")
                    if record_key in seen_records[kind]:
                        continue
                    seen_records[kind].add(record_key)
                    path = self._record_artifact_path(company_dir, record)
                    # HTML/description records are still valid normalization
                    # inputs even when no separate local file was required.
                    eligible = path is not None or bool(record.get("content_html") or record.get("description"))
                    if kind == "news":
                        news_records += int(eligible)
                    elif kind == "reports":
                        reports_records += int(eligible)
                    else:
                        # Events are discovery records even when replay media is
                        # unavailable; only locally materialized media is an
                        # eligible normalization artifact.
                        event_records += 1
                    if path is not None:
                        value = str(path)
                        if value not in target:
                            target.append(value)
                        if kind == "events" and (path.suffix.lower() == ".json" or record.get("transcript_method") or record.get("method") in {"official_transcript", "generated_transcript"}):
                            if value not in transcript_paths:
                                transcript_paths.append(value)
                    if record.get("cache_hit") or record.get("existing_artifact"):
                        reusable += 1
                    elif eligible:
                        saved += 1
            status = str(manifest.get("overall_status") or manifest.get("status") or "UNKNOWN")
            if status == "SUCCESS_WITH_LIMITATIONS":
                completeness = manifest.get("completeness") or {}
                warnings.extend(str(item) for item in completeness.get("limitations", []) if item)
                event_status = manifest.get("events_status")
                if event_status and event_status not in {"SUCCESS", "NOT_APPLICABLE"}:
                    warnings.append(f"events: {event_status}")
        dedup_warnings = list(dict.fromkeys(warnings))
        latest_manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
        status = str(latest_manifest.get("overall_status") or latest_manifest.get("status") or "UNKNOWN")
        cache_metrics = latest_manifest.get("cache_metrics") or {}
        latest_events = latest_manifest.get("events") or []
        if isinstance(latest_events, list):
            event_records = len(latest_events)
        if cache_metrics.get("artifact_cache_hits") is not None:
            reusable = int(cache_metrics.get("artifact_cache_hits") or 0)
        return AcquisitionResult(
            ticker=self.ticker,
            status=status,
            download_root=str(self.download_root),
            company_acquisition_root=str(company_dirs[0]),
            manifest_paths=[str(path) for path in manifest_paths],
            provenance_paths=[str(path) for path in manifest_paths],
            news_paths=news_paths,
            report_paths=report_paths,
            event_paths=event_paths,
            transcript_paths=transcript_paths,
            record_counts={
                "news": news_records,
                "reports": reports_records,
                "events": event_records,
                "transcripts": len(transcript_paths),
                "manifests": len(manifest_paths),
                "reused": reusable,
                "saved": saved,
                "eligible_source_artifacts": len(set(news_paths + report_paths)) + sum(
                    1 for path in event_paths if path not in set(news_paths + report_paths)
                ),
            },
            warnings=dedup_warnings,
        )

    def _invoke(self, module: str, args: list[str]) -> tuple[int, str]:
        if self.verbose:
            safe_args = " ".join(str(value) for value in args)
            print(f"Invoking {module}: python -m {module} {safe_args}")
        started = time.perf_counter()
        heartbeat_stop = threading.Event()
        heartbeat = None
        if module == MODULES["acquisition"]:
            heartbeat = threading.Thread(target=self._announce_heartbeat,
                                         args=("acquisition", started, heartbeat_stop), daemon=True)
            heartbeat.start()
        try:
            result = self.executor(module, args)
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=0.2)
        if isinstance(result, int):
            return result, ""
        output = str(getattr(result, "stdout", "") or "") + str(getattr(result, "stderr", "") or "")
        if self.verbose and output:
            print(output[-2000:])
        return int(getattr(result, "returncode", 1)), output

    def _stage(self, name: str, status: StageStatus, *, counts: dict[str, int] | None = None,
               inputs: list[Path] | None = None, outputs: list[Path] | None = None,
               warnings: list[str] | None = None, message: str = "", started: float | None = None,
               change: str | None = None, provenance: dict[str, Any] | None = None) -> StageResult:
        input_values = [str(p) for p in inputs or []]
        output_values = [str(p) for p in outputs or []]
        stage_provenance = provenance or {
            "ticker": self.ticker,
            "input_paths": input_values,
            "output_paths": output_values,
        }
        result = StageResult(name=name, status=status, record_counts=counts or {},
                             input_paths=input_values, output_paths=output_values,
                             warnings=warnings or [], message=message,
                             duration_seconds=(time.perf_counter() - started) if started else 0.0,
                             change=change or ("CREATED" if status == StageStatus.SUCCESS else "REUSED"),
                             provenance=stage_provenance)
        self._stages.append(result)
        self._warnings.extend(result.warnings)
        position = STAGE_NUMBERS.get(name)
        if position is not None and status != StageStatus.NOT_RUN:
            if status == StageStatus.FAILED:
                print(f"[{position}/{len(STAGE_ORDER)}] {self._stage_label(name)} — FAILED", flush=True)
                if result.message or result.warnings:
                    print(f"Reason: {result.message or '; '.join(result.warnings)}", flush=True)
            else:
                print(f"[{position}/{len(STAGE_ORDER)}] {self._stage_label(name)} — COMPLETE", flush=True)
        if self._active_stage == name:
            self._active_stage = ""
        self._write_manifest_snapshot()
        return result

    def _qual_counts(self) -> dict[str, int]:
        return {"documents": _count(self.artifacts_root, "qualitative_analysis/documents.jsonl", self.ticker),
                "claims": _count(self.artifacts_root, "qualitative_analysis/claims.jsonl", self.ticker),
                "temporal_changes": _count(self.artifacts_root, "qualitative_analysis/temporal_changes.jsonl", self.ticker),
                "states": _count(self.artifacts_root, "qualitative_analysis/qualitative_states.jsonl", self.ticker)}

    def _optional_counts(self) -> dict[str, dict[str, int]]:
        return {
            "insider_intelligence": {name[:-6]: _count(self.artifacts_root, f"insider_intelligence/{name}", self.ticker)
                                     for name in ("people.jsonl", "transactions.jsonl", "ownership.jsonl")},
            "management_intelligence": {name[:-6]: _count(self.artifacts_root, f"management_intelligence/{name}", self.ticker)
                                         for name in ("people.jsonl", "roles.jsonl")},
            "management_track_record": {name[:-6]: _count(self.artifacts_root, f"management_intelligence/{name}", self.ticker)
                                         for name in ("track_record_snapshots.jsonl", "guidance_commitments.jsonl", "strategic_commitments.jsonl", "operating_outcomes.jsonl")},
            "governance_intelligence": {name[:-6]: _count(self.artifacts_root, f"management_intelligence/{name}", self.ticker)
                                         for name in ("board_history.jsonl", "governance_committees.jsonl", "governance_snapshots.jsonl")},
        }

    def _qual_args(self, *extra: str) -> list[str]:
        # The frozen qualitative layer reads the downloader's cache.  Keep
        # that cache outside the cleanable production tree in every mode;
        # outputs are materialized into the numbered production folders later.
        return ["--ticker", self.ticker, "--download-root", str(self.download_root),
                "--store-root", str(self.artifacts_root / "qualitative_analysis"),
                "--as-of-date", self.as_of_date, *extra]

    def _run_qualitative(self, name: str, extra: list[str], before: dict[str, int], key: str,
                         *, source_root: Path | None = None,
                         provenance: dict[str, Any] | None = None) -> StageResult:
        started = time.perf_counter()
        self._announce_start(name)
        code, output = self._invoke(MODULES["qualitative"], self._qual_args(*extra))
        after = self._qual_counts()
        if code != 0:
            return self._stage(name, StageStatus.FAILED, counts=after, warnings=[f"{name} command failed"], message=output[-500:], started=started, change="UPDATED")
        changed = after.get(key, 0) != before.get(key, 0)
        return self._stage(name, StageStatus.SUCCESS if changed else StageStatus.REUSED, counts=after,
                           inputs=[source_root or (self.artifacts_root / "qualitative_analysis")],
                           outputs=[self.artifacts_root / "qualitative_analysis"],
                           provenance=provenance,
                           message=output[-500:], started=started, change="UPDATED" if changed else "REUSED")

    def _profile_version(self) -> str:
        rows = _rows(self.artifacts_root, "scoring_engine/company_scores.jsonl", self.ticker)
        versions = [str(row["profile_version"]) for row in rows if row.get("profile_version")]
        if versions:
            # Stores are append-only and may contain earlier component versions.
            # Select the greatest numeric version so dossier paths follow the
            # current scoring materialization rather than file order.
            def version_key(value: str) -> tuple[int, ...]:
                return tuple(int(part) for part in re.findall(r"\d+", value))
            return max(versions, key=version_key)
        return "core-v1.2" if self.profile == "core_v1" else self.profile

    def _run_optional_fresh(self, name: str, module_key: str, args: list[str], counts: dict[str, int]) -> StageResult:
        started = time.perf_counter()
        self._announce_start(name)
        code, output = self._invoke(MODULES[module_key], args)
        after = self._optional_counts().get(name, counts)
        if code == 0:
            return self._stage(name, StageStatus.SUCCESS if sum(after.values()) else StageStatus.PARTIAL,
                                counts=after, inputs=[self.artifacts_root], warnings=[] if sum(after.values()) else [f"{name.replace('_', ' ').title()} completed without records"],
                                message=output[-500:], started=started, change="UPDATED")
        return self._stage(name, StageStatus.PARTIAL, counts=after,
                           warnings=[f"{name.replace('_', ' ').title()} command was unavailable or failed"],
                           message=output[-500:], started=started, change="UPDATED")

    def _source_dossier_paths(self, profile_version: str) -> tuple[Path, Path]:
        directory = self.output_root / self.ticker
        stem = f"{self.ticker}_{self.as_of_date}_{profile_version}_dossier"
        return directory / f"{stem}.json", directory / f"{stem}.md"

    def _dossier_paths(self, profile_version: str) -> tuple[Path, Path]:
        source_json, source_md = self._source_dossier_paths(profile_version)
        return self.production_dossier_root / source_json.name, self.production_dossier_root / source_md.name

    def _run(self) -> PipelineRunResult:
        self._ensure_production_layout()
        self._manifest_ready = True
        self._write_manifest_snapshot()
        # Acquisition is the only stage that is intentionally allowed to touch the network.
        started = time.perf_counter()
        self._announce_start("acquisition")
        if self.skip_acquisition:
            self._acquisition_result = self._resolve_acquisition_result(allow_local_fallback=True)
            self._stage("acquisition", StageStatus.SKIPPED, counts=dict(self._acquisition_result.record_counts),
                        inputs=[Path(self._acquisition_result.download_root)],
                        warnings=["Acquisition explicitly skipped; using local artifacts"], started=started, change="REUSED",
                        provenance=self._acquisition_result.to_dict())
        elif self.mode == "existing":
            self._acquisition_result = self._resolve_acquisition_result(allow_local_fallback=True)
            self._stage("acquisition", StageStatus.REUSED, counts={**self._acquisition_result.record_counts, "local_artifacts": int(self.download_root.exists())},
                        inputs=[self.download_root], warnings=["Existing mode does not invoke acquisition"], started=started, change="REUSED",
                        provenance=self._acquisition_result.to_dict())
        else:
            issuer = None
            if self.manual_ticker:
                try:
                    issuer = self._resolve_manual_issuer()
                except IssuerResolutionError as exc:
                    reason = str(exc)
                    self._stage("acquisition", StageStatus.FAILED, inputs=[self.download_root],
                                warnings=["ISSUER_RESOLUTION_FAILED"], message=reason,
                                started=started, change="UPDATED")
                    return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)
            acquisition_module, acquisition_args = self.acquisition_invocation(issuer)
            self.download_root.mkdir(parents=True, exist_ok=True)
            try:
                code, output = self._invoke(acquisition_module, acquisition_args)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._stage("acquisition", StageStatus.FAILED, inputs=[self.download_root],
                            warnings=["Acquisition invocation raised an exception"], message=reason,
                            started=started, change="UPDATED")
                return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)
            self._acquisition_result = self._resolve_acquisition_result(allow_local_fallback=False)
            acquisition_warnings = list(self._acquisition_result.warnings)
            self._stage("acquisition", StageStatus.SUCCESS if code == 0 else StageStatus.FAILED,
                        inputs=[self.download_root, Path(self._acquisition_result.company_acquisition_root)] if self._acquisition_result.company_acquisition_root else [self.download_root],
                        counts=dict(self._acquisition_result.record_counts),
                        warnings=acquisition_warnings if code == 0 else ["Acquisition command failed"],
                        provenance=self._acquisition_result.to_dict(),
                        message=output[-500:], started=started, change="UPDATED")
            if code != 0:
                reason = output[-1000:] or f"Acquisition exited with return code {code}"
                return self._finish_failure("acquisition", reason, EXIT_ACQUISITION_FAILURE)

        counts = self._qual_counts()
        handoff = self._acquisition_result or self._resolve_acquisition_result()
        eligible_inputs = handoff.eligible_artifacts
        # A fresh run is admitted only by the current acquisition handoff.
        # Existing normalized rows in the generic store cannot substitute for
        # a missing current cache, unless acquisition was explicitly skipped.
        can_normalize = (
            eligible_inputs > 0
            if self.mode == "fresh" and not self.skip_acquisition
            else counts["documents"] > 0 or eligible_inputs > 0
        )
        if can_normalize:
            if self.mode == "existing":
                self._stage("normalization", StageStatus.REUSED, counts=counts,
                            inputs=[self.artifacts_root / "qualitative_analysis"],
                            outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED")
            else:
                normalization = self._run_qualitative(
                    "normalization", ["--replace-ticker"], counts, "documents",
                    source_root=Path(handoff.download_root),
                    provenance={
                        "input_root": handoff.download_root,
                        "company_acquisition_root": handoff.company_acquisition_root,
                        "input_manifests": list(handoff.manifest_paths),
                        "provenance_paths": list(handoff.provenance_paths),
                        "eligible_source_artifacts": eligible_inputs,
                        "output_root": str(self.artifacts_root / "qualitative_analysis"),
                        "ticker": self.ticker,
                    },
                )
                if normalization.status == StageStatus.FAILED:
                    reason = normalization.message or "NORMALIZATION_FAILED"
                    return self._finish_failure("normalization", reason, EXIT_NORMALIZATION_FAILURE)
        else:
            if self.verbose:
                print(f"Eligible acquisition artifacts: {eligible_inputs}", flush=True)
                print(f"Resolved source root: {handoff.download_root}", flush=True)
                print(f"Expected manifests: {len(handoff.manifest_paths)}", flush=True)
            self._stage("normalization", StageStatus.FAILED, counts=counts,
                        inputs=[Path(handoff.download_root)],
                        warnings=["No normalized documents are available for this ticker"],
                        message="MISSING_NORMALIZED_INPUTS", change="UPDATED",
                        provenance={
                            "input_root": handoff.download_root,
                            "input_manifests": list(handoff.manifest_paths),
                            "eligible_source_artifacts": eligible_inputs,
                            "expected_manifests": len(handoff.manifest_paths),
                            "ticker": self.ticker,
                        })
            return self._finish_failure("normalization", "MISSING_NORMALIZED_INPUTS", EXIT_NORMALIZATION_FAILURE)

        counts = self._qual_counts()
        if counts["documents"]:
            if self.mode == "existing":
                extraction = self._stage("qualitative_extraction", StageStatus.REUSED, counts=counts,
                                         inputs=[self.artifacts_root / "qualitative_analysis"],
                                         outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED")
            else:
                extraction = self._run_qualitative("qualitative_extraction", ["--extract"], counts, "claims")
        else:
            extraction = self._stage("qualitative_extraction", StageStatus.SKIPPED, counts=counts,
                                     warnings=["Skipped because normalization has no documents"], change="REUSED")
        counts = self._qual_counts()
        if counts["claims"] and extraction.status not in {StageStatus.FAILED, StageStatus.SKIPPED}:
            if self.mode == "existing":
                temporal = self._stage("temporal_comparison", StageStatus.REUSED, counts=counts,
                                       inputs=[self.artifacts_root / "qualitative_analysis"],
                                       outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED")
            else:
                temporal = self._run_qualitative("temporal_comparison", ["--compare", "--latest-vs-previous"], counts, "temporal_changes")
        else:
            temporal = self._stage("temporal_comparison", StageStatus.SKIPPED, counts=counts,
                                   warnings=["Skipped because qualitative claims are unavailable"], change="REUSED")
        counts = self._qual_counts()
        if counts["claims"] and temporal.status not in {StageStatus.FAILED, StageStatus.SKIPPED}:
            if self.mode == "existing":
                self._stage("state_synthesis", StageStatus.REUSED, counts=counts,
                            inputs=[self.artifacts_root / "qualitative_analysis"],
                            outputs=[self.artifacts_root / "qualitative_analysis"], change="REUSED")
            else:
                self._run_qualitative("state_synthesis", ["--state"], counts, "states")
        else:
            self._stage("state_synthesis", StageStatus.SKIPPED, counts=counts,
                        warnings=["Skipped because temporal comparison has no usable claims"], change="REUSED")

        optional = self._optional_counts()
        for name, values in optional.items():
            started = time.perf_counter()
            total = sum(values.values())
            if self.mode == "fresh":
                module_key = {"insider_intelligence": "insider", "management_intelligence": "management",
                              "management_track_record": "track_record", "governance_intelligence": "governance"}[name]
                optional_args = {
                    "insider_intelligence": ["--ticker", self.ticker, "--store-root", str(self.artifacts_root / "insider_intelligence")],
                    "management_intelligence": [self.ticker, "--as-of-date", self.as_of_date, "--store-root", str(self.artifacts_root / "management_intelligence"), "--normalized-root", str(self.artifacts_root / "qualitative_analysis")],
                    "management_track_record": [self.ticker, "--as-of-date", self.as_of_date, "--store-root", str(self.artifacts_root / "management_intelligence"), "--qualitative-root", str(self.artifacts_root / "qualitative_analysis")],
                    "governance_intelligence": [self.ticker, "--as-of-date", self.as_of_date, "--store-root", str(self.artifacts_root / "management_intelligence"), "--normalized-root", str(self.artifacts_root / "qualitative_analysis")],
                }[name]
                self._run_optional_fresh(name, module_key, optional_args, values)
            elif total:
                self._stage(name, StageStatus.REUSED, counts=values, inputs=[self.artifacts_root], started=started, change="REUSED")
            else:
                self._stage(name, StageStatus.PARTIAL, counts=values,
                            warnings=[f"No cached {name.replace('_', ' ')} records for {self.ticker}"], started=started, change="REUSED")

        started = time.perf_counter()
        self._announce_start("scoring")
        score_before = _rows(self.artifacts_root, "scoring_engine/company_scores.jsonl", self.ticker)
        score_args = ["--ticker", self.ticker, "--profile", self.profile, "--as-of-date", self.as_of_date,
                      "--artifacts-root", str(self.artifacts_root), "--config-root", str(self.config_root),
                      "--output-root", str(self.artifacts_root / "scoring_engine")]
        code, output = self._invoke(MODULES["scoring"], score_args)
        profile_version = self._profile_version()
        score_rows = _rows(self.artifacts_root, "scoring_engine/company_scores.jsonl", self.ticker)
        score_warnings = []
        if "INSUFFICIENT_SCORING_COVERAGE" in output:
            score_warnings.append("Overall score is unavailable because scoring coverage is insufficient; this is a valid result")
        score_stage = self._stage("scoring", StageStatus.SUCCESS if code == 0 else StageStatus.FAILED,
                                  counts={"company_scores": len(score_rows)}, outputs=[self.artifacts_root / "scoring_engine"],
                                  warnings=score_warnings if code == 0 else ["Scoring command failed"], message=output[-500:], started=started,
                                  change="REUSED" if score_before and code == 0 else ("CREATED" if code == 0 else "REUSED"))

        source_dossier_json, source_dossier_md = self._source_dossier_paths(profile_version)
        dossier_json, dossier_md = self._dossier_paths(profile_version)
        started = time.perf_counter()
        dossier_stage = None
        if score_stage.status != StageStatus.FAILED:
            self._announce_start("dossier_generation")
            existed_before = source_dossier_json.exists() and source_dossier_md.exists()
            code, output = self._invoke(MODULES["dossier"], ["--ticker", self.ticker, "--as-of-date", self.as_of_date,
                "--profile", self.profile, "--format", "both", "--detail", "standard", "--artifacts-root", str(self.artifacts_root),
                "--output-root", str(self.output_root)])
            exists = source_dossier_json.exists() and source_dossier_md.exists()
            dossier_stage = self._stage("dossier_generation", StageStatus.SUCCESS if code == 0 and exists else StageStatus.FAILED,
                                        counts={"files": int(source_dossier_json.exists()) + int(source_dossier_md.exists())},
                                        outputs=[dossier_json, dossier_md], warnings=[] if code == 0 and exists else ["Dossier generation failed"],
                                        message=output[-500:], started=started,
                                        change="REUSED" if existed_before and code == 0 and exists else ("CREATED" if code == 0 and exists else "REUSED"))
        else:
            dossier_stage = self._stage("dossier_generation", StageStatus.SKIPPED, warnings=["Skipped because scoring failed"], started=started, change="REUSED")

        if self.run_validation_audit:
            started = time.perf_counter()
            self._announce_start("validation_audit")
            code, output = self._invoke(MODULES["validation"], ["audit-data", "--config", str(self.validation_config)])
            self._warnings.append("Validation audit is informational and non-calibrating")
            self._stage("validation_audit", StageStatus.SUCCESS if code == 0 else StageStatus.PARTIAL,
                        warnings=[] if code == 0 else ["Validation audit did not complete"], message=output[-500:], started=started, change="UPDATED")

        statuses = [stage.status for stage in self._stages]
        if any(stage.status == StageStatus.FAILED for stage in self._stages if stage.name in {"scoring", "dossier_generation"}):
            final = StageStatus.FAILED
            self._exit_code = EXIT_UNEXPECTED_FAILURE
            failed_stage = next(stage for stage in self._stages if stage.status == StageStatus.FAILED and stage.name in {"scoring", "dossier_generation"})
            self._failure_stage = failed_stage.name
            self._failure_reason = failed_stage.message or "; ".join(failed_stage.warnings)
        elif any(status in {StageStatus.PARTIAL, StageStatus.FAILED} for status in statuses):
            final = StageStatus.PARTIAL
        else:
            final = StageStatus.SUCCESS
        production = self._materialize_outputs()
        result = self._build_result(final)
        # Keep the materialized dossier paths returned by the production copy.
        result.dossier_json = production["dossier_json"]
        result.dossier_markdown = production["dossier_markdown"]
        self._write_manifest_snapshot(final_status=final, exit_code=self._exit_code)
        return result

    def run(self) -> PipelineRunResult:
        """Run the orchestration with an auditable failure result."""
        try:
            return self._run()
        except Exception as exc:
            self._failure_stage = self._current_stage or "orchestration"
            self._failure_reason = f"{type(exc).__name__}: {exc}"
            self._exit_code = EXIT_UNEXPECTED_FAILURE
            self._report_unexpected_exception(exc)
            # The layout may not exist if validation failed before setup.  Do
            # not retry a requested cleanup; only create the reporting parent.
            try:
                if not self._manifest_ready:
                    if self.clean_output:
                        raise
                    self.clean_output = False
                    self._ensure_production_layout()
                    self._manifest_ready = True
                self._append_not_run_stages(self._current_stage or "")
                result = self._build_result(StageStatus.FAILED, exit_code=EXIT_UNEXPECTED_FAILURE)
                self._write_manifest_snapshot(final_status=StageStatus.FAILED, exit_code=EXIT_UNEXPECTED_FAILURE)
                return result
            except Exception:
                # The CLI will report a configuration/path failure if even
                # the failure manifest cannot be safely materialized.
                raise
