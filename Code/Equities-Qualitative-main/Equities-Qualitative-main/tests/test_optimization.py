import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.transcript_qa import assess,performance
from qualitative_ir_downloader.transcription_profiles import resolve
from qualitative_ir_downloader.transcription import transcribe
from qualitative_ir_downloader.webcast_providers.generic import candidate
from qualitative_ir_downloader.webcast_providers.zoom import is_zoom_recording,exposed_sources

class QualityTests(unittest.TestCase):
    def test_rates_and_timestamps(self):
        q=assess([{'start':0,'end':10,'text':'one two three'},{'start':9,'end':20,'text':'four five six'}],60)
        self.assertEqual(q['words_per_minute'],6)
        self.assertEqual(q['segments_per_minute'],2)
        self.assertEqual(q['characters_per_minute'],26)
        self.assertEqual(q['overlapping_timestamp_count'],1)
        self.assertEqual(q['non_monotonic_timestamp_count'],0)
        self.assertEqual(q['status'],'TRANSCRIPT_QA_WARNING')
        self.assertEqual(performance(30,120)['rtf'],.25)
        self.assertEqual(performance(30,120)['speed_x'],4)

    def test_advancing_timestamp_loop_warns_without_rewriting(self):
        segments=[{'start':i,'end':i+1,'text':'Also on this page you can find our slide deck'} for i in range(600)]
        q=assess(segments,600)
        self.assertGreater(q['duplicate_text_ratio'],.99)
        self.assertIn('repeated_phrase_loop',q['warnings'])
        self.assertIn('dense_short_segments',q['warnings'])
        self.assertEqual(len(segments),600)

    def test_ordinary_speech_passes(self):
        q=assess([{'start':0,'end':5,'text':'Thank you for joining today.'},{'start':6,'end':10,'text':'We will now discuss the quarter.'}],60)
        self.assertEqual(q['status'],'PASS')

class ProfileTests(unittest.TestCase):
    @patch('qualitative_ir_downloader.transcription_profiles.hardware',return_value={'physical_cores':4,'logical_cores':8,'name':'fixture'})
    def test_profiles_and_overrides(self,_):
        self.assertNotEqual(resolve(EventConfig())['model'],'large-v3')
        self.assertEqual(resolve(EventConfig(transcription_profile='maximum_accuracy'))['model'],'large-v3')
        self.assertEqual(resolve(EventConfig(model='small.en',cpu_threads=2))['cpu_threads'],2)
        self.assertEqual(resolve(EventConfig(model='small.en'))['model'],'small.en')
        self.assertFalse(resolve(EventConfig())['word_timestamps'])
        self.assertFalse(resolve(EventConfig())['condition_on_previous_text'])
        self.assertEqual(resolve(EventConfig(language=None))['model'],'medium')

    def test_generator_appended_once_and_cpu_settings_applied(self):
        with tempfile.TemporaryDirectory() as temp,patch('faster_whisper.WhisperModel') as model,patch('qualitative_ir_downloader.transcription_profiles.hardware',return_value={'physical_cores':4,'logical_cores':8,'name':'fixture'}):
            raw=[SimpleNamespace(start=i*10,end=i*10+5,text=f'Unmodified figure {i} dollars') for i in range(3)]
            model.return_value.transcribe.return_value=(iter(raw),SimpleNamespace(language='en',duration=30))
            cfg=EventConfig(device='cpu',model='small.en',cpu_threads=2,batch_size=1,min_transcript_segments=1,min_transcript_characters=1,model_cache=Path(temp))
            event=Event('fixture',None,'https://example.com',duration_seconds=30)
            result=transcribe(Path(temp)/'audio.wav',cfg,event,Path(temp)/'checkpoint.json')
            self.assertEqual([s['text'] for s in result],[s.text for s in raw])
            self.assertEqual(model.call_args.kwargs['cpu_threads'],2)
            self.assertEqual(model.call_args.kwargs['num_workers'],1)
            self.assertTrue(json.loads((Path(temp)/'checkpoint.json').read_text())['complete'])
            self.assertIn('fingerprint',event.transcription_config)

    def test_cancel_preserves_incomplete_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp,patch('faster_whisper.WhisperModel') as model:
            model.return_value.transcribe.return_value=(iter([SimpleNamespace(start=0,end=5,text='Speech')]),SimpleNamespace(language='en',duration=30))
            stop=threading.Event();stop.set()
            with self.assertRaises(InterruptedError):
                transcribe('audio',EventConfig(device='cpu',model='small.en',batch_size=1,model_cache=Path(temp)),Event('fixture',None,'x'),Path(temp)/'checkpoint.json',stop)
            record=json.loads((Path(temp)/'checkpoint.json').read_text())
            self.assertFalse(record['complete']);self.assertTrue(record['interrupted']);self.assertFalse(record['resume_supported'])

class MediaTests(unittest.TestCase):
    def test_audio_always_outranks_video(self):
        self.assertGreater(candidate('https://example.com/a.m4a').score,candidate('https://example.com/archive.mp4',source='media_element',duration=3600).score)
        self.assertEqual(candidate('https://example.com/archive.mp4').media_type,'video')

    def test_zoom_public_source_detection(self):
        self.assertTrue(is_zoom_recording('https://investor.zoom.us/rec/share/id'))
        self.assertFalse(is_zoom_recording('https://zoom.us.evil.example/rec/share/id'))
        self.assertEqual(exposed_sources({'audioOnlyURL':'https://ssrweb.zoom.us/replay/a.m4a'})[0].media_type,'audio')

    def test_zoom_signed_query_hidden(self):
        from qualitative_ir_downloader.privacy import redact_url
        value=redact_url('https://ssrweb.zoom.us/replay/a.mp4?token=private&unusualField=secret')
        self.assertNotIn('private',value);self.assertNotIn('secret',value)
        self.assertIn('signed-query-redacted',value)

class TargetedTests(unittest.TestCase):
    def test_local_repetition_warns_even_when_global_density_is_normal(self):
        from qualitative_ir_downloader.transcript_qa import assess
        segments=[{'start':0,'end':30,'text':'Yeah, '*30}, {'start':30,'end':60,'text':'The company discussed its revenue and guidance.'}]
        qa=assess(segments,60)
        self.assertEqual(qa['local_repetition_segment_indices'],[0])
        self.assertEqual(qa['status'],'TRANSCRIPT_QA_WARNING')
        self.assertEqual(segments[0]['text'],'Yeah, '*30)

    def test_zoom_cards_preserve_title_date_and_merge_archive_link(self):
        from qualitative_ir_downloader.event_discovery import parse_events
        html='''<a href="/events/event-details/quarterly-2027">Zoom Second Quarter Fiscal Year 2027 Earnings Webinar</a>
        <div class="event-card"><h3>August 25, 2026</h3><h3 class="card-title">Zoom Second Quarter Fiscal Year 2027 Earnings Webinar</h3>
        <a href="https://wsw.com/webcast/zoom/123456789">Webcast replay</a></div>'''
        events=parse_events(html,'https://ir.example.com/events')
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].date,'2026-08-25')
        self.assertEqual(events[0].webcast_url,'https://wsw.com/webcast/zoom/123456789')
        self.assertIn('/event-details/',events[0].event_url)
        self.assertTrue(events[0].title.startswith('Zoom Second'))

    def test_cached_ticker_avoids_sheets(self):
        from qualitative_ir_downloader.google_sheets import read_stocks
        from qualitative_ir_downloader.config import Config
        with tempfile.TemporaryDirectory() as temp,patch('gspread.service_account') as connect:
            config=Config(download_root=Path(temp))
            (Path(temp)/'stock_metadata.json').write_text(json.dumps({'spreadsheet_id':config.spreadsheet_id,'stocks':{'ZM':{'ticker':'ZM','company_name':'Zoom','worksheet':'Safe','spreadsheet_row':4}}}))
            self.assertEqual([s.ticker for s in read_stocks(config,ticker='ZM')],['ZM'])
            connect.assert_not_called()

    def test_force_transcription_alias(self):
        from qualitative_ir_downloader.main import parser
        self.assertTrue(parser().parse_args(['--force-transcription']).force_events)

    def test_undated_fiscal_archive_ranks_latest_period(self):
        from qualitative_ir_downloader.event_discovery import event_priority
        newer=Event('Zoom Second Quarter Fiscal Year 2027 Earnings Webinar',None,'https://example.com/new','earnings_call')
        older=Event('Zoom Third Quarter Fiscal Year 2026 Earnings Webinar',None,'https://example.com/old','earnings_call')
        self.assertGreater(event_priority(newer),event_priority(older))
        self.assertIsNone(newer.date)

    def test_shared_webcast_does_not_merge_different_dated_events(self):
        from qualitative_ir_downloader.event_discovery import merge_events,parse_events
        a=Event('Conference','2026-10-20','https://ir.example.com/future',webcast_url='https://zoom.us/recording/share/same')
        b=Event('First Quarter 2020','2019-06-06','https://ir.example.com/old',webcast_url=a.webcast_url)
        self.assertEqual(len(merge_events([a,b])),2)
        self.assertEqual(parse_events('<a href="https://wsw.com/webcast/event/1">Click here for webcast</a>','https://ir.example.com/events'),[])

class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_listing_inspects_only_best_event(self):
        from qualitative_ir_downloader.event_discovery import EventDiscovery
        from qualitative_ir_downloader.config import Config
        from qualitative_ir_downloader.models import Stock,IREcosystem
        from qualitative_ir_downloader.providers.base import ProviderContext
        with tempfile.TemporaryDirectory() as temp:
            browser=SimpleNamespace(config=Config(download_root=Path(temp),events=EventConfig(only=True,limit=1)))
            context=AsyncMock()
            env=ProviderContext(browser,context,Stock('ZM','Zoom','Safe',4),IREcosystem(verified_ir_urls=['https://ir.example.com/']))
            calls=[]
            async def fetch(url):
                calls.append(url)
                if url.endswith('/events'):return SimpleNamespace(body=b'<html><title>Zoom Events</title><body>Zoom Events</body></html>')
                if '/event-details/' in url:return SimpleNamespace(body=b'<main><a href="https://player.example.com/play">Webcast replay</a></main>')
                return None
            provider=SimpleNamespace(name='fixture',fetch=fetch)
            items=[Event('Quarterly Results','2026-01-01','https://ir.example.com/event-details/earnings','earnings_call'),Event('Investor conference','2026-02-01','https://ir.example.com/event-details/conference','conference')]
            with patch('qualitative_ir_downloader.event_discovery.parse_events',return_value=items),patch('qualitative_ir_downloader.event_discovery.find_events_section',AsyncMock()) as root_browser:
                result=await EventDiscovery(env,provider).discover()
                root_browser.assert_not_called()
            self.assertEqual(len(result),2)
            self.assertEqual([u for u in calls if '/event-details/' in u],['https://ir.example.com/event-details/earnings'])

    async def test_clip_limit_is_enforced(self):
        from qualitative_ir_downloader.benchmark import benchmark
        with tempfile.TemporaryDirectory() as temp:
            audio=Path(temp)/'audio.wav';audio.write_bytes(b'fixture')
            with self.assertRaises(ValueError):await benchmark(EventConfig(),audio,301,Path(temp)/'out')
            def fake(audio,config,event,path):
                event.timings={'transcription':1};event.performance=performance(1,120);event.transcript_qa={'status':'PASS'};return []
            with patch('qualitative_ir_downloader.benchmark.run_ffmpeg',AsyncMock()) as ffmpeg,patch('qualitative_ir_downloader.benchmark.media_info',return_value={'duration':120}),patch('qualitative_ir_downloader.benchmark.transcribe',fake):
                await benchmark(EventConfig(),audio,120,Path(temp)/'out',['small.en'])
                args=ffmpeg.call_args.args[1]
                self.assertEqual(args[args.index('-t')+1],'120')
