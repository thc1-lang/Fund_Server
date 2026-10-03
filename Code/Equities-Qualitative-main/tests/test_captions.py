import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pypdf import PdfReader
from qualitative_ir_downloader.captions import parse_captions, validate_completeness
from qualitative_ir_downloader.events import EventProcessor, completed_match
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.event_outcomes import metrics
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.models import Stock, CollectionError
from qualitative_ir_downloader.manifest import Manifest


def zoom_caption_fixture():
    """Synthetic rolling WebVTT; never represented as actual Zoom speech."""
    def clock(t):
        return f"00:{t//60:02}:{t%60:02}.000"
    cues = ["WEBVTT"]
    previous = ""
    for i in range(25):
        speech = f"Fixture passage {i}: This synthetic earnings webinar discusses the quarter and customer activity. These words test faithful caption conversion."
        text = (" ".join(previous.split()[-3:]) + " " if previous else "") + speech
        cues.append(f"{i}\n{clock(i*24)} --> {clock(i*24+25)} align:start position:0%\n{text}")
        previous = speech
    return "\n\n".join(cues).encode()


class CaptionParsingTests(unittest.TestCase):
    def test_live_zoom_chapter_track_is_not_speech(self):
        body=b"WEBVTT\n\nchapter-0\n 00:00:00.000 --> 00:18:11.480\n Sharing Started\n\nchapter-1\n 00:18:11.480 --> 01:04:23.080\n Sharing Stopped\n"
        result=parse_captions(body)
        self.assertEqual(result.diagnostics["track_kind"],"chapters")
        self.assertEqual(result.diagnostics["transcript_characters"],30)
        with self.assertRaises(CollectionError) as caught:
            validate_completeness(result,EventConfig(),3863.08)
        self.assertEqual(caught.exception.code,"CAPTIONS_NOT_TRANSCRIPT")

    def test_rolling_text_and_cue_settings(self):
        result = parse_captions(zoom_caption_fixture())
        self.assertEqual(result.format, "VTT")
        self.assertEqual(len(result.segments), 25)
        self.assertEqual(result.diagnostics["rolling_duplicate_words_removed"], 72)
        self.assertTrue(all(s["text"].startswith("Fixture passage") for s in result.segments))
        self.assertNotIn("align:start", str(result.segments))
        validate_completeness(result, EventConfig(), 601)

    def test_srt_and_repeated_speech_after_gap(self):
        result = parse_captions(b"1\r\n00:00:00,000 --> 00:00:03,000\r\nThank you.\r\n\r\n2\r\n00:00:10,000 --> 00:00:13,000\r\nThank you.")
        self.assertEqual(result.format, "SRT")
        self.assertEqual(len(result.segments), 2)

    def test_short_preview_and_duration_mismatch_rejected(self):
        result = parse_captions(zoom_caption_fixture())
        with self.assertRaises(CollectionError):
            validate_completeness(result, EventConfig(), 3600)
        result.diagnostics["caption_span_seconds"] = 90
        with self.assertRaises(CollectionError):
            validate_completeness(result, EventConfig())

    def test_sparse_and_nonmonotonic_tracks_rejected(self):
        result = parse_captions(zoom_caption_fixture())
        result.diagnostics["caption_time_coverage"] = 0.02
        with self.assertRaises(CollectionError):
            validate_completeness(result, EventConfig())
        with self.assertRaises(CollectionError):
            parse_captions(b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nLater\n\n00:00:01.000 --> 00:00:03.000\nEarlier")


class CaptionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapters_do_not_count_as_captions_or_cause_skip(self):
        for generated_only in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                stock=Stock("ZM","Zoom","Safe",6)
                event=Event("Zoom webinar",None,"https://ir.example.com/event")
                browser=SimpleNamespace(config=SimpleNamespace(events=EventConfig(generated_only=generated_only)))
                manifest=Manifest(root,stock,"test")
                with patch("qualitative_ir_downloader.events.ResourceClient") as client:
                    client.return_value.get=AsyncMock(return_value=SimpleNamespace(body=b"WEBVTT\n\nchapter-0\n00:00:00.000 --> 01:04:23.080\nSharing Started",url="https://ir.example.com/chapter.vtt"))
                    with self.assertRaises(CollectionError):
                        await EventProcessor(browser,AsyncMock(),stock,root,manifest).official_captions(event,"https://ir.example.com/chapter.vtt")
                self.assertFalse(event.official_captions_discovered)
                self.assertFalse(event.official_captions_saved)
                self.assertNotEqual(event.status,"SKIPPED_OFFICIAL_CAPTIONS")
                self.assertEqual(event.caption_diagnostics["rejection_code"],"CAPTIONS_NOT_TRANSCRIPT")
                self.assertEqual(metrics([event],[event],generated_only)["official_captions_discovered"],0)

    async def test_same_caption_source_skipped_or_saved_by_mode(self):
        discoveries = []
        for generated_only in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root/"Events").mkdir()
                stock = Stock("ZM", "Zoom", "Safe", 6)
                event = Event("Zoom Second Quarter Fiscal Year 2027 Earnings Webinar", "2026-08-24", "https://ir.example.com/events/zoom-q2", provider="fixture player", duration_seconds=601)
                config = EventConfig(generated_only=generated_only)
                browser = SimpleNamespace(config=SimpleNamespace(events=config, download_root=root))
                manifest = Manifest(root, stock, "test")
                source = "https://ir.example.com/zoom-captions.vtt"
                response = SimpleNamespace(body=zoom_caption_fixture(), url=source)
                with patch("qualitative_ir_downloader.events.ResourceClient") as client, patch("qualitative_ir_downloader.events.transcribe") as whisper:
                    client.return_value.get = AsyncMock(return_value=response)
                    await EventProcessor(browser, AsyncMock(), stock, root, manifest).official_captions(event, source)
                    whisper.assert_not_called()
                count = metrics([event], [event], generated_only)
                self.assertEqual(count["official_captions_discovered"], 1)
                self.assertEqual(count["official_captions_saved"], int(not generated_only))
                self.assertEqual(count["generated_transcript_created"], 0)
                self.assertEqual(count["generated_transcripts"], 0)
                discoveries.append((event.title, event.provider, event.official_captions_url, event.caption_format, event.caption_diagnostics))
                if generated_only:
                    self.assertEqual(event.status, "SKIPPED_OFFICIAL_CAPTIONS")
                    self.assertFalse(list((root/"Events").iterdir()))
                else:
                    self.assertEqual(event.status, "TRANSCRIBED")
                    record = json.loads((root/event.local_json).read_text())
                    self.assertEqual(record["transcript_method"], "official_captions")
                    self.assertFalse(record["generated_by_whisper"])
                    self.assertEqual(record["source"]["official_captions_url"], source)
                    text = " ".join(p.extract_text() for p in PdfReader(root/event.local_pdf).pages)
                    self.assertIn(event.title, " ".join(text.split()))
                    self.assertIn("Official webcast captions", text)
                    self.assertIn(source, text)
                    self.assertTrue((root/event.local_pdf).read_bytes().startswith(b"%PDF"))
                    self.assertIsNotNone(completed_match(event, root, stock, config=config))
                saved = json.loads((root/"manifest.json").read_text())["events"][0]
                self.assertTrue(saved["official_captions_discovered"])
                self.assertFalse(saved["generated_by_whisper"])
        self.assertEqual(discoveries[0], discoveries[1])

    async def test_official_transcript_skip_has_found_but_not_saved(self):
        for ticker in ("CARG", "QLYS", "DAVE"):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                stock=Stock(ticker,ticker,"Safe",4)
                event=Event("Q2 results",None,"https://ir.example.com/event",official_transcript_url="https://ir.example.com/transcript.pdf")
                browser=SimpleNamespace(config=SimpleNamespace(events=EventConfig(generated_only=True),download_root=root))
                await EventProcessor(browser,AsyncMock(),stock,root,Manifest(root,stock,"test")).process(event)
                count=metrics([event],[event],True)
                self.assertEqual(count["official_transcript_discovered"],1)
                self.assertEqual(count["official_transcript_saved"],0)
                self.assertEqual(count["generated_transcript_created"],0)
                self.assertEqual(count["event_failures"],0)

    async def test_summary_reports_found_without_claiming_artifact(self):
        from qualitative_ir_downloader.main import run, parser
        stock=Stock("ZM","Zoom","Safe",6)
        event=Event("Zoom earnings webinar",None,"https://ir.example.com/e",method="official_captions",status="SKIPPED_OFFICIAL_CAPTIONS")
        row={"ticker":"ZM","company_name":"Zoom","ir_official":True,"ir_accessible":True,"status":event.status,
             "selected_event":event.title,"webcast_provider":"fixture",**metrics([event],[event],True)}
        with tempfile.TemporaryDirectory() as temp, patch("qualitative_ir_downloader.main.setup_logging"), patch("qualitative_ir_downloader.main.read_stocks",return_value=[stock]), patch("qualitative_ir_downloader.main.process_company",AsyncMock(return_value=row)), patch("qualitative_ir_downloader.main.Browser") as browser:
            browser.return_value.__aenter__=AsyncMock(return_value=browser.return_value)
            browser.return_value.__aexit__=AsyncMock(return_value=False)
            with self.assertLogs("qualitative_ir_downloader.main",level="INFO") as logs:
                await run(parser().parse_args(["--ticker","ZM","--events-only","--download-root",temp]))
            text="\n".join(logs.output)
            self.assertIn("Official Captions Found",text)
            self.assertIn("official_captions_found=1 official_captions_saved=0 generated_created=0",text)
