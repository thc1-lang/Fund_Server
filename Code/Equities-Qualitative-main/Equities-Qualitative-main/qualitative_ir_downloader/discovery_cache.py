"""Short-lived discovery caches; failures never erase verified ownership."""
import json,time,re
from pathlib import Path
from urllib.parse import urlsplit
from .filesystem import atomic_json
class DiscoveryCache:
    def __init__(self,root):
        self.path=Path(root)/'ir_discovery_cache.json'
        try:self.data=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.data={'queries':{},'transport':{}}
    def save(self):atomic_json(self.path,self.data)
    def query(self,q):
        item=self.data['queries'].get(' '.join(q.casefold().split()),{})
        return item.get('results') if time.time()<item.get('retry_after',0) else None
    def store_query(self,q,results):
        self.data['queries'][' '.join(q.casefold().split())]={'results':results,'retry_after':time.time()+3600};self.save()
    def blocked(self,url):
        item=self.data['transport'].get(urlsplit(url).hostname,{})
        return item if time.time()<item.get('retry_after',0) else None
    def failure(self,url,kind):
        host=urlsplit(url).hostname;now=time.time()
        self.data['transport'][host]={'host':host,'failure_type':kind,'last_checked':now,'retry_after':now+900};self.save()
