from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline_runner.issuer_resolution import IssuerResolutionError, IssuerMetadata, resolve_issuer_metadata
from pipeline_runner.runner import PipelineRunner
from qualitative_ir_downloader.main import parser as downloader_parser, run as downloader_run
from qualitative_ir_downloader.models import Stock


class ManualTickerResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.download = self.root / "Downloads"
        self.data = self.root / "Microeconomics"
        self.artifacts = self.root / "artifacts"

    def tearDown(self):
        self.tmp.cleanup()

    def test_sec_issuer_resolution_does_not_use_worksheet(self):
        class Provider:
            def resolve_issuer(self, ticker):
                self.ticker = ticker
                return SimpleNamespace(
                    ticker=ticker,
                    company_name="NVIDIA Corporation",
                    issuer_cik="0001045810",
                    source_url="https://www.sec.gov/files/company_tickers.json",
                )

        provider = Provider()
        result = resolve_issuer_metadata("NVDA", download_root=self.download, data_root=self.data, sec_provider=provider)
        self.assertEqual(result.ticker, "NVDA")
        self.assertEqual(result.company_name, "NVIDIA Corporation")
        self.assertEqual(result.cik, "0001045810")
        self.assertEqual(result.source, "sec_edgar")
        self.assertEqual(provider.ticker, "NVDA")

    def test_resolution_is_generic_and_worksheet_cache_can_enrich(self):
        class FailingProvider:
            def resolve_issuer(self, ticker):
                raise LookupError("offline")

        metadata = {
            "spreadsheet_id": "fixture",
            "stocks": {"ACME": {"ticker": "ACME", "company_name": "Acme Holdings", "worksheet": "Safe", "spreadsheet_row": 7}},
        }
        self.download.mkdir(parents=True)
        (self.download / "stock_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        result = resolve_issuer_metadata("acme", download_root=self.download, data_root=self.data, sec_provider=FailingProvider())
        self.assertEqual(result.ticker, "ACME")
        self.assertEqual(result.company_name, "Acme Holdings")
        self.assertEqual(result.source, "worksheet_cache")
        self.assertEqual(result.worksheet, "Safe")

        class SecProvider:
            def resolve_issuer(self, ticker):
                return SimpleNamespace(ticker=ticker, company_name="ACME CORP", issuer_cik="0000000001", source_url="sec")

        authoritative = resolve_issuer_metadata("ACME", download_root=self.download, data_root=self.data, sec_provider=SecProvider())
        self.assertEqual(authoritative.source, "sec_edgar")
        self.assertEqual(authoritative.company_name, "ACME CORP")
        self.assertEqual(authoritative.worksheet, "Safe")

    def test_unresolvable_manual_ticker_is_explicit(self):
        class FailingProvider:
            def resolve_issuer(self, ticker):
                raise LookupError("not found")

        with self.assertRaisesRegex(IssuerResolutionError, "ISSUER_RESOLUTION_FAILED"):
            resolve_issuer_metadata("NOT_A_REAL_TICKER", download_root=self.download, data_root=self.data, sec_provider=FailingProvider())

    def test_runner_reports_resolution_failure_before_acquisition(self):
        with patch("pipeline_runner.runner.resolve_issuer_metadata", side_effect=IssuerResolutionError("ISSUER_RESOLUTION_FAILED: BAD")):
            result = PipelineRunner(
                "BAD", "2026-09-25", mode="fresh", manual_ticker=True,
                artifacts_root=self.artifacts, output_root=self.artifacts / "research_output",
                data_root=self.data, download_root=self.download,
                executor=lambda *_: SimpleNamespace(returncode=0, stdout="", stderr=""),
            ).run()
        self.assertIn("ISSUER_RESOLUTION_FAILED", result.failure_reason)
        self.assertEqual(result.stages[0].warnings, ["ISSUER_RESOLUTION_FAILED"])

    def test_manual_runner_passes_resolved_identity_when_sheet_is_absent(self):
        calls = []

        def execute(module, args):
            calls.append((module, list(args)))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        issuer = IssuerMetadata("NVDA", "NVIDIA Corporation", "0001045810", "sec_edgar")
        with patch("pipeline_runner.runner.resolve_issuer_metadata", return_value=issuer):
            result = PipelineRunner(
                "NVDA", "2026-09-25", mode="fresh", manual_ticker=True,
                artifacts_root=self.artifacts, output_root=self.artifacts / "research_output",
                data_root=self.data, download_root=self.download, executor=execute,
            ).run()
        self.assertEqual(calls[0][0], "qualitative_ir_downloader.main")
        invocation = calls[0][1]
        self.assertIn("--company-name", invocation)
        self.assertEqual(invocation[invocation.index("--company-name") + 1], "NVIDIA Corporation")
        self.assertEqual(invocation[invocation.index("--issuer-cik") + 1], "0001045810")
        self.assertIn("MISSING_NORMALIZED_INPUTS", result.failure_reason)

    def test_manual_runner_uses_same_path_when_worksheet_row_exists(self):
        issuer = IssuerMetadata("NVDA", "NVIDIA Corporation", "0001045810", "sec_edgar", worksheet="Safe", worksheet_row=9)
        module, args = PipelineRunner("NVDA", "2026-09-25", mode="fresh", manual_ticker=True,
                                      download_root=self.download, data_root=self.data).acquisition_invocation(issuer)
        self.assertEqual(module, "qualitative_ir_downloader.main")
        self.assertEqual(args[args.index("--ticker") + 1], "NVDA")
        self.assertIn("--company-name", args)

    def test_downloader_manual_mode_bypasses_sheet_lookup(self):
        args = downloader_parser().parse_args([
            "--ticker", "NVDA", "--company-name", "NVIDIA Corporation", "--issuer-cik", "0001045810",
            "--list-only", "--download-root", str(self.download),
        ])
        with patch("qualitative_ir_downloader.main.setup_logging"), patch("qualitative_ir_downloader.main.read_stocks") as read_stocks:
            code = asyncio.run(downloader_run(args))
        self.assertEqual(code, 0)
        read_stocks.assert_not_called()

    def test_batch_worksheet_workflow_still_reads_sheet(self):
        args = downloader_parser().parse_args(["--ticker", "ZM", "--list-only", "--download-root", str(self.download)])
        with patch("qualitative_ir_downloader.main.setup_logging"), patch("qualitative_ir_downloader.main.read_stocks", return_value=[Stock("ZM", "Zoom", "Safe", 4)]) as read_stocks:
            code = asyncio.run(downloader_run(args))
        self.assertEqual(code, 0)
        read_stocks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
