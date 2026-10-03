# Qualitative IR Downloader - Phase 1

A sequential, headless Python collection application for investor-relations news releases and investor report PDFs. It reads Google Sheets, discovers official IR pages, traverses their exposed archives, downloads original PDFs, and prints HTML-only releases with Chromium. It does not analyze documents or write to the spreadsheet.

## Setup (Windows PowerShell)

Requires Python 3.11 or newer. This workspace was tested with Python 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell does not allow activation, use `.\.venv\Scripts\python.exe` in place of `python`. Activation is optional. For the exact dependency versions used during validation, install `requirements.lock.txt`; it records the tested Windows/Python 3.14 environment. The bounded ranges in `requirements.txt` are preferable for other supported Python versions.

The service-account file defaults to:

```text
C:\Code\service_account.json
```

Enable the Google Sheets API in the service account's Google Cloud project. Share [the spreadsheet](https://docs.google.com/spreadsheets/d/1QF67DQEDF5QB8-q1JCuzyYPXSPJugxCMQ8DbUhOo_1c/edit) with the **client_email** from that JSON file, with Viewer access. Keep the private key out of source control and logs. The application requests only the `spreadsheets.readonly` scope and opens the sheet by ID; no Drive write scope is used.

Read-only authentication follows the [gspread authentication API](https://docs.gspread.org/en/v5.6.0/api/auth.html). Browser printing and interactions use the [Playwright Python API](https://playwright.dev/python/docs/api/class-page).

## Run

From this project directory, after activation:

```powershell
# Verify credentials and the EXEL row without crawling
python -m qualitative_ir_downloader.main --ticker EXEL --list-only

# First company test (complete exposed archive, subject to safety limits)
python -m qualitative_ir_downloader.main --ticker EXEL

# Full universe
python -m qualitative_ir_downloader.main

# First three qualifying rows
python -m qualitative_ir_downloader.main --limit 3

# One worksheet
python -m qualitative_ir_downloader.main --worksheet "Safe"

# Resume the universe at a ticker, inclusively
python -m qualitative_ir_downloader.main --start-ticker EXEL

# Small diagnostic crawl; any truncation is explicitly marked PARTIAL
python -m qualitative_ir_downloader.main --ticker EXEL --max-archive-pages 2 --max-interactions 3 --max-documents 2
```

The application always processes worksheets in this order:

1. Safe
2. High Growth Potential
3. Turnaround Story

Within each worksheet, column B is the ticker and C is the company, starting at row 4. Blank rows are ignored; incomplete pairs are logged with the original row number. Companies are never processed concurrently. Repeated tickers on different rows are separate processing units with their own worksheet provenance.

`--ticker` is case-insensitive and selects every matching row within the selected worksheet(s). `--start-ticker` starts at the first matching row. These two options are mutually exclusive. `--limit` is applied after filtering. A missing requested ticker is an error rather than silently processing something else.

Additional options:

| Option | Default | Purpose |
| --- | --- | --- |
| `--service-account PATH` | `C:\Code\service_account.json` | Credential file |
| `--download-root PATH` | Current user's `Downloads` | Output location |
| `--request-delay SECONDS` | 1.0 | Delay before navigations, downloads and archive clicks |
| `--max-archive-pages N` | 250 | Unique archive URLs per category |
| `--max-links-per-company N` | 10000 | Document-link ceiling across the company |
| `--max-interactions N` | 500 | Archive-control operations per category |
| `--max-documents N` | Unset | Diagnostic download cap per category |
| `--verbose` | Off | Detailed logs |
| `--list-only` | Off | Read and validate stock selection only |

The document-link budget is shared by both categories; the report collector receives the remaining budget after news discovery. Other settings (45-second navigation timeout, three attempts, 150 MiB maximum response, 15 discovery candidates, 20 section-search pages) are in `config.py`. TLS certificate verification remains enabled. Chromium always launches with `headless=True`.

Exit codes: **0** successful run/listing; **1** configuration, sheet-access or run failure; **2** at least one failed/partial company; **130** interrupted by the operator. `SUCCESS` means the configured traversal finished without a detected error; it is not proof that a publisher exposes its entire historical archive.

## Output

The normal output root is `Path.home() / "Downloads"`; the Windows username is not hard-coded.

```text
Downloads/
  qualitative_analysis_run_2026-09-06_10-45-32.log
  qualitative_analysis_run_summary_2026-09-06_10-45-32.json
  qualitative_analysis_run_summary_2026-09-06_10-45-32.csv
  EXEL_Exelixis_qualitative analysis_2026-09-06_10-45-32/
    News Releases/
      2026-08-14_EXEL_Release title.pdf
    Reports/
      2025_EXEL_Annual Report.pdf
    Events/
      2026-08-05_EXEL_Q2 Financial Results.pdf
      2026-08-05_EXEL_Q2 Financial Results.json
    logs/
      collection.log
    manifest.json
```

Invalid Windows filename characters, reserved device names, trailing dots/spaces and long titles are handled. Filename collisions receive `_2`, `_3`, etc. If an unusually long custom root leaves insufficient path space, the document fails with a clear message requesting a shorter root.

Each manifest includes the sheet coordinates, IR URL, scored discovery evidence, selected archive URLs, discovered document inventory, successful document metadata, errors, status, and archive page counts. Successful document records contain title, publication date when known, original source URL, final PDF source URL (for original PDFs), relative filename, method, SHA-256 hash and optional `duplicate_of`.

Manifests are checkpointed through atomic replacement after each saved document or error. PDFs are validated before an atomic `.part` replacement. Identical content is stored once per company, even across categories; all source records remain in the manifest and point to the retained file. Counts in the summary represent unique saved files, so a cross-category duplicate may point into the other category's folder.

The final JSON/CSV summaries are also saved after each company. Master logs include row-validation errors; company logs contain that company's discovery and collection details.

## Discovery, access and provenance

Official ownership and network access are independent. An `IRCandidate` retains `officiality_score`, `official`, `evidence`, `content_validated` and an `AccessResult` containing each transport's status. A corporate-verified IR site remains official when either transport fails.

1. Free browser search first finds corporate website candidates. Corporate validation requires a branded **registrable** domain and company identity in page title and content. Bundled public-suffix data handles domains such as `company.co.uk` without an additional online lookup.
2. Explicit Investors / Investors & News / Shareholders links on the verified corporate page establish IR ownership before access probes. Cross-domain targets establish hosted-provider provenance; a provider suffix alone never establishes ownership. The manifest records the source page, target URL and relationship.
3. Basic HTTP is probed separately, then ordinary Playwright Chromium navigation is attempted, including after HTTP 401/403/429. Chromium uses its default browser identity. No stealth, fingerprint evasion, proxy rotation, challenge-solving or bypass tools are used.
4. Search fallback remains conservative. Quartr, AlphaSpread and the existing financial-information aggregators are explicitly rejected. Search results alone cannot validate unknown third-party hosting.
5. A retrieved page must validate as company IR content before collection. Ownership, transport accessibility and content validation remain separately recorded. If Chromium is also blocked, the result is `SITE_BLOCKED`, not `IR_DISCOVERY_FAILED`. Only explicitly verified aliases/public links can provide alternate routes; the collector never invents a provider alias.
6. News and reports navigation uses scored anchor text, URL paths and context. Press Releases, All News, Featured Reports, View All Reports, Annual Reports, Financials and Resources are recognized generically. No ticker-specific URL mapping is used.
7. Archive traversal supports linked pagination, native year filters, Load More, accessible tabs and accordions, including year/tab combinations. Safety limits and stalled controls are explicit partial-run diagnostics.
8. RSS/Atom feeds explicitly exposed by official pages are recorded and parsed as supplemental news discovery. Feed URLs are deduplicated with archive URLs. RSS can supply releases if archive traversal fails, but the archive failure remains recorded: a feed never certifies historical completeness. Feed entries remain within the verified ecosystem.
9. Original PDFs, including controls labelled **PDF Version**, are preferred. Direct HTTP downloads carry ordinary cookies and a referer. If the direct request fails, the existing Playwright context can request the linked resource when its referring host was successfully loaded by that browser session. HTTP transport blocks do not blacklist browser navigation. [Playwright documents this shared cookie context](https://playwright.dev/python/docs/api/class-apirequestcontext).
10. Response validation, bounded redirects/viewers, byte limits, retry backoff, SHA-256 deduplication and HTML-to-PDF printing remain in place. The manifest retains the archive/referring page as `linked_from_url`, source URL, final PDF URL and saved filename.

The ownership/access fields introduced in schema v2 record `ecosystem` (corporate domain, selected IR domain, verified hosted domains, document domains and source-to-target evidence), `access_events`, `ir_access`, `ir_official`, `ir_accessible`, `ir_content_validated`, `rss_feeds`, discovery diagnostics and separate collection errors.

The final console/JSON/CSV summaries distinguish **Discovery Errors**, **Access Blocks**, and **Document Failures**, alongside officiality and accessibility. An HTTP denial recovered through normal browser access is an access event, not a terminal collection error. A blocked official site can therefore report `ir_official=true`, `ir_accessible=false`, zero document failures and `SITE_BLOCKED`. Document failures retain the compatibility field `failed_downloads` as well as `document_failures`.

## Public provider fallbacks (schema v3)

The existing generic browser crawler remains the first collection path. If it is blocked, empty or incomplete, the provider registry selects a verified adapter. GCS-Web recognition requires either a verified hosted domain or company-identified NIR platform markup plus an explicit RSS route on the already verified IR host. The adapter never invents a company alias or takes a provider suffix as ownership evidence.

Public convention probes include RSS subscription/feed routes and sitemaps. Every generated URL stays within verified IR hosts. Public HTML archives are validated by company identity and page topic; year-query names and values come from actual GET forms, and pagination comes from actual links. News and report collection remains targeted. A rejected or blocked endpoint is recorded, not worked around.

RSS records retain GUID, description, content, feed URL, release URL and provenance. Identity merges use canonical URL, GUID or title/date. Feed statistics record entry count, oldest date and coverage; an RSS feed alone is marked incomplete or unknown. Full-body PDF generation requires a full-content field plus conservative completeness evidence; truncated descriptions are never printed as complete releases. The completeness check is conservative and is not a publisher guarantee.

Retrieval methods are explicitly named `official_pdf`, `official_html_to_pdf`, `official_rss_to_pdf`, and `verified_wire_source_html_to_pdf`. An original PDF link is tried first. A publicly retrievable official HTML body can be printed even when browser navigation is unavailable. RSS and wire fallbacks retain their distinct sources. Wire fallback requires a first-party link to an actual wire release, not a title-search match or a wire tracking/navigation link.

`/static-files/...` assets do not need a `.pdf` suffix: bytes and PDF structure determine validity. Alternate asset URLs are used only when independently referenced by verified sources and merged as the same document. Requests use the HTTP library's own identity rather than impersonating a browser. Ordinary cookie-session fallback and the existing retry policy remain; no challenge-solving, fingerprint spoofing, stealth or proxy mechanism is used.

Every discovered document remains in its category list with status `DISCOVERED`, `DOWNLOADED`, `BLOCKED` or `FAILED`, even after a download error. `BLOCKED` records include the known HTTP status and log `DISCOVERED_BUT_BLOCKED`. Run metrics distinguish discovered, downloaded, blocked and failed counts, and separately count RSS/wire-generated news PDFs. Schema v3 replaces the former successful-documents-only category lists; downstream readers should filter `status == "DOWNLOADED"` before opening files.

## Events and webcast transcripts (schema v4)

News, Reports and Events run by default. Use these commands for bounded validation:

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker EXEL --events-only --event-limit 1
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker EXEL --skip-events
# Explicitly regenerate a completed event:
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker EXEL --events-only --event-limit 1 --force-events
```

`--events-only` reuses the newest company folder whose ticker, company, worksheet and row match. It preserves News and Reports. Completed events with valid PDFs and required JSON are reused across manifests; `--force-events` overrides reuse. Events no longer in a rolling archive remain in the existing manifest. An event limit counts replay candidates, newest historical first; future events without replay and presentation-only events do not consume the limit. The manifest and summary distinguish intentional limiting from discovery coverage.

Event discovery uses the existing browser archive traversal for pagination, years, tabs, accordions and Load More, plus validated public official HTML routes when browser access fails. An exposed archive does not establish that older replays still exist. Provider links are accepted only from official IR event pages; external players never establish IR ownership.

Webcast adapters inspect ordinary player/iframe markup, structured configuration and browser responses. The generic registration handler uses the configured profile in `events_config.py`. It submits at most once per form in a session, leaves marketing boxes unchecked, and accepts only required webcast terms/privacy checkboxes. Unknown required fields, passwords, CAPTCHA, email verification and other access gates stop the event with distinct statuses. A required country must be configured explicitly; locale is not used to invent one. No Gmail, login bypass, stealth or DRM processing is included.

Official transcripts are preferred over audio recognition. Otherwise direct media, finalized unencrypted HLS and supported static DASH are acquired using the legitimate session. Media cookies retain their domain/path scope; bearer headers are not forwarded to unrelated hosts. FFmpeg reads only local acquired files, with network/crypto protocols disabled. Signed URL query credentials are redacted in persisted provenance and logs. Registration contact details are not copied into event records.

### Local dependencies and Windows FFmpeg

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Dependencies include `faster-whisper`, `reportlab`, and `imageio-ffmpeg`. The last package supplies a project-local FFmpeg executable, so a PATH installation is optional. The application checks an explicit `--ffmpeg` path, then PATH, then the bundled executable. It reports a dependency error if none is available.

For a system installation, download a Windows build linked by [FFmpeg's download page](https://ffmpeg.org/download.html), extract it, add its `bin` directory to your user PATH, and open a new PowerShell window. Verify with:

```powershell
ffmpeg -version
# Or inspect this project's bundled executable:
.\.venv\Scripts\python.exe -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
```

The local engine defaults to `large-v3`, English, beam size 5, deterministic temperature and voice-activity detection. The first use downloads the model from the public Hugging Face model repository into `~/.cache/qualitative-ir/whisper`; large models require several GB of storage. Later runs reuse that cache. See the [faster-whisper installation and GPU requirements](https://github.com/SYSTRAN/faster-whisper). CUDA is detected at runtime; unavailable/failed GPU initialization falls back to CPU int8. CPU transcription can take a long time. No cloud transcription or LLM rewriting is used.

Options include `--whisper-model small|medium|large-v3` (or a local model path), `--whisper-language en|auto`, `--whisper-device auto|cpu|cuda`, and `--keep-event-media`. Unknown speakers remain unlabelled. Raw timestamped segments are preserved without financial corrections.

Successful output is a selectable-text PDF plus schema-v1 transcript JSON in `Events/`. JSON contains ticker/company, event metadata, stable source URLs, engine/model/language/duration and `{start,end,text,speaker}` segments. Official text without timestamps uses null start/end values. Temporary media is deleted only after PDF and JSON succeed, unless retention is enabled. Transcription failures retain media and partial checkpoints; PDF failures retain the complete JSON. Resume of failed events currently reacquires media, while completed events are skipped.

Default media limits are 60 seconds minimum, six hours maximum, 2 GiB acquisition budget per candidate and bounded candidate/segment counts. Multi-period DASH, open-ended DASH timelines and unfinished live playlists stop explicitly rather than producing a truncated transcript. Speaker diarization is not included. Optional debug snapshots are disabled by default because a registration/player page may contain personal or session information.

## Tests and validation

Generated-transcript integration search: `python -m qualitative_ir_downloader.main --find-generatable-event --max-companies 20`.
This mode tests one historical event per company, skips official transcript/caption sources,
and stops only for a generated transcript. Normal collection keeps its official-source priority.
Use `--skip-event-registration` to prohibit all profile submissions and test only ungated replays.
The default company bound is 20 for generated search and 10 for `--find-transcribable-event`.
Integration modes always enforce one event per company, even if `--event-limit` is supplied.
Generated output requires at least 10 segments and 1,000 characters (EventConfig settings),
valid timestamps, valid saved JSON and PDF, and at least 60 seconds of audio.

```powershell
# Pure/offline unit tests, no browser or live websites
python -m unittest discover -s tests -v

# Headless Chromium integration tests; all requests are fulfilled by local fixtures
python -m unittest discover -s tests/browser -v

# Syntax/import check
python -m compileall -q qualitative_ir_downloader tests
python -m qualitative_ir_downloader.main --help
```

The browser suite covers archive discovery, years, tabs, Load More, pagination, CDN report links, inline featured reports, safety limits and multi-page HTML printing. The PDF fixture is written into ignored `artifacts/pdf-qa/`. These tests are distinct from the explicit live smoke commands above.

The September 17 validation passed **132 tests** (108 offline/unit and 24 Chromium fixtures). A real 64.4-minute ZM call was transcribed from retained public audio in 15m43s, and completed-artifact reuse was verified by the live CLI in 13.5s. The generated transcript carries a local repetition warning; financial/name accuracy is not certified. See [ZM_PERFORMANCE_REPORT.md](ZM_PERFORMANCE_REPORT.md) for measurements, quality limits and exact outputs. Earlier validation reports remain historical records.

## Transcription profiles, benchmarking and reuse

The default `--transcription-profile balanced` uses medium.en, CPU int8, four threads, beam 1 and batch 2. `fast` uses small.en/beam 1/batch 1; `maximum_accuracy` uses large-v3/beam 5/batch 1 (the name does not establish measured accuracy). `--whisper-model`, `--cpu-threads`, `--beam-size` and `--batch-size` override these choices. Automatic/non-English language settings select multilingual small/medium unless the model is explicitly set. Profile settings, dependency versions, hardware, fingerprint, stage timings and QA are saved with generated output.

```powershell
python -m qualitative_ir_downloader.main --ticker ZM --events-only --event-limit 1
python -m qualitative_ir_downloader.main --benchmark-transcription --benchmark-audio artifacts/zm_benchmark/media/normalized.wav --benchmark-seconds 300 --benchmark-model medium.en --transcription-profile balanced
```

Benchmarks require existing local audio, use at most 300 seconds, and skip Sheets/IR/browser discovery. Repeat `--benchmark-model` to compare models. Model downloads can add setup time on first use; the report separates model loading from decoding. Memory reports are process-lifetime high-water marks.

Completed event PDF/JSON artifacts are validated and reused across runs. Changed date/media identity invalidates reuse; `--force-transcription` explicitly regenerates. QA warnings survive reuse as metadata; a validated generated artifact returns `GENERATED_TRANSCRIPT_ALREADY_AVAILABLE` and is not treated as a processing failure. Ctrl+C signals the transcription worker, retains audio and writes an incomplete checkpoint at the next yielded segment; interrupted partial decoding does not support automatic segment-level resume. No automatic repetition deletion or financial-number correction is performed.


## Operational limits and extensions

This is a generalized heuristic collector, not a claim of universal website compatibility. Free search HTML, site-specific navigation, inaccessible archives, custom controls without accessible labels, nested filters that reset one another, image-only release pages, unusual viewers, and publisher layout changes can require a provider adapter. A site can hide documents without an observable error; review discovery evidence and sample the resulting files before relying on coverage across a large universe.

General news/report collection and event transcript resume have different lifecycles. Event mode can reuse validated completed artifacts from earlier company folders and marks interrupted event work explicitly. Partial decoding checkpoints preserve progress evidence but are not a guarantee of partial transcription resume.

Modules separate access (`network.py`), supplemental feeds (`rss.py`), sources (`google_sheets.py`), policy (`scoring.py`, `urls.py`), browser lifecycle (`browser.py`), site discovery (`ir_discovery.py`, `sections.py`), collectors (`news.py`, `reports.py`, `crawler.py`, `events.py`), storage (`downloader.py`, `filesystem.py`, `pdf_utils.py`), and orchestration/provenance (`main.py`, `manifest.py`). Event transcription, provider adapters and QA are separate modules. No separate SEC crawler, company scoring, LLM call or valuation logic is included.


## Durable event state and generated search

`event_artifacts.json` under the download root is shared by all CLI modes and timestamped company folders. The canonical company bootstrap loads verified IR, access strategy, provider observations and event state before network discovery. Historical manifests are imported incrementally before a run can replace their current event records. Older artifact locations remain fallback candidates if the latest copy is deleted.

Every artifact reuse validates the PDF and, for generated transcripts/captions, the JSON, segments, timestamps, saved event identity and PDF transcript content. QA warnings do not invalidate structurally valid transcripts. Registry entries are hints, not unconditional completion claims.

Generated search accepts a validated existing generated transcript by default. `--require-new-generated-event` continues past completed events without retranscribing them. `--force-transcription` (alias `--force-events`) deliberately bypasses reuse. Summaries distinguish `generated_transcript_created_this_run`, `generated_transcript_reused`, and `generated_transcript_available`, plus media attempts, cached skips, downloads and Whisper calls.

Protected outcomes expire after seven days; metadata listings and discovered official transcript sources expire after 24 hours. A changed known date, webcast/media identity or official source invalidates reuse. `--force-events` rechecks immediately. Cached outcomes consume no new-media-attempt budget. Provider history influences ranking only; it does not blacklist a provider.

Unknown-company integration IR discovery has up to four uncached logical search queries, five browser probes, five network candidates and a 45-second overall IR-discovery timeout. Guessed corporate domains are candidates for verification only; retail/support/forum/unrelated search domains are filtered before navigation. Strong cached official identity is reused unless `--rediscover-ir` or explicit invalidation applies.

See [EVENT_CACHE_REGRESSION_REPORT.md](EVENT_CACHE_REGRESSION_REPORT.md) for current test evidence and the outstanding live-validation dependency.


## Company and event discovery diagnostics

Discovery keeps the spreadsheet name, normalized company identity and brand aliases separately. Security/legal suffixes are removed before querying; aliases and guessed domains are only hints. Officiality combines corporate navigation, independently identified corporate domains, company/ticker page content and search titles/snippets. A blocked request does not erase verified ownership. Unrelated result sets trigger an alternate engine/query rather than navigation to unrelated sites.

`ir_discovery_cache.json` in the download root caches normalized search queries for one hour and failed host transport for 15 minutes. Scan discovery remains bounded to 45 seconds per company. Summaries include queries, search time, HTTP/browser probes, candidate/rejection evidence and separate event-provider diagnostics. A later run can be faster because it reuses these caches; timings are not a cold-run benchmark.

Production manifests and run summaries expose `ir_status`, `news_status`,
`reports_status`, `events_status`, and the company-level `overall_status`
(`SUCCESS`, `SUCCESS_WITH_LIMITATIONS`, `PARTIAL`, or `FAILED`). News and
reports include explicit completeness states and discovered/newly saved/cached
counts. Incremental runs validate existing document identity and report
`artifact_cache_hits`, `documents_skipped_existing`, `event_cache_hits`, and
`network_requests_avoided`. See [PRODUCTION_STATUS_REPORT.md](PRODUCTION_STATUS_REPORT.md)
for the aggregation rules and live rerun measurements.

Event discovery recognizes quarterly report cards, embedded schema/Q4-style event records and public rendered event lists. It observes JSON returned during ordinary browser navigation; it does not guess private endpoints or bypass access restrictions. Empty cached listings are retried so parser improvements can take effect. The discovery statuses distinguish `IR_DISCOVERY_FAILED`, `IR_VERIFIED_EVENT_DISCOVERY_FAILED`, `IR_VERIFIED_ACCESS_BLOCKED` and `EVENTS_FOUND`; final processing outcomes remain separate.

See [DISCOVERY_UPGRADE_REPORT.md](DISCOVERY_UPGRADE_REPORT.md) for the September 18 measurements and remaining live limitations.
