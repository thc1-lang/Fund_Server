"""Media-server Angular player recognition from host plus actual player assets."""
from urllib.parse import urlsplit
from .generic import GenericWebcastAdapter
class MediaServerAdapter(GenericWebcastAdapter):
    name="Media Server"
    async def matches(self,page):
        host=urlsplit(page.url).hostname or ""
        return host=="edge.media-server.com" and await page.locator('script[src*="/mmc/player/client/"]').count()>0
