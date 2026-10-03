"""Configuration shared by collectors; companies always run sequentially."""
from dataclasses import dataclass, field
from pathlib import Path
from .events_config import EventConfig

DEFAULT_SERVICE_ACCOUNT_PATH = Path(r"C:\Fund_Server\Code\us_complete_pipeline\google_credentials.json")
SPREADSHEET_ID = "1vVb8EfJnsbPufiVH3ckznzu2TAOL3xW1WTdfceLALeA"
WORKSHEETS = (
    "Safe Secondary Summary",
    "High Growth Potential Secondary Summary",
    "Turnaround Story Secondary Summary",
    "Short Secondary Summary",
)
HEADLESS = True
REQUEST_DELAY_SECONDS = 1.0
NAVIGATION_TIMEOUT_MS = 45000
MAX_RETRIES = 3
DOWNLOAD_ROOT = Path(r"C:\Fund_Server\Data\Qual_Data") / "_Acquisition_Cache"

@dataclass(frozen=True)
class Config:
    events: EventConfig = field(default_factory=EventConfig)
    service_account_path: Path = DEFAULT_SERVICE_ACCOUNT_PATH
    spreadsheet_id: str = SPREADSHEET_ID
    download_root: Path = field(default_factory=lambda: DOWNLOAD_ROOT)
    request_delay: float = REQUEST_DELAY_SECONDS
    navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS
    retries: int = MAX_RETRIES
    max_archive_pages: int = 250
    max_links_per_company: int = 10000
    max_interactions: int = 500
    max_pdf_bytes: int = 150 * 1024 * 1024
    max_discovery_candidates: int = 15
    max_section_pages: int = 20
    revalidate_ir: bool = False
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
