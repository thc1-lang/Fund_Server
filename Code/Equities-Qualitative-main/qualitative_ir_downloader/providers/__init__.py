"""Provider registry. Detection never adds an unverified hostname."""
from .base import ProviderContext
from .generic import GenericAdapter
from .gcs_web import GCSWebAdapter

async def select_provider(context: ProviderContext):
    adapter = GCSWebAdapter(context)
    if await adapter.detect():
        return adapter
    return GenericAdapter(context)
