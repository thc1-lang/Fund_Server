import tempfile
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.models import Stock, IRCandidate, AccessResult, IREcosystem, Document, ArchiveResult, Issue
from qualitative_ir_downloader.main import process_company, parser, run

class FakeBrowser:
    def __init__(self, root):
        self.config = Config(download_root=root, events=EventConfig(enabled=False))
        self.context = AsyncMock()
        self.access_events = []

    @asynccontextmanager
    async def session(self):
        yield self.context

class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @patch("qualitative_ir_downloader.main.setup_logging")
    async def test_generated_search_skips_official_and_stops_at_generated(self,logging_setup):
        with tempfile.TemporaryDirectory() as temp:
            args=parser().parse_args(["--find-generatable-event","--max-companies","3","--event-limit","9","--download-root",temp])
            stocks=[Stock(str(i),"Acme","Safe",i) for i in range(5)]
            def result(stock,generated=0,official=0):
                return dict(ticker=stock.ticker,ir_official=True,ir_accessible=True,news=0,reports=0,discovery_errors=0,access_blocks=0,document_failures=0,status="SUCCESS",generated_transcripts=generated,generated_transcript_available=generated,generated_transcript_created=generated,official_transcripts=official)
            with patch("qualitative_ir_downloader.main.read_stocks",return_value=stocks), patch("qualitative_ir_downloader.main.process_company",new_callable=AsyncMock) as process, patch("qualitative_ir_downloader.main.Browser") as browser:
                browser.return_value.__aenter__=AsyncMock(return_value=browser.return_value)
                browser.return_value.__aexit__=AsyncMock(return_value=False)
                process.side_effect=[result(stocks[i]) for i in range(3)]+[result(stocks[0],official=1),result(stocks[1],generated=1)]
                await run(args)
                self.assertEqual(process.await_count,5)
                config=browser.call_args.args[0]
                self.assertTrue(config.events.generated_only)
                self.assertEqual(config.events.limit,9)

    @patch("qualitative_ir_downloader.main.setup_logging")
    async def test_generated_search_respects_company_limit(self,logging_setup):
        with tempfile.TemporaryDirectory() as temp:
            args=parser().parse_args(["--find-generatable-event","--max-companies","2","--download-root",temp])
            stocks=[Stock(str(i),"Acme","Safe",i) for i in range(5)]
            with patch("qualitative_ir_downloader.main.read_stocks",return_value=stocks), patch("qualitative_ir_downloader.main.process_company",new_callable=AsyncMock) as process, patch("qualitative_ir_downloader.main.Browser") as browser:
                browser.return_value.__aenter__=AsyncMock(return_value=browser.return_value)
                browser.return_value.__aexit__=AsyncMock(return_value=False)
                process.return_value=dict(ticker="ACME",ir_official=True,ir_accessible=True,news=0,reports=0,discovery_errors=0,access_blocks=0,document_failures=0,status="EVENT_MEDIA_PROTECTED",generated_transcripts=0)
                await run(args)
                self.assertEqual(process.await_count,4)
    @patch("qualitative_ir_downloader.main.setup_logging")
    async def test_generated_reuse_stops_unless_new_required(self,logging_setup):
        for require_new,expected_attempts in [(False,1),(True,2)]:
            with tempfile.TemporaryDirectory() as temp:
                flags=['--find-generatable-event','--max-companies','2','--download-root',temp]
                if require_new:flags.append('--require-new-generated-event')
                args=parser().parse_args(flags);stocks=[Stock('A','Acme','Safe',4),Stock('B','Beta','Safe',5)]
                def row(i,created=0,reused=0):return dict(ticker=stocks[i].ticker,company_name=stocks[i].company_name,ir_official=True,ir_accessible=True,status='GENERATED_TRANSCRIPT_ALREADY_AVAILABLE',generated_transcript_created=created,generated_transcript_reused=reused,generated_transcript_available=created+reused)
                with patch('qualitative_ir_downloader.main.read_stocks',return_value=stocks),patch('qualitative_ir_downloader.main.process_company',AsyncMock(side_effect=[row(0),row(1),row(0,reused=1),row(1,created=1)])) as process,patch('qualitative_ir_downloader.main.Browser') as browser:
                    browser.return_value.__aenter__=AsyncMock(return_value=browser.return_value);browser.return_value.__aexit__=AsyncMock(return_value=False)
                    self.assertEqual(await run(args),0);self.assertEqual(process.await_count,2+expected_attempts)

    async def test_cached_probe_failure_keeps_provider_roots_in_both_modes(self):
        for generated_only in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                browser=FakeBrowser(Path(temp))
                browser.config=Config(download_root=Path(temp),events=EventConfig(only=True,limit=1,generated_only=generated_only))
                browser.goto=AsyncMock(side_effect=RuntimeError("HTTP 403"))
                browser.assess=AsyncMock(return_value=AccessResult(blocked=True,browser_status=403))
                winner=IRCandidate("https://ir.acme.com/",100,official=True,content_validated=True)
                async def collect(browser,context,stock,ecosystem,folder,manifest):
                    self.assertEqual(ecosystem.verified_ir_urls,[winner.url])
                    self.assertEqual(ecosystem.official_ir_domain,"ir.acme.com")
                    return {"events_discovered":13,"event_failures":0}
                with patch("qualitative_ir_downloader.ir_state.recover",return_value=winner), patch("qualitative_ir_downloader.main.IRDiscovery.discover_investor_relations_site",new_callable=AsyncMock) as discover, patch("qualitative_ir_downloader.main.collect_events",side_effect=collect):
                    result=await process_company(browser,Stock("ACME","Acme","Safe",4),"test")
                    discover.assert_not_called()
                    self.assertEqual(result["events_discovered"],13)
    async def test_document_failure_does_not_stop_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = FakeBrowser(Path(temp))
            archive = [ArchiveResult([Document("Broken release", None, "https://ir.acme.com/news/one", "news")]),
                       ArchiveResult([Document("Annual report", "2025", "https://ir.acme.com/report.pdf", "reports", "https://ir.acme.com/report.pdf")])]
            good = Document("Annual report", "2025", "https://ir.acme.com/report.pdf", "reports", "https://ir.acme.com/report.pdf", "Reports/report.pdf", "original_pdf")
            with patch("qualitative_ir_downloader.main.IRDiscovery") as discovery, patch("qualitative_ir_downloader.main.find_news_section", AsyncMock(return_value=["https://ir.acme.com/news"])), patch("qualitative_ir_downloader.main.find_reports_section", AsyncMock(return_value=["https://ir.acme.com/reports"])), patch("qualitative_ir_downloader.main.ArchiveCrawler") as crawler, patch("qualitative_ir_downloader.main.Downloader") as downloader:
                discovery.return_value.candidates = []
                discovery.return_value.ecosystem = IREcosystem()
                discovery.return_value.rss_feeds = []
                discovery.return_value.errors = []
                discovery.return_value.discover_investor_relations_site = AsyncMock(return_value=IRCandidate("https://ir.acme.com/", 95, official=True, access=AccessResult(browser_accessible=True), content_validated=True))
                crawler.return_value.collect = AsyncMock(side_effect=archive)
                downloader.return_value.save = AsyncMock(side_effect=[RuntimeError("failed release"), good])
                result = await process_company(browser, Stock("ACME", "Acme", "Safe", 4), "2026-09-06_11-00-00")
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["failed_downloads"], 1)
            self.assertEqual(result["reports"], 1)
            import json
            manifest = json.loads((Path(result["folder"]) / "manifest.json").read_text())
            self.assertEqual(manifest["reports"][0]["source_url"], "https://ir.acme.com/report.pdf")
            self.assertEqual(len(manifest["errors"]), 1)

    async def test_discovery_failure_keeps_access_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("qualitative_ir_downloader.main.IRDiscovery") as discovery:
                discovery.return_value.candidates = []
                discovery.return_value.ecosystem = IREcosystem()
                discovery.return_value.rss_feeds = []
                discovery.return_value.errors = [Issue("SITE_BLOCKED", "HTTP 403", "https://ir.acme.com/")]
                discovery.return_value.discover_investor_relations_site = AsyncMock(return_value=None)
                result = await process_company(FakeBrowser(Path(temp)), Stock("ACME", "Acme", "Safe", 4), "2026-09-06_11-00-00")
            self.assertEqual(result["status"], "IR_DISCOVERY_FAILED")
            self.assertEqual(result["errors"], 2)
