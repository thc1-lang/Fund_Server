"""Parse public caption files without rewriting recognized speech."""
from dataclasses import dataclass
from html import unescape
import math
import re
from .models import CollectionError
from .transcription import validate_segments

TIMING = re.compile(r"^((?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?$")

@dataclass
class CaptionResult:
    format: str
    segments: list[dict]
    diagnostics: dict

def seconds(value):
    parts = value.replace(",", ".").split(":")
    if any(float(v) >= 60 for v in parts[-2:]):
        raise CollectionError("CAPTIONS_INVALID", "Invalid caption clock time")
    return sum(float(v) * 60**i for i, v in enumerate(reversed(parts)))

def parse_captions(body: bytes) -> CaptionResult:
    text = body.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    kind = "VTT" if text.lstrip().startswith("WEBVTT") else "SRT" if re.search(r"\d{2}:\d{2}:\d{2},\d{3}\s*-->", text) else "other"
    if kind == "other":
        raise CollectionError("CAPTIONS_UNSUPPORTED", "Caption source is not supported WebVTT or SRT")
    cues = []
    chapter_cues = 0
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip().splitlines()
        if not lines or re.match(r"^(NOTE|STYLE|REGION)(?:\s|$)", lines[0]):
            continue
        for i, line in enumerate(lines):
            match = TIMING.fullmatch(line.strip())
            if not match:
                if "-->" in line:
                    raise CollectionError("CAPTIONS_INVALID", "Malformed caption timestamp")
                continue
            content = "\n".join(lines[i+1:])
            speaker = re.search(r"<v(?:\.[^ >]+)?\s+([^>]+)>", content)
            content = unescape(re.sub(r"<[^>]*>", "", content)).strip()
            if content:
                cue = {"start": seconds(match[1]), "end": seconds(match[2]), "text": content}
                if speaker:
                    cue["speaker"] = unescape(speaker[1])
                cues.append(cue)
                if i and re.fullmatch(r"chapter[-_]\d+", lines[i-1].strip(), re.I):
                    chapter_cues += 1
            break
    if not cues:
        raise CollectionError("CAPTIONS_INVALID", "Caption source contains no speech cues")
    previous = -1
    for cue in cues:
        if cue["start"] < previous or cue["end"] <= cue["start"]:
            raise CollectionError("CAPTIONS_INVALID", "Caption timestamps are not monotonic")
        previous = cue["start"]
    segments = []
    removed = 0
    prior = None
    for cue in cues:
        words = cue["text"].split()
        overlap = 0
        if prior and cue.get("speaker") == prior.get("speaker") and cue["start"] <= prior["end"] + 0.05:
            previous_words = prior["text"].split()
            if words == previous_words:
                overlap = len(words)
            else:
                # Partial overlap is removed only for overlapping cues or an
                # explicit repeated rolling line. Adjacent repeated speech stays.
                rolling_line = "\n" in cue["text"] and cue["text"].splitlines()[0] == prior["text"].splitlines()[-1]
                if cue["start"] < prior["end"] or rolling_line:
                    for size in range(min(len(words), len(previous_words)), 1, -1):
                        if previous_words[-size:] == words[:size]:
                            overlap = size
                            break
        removed += overlap
        if overlap == len(words) and segments:
            segments[-1]["end"] = max(segments[-1]["end"], cue["end"])
        else:
            segments.append({**cue, "text": " ".join(words[overlap:])})
        prior = cue
    first, last = cues[0]["start"], max(c["end"] for c in cues)
    covered, end = 0.0, first
    for cue in cues:
        covered += max(0.0, cue["end"] - max(end, cue["start"]))
        end = max(end, cue["end"])
    diagnostics = {"track_kind": "chapters" if chapter_cues == len(cues) else "captions",
                   "raw_cues": len(cues), "segment_count": len(segments),
                   "transcript_characters": sum(len(c["text"]) for c in segments),
                   "first_timestamp": first, "last_timestamp": last,
                   "rolling_duplicate_words_removed": removed,
                   "caption_span_seconds": last-first,
                   "caption_time_coverage": covered / max(last-first, 1)}
    return CaptionResult(kind, segments, diagnostics)

def validate_completeness(result, config, duration=None):
    if result.diagnostics.get("track_kind") == "chapters":
        raise CollectionError("CAPTIONS_NOT_TRANSCRIPT", "WebVTT contains chapter markers, not speech captions")
    validate_segments(result.segments, config)
    stats = result.diagnostics
    if stats["caption_span_seconds"] < max(config.min_duration, 300):
        raise CollectionError("CAPTIONS_INCOMPLETE", "Caption source is too short to establish a full earnings webcast")
    if stats["caption_time_coverage"] < 0.4:
        raise CollectionError("CAPTIONS_INCOMPLETE", "Caption source has too little coverage of its timestamp range")
    if duration is not None and math.isfinite(duration) and duration > 0:
        stats["media_duration_seconds"] = duration
        stats["last_timestamp_duration_ratio"] = stats["last_timestamp"] / duration
        if stats["last_timestamp"] < duration * 0.9 or stats["last_timestamp"] > duration + max(30, duration * 0.05) or stats["first_timestamp"] > max(120, duration * 0.1):
            raise CollectionError("CAPTIONS_INCOMPLETE", "Caption timestamps do not cover the known webcast duration")
        stats["completeness_check"] = "duration_and_content"
    else:
        if stats["first_timestamp"] > 120:
            raise CollectionError("CAPTIONS_INCOMPLETE", "Caption source is missing the start of the webcast")
        stats["completeness_check"] = "content_and_timestamps_only; media duration unavailable"
