import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import asdict
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from qualitative_ir_downloader import ir_state
from qualitative_ir_downloader.models import Stock, IRCandidate, AccessResult, AccessEvent, CollectionError
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.events import select_candidates
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.event_outcomes import metrics, company_outcome
from qualitative_ir_downloader.company_context import MediaBudget
from qualitative_ir_downloader.ir_discovery import corporate_identity, IRDiscovery
from qualitative_ir_downloader.network import ResourceResponse
from qualitative_ir_downloader.main import process_company, save_summary
from qualitative_ir_downloader.registration import approved_destination
from qualitative_ir_downloader.scoring import company_key
from qualitative_ir_downloader.transcription import validate_segments

class IdentityTests(unittest.TestCase):
    def test_brand_normalization_and_aggregator_rejection(self):
        for company,domain in [("Zoom Communications, Inc.","zoom.com"),("ResMed","resmed.com"),("CarGurus","cargurus.com"),("Qualys","qualys.com")]:
            brand=domain.split('.')[0]
            with self.subTest(company=company):
                self.assertTrue(corporate_identity('https://'+domain,brand,company+' About Investors Careers Products',company))
                self.assertEqual(IRDiscovery.relevant_search_results(['https://'+domain,'https://reddit.com/investors/'+brand],company,'ZM'),['https://'+domain])
                self.assertFalse(corporate_identity('https://reddit.com/'+brand,company,company+' Investors Careers',company))
        self.assertEqual(company_key('CarGurus, Incorporated Class A'),'cargurus')
        self.assertEqual(company_key('Zoom Holdings Group Ltd.'),'zoom')
        self.assertFalse(IRDiscovery.relevant_search_results(['https://example.com/ZM/investor'], 'Zoom','ZM'))

    def test_cache_is_monotonic_and_recovers_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); stock=Stock('CARG','CarGurus Inc.','Safe',10)
            candidate=IRCandidate('https://investors.cargurus.com/',100,['Direct corporate Investors link'],True,corporate_url='https://www.cargurus.com/')
            ir_state.upsert(root,stock,candidate,confirmed=True)
            before=ir_state.read_store(root)['CARG']
            weaker=IRCandidate(candidate.url,90,['Weaker observation'],True)
            ir_state.upsert(root,stock,weaker)
            ir_state.upsert(root,stock,candidate)
            after=ir_state.read_store(root)['CARG']
            self.assertEqual(before,after)
            self.assertIsNotNone(ir_state.load(root,Stock('CARG','CarGurus','Elsewhere',20)))
            (root/'verified_ir.json').unlink()
            (root/'historical').mkdir()
            (root/'historical'/'manifest.json').write_text(json.dumps({**asdict(stock),'discovery':[asdict(candidate)],'run_timestamp':'2026-08-01'}))
            self.assertIsNotNone(ir_state.recover(root,stock))
            self.assertTrue(ir_state.read_store(root)['CARG']['verification_source'].startswith('historical_manifest:'))
            ir_state.remove(root,'CARG')
            self.assertIsNone(ir_state.recover(root,stock))

    def test_operator_repair_replaces_invalidated_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); stock=Stock('SE','Sea Limited','',0)
            ir_state.upsert(root,stock,IRCandidate('https://investor.sea.co.uk/',100,['Incorrect prior identity'],True))
            ir_state.remove(root,'SE')
            replacement=IRCandidate('https://www.sea.com/investor/home',100,['Explicit operator verification'],True,corporate_url='https://www.sea.com/')
            ir_state.upsert(root,stock,replacement,source='operator_verification',confirmed=True)
            record=ir_state.read_store(root)['SE']
            self.assertEqual(record['official_ir_url'],replacement.url)
            self.assertEqual(record['evidence'],['Explicit operator verification'])
            self.assertEqual(record['verification_source'],'operator_verification')
            self.assertFalse(record['invalidated_explicitly'])

    def test_corrupt_store_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'verified_ir.json';path.write_text('{broken')
            with self.assertRaises(ValueError):
                ir_state.upsert(root,Stock('CARG','CarGurus','',0),IRCandidate('https://investors.cargurus.com/',100,['Evidence'],True))
            self.assertEqual(path.read_text(),'{broken')

    def test_status_precedence_and_expected_outcomes(self):
        protected=Event('Call',None,'https://example.com',status='SITE_BLOCKED',protected_media_type='encrypted HLS')
        self.assertEqual(company_outcome([protected],'SITE_BLOCKED'),'EVENT_MEDIA_PROTECTED')
        skipped=Event('Call',None,'https://example.com',status='SKIPPED_OFFICIAL_TRANSCRIPT')
        result=metrics([protected,skipped],[protected,skipped],True)
        self.assertEqual(result['event_failures'],0)
        self.assertEqual(result['media_blocked'],1)
        approval=Event('Call',None,'https://example.com',status='REGISTRATION_APPROVAL_REQUIRED')
        self.assertEqual(metrics([approval],[approval])['registration_approval_required'],1)
        self.assertEqual(metrics([approval],[approval])['media_blocked'],0)

    def test_budget_is_global_not_url_count(self):
        budget=MediaBudget(5)
        self.assertEqual([budget.claim() for _ in range(8)],[True]*5+[False]*3)
        self.assertEqual(budget.used,5)

    def test_registration_exact_domains_and_https(self):
        for url,allowed in [('https://edge.media-server.com/form',True),('https://media-server.com.evil.example/form',False),('http://edge.media-server.com/form',False),('https://wsw.com/form',False)]:
            self.assertEqual(approved_destination(url,('media-server.com',)),allowed)

    def test_latest_earnings_is_same_candidate(self):
        events=[Event('New conference','2026-09-08','https://ir.example.com/new',event_type='conference',webcast_url='https://wsw.com/new'),Event('Q2 earnings','2026-08-05','https://ir.example.com/q2',event_type='earnings_call',webcast_url='https://edge.media-server.com/q2')]
        self.assertEqual(select_candidates(events,1)[0].title,'Q2 earnings')

    def test_saved_segments_reject_empty_nonmonotonic_and_nan(self):
        config=EventConfig(min_transcript_segments=2,min_transcript_characters=10)
        for segments in [[],[{'start':10,'end':11,'text':'welcome call'},{'start':0,'end':1,'text':'goodbye call'}],[{'start':float('nan'),'end':11,'text':'welcome call'},{'start':12,'end':13,'text':'goodbye call'}]]:
            with self.assertRaises(CollectionError):validate_segments(segments,config)

    def test_summary_handles_different_company_metric_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            save_summary(Path(temp),'test',[{'ticker':'FAIL','status':'FAILED'},{'ticker':'CARG','status':'OFFICIAL_TRANSCRIPT_USED','events_discovered':18}])

class FixtureBrowser:
    def __init__(self, config):
        self.config=config;self.context=AsyncMock();self.access_events=[]
        self.assess=AsyncMock(return_value=AccessResult(browser_status=403,http_status=403,blocked=True))
    @asynccontextmanager
    async def session(self):
        self.access_events=[]
        yield self.context

class ModeFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_exel_carg_same_discovery_in_all_event_modes(self):
        for ticker,company,host,count in [('EXEL','Exelixis','ir.exelixis.com',13),('CARG','CarGurus','investors.cargurus.com',18)]:
            observations=[]
            for mode in ['events','transcribable','generated','destinations']:
                with self.subTest(ticker=ticker,mode=mode),tempfile.TemporaryDirectory() as temp:
                    root=Path(temp);stock=Stock(ticker,company,'Safe',4)
                    ir_state.upsert(root,stock,IRCandidate('https://'+host+'/',100,['Verified corporate Investors link'],True))
                    browser=FixtureBrowser(Config(download_root=root,request_delay=0,max_archive_pages=5,events=EventConfig(only=True,generated_only=mode=='generated',discover_registration_destinations=mode=='destinations')))
                    cards=[]
                    for i in range(count):
                        title=company+' Q2 2026 Financial Results Conference Call' if i==0 else company+' Investor Conference '+str(i)
                        date='2026-08-05' if i==0 else '2025-07-01'
                        transcript='<a href="/static-files/transcript">Transcript</a>' if ticker=='CARG' and i==0 else ''
                        webcast=f'<a href="https://edge.media-server.com/mmc/p/fixture{i}">Webcast replay</a>' if ticker=='EXEL' or i<14 else ''
                        cards.append(f'<article><time datetime="{date}">{date}</time><a href="/events/event-details/event-{i:08}">{title}</a>{webcast}{transcript}</article>')
                    html=f'<title>{company} Events</title><main>'+''.join(cards)+'</main>'
                    async def get(client,url,referer='',**kwargs):
                        if 'rss-subscription-links' in url:body=f'<div class="nir-platform">{company}<a href="/rss/news-releases.xml">News</a></div>'
                        elif '/events/event-details/' in url:body=''
                        elif '/event-calendar' in url:body=html
                        elif url.endswith('/'):body='<a href="/event-calendar">Events</a>'
                        else:raise CollectionError('HTTP_FAILED','fixture 404')
                        browser.access_events.append(AccessEvent(url,'http',200))
                        return ResourceResponse(url,200,{'content-type':'text/html'},body.encode(),'http')
                    async def process(processor,event):
                        # Transport/registration/media have separate browser fixtures.
                        event.registration_required=True
                        if mode=='destinations':event.transition('REGISTRATION_DESTINATION_DISCOVERED')
                        elif ticker=='EXEL':event.protected_media_type='encrypted HLS';event.transition('DRM_PROTECTED')
                        elif mode=='generated':event.method='official_transcript';event.transition('SKIPPED_OFFICIAL_TRANSCRIPT')
                        else:event.method='official_transcript';event.transition('TRANSCRIBED')
                    with patch('qualitative_ir_downloader.network.ResourceClient.get',get),patch('qualitative_ir_downloader.event_discovery.find_events_section',AsyncMock(side_effect=CollectionError('SITE_BLOCKED','fixture 403'))),patch('qualitative_ir_downloader.events.EventProcessor.process',process),patch('qualitative_ir_downloader.main.IRDiscovery.discover_investor_relations_site',AsyncMock()) as discover:
                        result=await process_company(browser,stock,'test')
                    discover.assert_not_called()
                    self.assertTrue(result['ir_official']);self.assertTrue(result['ir_public_resource_accessible']);self.assertFalse(result['ir_browser_accessible'])
                    self.assertEqual(result['events_discovered'],count)
                    self.assertEqual(result['events_with_webcasts'],13 if ticker=='EXEL' else 14)
                    runtime=browser.current_company;selected=runtime.selected[0]
                    observations.append((result['official_ir_url'],result['ir_provider'],result['events_discovered'],selected.title,selected.webcast_url,selected.registration_required))
                    if mode=='generated':self.assertEqual(result['status'],'EVENT_MEDIA_PROTECTED' if ticker=='EXEL' else 'SKIPPED_OFFICIAL_TRANSCRIPT')
            self.assertTrue(all(o==observations[0] for o in observations))

    async def test_failed_explicit_rediscovery_retains_identity_all_modes(self):
        for mode in ['normal','events','transcribable','generated','destinations']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);stock=Stock('CARG','CarGurus','Safe',10)
                ir_state.upsert(root,stock,IRCandidate('https://investors.cargurus.com/',100,['Verified corporate navigation'],True))
                browser=FixtureBrowser(Config(download_root=root,revalidate_ir=True,events=EventConfig(only=mode!='normal',generated_only=mode=='generated',discover_registration_destinations=mode=='destinations')))
                from qualitative_ir_downloader.models import ArchiveResult,Document
                from types import SimpleNamespace
                provider=SimpleNamespace(name='fixture',evidence=[],discover_news=AsyncMock(return_value=ArchiveResult()),discover_reports=AsyncMock(return_value=ArchiveResult()))
                with patch('qualitative_ir_downloader.main.IRDiscovery.discover_investor_relations_site',AsyncMock(return_value=None)),patch('qualitative_ir_downloader.main.collect_events',AsyncMock(return_value={'events_discovered':18,'event_failures':0,'event_outcome':'SKIPPED_OFFICIAL_TRANSCRIPT'})),patch('qualitative_ir_downloader.main.find_news_section',AsyncMock(return_value=[])),patch('qualitative_ir_downloader.main.find_reports_section',AsyncMock(return_value=[])),patch('qualitative_ir_downloader.company_context.select_provider',AsyncMock(return_value=provider)):
                    result=await process_company(browser,stock,'test')
                self.assertTrue(result['ir_official']);self.assertNotEqual(result['status'],'IR_DISCOVERY_FAILED')
                self.assertIsNotNone(ir_state.load(root,stock))

    async def test_full_attempt_budget_excludes_cheap_official_skip(self):
        from types import SimpleNamespace
        from qualitative_ir_downloader.events import EventProcessor
        from qualitative_ir_downloader.manifest import Manifest
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'Events').mkdir();stock=Stock('ACME','Acme','Safe',4)
            page=SimpleNamespace(on=lambda *a:None,remove_listener=lambda *a:None,close=AsyncMock())
            context=SimpleNamespace(new_page=AsyncMock(return_value=page))
            browser=SimpleNamespace(config=Config(download_root=root,events=EventConfig(generated_only=True,force=True,max_media_attempts=2)),media_budget=MediaBudget(2),goto=AsyncMock(side_effect=CollectionError('SITE_BLOCKED','fixture 403')),navigation_status={})
            processor=EventProcessor(browser,context,stock,root,Manifest(root,stock,'test'))
            official=Event('Official',None,'https://ir.example.com/official',official_transcript_url='https://ir.example.com/transcript')
            await processor.process(official)
            self.assertEqual(browser.media_budget.used,0)
            with patch.object(processor,'search_official_transcript',AsyncMock(return_value=None)):
                events=[Event('Call '+str(i),None,'https://ir.example.com/'+str(i),webcast_url='https://player.example.com/'+str(i)) for i in range(4)]
                for event in events:
                    try:await processor.process(event)
                    except CollectionError:pass
            self.assertEqual(browser.goto.await_count,2)
            self.assertEqual(browser.media_budget.used,2)
            self.assertEqual([e.status for e in events[-2:]],['MEDIA_ATTEMPT_LIMIT']*2)
