from .generic import GenericWebcastAdapter
from .media_server import MediaServerAdapter
from urllib.parse import urlsplit
def provider_name(url):
    from .zoom import is_zoom_recording
    if is_zoom_recording(url):return 'Zoom recording'
    host=(urlsplit(url or "").hostname or "").casefold()
    for domain,name in (("media-server.com","Media Server"),("webcasts.com","Webcasts.com"),("wsw.com","Wall Street Webcasting")):
        if host==domain or host.endswith("."+domain):return name
    return "generic"
async def select_webcast_provider(page):
    from .zoom import ZoomRecordingProvider
    zoom=ZoomRecordingProvider()
    if await zoom.matches(page):return zoom
    adapter=MediaServerAdapter()
    if await adapter.matches(page):return adapter
    adapter=GenericWebcastAdapter()
    adapter.name=provider_name(page.url)
    return adapter
