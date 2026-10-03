"""Resources explicitly exposed by public Zoom recording players."""
from urllib.parse import urlsplit
from .generic import GenericWebcastAdapter, candidate

def is_zoom_recording(url):
    p=urlsplit(url or '')
    return (p.hostname=='zoom.us' or (p.hostname or '').endswith('.zoom.us')) and any(v in p.path for v in ('/rec/play/','/rec/share/','/nws/recording/','/replay'))

def exposed_sources(value,base=''):
    found=[]
    if isinstance(value,dict):
        for v in value.values():found.extend(exposed_sources(v,base))
    elif isinstance(value,list):
        for v in value:found.extend(exposed_sources(v,base))
    elif isinstance(value,str):
        c=candidate(value,source='public_player_metadata',frame_url=base)
        if c:found.append(c)
    return found

class ZoomRecordingProvider(GenericWebcastAdapter):
    name='Zoom recording'
    async def matches(self,page):return is_zoom_recording(page.url)
