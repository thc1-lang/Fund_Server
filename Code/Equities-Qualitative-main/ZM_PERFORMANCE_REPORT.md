# ROOT CAUSES

1. **Why targeted startup was slow:** a single ticker still incurred spreadsheet-universe work, repeated IR setup/root probes and unsuccessful provider discovery. Targeted runs now recover cached stock metadata and verified IR provenance, reuse scoped provider/section strategies, and try public HTTP before browser fallback. Latest measured Sheets metadata time: 0.005 seconds; IR setup: 1.846 seconds.
2. **Why event selection was slow:** discovery fetched details for the whole archive before applying the limit. It now ranks listing records first and stops after enough usable candidates. Undated fiscal links require fiscal-year/quarter ranking, not lexical URL sorting. The latest ZM run inspected one detail page.
3. **Why transcription took ~3.5 hours:** CPU large-v3 with beam 5 was expensive, and previous-text conditioning propagated a severe recognition loop. The original log spans 12,832.640 seconds from transcription/model initialization to completion. On a 120-second clip the old configuration reproduced repetition and timestamps beyond the clip. Model size, beam, threads and batching all materially affected measured runtime; this was not merely PDF generation overhead.
4. **Why 3740 segments were produced:** Whisper generated thousands of advancing, mostly one-second repeated segments. The writer appended each generated segment once; inspection found no duplicate append/resume mechanism causing the explosion.
5. **Whether 222,723 characters represented valid speech or duplication:** predominantly duplication. The prepared-remarks phrase appeared 1,856 times and its slide-deck continuation 1,855 times. Duplicate-segment ratio was 99.36%. Deduplication cannot recover speech that was never recognized, so the original was preserved and a fresh transcription was performed.

# CHANGES

**Files changed:** `google_sheets.py`, `main.py`, `company_context.py`, `event_discovery.py`, `events.py`, `event_models.py`, `event_outcomes.py`, `events_config.py`, `transcription.py`, `transcript_pdf.py`, `webcast_media.py`, `privacy.py`, `webcast_providers/generic.py`, `webcast_providers/__init__.py`; new `access_strategy.py`, `transcription_profiles.py`, `transcript_qa.py`, `benchmark.py`, `webcast_providers/zoom.py`; regression tests and documentation. All module paths are under `qualitative_ir_downloader/`.

**Provider changes:** recognize public Zoom recording/replay pages and literal media URLs exposed by the player or its JSON responses. No URL guessing, registration expansion or access-control bypass. Official captions remain preferred; chapter tracks remain rejected as transcripts.

**Media-ranking changes:** audio-only sources outrank video. This actual ZM player exposed seven video candidates and no audio-only candidate, so video fallback was necessary. Signed Zoom query strings are redacted. Normalization remains local FFmpeg after the bounded, credential-aware downloader: remote FFmpeg was not enabled because it would weaken existing redirect, credential and transfer enforcement.

**Transcription changes:** fast, balanced and maximum_accuracy profiles; explicit model/thread/beam/batch overrides; stable configuration fingerprint; CPU and dependency metadata; separate model-load/transcription timings, RTF and throughput; cached model resolution avoids unnecessary Hub requests. No previous-text conditioning, temperature 0, best_of 1, no word timestamps, VAD with 500 ms minimum silence. No invented glossary, speaker identities or financial corrections.

**Caching/resume changes:** ticker metadata and scoped access strategies; validated completed PDF/JSON reuse; changed date/media identity prevents reuse; `--force-transcription` explicitly reruns. Ctrl+C signals the worker, retains media and checkpoints at a yielded segment, and records interruption. Partial checkpoints explicitly say that partial transcription resume is unsupported; they are not silently appended to a new decode. Completed artifact reuse remains supported. Runtime history is keyed by configuration and estimates are labeled as estimates.

**QA changes:** segment/word/character rates, segment durations, exact/near repetition, repeated n-grams, timestamp ordering/overlap/end boundaries and local repeated-word runs. Warnings preserve text. Visual inspection found a local repetition missed by global metrics, so the delivered PDF, JSON and latest manifest now carry `TRANSCRIPT_QA_WARNING`.

**31 versus 30:** evidence in `artifacts/zm_benchmark/event_merge_explanation.json` shows a false merge, not a simple historical/future split. A generic external webcast anchor inherited an unrelated date and shared the 2019 Q1 FY2020 replay URL. External provider paths no longer masquerade as IR detail links; conflicting dated events do not merge solely on webcast URL. Zoom `.event-card` records now retain their actual heading/date and merge with corresponding undated archive links. Latest discovery: 52 records, 42 dated historical, 1 future, 9 undated; 43 known webcast links. This is the exposed parsed archive, not a completeness claim. One selected detail was inspected.

# BENCHMARKS

**Hardware detected:** Intel Core Ultra 7 258V, 8 physical/8 logical cores, approximately 32 GB RAM; Windows; faster-whisper 1.2.1 / CTranslate2 4.8.2. CPU int8 for all reported trials.

**Chosen profile:** balanced. **Model:** medium.en. **CPU threads:** 4. **Batching:** 2. **Beam size:** 1. **Workers:** 1. **Language:** English. Small/medium multilingual models are selected when language is automatic or non-English unless explicitly overridden.

| Trial | Audio seconds | Threads | Batch | Beam | Decode seconds | RTF | Realtime speed |
|---|---:|---:|---:|---:|---:|---:|---:|
| large-v3 legacy, previous-text conditioning | 120 | 8 | 1 | 5 | 146.22 | 1.218 | 0.82x |
| large-v3, conditioning off | 120 | 4 | 1 | 1 | 50.25 | 0.419 | 2.39x |
| large-v3, conditioning off | 120 | 4 | 2 | 1 | 45.61 | 0.380 | 2.63x |
| medium.en | 300 | 4 | 1 | 1 | 80.46 | 0.268 | 3.73x |
| medium.en | 300 | 8 | 1 | 1 | 90.32 | 0.301 | 3.32x |
| **balanced: medium.en** | **300** | **4** | **2** | **1** | **73.07** | **0.244** | **4.11x** |
| **fast: small.en** | **300** | **4** | **1** | **1** | **32.53** | **0.108** | **9.22x** |
| distil-large-v3 | 300 | 4 | 2 | 1 | 133.79 | 0.446 | 2.24x |
| balanced, complete ZM call | 3863.104 | 4 | 2 | 1 | 942.56 | 0.244 | 4.10x |

Benchmark runtime excludes model loading. Models ran sequentially; some short trials overlapped development/browser-test activity, so these are practical workstation measurements rather than controlled laboratory results. The full call corroborated the balanced five-minute estimate. Recorded peak working set for the balanced five-minute trial was 1,840.52 MiB, a process-lifetime high-water mark, not isolated model memory. Reports retain configuration, segments and metrics under `artifacts/zm_benchmark/`.

The `maximum_accuracy` profile is large-v3/beam 5 with conditioning disabled. Its name is a configuration choice, not evidence of superior accuracy; that exact profile has not received a full-call comparison. The legacy baseline is not equivalent because its conditioning differs. No ground-truth WER study was performed.

# QUALITY COMPARISON

| Metric | Original large-v3 output | New balanced output |
|---|---:|---:|
| Segments | 3,740 | 159 |
| Words (`\w+` token count) | 44,888 | 10,155 |
| Words/minute | 697.18 | 157.72 |
| Characters | 222,723 | 56,104 |
| Duplicate segment ratio | 99.36% | 0% |
| Repeated five-gram ratio | 99.59% | 2.17% |
| Overlap/nonmonotonic timestamps | 0 / 0 | 0 / 0 |
| Median segment duration | 1.00 second | 25.37 seconds |
| QA result | Multiple repetition/density warnings | Local repetition warning |

The new output covers the introduction, prepared remarks, financial discussion, Q&A and closing at 3862.54 seconds. All 18 PDF pages were rendered, with layout inspection and text samples across the call. There is no clipping apparent in the reviewed rendering. However segment 61 (zero-based), 1560.43–1589.49 seconds (26:00–26:29), repeats “yeah” extensively. Global duplicate-segment checks miss this because it occurs inside one segment; the added local check catches it. This passage remains unchanged and requires audio review. Proper nouns and numerical accuracy are not certified. The catastrophic whole-call loop is resolved in this run, but transcript quality is **not fully fixed**.

# TARGETED ZM RUN

Two distinct measurements must not be combined into a fictitious fresh end-to-end acquisition run:

| Stage | Reviewed repeat CLI | Full retained-audio validation |
|---|---:|---:|
| Sheets metadata | 0.005 s | Not run |
| IR lookup/setup | 1.846 s | Not run |
| Event discovery | 9.602 s | Not run |
| Player/media | Skipped by completed-artifact reuse | Not run; previously acquired public audio |
| Media acquisition | Skipped | Not run; retained media |
| Normalization | Skipped | Not run; retained normalized WAV |
| Model load | Skipped | 3.361 s |
| Transcription | Skipped | 942.564 s |
| Artifact write | No rewrite | 0.379 s |
| Resume validation | 0.641 s | Not applicable |
| Total | 13.507 s | 949.028 s |

The original baseline's 12,832.640 seconds includes model initialization; the new full validation includes 949.028 seconds of loading, decoding and output work. These are measured runs with different model/configuration settings, not an isolated single-variable speed comparison. Initial public acquisition was done once for this investigation; its individual acquisition/normalization stage timings were not retained and are not invented here.

**Output folder:** `C:\Users\Nicholas\Downloads\ZM_Zoom Communications, Inc_qualitative analysis_2026-09-17_07-46-14`

**JSON:** `Events/2026-08-25_ZM_Zoom Second Quarter Fiscal Year 2027 Earnings Webinar.json`

**PDF:** `Events/2026-08-25_ZM_Zoom Second Quarter Fiscal Year 2027 Earnings Webinar.pdf`

**Manifest:** `manifest.json` in that folder.

**Final status:** `TRANSCRIPT_QA_WARNING`. The artifact is structurally valid and completed; review warning takes precedence over the reuse success label. The repeat run created zero transcripts, performed no Whisper work and preserved PDF/JSON SHA256 hashes. The initial pre-local-check run reported success; that historical record is superseded by the reviewed QA status, not hidden.

# TESTS

**Tests passed:** 108 offline/unit tests; 24 Chromium fixture tests (132 total). The full browser suite passed; the final local-repetition addition was then covered by the complete 108-test unit rerun. Real full-call artifact validation and reviewed live CLI reuse also passed their structural/reuse checks.

**Tests added:** profile/model overrides; thread settings and one append per generated segment; cancellation checkpoint; QA density/repetition/timestamps; local repetition; audio-first ranking; Zoom exposed sources and signed-URL redaction; cached ticker; force alias; bounded benchmark; shallow discovery; fiscal period ranking; conflicting dated webcast identity; actual Zoom card heading/date and archive merging. Browser resume tests verify date/media invalidation and explicit force invocation. Existing gate, captions/chapter, DRM and registration fixtures remain passing.

**Failures:** no remaining automated test failures. Product limitation: localized recognition repetition, explicitly warned. No fresh second full transcription or full acquisition was run after discovering it.

# RECOMMENDED DEFAULT

**Production transcription profile:** balanced (`medium.en`, CPU int8, 4 threads, batch 2, beam 1), with mandatory QA review for warned output. This is a measured performance default, not a guarantee of financial transcript accuracy.

**Reason:** 4 threads beat 8 for this CPU; batch 2 modestly improved medium.en; distil-large-v3 was slower in the measured five-minute trial. Small.en is useful for fast drafts but has no full-call accuracy validation here. Avoid automatically deleting repetitions or rewriting figures.

**Expected throughput:** measured 4.10x realtime on this 64.4-minute call; roughly 15.7 minutes decoding per comparable call on this machine. Future events/hardware may differ. Fast profile measured 9.22x on five minutes only; do not extrapolate it as a validated full-call result.

# NEXT COMMAND TO RUN

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker ZM --events-only --event-limit 1
```

This reuses the completed reviewed output and exposes the QA warning. To benchmark retained audio without IR/Sheets/browser discovery:

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --benchmark-transcription --benchmark-audio artifacts/zm_benchmark/media/normalized.wav --benchmark-seconds 300 --benchmark-model medium.en --transcription-profile balanced
```

Do not use `--force-transcription` merely to inspect this result; it deliberately incurs another full decode.
