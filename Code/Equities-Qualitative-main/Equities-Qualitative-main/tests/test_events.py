import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import asdict
from datetime import date
from unittest.mock import AsyncMock,patch
from qualitative_ir_downloader.event_models import Event,TranscriptSegment
from qualitative_ir_downloader.event_discovery import parse_events,parse_detail,merge_events,event_type,is_future
from qualitative_ir_downloader.registration import field_meaning,access_state
from qualitative_ir_downloader.webcast_providers.generic import candidate,media_type,structured_sources
from qualitative_ir_downloader.webcast_media import MediaAcquirer,media_info,validate_duration
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.models import Stock,Link,CollectionError
from qualitative_ir_downloader.scoring import section_score
from qualitative_ir_downloader.events import completed_match
from qualitative_ir_downloader.transcript_pdf import transcript_record,create_pdf
from qualitative_ir_downloader.privacy import redact_url,safe_text
from qualitative_ir_downloader.main import parser, load_previously_verified_ir
from qualitative_ir_downloader.ir_discovery import IRDiscovery

class EventTests(unittest.TestCase):
    def test_reuse_only_strong_verified_ir_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({"ticker":"EXEL","company_name":"Exelixis","ir_official":True,"investor_relations_url":"https://ir.exelixis.com/"}), encoding="utf-8")
            result = load_previously_verified_ir(root, Stock("EXEL", "Exelixis", "", 1))
            self.assertIsNotNone(result)
            self.assertEqual(result.url, "https://ir.exelixis.com/")

    def test_unverified_manifest_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({"ticker":"ZZZ","company_name":"Example","ir_official":False,"investor_relations_url":"https://ir.example.com/"}), encoding="utf-8")
            self.assertIsNone(load_previously_verified_ir(root, Stock("ZZZ", "Example", "", 1)))

    def test_ir_search_relevance_filter(self):
        kept = IRDiscovery.relevant_search_results(["https://www.netweather.tv/", "https://ir.exelixis.com/"], "Exelixis", "EXEL")
        self.assertEqual(kept, ["https://ir.exelixis.com/"])
    def test_event_sections(self):
        for text in ['Events & Presentations','Past Events','Upcoming Events & Presentations','Investor Events']:
            self.assertGreaterEqual(section_score(Link('https://ir.acme.com/events',text),'events').score,12)
        self.assertLess(section_score(Link('https://ir.acme.com/community-events','Community Events'),'events').score,0)
    def test_archive_and_webcast(self):
        html='<article><time datetime="2026-08-05">August 5, 2026</time><a href="/events/event-details/q2-results-call">Q2 Financial Results Call</a><a href="https://player.example.com/replay">Webcast replay</a><a href="/slides.pdf">Presentation</a></article>'
        e=parse_events(html,'https://ir.acme.com/events')[0]
        self.assertEqual(e.date,'2026-08-05');self.assertEqual(e.event_type,'earnings_call');self.assertTrue(e.replay_available);self.assertEqual(len(e.presentation_urls),1)
    def test_presentation_only(self):
        html='<article><a href="/events/event-details/investor-presentation">Investor Presentation</a><a href="/slides.pdf">Presentation</a></article>'
        self.assertIsNone(parse_events(html,'https://ir.acme.com/events')[0].webcast_url)
    def test_upcoming_event(self):
        self.assertTrue(is_future(Event('Future','2099-01-01','https://ir.acme.com/e'),date(2026,9,8)))
    def test_event_dedup(self):
        a=Event('Q2 Call','2026-08-05','https://ir.acme.com/events/a')
        b=Event('Q2 Call','2026-08-05','https://ir.acme.com/events/b',webcast_url='https://player.example.com/a')
        events=merge_events([a,b]);self.assertEqual(len(events),1);self.assertTrue(events[0].webcast_url)
    def test_direct_cards_remain_distinct(self):
        events=[Event('One','2026-08-05','https://ir.acme.com/events#event-one'),Event('Two','2026-08-04','https://ir.acme.com/events#event-two')]
        self.assertEqual(len(merge_events(events)),2)
    def test_registration_mapping(self):
        for label,key in [('First Name','first_name'),('Last Name','last_name'),('Name','name'),('E-mail','email'),('Organisation','company'),('Occupation','occupation'),('Job Title','occupation')]:self.assertEqual(field_meaning(label),key)
    def test_media_types(self):
        for suffix,kind in [('mp3','audio'),('m4a','audio'),('mp4','video'),('m3u8','hls'),('mpd','dash')]:self.assertEqual(candidate('https://media.example.com/replay.'+suffix).media_type,kind)
        self.assertEqual(candidate('https://media.example.com/opaque','audio/mpeg').media_type,'audio')
    def test_tracking_rejected(self):
        self.assertIsNone(candidate('https://media.example.com/tracking.mp3'))
        self.assertIsNone(candidate('https://media.example.com/effect.mp3',duration=2))
    def test_structured_sources(self):
        self.assertEqual(len(structured_sources({'sources':[{'src':'https://media.example.com/replay.m3u8'}]})),1)
    def test_duration(self):
        for duration in [None,2,7*3600]:
            with self.assertRaises(CollectionError):validate_duration(duration,EventConfig())
        validate_duration(3600,EventConfig())
    def test_malformed_media(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'bad.mp3';p.write_text('not audio')
            with self.assertRaises(CollectionError):media_info(p)
    def test_access_states(self):
        for text,state in [('Please verify your email','EMAIL_VERIFICATION_REQUIRED'),('Enter your password','AUTH_REQUIRED'),('Verify that you are human','CAPTCHA_REQUIRED')]:self.assertEqual(access_state(text),state)
    def test_signed_url_redaction(self):
        value=redact_url('https://media.example.com/replay?event=123&token=private&X-Amz-Signature=secret')
        self.assertIn('event=123',value);self.assertNotIn('private',value);self.assertNotIn('secret',value)
        self.assertNotIn('a@example.com',safe_text('Submitting a@example.com'))
    def test_transcript_json_pdf_and_resume(self):
        from pypdf import PdfReader
        stock=Stock('ACME','Acme','Safe',4)
        event=Event('Q2 Earnings Call','2026-08-05','https://ir.acme.com/events/q2',webcast_url='https://player.example.com/q2',method='generated_transcript',transcription_engine='faster-whisper',transcription_model='large-v3')
        segments=[asdict(TranscriptSegment(i*10,i*10+8,'Revenue was $25 million, up 12%. This is a test transcript.')) for i in range(100)]
        record=transcript_record(stock,event,segments)
        self.assertEqual(record['segments'][2]['start'],20)
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'company';(folder/'Events').mkdir(parents=True)
            pdf=folder/'Events'/'test.pdf';create_pdf(pdf,record)
            reader=PdfReader(pdf);self.assertGreater(len(reader.pages),1)
            self.assertIn('12%',reader.pages[1].extract_text())
            import re
            for page in reader.pages:
                self.assertIsNone(re.fullmatch(r'\[\d{2}:\d{2}:\d{2}\]',page.extract_text().strip().splitlines()[-1]))
            event.local_pdf='Events/test.pdf';event.local_json='Events/test.json';event.status='TRANSCRIBED'
            (folder/event.local_json).write_text(json.dumps(record))
            (folder/'manifest.json').write_text(json.dumps({**asdict(stock),'events':[event.record()]}))
            self.assertIsNotNone(completed_match(event,Path(t),stock))
            pdf.unlink();self.assertIsNone(completed_match(event,Path(t),stock))
    def test_cli_modes(self):
        args=parser().parse_args(['--events-only','--event-limit','1']);self.assertTrue(args.events_only);self.assertEqual(args.event_limit,1)
        self.assertIsNone(args.whisper_model)
        self.assertEqual(args.transcription_profile,'balanced')

class ManifestMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_hls_stops(self):
        with tempfile.TemporaryDirectory() as t:
            a=MediaAcquirer(AsyncMock(),EventConfig(),Path(t));a.text=AsyncMock(return_value=('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="secret"\n#EXT-X-ENDLIST','https://media.example.com/a.m3u8'))
            with self.assertRaises(CollectionError) as cm:await a.hls('https://media.example.com/a.m3u8','')
            self.assertEqual(cm.exception.code,'DRM_PROTECTED')
    async def test_protected_dash_stops(self):
        with tempfile.TemporaryDirectory() as t:
            a=MediaAcquirer(AsyncMock(),EventConfig(),Path(t));a.text=AsyncMock(return_value=('<MPD><Period><ContentProtection/></Period></MPD>','https://media.example.com/a.mpd'))
            with self.assertRaises(CollectionError) as cm:await a.dash('https://media.example.com/a.mpd','')
            self.assertEqual(cm.exception.code,'DRM_PROTECTED')
    async def test_live_hls_not_partial_transcript(self):
        with tempfile.TemporaryDirectory() as t:
            a=MediaAcquirer(AsyncMock(),EventConfig(),Path(t));a.text=AsyncMock(return_value=('#EXTM3U\n#EXTINF:10,\na.ts','https://media.example.com/a.m3u8'))
            with self.assertRaises(CollectionError) as cm:await a.hls('https://media.example.com/a.m3u8','')
            self.assertEqual(cm.exception.code,'NO_REPLAY_AVAILABLE')

class RealMediaFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_hls_and_dash_normalization(self):
        import subprocess,shutil
        from urllib.parse import urlsplit
        from qualitative_ir_downloader.webcast_media import ffmpeg_path
        config=EventConfig(min_duration=60)
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source';source.mkdir()
            ffmpeg=ffmpeg_path(config)
            for kind in ['direct','hls','dash']:
                folder=source/kind;folder.mkdir()
                filename={'direct':'audio.m4a','hls':'replay.m3u8','dash':'replay.mpd'}[kind]
                args=[ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','65','-c:a','aac']
                if kind=='hls':args+=['-f','hls','-hls_time','10','-hls_playlist_type','vod']
                if kind=='dash':args+=['-f','dash','-seg_duration','10']
                subprocess.run(args+[filename],cwd=folder,check=True,capture_output=True)
                acquirer=MediaAcquirer(AsyncMock(),config,root/'downloads'/kind)
                async def fetch(url,target,referer='',headers=None,limit=None):
                    shutil.copy2(folder/Path(urlsplit(url).path).name,target)
                    return url,'audio/mp4'
                acquirer.fetch=fetch
                c=candidate('https://media.example.com/'+filename)
                audio,info=await acquirer.acquire(c)
                self.assertTrue(audio.exists());self.assertGreater(info['duration'],64)
                self.assertEqual(info['sample_rate'],16000);self.assertEqual(info['channels'],1)
