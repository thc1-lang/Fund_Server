import json,tempfile,unittest,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from qualitative_ir_downloader.company_identity import identify,domain_candidates
from qualitative_ir_downloader.ir_evidence import ownership,rank_result
from qualitative_ir_downloader.ir_discovery import IRDiscovery,corporate_identity
from qualitative_ir_downloader.discovery_cache import DiscoveryCache
from qualitative_ir_downloader.event_discovery import parse_events,parse_event_json
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.event_registry import EventRegistry,merge_event_state,event_match_method
from qualitative_ir_downloader.events import select_candidates
from qualitative_ir_downloader.event_outcomes import metrics as event_metrics,company_outcome
from qualitative_ir_downloader.config import Config
FIXTURES=Path(__file__).parent/'fixtures'/'discovery'
class DiscoveryIdentityTests(unittest.TestCase):
    def test_security_suffixes_removed_without_losing_brand(self):
        cases={'Atour Lifestyle Holdings Limited Sponsored ADR':'Atour Lifestyle','BeOne Medicines Ltd. - Sponsored ADR':'BeOne Medicines','Trip.com Group Limited':'Trip.com Group','Global-e Online':'Global-e Online','Acme Corporation Class B Common Stock':'Acme','Acme PLC American Depositary Shares':'Acme'}
        for raw,expected in cases.items():
            with self.subTest(raw=raw):
                identity=identify(raw);self.assertEqual(identity.raw_company_name,raw);self.assertEqual(identity.normalized_company_name,expected)
                self.assertFalse(any('sponsored' in d or 'adr' in d for d in domain_candidates(raw)))
        self.assertIn('trip.com',domain_candidates('Trip.com Group Limited'))
    def test_aliases_are_hints_not_verified_ownership(self):
        self.assertIn('Atour',identify('Atour Lifestyle').brand_candidates)
        self.assertIn('Globale',identify('Global-e Online').brand_candidates)
        self.assertLess(ownership('https://credo.com/','Credo Technology Group','CRDO')[0],90)
        self.assertFalse(corporate_identity('https://credogroup.com/','Credo Group','Credo Group About Careers Products Investors','Credo Technology Group'))
    def test_atat_unrelated_hostname_is_investigated_and_page_verifies(self):
        from bs4 import BeautifulSoup
        html=(FIXTURES/'atat_ir.html').read_text();soup=BeautifulSoup(html,'html.parser')
        self.assertGreater(rank_result('https://ir.yaduo.com/','Atour Lifestyle Holdings Limited Sponsored ADR','ATAT',soup.title.text,soup.get_text()),0)
        score,reasons=ownership('https://ir.yaduo.com/','Atour Lifestyle Holdings Limited Sponsored ADR','ATAT',soup.title.text,soup.get_text())
        self.assertGreaterEqual(score,90)
        self.assertLess(ownership('https://ir.yaduo.com/','Atour Lifestyle','ATAT',search_title=soup.title.text,snippet='Atour Lifestyle ATAT')[0],90)
    def test_anet_and_rmd_blocked_identity_uses_independent_corporate_evidence(self):
        for name,ticker,domain in [('Arista Networks','ANET','arista.com'),('ResMed','RMD','resmed.com')]:
            score,_=ownership('https://investors.'+domain+'/',name,ticker,search_title=name+' - Investor Relations',snippet=name+' '+ticker+' earnings',corporate_domain=domain)
            self.assertGreaterEqual(score,90)
            unverified,_=ownership('https://investors.'+domain+'/',name,ticker,search_title=name+' - Investor Relations',snippet=name+' '+ticker+' earnings')
            self.assertLess(unverified,90)
    def test_credo_semiconductor_domain_is_candidate_without_hardcoded_url(self):
        self.assertGreater(rank_result('https://investors.credosemi.com/','Credo Technology Group','CRDO','Credo Investor Relations','Credo Technology Group CRDO'),0)
        self.assertLess(ownership('https://credogroup.com/','Credo Technology Group','CRDO',title='Credo Group',body='Credo Group Investor Relations Careers')[0],90)
    def test_corporate_brand_requires_independent_identity_when_legal_name_absent(self):
        args=('https://credosemi.com/','Credo | We Connect','About Credo Products Careers Investors ©2026 Credo, Inc.','Credo Technology Group')
        self.assertFalse(corporate_identity(*args))
        self.assertTrue(corporate_identity(*args,search_support='Credo Technology Group Investor Relations',ticker='CRDO'))
        self.assertFalse(corporate_identity(*args,search_support='Credo Investor Relations',ticker='CRDO'))
        self.assertTrue(corporate_identity('https://sezzle.com/','Buy Now Pay Later','About Careers Investors Copyright 2026 Sezzle Inc.','Sezzle Inc.'))

    def test_atat_public_http_identity_survives_separate_browser_403(self):
        from qualitative_ir_downloader.models import IRCandidate,AccessResult
        from qualitative_ir_downloader.network import ResourceResponse
        with tempfile.TemporaryDirectory() as temp:
            d=IRDiscovery(SimpleNamespace(config=Config(download_root=Path(temp))))
            d.identity=identify('Atour Lifestyle Holdings Limited Sponsored ADR')
            candidate=IRCandidate('https://ir.yaduo.com/',access=AccessResult(browser_status=403,blocked=True,http_status=200,http_accessible=True))
            response=ResourceResponse(candidate.url,200,{'content-type':'text/html'},(FIXTURES/'atat_ir.html').read_bytes(),'http')
            self.assertTrue(d.accept_public_html(candidate,response,d.identity.raw_company_name,'ATAT'))
            self.assertTrue(candidate.official);self.assertTrue(candidate.access.blocked)
            self.assertIn('yaduo',d.identity.brand_candidates)

    def test_exchange_prefixed_investor_navigation(self):
        from qualitative_ir_downloader.ir_discovery import endorsed_candidate
        from qualitative_ir_downloader.models import Link
        candidate=endorsed_candidate('https://beonemedicines.com/',Link('https://ir.beonemedicines.com/','NASDAQ Investors'))
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.official)
        self.assertIsNone(endorsed_candidate('https://acme.com/',Link('https://unrelated.com/','NASDAQ news')))
    def test_corporate_search_prefers_global_domain_before_regional_site(self):
        with tempfile.TemporaryDirectory() as temp:
            d=IRDiscovery(SimpleNamespace(config=Config(download_root=Path(temp))))
            urls=['https://www.resmed.co.uk/','https://www.resmed.com/en-us']
            d.search_evidence={u:{'title':'ResMed Sleep Health','snippet':'ResMed corporate information'} for u in urls}
            d.corporate_search_identity(urls,'ResMed')
            self.assertEqual(d.ecosystem.official_corporate_domain,'resmed.com')

    def test_search_discovered_same_domain_ir_subdomain_is_promoted_generically(self):
        with tempfile.TemporaryDirectory() as temp:
            d=IRDiscovery(SimpleNamespace(config=Config(download_root=Path(temp))))
            d.ticker='RMD'
            url='https://investor.resmed.com/'
            d.search_evidence[url]={'title':'ResMed Investor Relations','snippet':'ResMed (RMD) earnings and financial results'}
            d.corporate_search_identity([url],'RESMED INC')
            self.assertEqual(d.ecosystem.official_corporate_domain,'resmed.com')
            candidates = [candidate.url for candidate in d.conventional_ir_candidates()]
            self.assertIn('https://investor.resmed.com/', candidates)
            self.assertIn('https://resmed.com/investor-relations', candidates)

    def test_unrelated_investor_subdomain_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            d=IRDiscovery(SimpleNamespace(config=Config(download_root=Path(temp))))
            d.ticker='RMD'
            url='https://investor.unrelated.com/'
            d.search_evidence[url]={'title':'ResMed Investor Relations','snippet':'ResMed RMD earnings'}
            d.corporate_search_identity([url],'RESMED INC')
            self.assertIsNone(d.ecosystem.official_corporate_domain)

    def test_noise_results_are_never_candidates(self):
        for url in ['https://timeanddate.com/','https://homedepot.com/','https://minecraft.net/','https://cinema.example.com/','https://crypto.example.com/']:
            self.assertLess(rank_result(url,'Sezzle','SEZL','Cinema and games','No company evidence'),0)
    def test_pltr_rendered_financial_cards(self):
        events=parse_events((FIXTURES/'pltr_events.html').read_text(encoding='utf-8'),'https://investors.palantir.com/events')
        self.assertEqual(len(events),3);self.assertEqual(events[0].date,'2026-08-03');self.assertEqual(events[0].event_type,'earnings_call')
        self.assertIn('youtube.com',events[0].webcast_url);self.assertEqual(events[0].title,'Q2 2026 Earnings')
    def test_q4_event_json_and_schema_event(self):
        q4={'GetEventListResult':[{'EventName':'NVIDIA Q2 Earnings','EventDate':'2026-08-26T16:00:00','EventUrl':'/events/event-details/quarterly','WebcastLink':'https://player.example.com/call'}]}
        events=parse_event_json(q4,'https://investor.nvidia.com/')
        self.assertEqual(len(events),1);self.assertEqual(events[0].date,'2026-08-26')
        schema={'@type':'Event','name':'Quarterly call','startDate':'2026-08-26','url':'/event-details/call'}
        self.assertEqual(len(parse_event_json(schema,'https://ir.example.com')),1)
    def test_empty_listing_does_not_suppress_new_discovery(self):
        from qualitative_ir_downloader.event_registry import EventRegistry
        from qualitative_ir_downloader.events_config import EventConfig
        from qualitative_ir_downloader.models import Stock
        with tempfile.TemporaryDirectory() as temp:
            reg=EventRegistry(temp);stock=Stock('PLTR','Palantir Technologies','Safe',4)
            reg.save_listing(stock,'https://investors.palantir.com/',[])
            self.assertIsNone(reg.listing(stock,'https://investors.palantir.com/',EventConfig()))

    def test_transport_and_query_cache_expire(self):
        with tempfile.TemporaryDirectory() as temp:
            cache=DiscoveryCache(temp);cache.failure('https://example.com/','CONNECTION_REFUSED')
            self.assertIsNotNone(DiscoveryCache(temp).blocked('https://example.com/a'))
            cache.data['transport']['example.com']['retry_after']=time.time()-1;self.assertIsNone(cache.blocked('https://example.com/'))
            cache.store_query('Acme  Investors',[{'url':'https://ir.acme.com','title':'Acme','snippet':'Acme'}]);self.assertIsNotNone(cache.query('acme investors'))
class SearchCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_query_skips_navigation(self):
        with tempfile.TemporaryDirectory() as temp:
            b=SimpleNamespace(config=Config(download_root=Path(temp)),goto=AsyncMock())
            d=IRDiscovery(b);d.cache.store_query('"Acme" investors',[{'url':'https://ir.acme.com/','title':'Acme Investors','snippet':'Acme'}])
            self.assertEqual(await d.search(AsyncMock(),'"Acme" investors'),['https://ir.acme.com/']);b.goto.assert_not_called();self.assertEqual(d.search_queries,0)
    async def test_irrelevant_results_trigger_alternate_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            b=SimpleNamespace(config=Config(download_root=Path(temp)),goto=AsyncMock());page=SimpleNamespace(locator=lambda s:SimpleNamespace(evaluate_all=AsyncMock(return_value=[{'url':'https://timeanddate.com/','title':'Calendar','snippet':'World time'}])))
            d=IRDiscovery(b);d.identity=identify('Sezzle');d.ticker='SEZL'
            self.assertEqual(await d.search(page,'"Sezzle" investor relations'),[]);self.assertEqual(b.goto.await_count,2)
            self.assertTrue(any(e.code=='SEARCH_RESULTS_IRRELEVANT' for e in d.errors))

class CorporateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_corporate_html_verifies_without_repeating_timed_out_browser(self):
        from qualitative_ir_downloader.network import ResourceResponse
        with tempfile.TemporaryDirectory() as temp:
            browser=SimpleNamespace(config=Config(download_root=Path(temp)),current_company=SimpleNamespace(scan_only=True),goto=AsyncMock())
            d=IRDiscovery(browser);d.ticker='CRDO'
            url='https://investors.credosemi.com/'
            d.search_evidence[url]={'title':'Credo Investor Relations','snippet':'Credo Technology Group CRDO'}
            d.cache.failure('https://credosemi.com/','TimeoutError')
            html=b'<title>Credo | We Connect</title><p>About Credo Products Careers</p><a href="https://investors.credosemi.com/">Investors</a>'
            response=ResourceResponse('https://credosemi.com/',200,{'content-type':'text/html'},html,'http')
            with patch('qualitative_ir_downloader.network.ResourceClient.http',AsyncMock(return_value=response)):
                candidates=await d.discover_ir_from_corporate_site(SimpleNamespace(context=AsyncMock()),[url],'Credo Technology Group')
            self.assertEqual(len(candidates),1);self.assertTrue(candidates[0].official)
            browser.goto.assert_not_called()
    async def test_http_denial_does_not_trigger_browser_retry(self):
        from qualitative_ir_downloader.network import ResourceResponse
        with tempfile.TemporaryDirectory() as temp:
            browser=SimpleNamespace(config=Config(download_root=Path(temp)),current_company=SimpleNamespace(scan_only=True),goto=AsyncMock())
            d=IRDiscovery(browser)
            response=ResourceResponse('https://acme.com/',403,{},b'Access denied','http')
            with patch('qualitative_ir_downloader.network.ResourceClient.http',AsyncMock(return_value=response)):
                result=await d.discover_ir_from_corporate_site(SimpleNamespace(context=AsyncMock()),['https://acme.com/'],'Acme')
            self.assertEqual(result,[]);browser.goto.assert_not_called()

class EventReconciliationTests(unittest.TestCase):
    def make_events(self):
        cached=Event('Q2 2026 Incyte Corporation Earnings Conference Call','2026-07-28','https://investor.incyte.com/event?id=42',webcast_url='https://media.example/call?signature=old',method='generated_transcript',status='TRANSCRIBED',local_pdf='Events/incyte.pdf',local_json='Events/incyte.json',transcript_qa={'status':'PASS'},transcription_model='medium.en',protected_media_type=None)
        fresh=Event('Q2 2026 Incyte Corporation Earnings Conference Call','2026-07-28','https://investor.incyte.com/event?id=42&utm_source=refresh',webcast_url='https://media.example/call?signature=new')
        return cached,fresh

    def test_date_title_match_preserves_generated_state(self):
        cached,fresh=self.make_events();merged=merge_event_state(fresh,cached)
        self.assertEqual(event_match_method(fresh,cached),'canonical_url');self.assertEqual(merged.local_pdf,cached.local_pdf);self.assertEqual(merged.method,'generated_transcript')

    def test_fresh_missing_fields_never_overwrites_enrichment(self):
        cached,fresh=self.make_events();fresh.official_transcript_url=None;fresh.transcript_qa={};merged=merge_event_state(fresh,cached)
        self.assertEqual(merged.transcript_qa,{'status':'PASS'});self.assertEqual(merged.transcription_model,'medium.en')

    def test_signed_webcast_change_does_not_change_identity(self):
        cached,fresh=self.make_events();fresh.event_url='https://investor.incyte.com/event?id=42&token=temporary';self.assertEqual(event_match_method(fresh,cached),'canonical_url')

    def test_date_title_match_when_canonical_url_changes(self):
        cached,fresh=self.make_events();fresh.event_url='https://investor.incyte.com/events/q2-2026';cached.event_url='https://investor.incyte.com/event?id=42';self.assertEqual(event_match_method(fresh,cached),'date_title')

    def test_registry_reconcile_retains_cached_event(self):
        with tempfile.TemporaryDirectory() as temp:
            stock=SimpleNamespace(ticker='INCY',company_name='Incyte')
            reg=EventRegistry(temp);cached,fresh=self.make_events();reg.record(stock,cached,temp)
            merged=reg.reconcile_events(stock,[fresh]);self.assertEqual(len(merged),1);self.assertEqual(merged[0].local_json,cached.local_json)

    def test_protected_state_survives_fresh_listing(self):
        cached,fresh=self.make_events();cached.method=None;cached.local_pdf=None;cached.local_json=None;cached.protected_media_type='encrypted HLS';cached.status='EVENT_MEDIA_PROTECTED';merged=merge_event_state(fresh,cached)
        self.assertEqual(merged.protected_media_type,'encrypted HLS');self.assertEqual(merged.status,'EVENT_MEDIA_PROTECTED')

    def test_require_new_skips_completed_generated_event(self):
        cached,fresh=self.make_events();fresh.cached_outcome='artifact';fresh.cached_method='generated_transcript';fresh.already_completed=True
        other=Event('Q1 2026 Earnings','2026-04-30','https://investor.incyte.com/event?q1',webcast_url='https://media.example/q1')
        selected=select_candidates([fresh,other],1,SimpleNamespace(require_new_generated_event=True))
        self.assertEqual(selected[0].title,'Q1 2026 Earnings')

    def test_invalid_completed_artifact_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            stock=SimpleNamespace(ticker='INCY',company_name='Incyte');reg=EventRegistry(temp);cached,fresh=self.make_events();reg.record(stock,cached,temp)
            self.assertIsNone(reg.lookup(stock,fresh,SimpleNamespace(force=False,protected_cache_hours=168,event_cache_hours=24,generated_only=True,min_transcript_segments=10,min_transcript_characters=1000)))

    def test_valid_events_cannot_report_discovery_failure(self):
        event=Event('Q2 2026 Earnings','2026-07-28','https://ir.example/event',webcast_url='https://media.example/replay')
        self.assertNotEqual(event_metrics([event],[],True)['event_outcome'],'EVENT_DISCOVERY_FAILED')

    def test_access_block_precedes_event_discovery_failure(self):
        event=Event('Q2 2026 Earnings','2026-07-28','https://ir.example/event',status='IR_VERIFIED_ACCESS_BLOCKED')
        self.assertEqual(company_outcome([event],'EVENT_DISCOVERY_FAILED'),'IR_VERIFIED_ACCESS_BLOCKED')
