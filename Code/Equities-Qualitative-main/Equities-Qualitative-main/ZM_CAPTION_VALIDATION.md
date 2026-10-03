# ZM caption validation — 9 September 2026

The accounting changes pass validation. The live ZM source is **not a speech caption file**, so this event does not establish live official-caption artifact success.

## Live evidence

The requested normal-mode command was run:

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker ZM --events-only --event-limit 1
```

- Ticker: ZM
- Event: Zoom Second Quarter Fiscal Year 2027 Earnings Webinar, 25 August 2026
- Provider: Zoom recording player, handled by the generic adapter
- Discovery: 31 current archive events; the requested earnings webinar was selected
- Official transcript found: no
- Official speech captions found: no
- Candidate format: WebVTT
- Source: the public player response's `result.chapterUrl`, on `investor.zoom.us/nws/recording/1.0/play/vtt`; the observed session URL is retained in `artifacts/zm_caption_live_validation.json`
- Public player metadata: `hasTranscript: false`; no visible transcript control
- Candidate cues: 2; genuine transcript segments: 0
- Candidate characters: 30; genuine transcript characters: 0
- First timestamp: 00:00:00.000
- Last timestamp: 01:04:23.080
- Media duration: 3863.1 seconds, independently measured during media normalization
- Cue contents: `Sharing Started`, `Sharing Stopped`, with IDs `chapter-0` and `chapter-1`
- Rolling duplicates: none
- Live transcript JSON/PDF: not produced
- Manifest: `C:/Users/Nicholas/Downloads/ZM_Zoom Communications, Inc_qualitative analysis_2026-09-09_14-10-14/manifest.json`
- Caption outcome: `CAPTIONS_NOT_TRANSCRIPT`
- CLI outcome: manually interrupted during media fallback. The fallback had normalized audio and started Whisper (one segment logged); it did not complete a transcript. The manifest records `INTERRUPTED`, discovered/saved caption counters zero, and newly generated transcripts zero. Its attempted method remains generated transcription, not official captions.

The earlier `SKIPPED_OFFICIAL_CAPTIONS` was a false positive caused by treating any timestamped VTT as speech. Do not preserve that outcome for this chapter-only source. Both modes must reject it and may continue to media under the existing production precedence. No registration was submitted in this check.

## Implementation and tests

- Explicit discovered, saved, and newly created fields and summary counters; legacy plural counters retain saved-artifact semantics for integration stopping.
- VTT/SRT parsing, monotonic timestamps, rolling-text deduplication, minimum content, coverage and duration validation.
- Chapter-ID rejection before counting a valid caption source or skipping generation.
- Official-caption JSON/PDF provenance, caption diagnostics and `generated_by_whisper: false` for successful caption artifacts.
- CARG/QLYS/DAVE skipped-source accounting fixtures and identical normal/generated-only caption discovery fixtures.
- 91 unit/offline tests and 10 event browser tests passed after the final changes.
- A clearly labeled synthetic rolling-caption fixture produced JSON and a three-page PDF; artifact validation and visual inspection of all three pages passed. This is fixture coverage, not a live ZM transcript.

Live official-caption success remains unproven because this replay currently exposes chapters without a speech transcript. No broad company search or IR rewrite was performed.
