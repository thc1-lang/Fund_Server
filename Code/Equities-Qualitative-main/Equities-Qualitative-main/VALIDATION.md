# Events module validation - 8 September 2026

Implementation is ready for a live test, but live acceptance is **not complete**. The user explicitly approved submitting the supplied profile to EXEL's linked provider at `edge.media-server.com` with optional marketing disabled and access-control barriers respected. The first approved run reached the sheet stage but search returned unrelated pages and IR discovery stopped before event processing. An events-only resume fallback was added to reuse the previously verified official IR URL. A second network rerun was then rejected before process creation because the environment usage limit was reached; no registration submission or live media acquisition is claimed.

## Checks completed

- 63 offline/unit/media tests and 19 Chromium fixture tests passed: **82 total**, preserving all prior tests.
- Direct audio, HLS and DASH fixtures were generated with FFmpeg, acquired without network access, and normalized to mono 16 kHz WAV with verified duration.
- Registration fixtures cover the supplied fields, Other selection, mandatory terms, optional marketing left unchecked, password/CAPTCHA gates, iframe audio and end-to-end fixture PDF/JSON creation.
- Completed-event resume was checked: no second media or transcription call.
- An eight-page synthetic transcript PDF was rendered and all pages visually reviewed. Text is selectable; timestamps stay with following paragraphs; source metadata and page numbers are readable. This fixture is not a live transcript.
- Local faster-whisper, CTranslate2, ReportLab, PyAV and bundled FFmpeg dependencies installed successfully. The large-v3 model is cached. Eight CPU threads and zero CUDA devices were detected.

## Exact live-validation state

| Requested result | Observed state |
| --- | --- |
| Ticker inspected | EXEL |
| IR Events URL | https://ir.exelixis.com/event-calendar |
| Events discovered in public diagnostic page | 13 |
| Historical events in that page | 10 before 8 September 2026 |
| Webcasts verified | 1 selected event verified; live processing pending environment availability |
| Event selected | Exelixis Q2 2026 Financial Results Conference Call |
| Event date | 2026-08-05 |
| Official event URL | https://ir.exelixis.com/events/event-details/exelixis-q2-2026-financial-results-conference-call |
| Webcast URL | https://edge.media-server.com/mmc/p/vfpzg7ix |
| Registration required | Yes; public first name, last name, email, company, occupation form |
| Registration result | Not submitted; approved rerun was unavailable after environment usage-limit rejection |
| Provider | Media Server; verified hostname and actual player script assets |
| Provider HTTP / ordinary Chromium | 200 / 200 in read-only diagnostics |
| Media type | Not yet acquired or verified |
| Duration | Not yet measured |
| Transcription engine | faster-whisper configured; live transcription not run |
| Model / device | large-v3 cached / CPU int8 selected by hardware detection |
| Transcript segments | 0 live segments |
| PDF created | No live PDF; fixture PDF verified |
| JSON created | No live transcript JSON; fixture JSON verified |
| Manifest updated | No live Events update; existing collection preserved |
| Temporary media removed | Not applicable to live run; successful cleanup tested with fixtures |
| Failures | Automatic approval review rejected external profile submission |
| Remaining limitations | Real provider registration, media acquisition and real audio transcription remain unverified until approved |

The event archive page exposes no year selector or ordinary pagination in its retrieved HTML. Its 13 entries do not prove complete historical coverage. No second company or additional webcast was processed.

## Reproduction command after approval

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker EXEL --events-only --event-limit 1
```

[REGISTRATION_REVIEW.md](REGISTRATION_REVIEW.md) identifies the exact destination and supplied profile for approval. The local registration configuration contains the profile supplied by the user; logs and manifests redact contact/temporary query credentials.

## Existing News and Reports evidence

The retained manifest at `C:/Users/Nicholas/Downloads/EXEL_Exelixis_qualitative analysis_2026-09-06_12-59-49/manifest.json` records 692 news releases discovered and saved, 29 reports discovered, 27 reports saved and 2 report failures. It retains PARTIAL status, with exposed archive traversal recorded for both categories. These are retained-run metrics, not a new collection performed in this Events task.

---

# Historical validation: corporate-first discovery/access refactor

Date: 6 September 2026. Final EXEL run: 2026-09-06_11-45-25.

- 23 offline unit tests and 11 headless Chromium integration tests passed (34 total).
- Syntax compilation, dependency consistency and CLI checks passed.
- The exact requested command ran with the default Downloads destination: `.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --ticker EXEL`.
- Selected IR URL: https://ir.exelixis.com/.
- Officiality: 100, verified from the official corporate site's explicit **Investors & News** link and the corporate/IR domain relationship.
- Basic HTTP: 403. Ordinary Chromium: 403, page title **Access Denied**.
- Final status: **SITE_BLOCKED**, with `ir_official=true` and `ir_accessible=false`.
- News releases discovered: 0; news PDFs saved: 0.
- Reports discovered: 0; reports saved: 0.
- Discovery errors: 0; access blocks: 2; document failures: 0.
- Both news and report section discovery were attempted and stopped on the browser-confirmed access restriction. Their archive URLs and RSS could not be extracted from the inaccessible IR homepage.
- No hosted alias was asserted from a provider suffix or search snippet alone. No aggregator was accepted. No access-circumvention mechanism was added.
- The manifest and output directory were inspected; there are zero PDFs, so there were no live downloaded PDFs to visually inspect.

[Final manifest](<C:/Users/Nicholas/Downloads/EXEL_Exelixis_qualitative analysis_2026-09-06_11-45-25/manifest.json>) contains ownership evidence, transport results, errors and metrics. The full-universe run was not started.

The architecture regression is fixed. Successful live EXEL document collection remains unverified and blocked from this environment. The fixture tests prove generic archive, RSS and PDF behavior; they do not establish live historical coverage.

---

# Historical initial validation

Date: 6 September 2026

## Local checks

- Python 3.14.7, isolated project virtual environment.
- Dependencies installed successfully; `pip check` reports no broken requirements.
- Package and test sources compile with `compileall`.
- CLI help/import check passed.
- 16 offline unit tests passed.
- 7 headless Chromium integration tests passed using in-memory web routes.
- Additional focused regression covers pagination inside a navigation landmark.

Unit coverage includes Windows names and collisions, folder layout, URL identity, scope, row offsets and incomplete rows, company/section scoring, dates, PDF validation, byte-hash deduplication, document-viewer resolution, blocked responses, CLI selection, document failure isolation, and retained discovery diagnostics.

Browser coverage includes dynamic section discovery, Load More, native years, linked pagination, CDN report assets, inline featured reports, year/tab combinations, explicit safety-limit reporting, original-PDF preference, HTML fallback, and complete multi-page printing.

The three-page PDF fixture was rendered with Poppler and every page visually inspected. The title, date, body and final release paragraph were present without clipping. Fixture PDFs and rendered pages are diagnostic outputs under ignored `artifacts/pdf-qa/`.

## Authorized live smoke test

The service account at the requested default path successfully read all three worksheets, in order, with the read-only Sheets scope. EXEL was found at Safe row 4 with company name Exelixis.

A bounded EXEL run used public search, at most two archive URLs per category, three archive interactions and two downloads per category. Search discovered the Exelixis IR domain. Requests to that domain returned HTTP 403; collection stopped at IR discovery and no PDFs were downloaded. The company result was IR_DISCOVERY_FAILED.

The run's console/file diagnostics are in ignored `artifacts/live-smoke/`; the read-only check is in `artifacts/sheet-check/`. Subsequent changes retain blocked candidate details in the manifest and prevent repeated requests to blocked hosts within a company session; these behaviors are verified with offline tests.

This establishes sheet access, conservative blocked-site behavior, and fixture-based collection behavior. It does not establish successful live EXEL collection or complete coverage of other publishers. No full-universe crawl, document analysis or spreadsheet modification was performed.

