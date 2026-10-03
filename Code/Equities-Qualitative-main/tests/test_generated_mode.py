import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from qualitative_ir_downloader.events import EventProcessor
from qualitative_ir_downloader.events_config import EventConfig
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.models import Stock
from qualitative_ir_downloader.manifest import Manifest


class GeneratedModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_source_skipped_without_media_or_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); (folder/'Events').mkdir()
            stock=Stock('ACME','Acme','Safe',4)
            browser=SimpleNamespace(config=SimpleNamespace(events=EventConfig(generated_only=True),download_root=folder))
            event=Event('Acme Q2 Call','2026-08-05','https://ir.acme.com/e',official_transcript_url='https://ir.acme.com/transcript')
            processor=EventProcessor(browser,AsyncMock(),stock,folder,Manifest(folder,stock,'test'))
            with patch('qualitative_ir_downloader.events.ResourceClient') as client, patch('qualitative_ir_downloader.events.transcribe') as whisper:
                await processor.process(event)
                client.assert_not_called(); whisper.assert_not_called()
            self.assertEqual(event.status,'SKIPPED_OFFICIAL_TRANSCRIPT')
            self.assertFalse(list((folder/'Events').glob('*')))

    async def test_caption_source_skipped_without_whisper(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); (folder/'Events').mkdir()
            stock=Stock('ACME','Acme','Safe',4)
            browser=SimpleNamespace(config=SimpleNamespace(events=EventConfig(generated_only=True)))
            event=Event('Call','2026-08-05','https://ir.acme.com/e')
            processor=EventProcessor(browser,AsyncMock(),stock,folder,Manifest(folder,stock,'test'))
            from tests.test_captions import zoom_caption_fixture
            body=zoom_caption_fixture()
            with patch('qualitative_ir_downloader.events.ResourceClient') as client, patch('qualitative_ir_downloader.events.create_pdf') as pdf:
                client.return_value.get=AsyncMock(return_value=SimpleNamespace(body=body))
                await processor.official_captions(event,'https://ir.acme.com/captions.vtt')
                pdf.assert_not_called()
            self.assertEqual(event.status,'SKIPPED_OFFICIAL_CAPTIONS')
