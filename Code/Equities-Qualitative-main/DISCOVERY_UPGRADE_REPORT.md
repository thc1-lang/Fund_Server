# Discovery upgrade validation

The September 18 acceptance run used the requested command with the approved webcast registration domains. It scanned the same 19 spreadsheet companies as the baseline.

## Root causes

- IR discovery treated spreadsheet security suffixes as company identity, generated poor inferred domains, and rejected branded or hosted IR domains too early.
- Search result quality was not assessed as a whole result set, and repeated queries and failed hosts consumed most of the scan budget.
- A 403 was incorrectly allowed to erase otherwise strong corporate and search evidence.
- Event discovery treated empty cached listings as final and did not parse rendered report cards or bounded public JSON/schema event records.

## Changes

`company_identity.py`, `ir_evidence.py`, `ir_discovery.py`, `discovery_cache.py`, `event_discovery.py`, `event_registry.py`, `main.py`, and `browser.py` now provide separate raw/normalized/brand identity, evidence-based ownership, bounded alternate search, one-hour query and 15-minute transport caches, public-HTML verification, exchange-prefixed investor navigation, access-block retention, rendered event-card/JSON parsing, and per-company diagnostics. `README.md` documents the behavior and limits.

The transcription, event-processing, and media-acquisition modules were preserved. The only registry behavior change retries an empty cached listing so a parser improvement cannot be hidden by an empty cache.

## Measured acceptance result

Baseline: 9 verified IR companies, 10 IR failures, 0 newly recognized PLTR events, 20 search queries in the log, 352.3 seconds of total scan time.

Completed acceptance run: 12 verified IR companies, 7 IR failures, 4 separate verified-IR event-discovery failures, 27 PLTR events, 25 search queries, 9 HTTP probes, 1 browser probe, 185.1 seconds total scan time, and 1.80 seconds average discovery time per company. The final rerun reused 2 generated transcripts, skipped 7 cached media outcomes, made 0 Whisper calls, and used 1 new media attempt. The final log is `artifacts/discovery_upgrade/acceptance_complete.log`.

## Targeted cases

- RMD: global corporate identity is ranked ahead of the UK site; the live corporate root is 403, so ownership remains an access-limited discovery case.
- CRDO: `credosemi.com` is a compound brand candidate and is never accepted from the alias alone; its blocked IR page remains rejected without independent page evidence.
- ATAT: `ir.yaduo.com` is investigated and can be accepted when the saved page contains company identity; live access remains blocked in this run.
- ANET: `investors.arista.com` is accepted from same-registrable-domain and search evidence despite 403; status is `IR_VERIFIED_ACCESS_BLOCKED`.
- PLTR: rendered quarterly report cards now yield 27 dated events; the selected webcast was not downloadable, so no transcript was fabricated.
- NVDA: the verified IR identity is retained; ordinary event routes return 403 and are reported as access blocked.
- ONC and ERO: official corporate/IR relationships were verified; their exposed event archives contained no recognized event records and are reported as `IR_VERIFIED_EVENT_DISCOVERY_FAILED`.

## Regression and tests

The established EXEL, AFRM, QLYS, CARG, DAVE, ZM, and INCY paths remain cached/reused or protected as before. Unit tests: 146 passed. Chromium fixture tests: 24 passed. `compileall` and CLI help checks passed. Hash checks confirm `transcription.py`, `events.py`, and `webcast_media.py` are unchanged; the registry hash differs only for the documented empty-list retry.

## Next command

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --find-generatable-event --require-new-generated-event --max-companies 50 --max-media-attempts 5 --approved-registration-domain media-server.com --approved-registration-domain wsw.com --approved-registration-domain webcasts.com
```
