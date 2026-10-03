# Event reconciliation regression fix

## Root cause

Fresh event listings were being treated as replacements for durable event state. Selection happened before enriched registry state was reattached, so a fresh INCY/EXEL event could look like a new sparse listing. A later empty selection then allowed `EVENT_DISCOVERY_FAILED` to overwrite a valid generated or protected outcome. NVDA also allowed a later generic event failure to replace a stronger access-block result.

## Fix

`event_registry.py` now provides the canonical reconciliation stage. It matches by canonical event URL, provider event ID, or date plus normalized title. Event URLs ignore tracking/signature query data; webcast identity is never primary, while a changed webcast path still invalidates an exact same-URL record. Field-specific merging retains validated transcript artifacts, QA/model metadata, protected media, provider/registration state, and terminal outcomes while allowing fresh title/date/webcast metadata.

`events.py` reconciles every fresh listing before selection, hydrates validated cached outcomes before ranking, skips completed generated events for `--require-new-generated-event`, and retains a cached terminal candidate when no new candidate exists. `event_outcomes.py` now refuses to call a non-empty reconciled event set an event-discovery failure. `main.py` preserves access-block classification using event-discovery diagnostics. `event_discovery.py` records whether no-events was caused by an access block.

## Tests

The regression suite covers generated-state retention, sparse fresh records, date/title matching, URL normalization, signed webcast changes, protected-media retention, invalid artifact invalidation, require-new selection, access-block precedence, and the non-empty-event invariant. The full unit suite now passes **156 tests**. The focused ordinary-mode checks confirmed INCY `generated_reused=1`, `generated_available=1`, `media_attempts_used=0`, and `GENERATED_TRANSCRIPT_ALREADY_AVAILABLE`; EXEL and FUTU both returned `CACHED_EVENT_MEDIA_PROTECTED` with zero media attempts; NVDA retained `IR_VERIFIED_ACCESS_BLOCKED`.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
```

The existing Chromium fixture suite remains unchanged and should be run with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/browser -v
```

The acceptance command remains the requested generated-search command from the prior report. Its key expected outcomes are: INCY generated reuse, EXEL and AFRM cached protected outcomes, FUTU persisted protected state after its first protected result, NVDA access-block precedence, PLTR 27 events with legitimate media handling only, and zero Whisper runs unless a genuinely new candidate is available.

The reconciliation acceptance run did find a genuinely new ZM Q1 FY2027 event, so it intentionally created one transcript and stopped after that integration candidate. The existing ZM Q2 and INCY Q2 artifacts were not retranscribed; the focused checks above verify their ordinary reuse path separately.
