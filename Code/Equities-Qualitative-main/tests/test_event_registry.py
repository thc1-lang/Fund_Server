import json
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace,asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from qualitative_ir_downloader.event_registry import EventRegistry
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.models import Stock,IRCandidate,CollectionError
from qualitative_ir_downloader.transcript_pdf import transcript_record,create_pdf
from qualitative_ir_downloader.events import EventProcessor
from qualitative_ir_downloader.event_outcomes import metrics
from qualitative_ir_downloader.company_context import CompanyContext,MediaBudget,prepare_company_context
from qualitative_ir_downloader.manifest import Manifest
from qualitative_ir_downloader import ir_state

class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.old=self.root/'ACME_run_A';self.old.mkdir();(self.old/'Events').mkdir()
        self.stock=Stock('ACME','Acme','Safe',4)
        self.config=EventConfig(only=True,generated_only=True,min_transcript_segments=2,min_transcript_characters=10)
        self.event=Event('Acme Q2 2026 Earnings','2026-08-25','https://ir.acme.com/event-details/quarter-two',webcast_url='https://zoom.us/rec/share/test',method='generated_transcript',status='TRANSCRIBED',duration_seconds=120,local_pdf='Events/transcript.pdf',local_json='Events/transcript.json',transcript_qa={'status':'TRANSCRIPT_QA_WARNING'},transcription_engine='faster-whisper',transcription_model='medium.en')
        segments=[{'start':0,'end':30,'text':'yeah '*20},{'start':30,'end':60,'text':'The business discussed revenue and guidance.'}]
        record=transcript_record(self.stock,self.event,segments)
        (self.old/self.event.local_json).write_text(json.dumps(record));create_pdf(self.old/self.event.local_pdf,record)
        self.registry=EventRegistry(self.root);self.registry.record(self.stock,self.event,self.old)
    def candidate(self):return Event(self.event.title,self.event.date,self.event.event_url,webcast_url=self.event.webcast_url)
    def processor(self,config=None):
        folder=self.root/'ACME_run_B';folder.mkdir(exist_ok=True);(folder/'Events').mkdir(exist_ok=True)
        context=AsyncMock();browser=SimpleNamespace(config=Config(download_root=self.root,events=config or self.config),media_budget=MediaBudget(3),current_company=CompanyContext(self.stock,registry=self.registry),goto=AsyncMock(side_effect=CollectionError('SITE_BLOCKED','fixture stop')))
        manifest=Manifest(folder,self.stock,'2026-09-18_00-00-00')
        return EventProcessor(browser,context,self.stock,folder,manifest),browser,context
    async def test_cross_run_generated_reuse_never_opens_player(self):
        processor,browser,context=self.processor();e=self.candidate()
        with patch('qualitative_ir_downloader.events.transcribe') as whisper,patch('qualitative_ir_downloader.events.MediaAcquirer') as media:
            await processor.process(e);whisper.assert_not_called();media.assert_not_called();context.new_page.assert_not_called()
        self.assertTrue(e.resumed);self.assertTrue((processor.folder/e.local_pdf).is_file())
        result=metrics([e],[e],True)
        self.assertEqual((result['generated_transcript_created'],result['generated_transcript_reused'],result['generated_transcript_available']),(0,1,1))
        self.assertEqual(e.transcript_qa['status'],'TRANSCRIPT_QA_WARNING')
        self.assertEqual(result['event_outcome'],'GENERATED_TRANSCRIPT_ALREADY_AVAILABLE');self.assertEqual(result['event_failures'],0)
        self.assertEqual(browser.media_budget.used,0);self.assertEqual(browser.media_budget.skipped_cached,1)
    async def test_require_new_also_reuses_without_decode(self):
        processor,_,context=self.processor(replace(self.config,require_new_generated_event=True));e=self.candidate()
        await processor.process(e);context.new_page.assert_not_called();self.assertTrue(e.resumed)
    async def test_force_enters_real_attempt_and_claims_budget(self):
        processor,browser,context=self.processor(replace(self.config,force=True));e=self.candidate()
        processor.search_official_transcript=AsyncMock(return_value=None)
        context.new_page.return_value.on=Mock();context.new_page.return_value.remove_listener=Mock()
        with self.assertRaises(CollectionError):await processor.process(e)
        self.assertFalse(e.resumed);self.assertEqual(browser.media_budget.used,1);context.new_page.assert_awaited()
    def test_registry_survives_new_instance_and_missing_manifests(self):
        self.assertIsNotNone(EventRegistry(self.root).lookup(self.stock,self.candidate(),self.config))
    async def test_deleted_latest_copy_falls_back_to_earlier_valid_artifact(self):
        processor,_,_=self.processor();event=self.candidate();await processor.process(event)
        (processor.folder/event.local_pdf).unlink()
        hit=EventRegistry(self.root).lookup(self.stock,self.candidate(),self.config)
        self.assertIsNotNone(hit);self.assertEqual(hit[2],self.old)

    def test_deleted_pdf_invalidates(self):
        (self.old/self.event.local_pdf).unlink();self.assertIsNone(self.registry.lookup(self.stock,self.candidate(),self.config))
    def test_deleted_json_invalidates(self):
        (self.old/self.event.local_json).unlink();self.assertIsNone(self.registry.lookup(self.stock,self.candidate(),self.config))
    def test_corrupt_json_invalidates(self):
        (self.old/self.event.local_json).write_text('{broken');self.assertIsNone(self.registry.lookup(self.stock,self.candidate(),self.config))
    def test_wrong_artifact_identity_invalidates(self):
        p=self.old/self.event.local_json;data=json.loads(p.read_text());data['ticker']='OTHER';p.write_text(json.dumps(data))
        self.assertIsNone(self.registry.lookup(self.stock,self.candidate(),self.config))
    def test_changed_date_or_webcast_invalidates(self):
        for e in [replace(self.candidate(),date='2026-08-26'),replace(self.candidate(),webcast_url='https://zoom.us/rec/share/new')]:self.assertIsNone(self.registry.lookup(self.stock,e,self.config))
    def test_history_import_accepts_completed_skip_status(self):
        event=replace(self.event,status='SKIPPED_COMPLETED_GENERATED',resumed=True)
        (self.old/'manifest.json').write_text(json.dumps({**asdict(self.stock),'events':[event.record()],'run_timestamp':'2026-09-18_00-00-00'}))
        self.registry.path.unlink();registry=EventRegistry(self.root);registry.import_history(self.stock)
        self.assertIsNotNone(registry.lookup(self.stock,self.candidate(),self.config))
    async def test_cached_protected_uses_no_budget(self):
        event=replace(self.event,event_url='https://ir.acme.com/protected',title='Protected different call',webcast_url='https://player.example.com/protected',method=None,local_pdf=None,local_json=None,status='HLS_ENCRYPTED',protected_media_type='encrypted HLS')
        self.registry.record(self.stock,event,self.old);processor,browser,context=self.processor()
        e=replace(event,resumed=False);await processor.process(e)
        self.assertEqual(e.status,'CACHED_EVENT_MEDIA_PROTECTED');self.assertEqual(browser.media_budget.used,0);context.new_page.assert_not_called()
    def test_protected_expires_and_force_bypasses(self):
        event=replace(self.event,event_url='https://ir.acme.com/protected',title='Protected different call',webcast_url='https://player.example.com/protected',method=None,local_pdf=None,local_json=None,status='HLS_ENCRYPTED')
        self.registry.record(self.stock,event,self.old,checked=time.time()-8*86400)
        self.assertIsNone(self.registry.lookup(self.stock,event,self.config));self.assertIsNone(self.registry.lookup(self.stock,self.candidate(),replace(self.config,force=True)))
    async def test_cached_official_source_uses_no_budget(self):
        event=replace(self.event,event_url='https://ir.acme.com/official',method=None,local_pdf=None,local_json=None,status='SKIPPED_OFFICIAL_TRANSCRIPT',official_transcript_url='https://ir.acme.com/transcript.pdf')
        self.registry.record(self.stock,event,self.old);processor,browser,context=self.processor()
        await processor.process(event);self.assertEqual(event.status,'SKIPPED_OFFICIAL_TRANSCRIPT');self.assertEqual(browser.media_budget.used,0);context.new_page.assert_not_called()
    def test_shared_bootstrap_loads_ir_and_registry_before_network(self):
        ir_state.upsert(self.root,self.stock,IRCandidate('https://ir.acme.com/',100,['Corporate link'],True))
        browser=SimpleNamespace(config=Config(download_root=self.root))
        runtime=prepare_company_context(browser,self.stock,self.root)
        self.assertTrue(runtime.verified_cache_hit);self.assertIs(runtime,prepare_company_context(browser,self.stock,self.root));self.assertIs(runtime.registry,browser.event_registry)
    async def test_real_candidate_scan_uses_verified_ir_and_cached_generated(self):
        from contextlib import asynccontextmanager
        from qualitative_ir_downloader.main import process_company
        from qualitative_ir_downloader.access_strategy import save
        ir_state.upsert(self.root,self.stock,IRCandidate('https://ir.acme.com/',100,['Corporate link'],True))
        self.registry.save_listing(self.stock,'https://ir.acme.com/',[self.candidate()])
        save(self.root,self.stock,'https://ir.acme.com/',{'provider':'generic','transport':'public_http','browser_root_required':False})
        context=AsyncMock()
        class BrowserFixture:
            config=Config(download_root=self.root,events=self.config)
            access_events=[]
            @asynccontextmanager
            async def session(self):yield context
        browser=BrowserFixture()
        with patch('qualitative_ir_downloader.main.IRDiscovery.discover_investor_relations_site',AsyncMock()) as discover,patch('qualitative_ir_downloader.event_discovery.EventDiscovery.discover',AsyncMock()) as events,patch('qualitative_ir_downloader.events.transcribe') as whisper:
            scan=await process_company(browser,self.stock,'2026-09-18_00-00-01',scan_only=True)
            result=await process_company(browser,self.stock,'2026-09-18_00-00-01')
            discover.assert_not_called();events.assert_not_called();whisper.assert_not_called();context.new_page.assert_not_called()
        self.assertTrue(scan['verified_ir_cache_hit']);self.assertEqual(scan['generated_transcript_available'],1)
        self.assertEqual(result['generated_transcript_reused'],1);self.assertEqual(result['media_attempts_used'],0)

    async def test_discovery_timeout_preserves_already_verified_identity(self):
        from contextlib import asynccontextmanager
        from qualitative_ir_downloader.main import process_company
        from qualitative_ir_downloader.access_strategy import save
        self.registry.save_listing(self.stock,'https://ir.acme.com/',[self.candidate()])
        save(self.root,self.stock,'https://ir.acme.com/',{'provider':'generic','transport':'public_http','browser_root_required':False})
        context=AsyncMock()
        class BrowserFixture:
            config=Config(download_root=self.root,events=self.config)
            access_events=[]
            @asynccontextmanager
            async def session(self):yield context
        async def timed_out(discovery,*args):
            discovery.candidates.append(IRCandidate('https://ir.acme.com/',100,['Verified corporate navigation'],True))
            raise TimeoutError('Later probe exhausted scan budget')
        with patch('qualitative_ir_downloader.main.IRDiscovery.discover_investor_relations_site',timed_out):
            result=await process_company(BrowserFixture(),self.stock,'2026-09-18_00-00-02',scan_only=True)
        self.assertTrue(result['ir_official'])
        self.assertEqual(result['generated_transcript_available'],1)
        self.assertIsNotNone(ir_state.load(self.root,self.stock))

    def test_cached_and_new_budget_counts_separate(self):
        budget=MediaBudget(3);budget.skipped_cached=8
        self.assertEqual([budget.claim() for _ in range(4)],[True,True,True,False]);self.assertEqual(budget.used,3)

class DiscoveryEfficiencyTests(unittest.TestCase):
    def test_bad_domains_rejected_and_credo_disambiguated(self):
        from qualitative_ir_downloader.ir_discovery import candidate_domain_score,corporate_domains
        for url in ['https://shop.resmed.com/','https://myair.resmed.com/','https://amazon.com/resmed','https://chatgpt.com/futu','https://forums.example.com/futu','https://weather.com/incy']:
            self.assertLess(candidate_domain_score(url,'ResMed'),0)
        self.assertGreater(candidate_domain_score('https://credosemi.com/','Credo Technology Group'),0)
        self.assertLess(candidate_domain_score('https://credogroup.com/','Credo Technology Group'),0)
        self.assertGreater(candidate_domain_score('https://investors.resmed.com/','ResMed'),0)
