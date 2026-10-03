"""Stable event records; runtime media credentials never enter manifests."""
from dataclasses import dataclass, field, asdict
from .privacy import safe_record

@dataclass
class Event:
    title: str
    date: str | None
    event_url: str
    event_type: str = "other"
    webcast_url: str | None = None
    presentation_urls: list[str] = field(default_factory=list)
    provider: str | None = None
    status: str = "DISCOVERED"
    official_transcript_url: str | None = None
    official_transcript_discovered: bool = False
    official_transcript_saved: bool = False
    official_captions_discovered: bool = False
    official_captions_saved: bool = False
    generated_transcript_created: bool = False
    official_captions_url: str | None = None
    caption_format: str | None = None
    caption_diagnostics: dict = field(default_factory=dict)
    transcription_config: dict = field(default_factory=dict)
    transcript_qa: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    performance: dict = field(default_factory=dict)
    linked_from_url: str | None = None
    discovery_method: str = "official_ir_events"
    provider_event_id: str | None = None
    registration_required: bool = False
    registration_status: str = "NOT_REQUIRED"
    registration_acceptances: list[str] = field(default_factory=list)
    media_type: str | None = None
    media_url: str | None = None
    media_provenance: list[dict] = field(default_factory=list)
    duration_seconds: float | None = None
    transcription_engine: str | None = None
    transcription_model: str | None = None
    language: str | None = None
    device: str | None = None
    local_pdf: str | None = None
    local_json: str | None = None
    retained_media: str | None = None
    method: str | None = None
    replay_available: bool = False
    resumed: bool = False
    media_attempted: bool = False
    media_downloads: int = 0
    whisper_runs: int = 0
    errors: list[dict] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["DISCOVERED"])
    speakers: list[str] = field(default_factory=list)
    transcript_candidates_searched: int = 0
    captions_sources_searched: int = 0
    provider_responses_inspected: int = 0
    protected_media_type: str | None = None
    registration_page_url: str | None = None
    registration_form_action: str | None = None
    registration_destination_domain: str | None = None
    registration_fields: list[str] = field(default_factory=list)
    processing_stage: str = "discovery"
    submission_attempted: bool = False
    post_submit_url: str | None = None
    player_loaded: bool = False
    registration_redirect_chain: list[str] = field(default_factory=list)

    @property
    def source_url(self): return self.event_url

    def transition(self, status):
        self.status = status
        if not self.states or self.states[-1] != status: self.states.append(status)

    def record(self):
        return safe_record({**asdict(self), "transcript_method": self.method,
                            "generated_by_whisper": self.method == "generated_transcript" and self.transcription_engine == "faster-whisper"})

@dataclass
class MediaCandidate:
    url: str
    media_type: str
    content_type: str = ""
    source: str = "player"
    frame_url: str = ""
    duration: float | None = None
    size: int | None = None
    score: int = 0
    headers: dict = field(default_factory=dict, repr=False)

@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
