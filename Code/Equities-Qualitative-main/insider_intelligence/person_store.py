"""Person-store compatibility façade."""

from .store import InsiderStore


class PersonStore(InsiderStore):
    """Expose people-focused access while sharing the canonical store."""

    def get_by_ticker(self, ticker: str):
        return [item for item in self.list_people() if item.ticker.upper() == ticker.upper()]


__all__ = ["PersonStore"]
