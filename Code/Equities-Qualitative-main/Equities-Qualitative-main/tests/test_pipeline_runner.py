from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from pipeline_runner.models import StageStatus
from pipeline_runner.main import _print_result, build_parser, write_batch_artifacts
from pipeline_runner.runner import DEFAULT_DATA_ROOT, FOLDER_NAMES, PipelineRunner


class PipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "artifacts"
        self.output = self.root / "research_output"
        self.data_root = Path(self.tmp.name) / "Microeconomics"
        self._write("qualitative_analysis/documents.jsonl", {"ticker": "ZM", "document_id": "d1"})
        self._write("qualitative_analysis/claims.jsonl", {"ticker": "ZM", "claim_id": "c1"})
        self._write("qualitative_analysis/temporal_changes.jsonl", {"ticker": "ZM", "change_id": "t1"})
        self._write("qualitative_analysis/qualitative_states.jsonl", {"ticker": "ZM", "state_id": "s1"})
        self._write("scoring_engine/company_scores.jsonl", {"ticker": "ZM", "profile": "core_v1", "profile_version": "core-v1.2"})
        for relative in (
            "insider_intelligence/people.jsonl", "management_intelligence/people.jsonl",
            "management_intelligence/roles.jsonl", "management_intelligence/track_record_snapshots.jsonl",
            "management_intelligence/board_history.jsonl", "management_intelligence/governance_committees.jsonl",
            "management_intelligence/governance_snapshots.jsonl",
        ):
            self._write(relative, {"ticker": "ZM", "id": relative})
        dossier_dir = self.output / "ZM"
        dossier_dir.mkdir(parents=True, exist_ok=True)
        (dossier_dir / "ZM_2026-09-24_core-v1.2_dossier.json").write_text("{}", encoding="utf-8")
        (dossier_dir / "ZM_2026-09-24_core-v1.2_dossier.md").write_text("# ZM", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, relative: str, row: dict):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def _executor(self, calls, fail_extract=False):
        def execute(module, args):
            calls.append((module, list(args)))
            if fail_extract and module == "qualitative_analysis.main" and "--extract" in args:
                return SimpleNamespace(returncode=1, stdout="", stderr="extract failed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return execute

    def _write_pltr_cache_fixture(self):
        """Offline equivalent of the live PLTR cache-hit/limited-event run."""
        source = Path(self.tmp.name) / "source-cache"
        company = source / "PLTR_Palantir Technologies_qualitative analysis_2026-09-25"
        (company / "News Releases").mkdir(parents=True, exist_ok=True)
        (company / "Reports").mkdir(parents=True, exist_ok=True)
        news = []
        reports = []
        for index in range(299):
            relative = f"News Releases/2026-09-25_PLTR_news_{index}.txt"
            (company / relative).write_text(f"PLTR news release {index}", encoding="utf8")
            news.append({"title": f"News {index}", "source_url": f"https://ir.test/news/{index}",
                         "category": "news", "local_filename": relative, "cache_hit": True,
                         "existing_artifact": True})
        for index in range(49):
            relative = f"Reports/2026-09-25_PLTR_report_{index}.txt"
            (company / relative).write_text(f"PLTR report {index}", encoding="utf8")
            reports.append({"title": f"Report {index}", "source_url": f"https://ir.test/reports/{index}",
                            "category": "reports", "local_filename": relative, "cache_hit": True,
                            "existing_artifact": True})
        events = [{"title": f"Event {index}", "event_url": f"https://ir.test/events/{index}",
                   "status": "DISCOVERED"} for index in range(27)]
        manifest = {
            "ticker": "PLTR", "company_name": "Palantir Technologies", "run_timestamp": "2026-09-25_07-38-00",
            "news": news, "reports": reports, "events": events,
            "overall_status": "SUCCESS_WITH_LIMITATIONS", "status": "SUCCESS_WITH_LIMITATIONS",
            "news_status": "SUCCESS", "reports_status": "SUCCESS", "events_status": "MEDIA_NOT_FOUND",
            "completeness": {"limitations": ["event_media_unavailable"]},
            "cache_metrics": {"artifact_cache_hits": 348, "documents_skipped_existing": 348},
        }
        (company / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
        return source, company

    def _run(self, calls, **kwargs):
        return PipelineRunner("ZM", "2026-09-24", artifacts_root=self.root,
                              output_root=self.output, data_root=self.data_root,
                              executor=self._executor(calls), **kwargs).run()

    def test_stage_order_and_existing_mode_never_acquires(self):
        calls = []
        result = self._run(calls, mode="existing")
        self.assertEqual([s.name for s in result.stages], [
            "acquisition", "normalization", "qualitative_extraction", "temporal_comparison", "state_synthesis",
            "insider_intelligence", "management_intelligence", "management_track_record", "governance_intelligence",
            "scoring", "dossier_generation",
        ])
        self.assertNotIn("qualitative_ir_downloader.main", [module for module, _ in calls])
        self.assertEqual(result.final_status, StageStatus.SUCCESS)

    def test_skip_acquisition_applies_to_fresh_mode(self):
        calls = []
        result = self._run(calls, mode="fresh", skip_acquisition=True)
        self.assertEqual(result.stages[0].status, StageStatus.SKIPPED)
        self.assertNotIn("qualitative_ir_downloader.main", [module for module, _ in calls])

    def test_required_failure_skips_dependents(self):
        calls = []
        result = PipelineRunner("ZM", "2026-09-24", mode="fresh", artifacts_root=self.root, output_root=self.output,
                                data_root=self.data_root,
                                executor=self._executor(calls, fail_extract=True)).run()
        by_name = {stage.name: stage for stage in result.stages}
        self.assertEqual(by_name["qualitative_extraction"].status, StageStatus.FAILED)
        self.assertEqual(by_name["temporal_comparison"].status, StageStatus.SKIPPED)
        self.assertEqual(by_name["state_synthesis"].status, StageStatus.SKIPPED)
        self.assertEqual(result.final_status, StageStatus.PARTIAL)

    def test_optional_missing_layers_are_partial_but_dossier_succeeds(self):
        # Remove the optional rows while keeping the required and dossier inputs.
        for path in (self.root / "insider_intelligence").glob("*.jsonl"):
            path.unlink()
        for path in (self.root / "management_intelligence").glob("*.jsonl"):
            path.unlink()
        calls = []
        result = self._run(calls, mode="existing")
        self.assertEqual(result.final_status, StageStatus.PARTIAL)
        self.assertEqual(next(s for s in result.stages if s.name == "dossier_generation").status, StageStatus.SUCCESS)

    def test_manifest_and_stable_dossier_paths_and_second_run_reuses(self):
        calls = []
        first = self._run(calls, mode="existing")
        second = self._run(calls, mode="existing")
        self.assertTrue(Path(first.manifest_path).exists())
        self.assertEqual(first.dossier_json, second.dossier_json)
        self.assertEqual(first.dossier_markdown, second.dossier_markdown)
        self.assertEqual(next(s for s in second.stages if s.name == "dossier_generation").change, "REUSED")
        manifest = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["ticker"], "ZM")
        self.assertIn("git_commit", manifest)

    def test_validation_audit_is_opt_in(self):
        calls = []
        self._run(calls, mode="existing")
        self.assertNotIn("empirical_validation.main", [module for module, _ in calls])
        calls.clear()
        result = self._run(calls, mode="existing", run_validation_audit=True)
        self.assertIn("empirical_validation.main", [module for module, _ in calls])
        self.assertEqual(next(s for s in result.stages if s.name == "validation_audit").status, StageStatus.SUCCESS)

    def test_null_overall_score_is_a_successful_coverage_limited_run(self):
        calls = []
        def execute(module, args):
            calls.append((module, list(args)))
            output = "Status: INSUFFICIENT_SCORING_COVERAGE" if module == "scoring_engine.main" else ""
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        result = PipelineRunner("ZM", "2026-09-24", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root, executor=execute).run()
        scoring = next(stage for stage in result.stages if stage.name == "scoring")
        self.assertEqual(scoring.status, StageStatus.SUCCESS)
        self.assertTrue(any("valid result" in warning for warning in scoring.warnings))
        self.assertEqual(result.final_status, StageStatus.SUCCESS)

    def test_default_production_root_and_numbered_layout(self):
        self.assertEqual(Path(build_parser().parse_args(["--ticker", "ZM", "--as-of-date", "2026-09-24"]).data_root), DEFAULT_DATA_ROOT)
        calls = []
        result = self._run(calls, mode="existing")
        root = Path(result.production_root)
        for folder in FOLDER_NAMES.values():
            self.assertTrue((root / folder).exists(), folder)
        self.assertTrue((root / "10_Research_Dossier" / "ZM_2026-09-24_core-v1.2_dossier.json").exists())
        self.assertTrue((root / "11_Run_Summaries" / "Pipeline" / "ZM_2026-09-24_pipeline_run.json").exists())
        self.assertFalse((self.data_root / "ZM_2026-09-24_pipeline_run.json").exists())
        self.assertFalse((self.data_root / "ZM_2026-09-24_core-v1.2_dossier.json").exists())
        self.assertFalse(list((root).glob("*.json")))
        self.assertFalse(list((root).glob("*.md")))

    def test_clean_output_deletes_only_selected_ticker_and_preserves_other_ticker(self):
        (self.data_root / "ZM" / "old.txt").parent.mkdir(parents=True, exist_ok=True)
        (self.data_root / "ZM" / "old.txt").write_text("old", encoding="utf-8")
        (self.data_root / "PLTR").mkdir(parents=True, exist_ok=True)
        (self.data_root / "PLTR" / "keep.txt").write_text("keep", encoding="utf-8")
        calls = []
        self._run(calls, mode="existing", clean_output=True)
        self.assertFalse((self.data_root / "ZM" / "old.txt").exists())
        self.assertTrue((self.data_root / "PLTR" / "keep.txt").exists())

    def test_clean_output_preserves_source_cache_outside_ticker_root(self):
        source_cache = Path(self.tmp.name) / "source-cache"
        source_cache.mkdir()
        marker = source_cache / "cached-source.json"
        marker.write_text("cache", encoding="utf-8")
        calls = []
        self._run(calls, mode="existing", clean_output=True, download_root=source_cache)
        self.assertTrue(marker.exists())

    def test_clean_output_rejects_target_equal_to_root(self):
        runner = PipelineRunner("ZM", "2026-09-24", data_root=self.data_root, clean_output=True)
        runner.ticker_root = runner.data_root
        with self.assertRaises(ValueError):
            runner._safe_clean_target()

    def test_clean_output_rejects_parent_directory(self):
        runner = PipelineRunner("ZM", "2026-09-24", data_root=self.data_root, clean_output=True)
        runner.ticker_root = self.data_root.parent
        with self.assertRaises(ValueError):
            runner._safe_clean_target()

    def test_clean_output_requires_explicit_ticker_scope(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--as-of-date", "2026-09-24", "--clean-output"])

    def test_verbose_flag_and_dry_invocation_shape(self):
        parsed = build_parser().parse_args(["--ticker", "ZM", "--as-of-date", "2026-09-24", "--verbose"])
        self.assertTrue(parsed.verbose)
        runner = PipelineRunner("ZM", "2026-09-24", data_root=self.data_root,
                                download_root=self.tmp.name + "\\source-cache")
        module, args = runner.acquisition_invocation()
        self.assertEqual(module, "qualitative_ir_downloader.main")
        self.assertEqual(args[:2], ["--ticker", "ZM"])

    def test_fresh_mode_rejects_cache_inside_production_root(self):
        with self.assertRaises(ValueError):
            PipelineRunner("ZM", "2026-09-24", mode="fresh", data_root=self.data_root,
                           download_root=self.data_root / "ZM" / "01_Acquisition")

    def test_ticker_cannot_escape_production_root(self):
        with self.assertRaises(ValueError):
            PipelineRunner("..\\PLTR", "2026-09-24", data_root=self.data_root)

    def test_fresh_mode_routes_acquisition_and_qualitative_reads_to_external_cache(self):
        runner = PipelineRunner("ZM", "2026-09-24", mode="fresh", data_root=self.data_root,
                                artifacts_root=self.root, output_root=self.output,
                                download_root=self.tmp.name + "\\source-cache")
        expected = str(Path(self.tmp.name) / "source-cache")
        self.assertEqual(runner._qual_args()[3], expected)
        self.assertEqual(runner.acquisition_invocation()[1][3], expected)
        calls = []
        runner = PipelineRunner("ZM", "2026-09-24", mode="fresh", data_root=self.data_root,
                                artifacts_root=self.root, output_root=self.output,
                                download_root=expected, skip_acquisition=True, executor=self._executor(calls))
        runner.run()
        self.assertNotIn("qualitative_ir_downloader.main", [module for module, _ in calls])

    def test_fresh_mode_reaches_acquisition_after_cleanup_and_propagates_failure(self):
        calls = []
        def fail_acquisition(module, args):
            calls.append((module, list(args)))
            return SimpleNamespace(returncode=17, stdout="", stderr="cache unavailable")
        result = PipelineRunner("ZM", "2026-09-24", mode="fresh", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root,
                                download_root=Path(self.tmp.name) / "source-cache",
                                clean_output=True, executor=fail_acquisition).run()
        self.assertEqual(calls[0][0], "qualitative_ir_downloader.main")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.final_status, StageStatus.FAILED)
        self.assertEqual(next(stage for stage in result.stages if stage.name == "acquisition").status, StageStatus.FAILED)
        self.assertTrue(all(stage.status == StageStatus.NOT_RUN for stage in result.stages[1:]))
        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["clean_output"], "SUCCESS")
        self.assertEqual(manifest["exit_code"], 2)
        self.assertEqual(manifest["failure_stage"], "acquisition")

    def test_fresh_acquisition_exception_is_visible_and_manifested(self):
        calls = []
        def raise_acquisition(module, args):
            calls.append((module, list(args)))
            raise RuntimeError("synthetic acquisition failure")
        result = PipelineRunner("ZM", "2026-09-24", mode="fresh", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root,
                                download_root=Path(self.tmp.name) / "source-cache",
                                executor=raise_acquisition).run()
        self.assertEqual(result.exit_code, 2)
        self.assertIn("synthetic acquisition failure", result.failure_reason)
        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["failure_stage"], "acquisition")

    def test_acquisition_started_message_is_emitted_before_invocation(self):
        calls = []
        output = StringIO()
        with redirect_stdout(output):
            PipelineRunner("ZM", "2026-09-24", mode="fresh", artifacts_root=self.root,
                           output_root=self.output, data_root=self.data_root,
                           download_root=Path(self.tmp.name) / "source-cache",
                           executor=lambda module, args: (calls.append((module, args)) or 0)).run()
        text = output.getvalue()
        self.assertIn("[1/11] ACQUISITION — STARTED", text)
        self.assertEqual(calls[0][0], "qualitative_ir_downloader.main")

    def test_unexpected_exception_cannot_be_swallowed(self):
        def explode(module, args):
            if module == "scoring_engine.main":
                raise RuntimeError("synthetic scoring crash")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        result = PipelineRunner("ZM", "2026-09-24", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root,
                                executor=explode).run()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.failure_stage, "scoring")
        self.assertTrue(Path(result.manifest_path).exists())

    def test_successful_mocked_fresh_acquisition_continues_to_normalization(self):
        calls = []
        source = Path(self.tmp.name) / "source-cache"
        company = source / "ZM_Zoom_Communications_qualitative analysis_2026-09-24"
        (company / "News Releases").mkdir(parents=True, exist_ok=True)
        relative = "News Releases/release.txt"
        (company / relative).write_text("ZM release", encoding="utf8")
        (company / "manifest.json").write_text(json.dumps({
            "ticker": "ZM",
            "run_timestamp": "2026-09-24_00-00-00",
            "news": [{"title": "Release", "source_url": "https://ir.test/zm/release",
                       "local_filename": relative, "cache_hit": True}],
            "reports": [], "events": [], "overall_status": "SUCCESS",
            "cache_metrics": {"artifact_cache_hits": 1},
        }), encoding="utf8")
        result = PipelineRunner("ZM", "2026-09-24", mode="fresh", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root,
                                download_root=source,
                                executor=self._executor(calls)).run()
        modules = [module for module, _ in calls]
        self.assertIn("qualitative_ir_downloader.main", modules)
        self.assertIn("qualitative_analysis.main", modules)
        self.assertEqual(next(stage for stage in result.stages if stage.name == "normalization").status, StageStatus.REUSED)

    def test_fresh_cache_only_limited_acquisition_handoff_is_ticker_scoped(self):
        source, company = self._write_pltr_cache_fixture()
        calls = []

        def execute(module, args):
            calls.append((module, list(args)))
            if module == "qualitative_analysis.main":
                store = Path(args[args.index("--store-root") + 1])
                if "--extract" in args:
                    path, row = "claims.jsonl", {"ticker": "PLTR", "claim_id": "claim-1"}
                elif "--compare" in args:
                    path, row = "temporal_changes.jsonl", {"ticker": "PLTR", "change_id": "change-1"}
                elif "--state" in args:
                    path, row = "qualitative_states.jsonl", {"ticker": "PLTR", "state_id": "state-1"}
                else:
                    path, row = "documents.jsonl", {"ticker": "PLTR", "document_id": "doc-1"}
                store.mkdir(parents=True, exist_ok=True)
                (store / path).write_text(json.dumps(row) + "\n", encoding="utf8")
            elif module == "scoring_engine.main":
                path = self.root / "scoring_engine" / "company_scores.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"ticker": "PLTR", "profile_version": "core-v1.2"}) + "\n", encoding="utf8")
            elif module == "research_output.main":
                output = Path(args[args.index("--output-root") + 1]) / "PLTR"
                output.mkdir(parents=True, exist_ok=True)
                (output / "PLTR_2026-09-25_core-v1.2_dossier.json").write_text("{}", encoding="utf8")
                (output / "PLTR_2026-09-25_core-v1.2_dossier.md").write_text("# PLTR", encoding="utf8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = PipelineRunner("PLTR", "2026-09-25", mode="fresh", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root,
                                download_root=source, executor=execute).run()
        by_name = {stage.name: stage for stage in result.stages}
        self.assertEqual(by_name["acquisition"].status, StageStatus.SUCCESS)
        self.assertEqual(by_name["acquisition"].record_counts["reused"], 348)
        self.assertEqual(by_name["acquisition"].record_counts["news"], 299)
        self.assertEqual(by_name["acquisition"].record_counts["reports"], 49)
        self.assertEqual(by_name["acquisition"].record_counts["events"], 27)
        self.assertIn("events: MEDIA_NOT_FOUND", by_name["acquisition"].warnings)
        self.assertEqual(by_name["normalization"].status, StageStatus.SUCCESS)
        normalization = by_name["normalization"]
        self.assertEqual(normalization.provenance["input_root"], str(source))
        self.assertEqual(normalization.provenance["company_acquisition_root"], str(company))
        self.assertEqual(normalization.provenance["eligible_source_artifacts"], 348)
        self.assertEqual(normalization.provenance["ticker"], "PLTR")
        invocation = next(args for module, args in calls if module == "qualitative_analysis.main" and "--extract" not in args and "--compare" not in args and "--state" not in args)
        self.assertEqual(invocation[invocation.index("--ticker") + 1], "PLTR")
        self.assertEqual(invocation[invocation.index("--download-root") + 1], str(source))
        self.assertEqual(result.final_status, StageStatus.PARTIAL)

    def test_failed_normalization_does_not_advertise_dossier_paths(self):
        source = Path(self.tmp.name) / "empty-source"
        source.mkdir()
        calls = []

        def execute(module, args):
            calls.append((module, list(args)))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = PipelineRunner("PLTR", "2026-09-25", mode="fresh", artifacts_root=self.root,
                                output_root=self.output, data_root=self.data_root,
                                download_root=source, executor=execute).run()
        self.assertEqual(result.failure_stage, "normalization")
        self.assertEqual(result.dossier_json, "")
        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf8"))
        self.assertIsNone(manifest["dossier_paths"]["json"])
        self.assertIsNone(manifest["dossier_paths"]["markdown"])
        output = StringIO()
        with redirect_stdout(output):
            _print_result(result)
        self.assertEqual(output.getvalue().count("NOT_CREATED"), 2)

    def test_acquisition_views_and_summaries_are_materialized_in_production_sections(self):
        acquisition = self.data_root / "ZM" / "01_Acquisition"
        company = acquisition / "ZM_Zoom_qualitative analysis_20260924"
        (company / "News Releases").mkdir(parents=True, exist_ok=True)
        (company / "News Releases" / "release.pdf").write_bytes(b"pdf")
        (company / "manifest.json").write_text(json.dumps({"ticker": "ZM"}), encoding="utf-8")
        (acquisition / "production_run_summary_20260924.json").write_text("{}", encoding="utf-8")
        runner = PipelineRunner("ZM", "2026-09-24", data_root=self.data_root,
                                artifacts_root=self.root, output_root=self.output,
                                download_root=acquisition)
        runner._materialize_outputs()
        self.assertTrue((acquisition / "News_Releases" / company.name / "release.pdf").exists())
        self.assertTrue((self.data_root / "ZM" / "11_Run_Summaries" / "Production" / "production_run_summary_20260924.json").exists())
        self.assertFalse((acquisition / "production_run_summary_20260924.json").exists())

    def test_five_ticker_batch_has_cross_company_manifest_only(self):
        parsed = build_parser().parse_args(["--tickers", "ZM", "PLTR", "EXEL", "INCY", "CARG", "--as-of-date", "2026-09-24"])
        self.assertEqual(parsed.tickers, ["ZM", "PLTR", "EXEL", "INCY", "CARG"])
        self.assertEqual(Path(parsed.data_root), DEFAULT_DATA_ROOT)
        results = []
        for ticker in parsed.tickers:
            self._write(f"qualitative_analysis/documents.jsonl", {"ticker": ticker, "document_id": f"{ticker}-d1"})
            self._write(f"qualitative_analysis/claims.jsonl", {"ticker": ticker, "claim_id": f"{ticker}-c1"})
            self._write(f"scoring_engine/company_scores.jsonl", {"ticker": ticker, "profile": "core_v1", "profile_version": "core-v1.2"})
            dossier_dir = self.output / ticker
            dossier_dir.mkdir(parents=True, exist_ok=True)
            (dossier_dir / f"{ticker}_2026-09-24_core-v1.2_dossier.json").write_text("{}", encoding="utf-8")
            (dossier_dir / f"{ticker}_2026-09-24_core-v1.2_dossier.md").write_text(f"# {ticker}", encoding="utf-8")
            results.append(PipelineRunner(ticker, "2026-09-24", artifacts_root=self.root,
                                           output_root=self.output, data_root=self.data_root,
                                           executor=self._executor([])).run())
        batch_manifest = write_batch_artifacts(data_root=self.data_root, as_of_date="2026-09-24",
                                               profile="core_v1", tickers=parsed.tickers,
                                               results=results, errors=[])
        self.assertTrue(batch_manifest.exists())
        self.assertEqual(set(ticker for ticker in parsed.tickers),
                         {path.name for path in self.data_root.iterdir() if path.name != "_Batch_Runs"})
        self.assertTrue((batch_manifest.parent / "batch_summary.md").exists())
        self.assertTrue(all(result.batch_manifest_path == str(batch_manifest) for result in results))


if __name__ == "__main__":
    unittest.main()
