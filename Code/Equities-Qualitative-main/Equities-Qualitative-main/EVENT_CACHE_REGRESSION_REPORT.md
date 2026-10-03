# Event cache regression: implementation and validation status

The code and fixture work are implemented. **The requested live ten-company rerun and incident-specific root-cause confirmation remain pending.** No full ZM transcription, registration submission or expanded company search was run during this work.

## Root cause of ZM retranscription

Confirmed code defects: candidate scanning marked selected events `CANDIDATE_SCANNED` without first checking completed artifacts; there was no durable event index; historical completion lookup recognized only `TRANSCRIBED` / `PDF_CREATED`, although generated-search reuse changed status to `SKIPPED_COMPLETED_GENERATED`; generated-search stopping/counters explicitly excluded resumed transcripts and QA warnings. These made completion knowledge dependent on mutable manifest history and caused completed candidates to be ranked as new media work.

These are architectural defects, not a proven reconstruction of the specific 950.64-second run. Its log and manifests are currently absent, so the exact cache-miss condition for that incident cannot honestly be asserted yet. The previous shared `process_company` path already attempted verified-IR recovery; a claim that it categorically bypassed lookup would be inaccurate.

## Root cause of verified-IR rediscovery

Pending the incident cache/log. The current Downloads directory has no `verified_ir.json`; the workspace seed contains EXEL and CARG, but no ZM. The original ZM output directory recorded in `artifacts/zm_benchmark/full_validation_location.json` is also absent. These observations explain why a live verification now would not reproduce the user's established cache state, but do not establish what happened in the earlier run.

The new canonical `prepare_company_context` loads verified IR, provider/access observations and global event state before manifest mutation or network work. All modes use it. Cache recovery accepts the existing `officiality_score` and legacy `officiality` spelling, handles UTF-8 BOMs, respects explicit invalidation, and preserves identity/evidence requirements. Access-strategy keys normalize company names and URLs, with compatibility for older keys.

## Persistent event registry design

`<download-root>/event_artifacts.json`, schema version 1. Primary key: ticker + event date + canonical event URL, with normalized title fallback for synthetic listing anchors. Company identity remains an additional guard. Each entry retains the event, folder, absolute PDF/JSON locations, redacted media identity, model, validation state and last checked time. Previous artifact locations are retained as fallback copies. Historical manifest imports are tracked by path and modification time.

A lookup validates PDF existence/structure, JSON existence/parseability, nonempty valid segments/timestamps, transcript content in the PDF and saved event identity. Original official PDFs use their recorded event/source provenance and PDF validation. QA-warning generated transcripts are reusable. Changing a known date, webcast/media URL or official source invalidates the match. Registry booleans never substitute for validation.

Protected outcomes have a seven-day TTL; event listings and discovered official-source hints have a 24-hour TTL. `--force-events` / `--force-transcription` bypass cached outcomes. These are expiring observations, not permanent provider exclusions.

Ordinary generated search stops on validated existing generated availability. `--require-new-generated-event` continues to another candidate without repeating the completed decode. Generated-created, generated-reused and generated-available counters are separate; QA state remains visible metadata. Cached official/protected/generated outcomes do not consume the new-media budget.

## Files changed

New: `qualitative_ir_downloader/event_registry.py`, `tests/test_event_registry.py`, this report.

Updated: `company_context.py`, `main.py`, `events.py`, `event_discovery.py`, `event_models.py`, `event_outcomes.py`, `events_config.py`, `ir_state.py`, `ir_discovery.py`, `access_strategy.py`, `tests/test_orchestration.py`, `README.md`.

## Cross-run resume test

A genuine fixture PDF/JSON created under run A is reused under run B. The test asserts no player creation, media acquirer or Whisper call; created=0, reused=1, available=1, with the prior QA warning retained. Tests also cover deleting the latest copy and falling back to the older valid artifact, importing historical completed-skip statuses, corrupt/deleted artifacts, mismatched saved ticker, changed date/webcast and explicit force bypass.

## Verified IR cache test

The actual `process_company(scan_only=True)` then processing path is tested with durable verified IR and a cached listing/artifact. IR discovery, event network discovery, player creation and Whisper are all asserted absent. Existing mode fixtures also exercise production/event/search/destination discovery paths.

## max-media-attempts test

Cached generated, official and protected records leave budget.used=0. Explicit forced processing enters the real player path and increments it. The shared budget permits three claims and rejects a fourth independently of cached-skip count. Summaries expose media attempts, cache skips, media downloads and Whisper runs. Separate orchestration tests verify ordinary reuse stops search and require-new continues to a newly created result.

## RMD/FUTU/INCY search-efficiency changes

Conservative domain candidates precede generic search; user-specified Credo semiconductor and Futu corporate hints are candidates only, not ownership assertions. Full company identity is still required for verification. Credo Technology does not accept credogroup.com merely on a shared token. Retail, myair, forums, generic AI/weather/dictionary and aggregator domains are rejected before browser navigation. IR subdomains are prioritized; failed corporate-root access still permits public IR search/content verification. Already-tested corporate roots are not probed repeatedly within the same discovery.

Integration scanning is bounded to two logical search queries, five network candidates, five browser probes and 45 seconds total for IR discovery. This is a discovery timeout, not a promised overall ten-company runtime. Provider/media stages remain separately bounded by their existing controls.

## Tests passed

127 offline/unit tests and 24 Chromium fixture tests passed (151 total). The registry suite contains 18 focused tests; orchestration adds a further require-new/ordinary-reuse stopping test. The browser suite passed during implementation; the final history-fallback, strategy-key normalization and counter adjustments were covered by the subsequent full unit rerun. See `artifacts/registry_tests.log`, `artifacts/registry_focused_tests.log` and `artifacts/registry_browser_tests.log`. No live provider/cache assertion should be inferred from fixtures.

## Expected runtime for the same 10-company command

Not measured yet. Validated cached events avoid all media/transcription work; unknown-company discovery remains bounded. The prior 950-second duplicate decode should be absent when the prior artifacts are accessible and validate. An exact overall duration requires the requested live rerun against the actual cache.

## Exact command to run next

After locating/restoring the actual prior output root and verified IR cache:

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main `
  --find-generatable-event `
  --max-companies 10 `
  --max-media-attempts 3 `
  --approved-registration-domain media-server.com `
  --approved-registration-domain wsw.com `
  --approved-registration-domain webcasts.com
```

If those outputs were moved to another root, supply that verified root with `--download-root`. Do not force transcription for this verification. The live command has deliberately not been launched with the missing historical state, because that would risk repeating the explicitly prohibited full ZM decode.
