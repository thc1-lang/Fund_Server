"""Typed collection records, including unsuccessful collection evidence."""
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Stock:
    ticker: str
    company_name: str
    worksheet: str
    spreadsheet_row: int

@dataclass(frozen=True)
class Link:
    url: str
    text: str = ""
    context: str = ""

@dataclass
class Candidate:
    url: str
    score: int
    reasons: list[str] = field(default_factory=list)

@dataclass
class Document:
    title: str
    date: str | None
    source_url: str
    category: str
    pdf_source_url: str | None = None
    local_filename: str | None = None
    method: str | None = None
    sha256: str | None = None
    duplicate_of: str | None = None
    linked_from_url: str | None = None
    discovery_method: str = "official_ir_archive"
    official_ir_url: str | None = None
    download_url: str | None = None
    feed_url: str | None = None
    guid: str | None = None
    description: str | None = None
    content_html: str | None = None
    content_complete: bool = False
    wire_source_url: str | None = None
    wire_provenance_url: str | None = None
    status: str = "DISCOVERED"
    http_status: int | None = None
    file_size: str | None = None
    # True when a previously validated local artifact was reused on an
    # incremental run.  This is distinct from duplicate_of, which can also
    # describe a duplicate discovered during the current run.
    cache_hit: bool = False
    # Report-accounting evidence.  These fields are populated only when a
    # report download is attempted and do not participate in cache identity.
    failure_stage: str | None = None
    failure_code: str | None = None
    retry_count: int = 0
    existing_artifact: bool = False
    failure_classification: str | None = None
    failure_reason: str | None = None
    attempt_count: int = 0
    alternate_pdf_urls: list[str] = field(default_factory=list)
    # Official-source acquisition provenance.  These fields are deliberately
    # optional so manifests written by earlier acquisition versions remain
    # readable and cache validation remains backwards compatible.
    source_family: str | None = None
    discovered_from: str | None = None
    artifact_url: str | None = None
    issuer_verified: bool = False
    retrieved_at: str | None = None
    provenance: list[dict[str, str]] = field(default_factory=list)

@dataclass
class Issue:
    code: str
    message: str
    url: str | None = None

@dataclass
class ArchiveResult:
    documents: list[Document] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    pages: int = 0
    rss_feeds: list[str] = field(default_factory=list)
    coverage: str = "unknown"
    diagnostics: dict = field(default_factory=dict)

class CollectionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class AccessEvent:
    url: str
    transport: str
    status: int | None = None
    blocked: bool = False
    error: str | None = None

@dataclass
class AccessResult:
    http_status: int | None = None
    browser_status: int | None = None
    http_accessible: bool = False
    browser_accessible: bool = False
    blocked: bool = False
    error: str | None = None
    final_url: str | None = None
    page_title: str | None = None

@dataclass
class IRCandidate:
    url: str
    officiality_score: int = 0
    evidence: list[str] = field(default_factory=list)
    official: bool = False
    access: AccessResult = field(default_factory=AccessResult)
    corporate_url: str | None = None
    verified_hosted_ir_domain: str | None = None
    content_validated: bool = False

    @property
    def accepted(self) -> bool:
        return self.official and self.access.browser_accessible and self.content_validated

@dataclass
class IREcosystem:
    official_corporate_domain: str | None = None
    official_corporate_url: str | None = None
    official_ir_domain: str | None = None
    verified_ir_urls: list[str] = field(default_factory=list)
    verified_hosted_ir_domains: list[str] = field(default_factory=list)
    allowed_document_domains: list[str] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
