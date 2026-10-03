"""Local event acquisition and transcription settings."""
from dataclasses import dataclass, field
from pathlib import Path

@dataclass(frozen=True)
class RegistrationProfile:
    first_name: str = "Nicholas"
    last_name: str = "Allen"
    name: str = "Nicholas Allen"
    email: str = "nicholaslallen1@gmail.com"
    company: str = "Allen & Cooper"
    occupation: str = "Other"
    country: str = ""  # Never infer a required country from locale.

@dataclass(frozen=True)
class EventConfig:
    enabled: bool = True
    generated_only: bool = False
    require_new_generated_event: bool = False
    event_cache_hours: float = 24
    protected_cache_hours: float = 168
    skip_registration: bool = False
    discover_registration_destinations: bool = False
    approved_registration_domains: tuple[str,...] = ()
    only: bool = False
    force: bool = False
    limit: int | None = 1
    model: str | None = None
    transcription_profile: str = "balanced"
    cpu_threads: int | None = None
    beam_size: int | None = None
    batch_size: int | None = None
    language: str | None = "en"
    device: str = "auto"
    keep_media: bool = False
    transcript_json: bool = True
    profile: RegistrationProfile = field(default_factory=RegistrationProfile)
    ffmpeg: str | None = None
    model_cache: Path = field(default_factory=lambda: Path.home()/".cache"/"qualitative-ir"/"whisper")
    min_duration: float = 60.0
    min_transcript_segments: int = 10
    min_transcript_characters: int = 1000
    max_duration: float = 6 * 3600.0
    max_media_bytes: int = 2 * 1024**3
    max_media_candidates: int = 12
    max_media_attempts: int | None = None
    media_timeout: float = 120.0
    process_timeout: float = 4 * 3600.0
    debug_failures: bool = False
