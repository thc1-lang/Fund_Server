"""One per-company runtime shared by every CLI mode and collection adapter."""
from dataclasses import dataclass, field
from .models import Stock, IRCandidate, IREcosystem
from .providers import ProviderContext, select_provider

@dataclass
class MediaBudget:
    limit: int | None = None
    used: int = 0
    skipped_cached: int = 0

    def claim(self):
        if self.limit is not None and self.used >= self.limit:
            return False
        self.used += 1
        return True

@dataclass
class CompanyContext:
    stock: Stock
    verified_ir: IRCandidate | None = None
    ecosystem: IREcosystem = field(default_factory=IREcosystem)
    provider_context: object = None
    provider: object = None
    events: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    public_resources_usable: bool = False
    scanned: bool = False
    scan_only: bool = False
    discovery: object = None
    registry: object = None
    access_strategy: dict = field(default_factory=dict)
    provider_history: dict = field(default_factory=dict)
    verified_cache_hit: bool = False

    async def resolve_provider(self, browser, context):
        if self.provider_context is None or self.provider_context.context is not context:
            cache = self.provider_context.cache if self.provider_context else {}
            self.provider_context = ProviderContext(browser, context, self.stock, self.ecosystem, cache)
            if self.provider:
                evidence = self.provider.evidence
                self.provider = type(self.provider)(self.provider_context)
                self.provider.evidence = evidence
        if self.provider is None:
            from .access_strategy import load
            from .providers.gcs_web import GCSWebAdapter
            known=load(browser.config.download_root,self.stock,self.ecosystem.verified_ir_urls[0]) if self.ecosystem.verified_ir_urls else {}
            if known.get('provider')=='GCS-Web':
                self.provider=GCSWebAdapter(self.provider_context)
                self.provider.evidence=['Previously observed public IR provider; resources rechecked on use']
            elif known.get('provider')=='generic':
                from .providers.generic import GenericAdapter
                self.provider=GenericAdapter(self.provider_context)
                self.provider.evidence=['Previously observed public HTTP events routes']
            else:self.provider = await select_provider(self.provider_context)
        return self.provider

    def access_record(self, access_events):
        from urllib.parse import urlsplit
        hosts = {urlsplit(u).hostname for u in self.ecosystem.verified_ir_urls}
        access = self.verified_ir.access if self.verified_ir else None
        http = bool(access and access.http_accessible)
        browser = bool(access and access.browser_accessible)
        public = any(urlsplit(e.url).hostname in hosts and e.status is not None and 200 <= e.status < 300 for e in access_events)
        self.public_resources_usable = self.public_resources_usable or public
        public = self.public_resources_usable
        return {"ir_browser_accessible": browser, "ir_http_accessible": http,
                "ir_public_resource_accessible": public, "ir_accessible": http or browser or public}

def prepare_company_context(browser,stock,seed_root):
    """Canonical pre-network bootstrap, shared by scan and processing modes."""
    from .scoring import company_key
    from . import ir_state
    from .event_registry import EventRegistry
    key=(stock.ticker.casefold(),company_key(stock.company_name))
    prepared=getattr(browser,'prepared_companies',{})
    runtime=prepared.get(key)
    if runtime:return runtime
    runtime=CompanyContext(stock)
    runtime.verified_ir=ir_state.recover(browser.config.download_root,stock,seed_root)
    runtime.verified_cache_hit=bool(runtime.verified_ir)
    runtime.registry=getattr(browser,"event_registry",None) or EventRegistry(browser.config.download_root)
    browser.event_registry=runtime.registry
    from .access_strategy import load
    from .provider_capabilities import load as load_provider_history
    runtime.provider_history=load_provider_history(browser.config.download_root)
    if runtime.verified_ir:runtime.access_strategy=load(browser.config.download_root,stock,runtime.verified_ir.url)
    runtime.registry.import_history(stock)
    prepared[key]=runtime;browser.prepared_companies=prepared
    return runtime
