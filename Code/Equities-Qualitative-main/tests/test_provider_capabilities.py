import tempfile
import unittest
from pathlib import Path
from qualitative_ir_downloader.event_models import Event
from qualitative_ir_downloader.provider_capabilities import observe,load,integration_penalty

class ProviderCapabilityTests(unittest.TestCase):
    def test_protected_observation_lowers_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            event=Event('Call','2026-08-05','https://ir.example.com/e',provider='Media Server',protected_media_type='DRM_PROTECTED',status='DRM_PROTECTED')
            event.registration_destination_domain='media-server.com'; observe(Path(temp),event)
            self.assertEqual(load(Path(temp))['media-server.com']['observations']['protected'],1)
            self.assertGreater(integration_penalty(Path(temp),'media-server.com'),0)

    def test_hostname_and_duplicate_event_observations_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            event=Event('Call','2026-08-05','https://ir.example.com/e',webcast_url='https://edge.media-server.com/mmc/p/test',provider='Media Server',protected_media_type='encrypted HLS')
            observe(Path(temp),event);observe(Path(temp),event)
            self.assertEqual(load(Path(temp))['media-server.com']['observations']['events_inspected'],1)
            self.assertEqual(integration_penalty(Path(temp),'https://edge.media-server.com/player'),integration_penalty(Path(temp),'media-server.com'))
