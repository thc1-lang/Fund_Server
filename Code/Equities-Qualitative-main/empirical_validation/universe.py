from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    company_id: str
    valid_from: str | None = None
    valid_to: str | None = None
    sector: str | None = None
    industry: str | None = None
    delisted: bool = False


@dataclass
class PilotUniverse:
    version: str
    members: list[UniverseMember]
    survivorship_status: str = "SURVIVORSHIP_COVERAGE_INCOMPLETE"

    @classmethod
    def from_tickers(cls, tickers: Iterable[str], version: str = "pilot-universe-v1") -> "PilotUniverse":
        return cls(version, [UniverseMember(t.upper(), t.upper()) for t in sorted(set(tickers))])

    def tickers(self) -> list[str]:
        return [m.ticker for m in self.members]
