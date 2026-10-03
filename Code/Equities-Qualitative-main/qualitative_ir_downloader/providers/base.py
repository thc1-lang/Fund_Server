from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from ..models import ArchiveResult, IREcosystem, Stock
from ..urls import Scope

@dataclass
class ProviderContext:
    browser: object
    context: object
    stock: Stock
    ecosystem: IREcosystem
    cache: dict = field(default_factory=dict)

    @property
    def roots(self):
        return list(dict.fromkeys(urlunsplit((p.scheme,p.netloc,"/","","")) for p in (urlsplit(u) for u in self.ecosystem.verified_ir_urls)))

    @property
    def scope(self):
        return Scope(self.roots)

class IRProviderAdapter(Protocol):
    name: str
    evidence: list[str]
    async def discover_news(self) -> ArchiveResult: ...
    async def discover_reports(self) -> ArchiveResult: ...
