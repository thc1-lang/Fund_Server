# Generated-transcript integration validation

The generated-only mode is separate from normal collection. It tests one historical
event per company, skips official transcripts/captions, and stops only after a new
generated transcript. Existing completed artifacts are reused without transcription.
The default company limit is 20; duplicate ticker/company identities are deduplicated.

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --find-generatable-event --max-companies 20
```

For a search without any registration profile submission:

```powershell
.\.venv\Scripts\python.exe -m qualitative_ir_downloader.main --find-generatable-event --max-companies 20 --skip-event-registration
```

Verification: 68 offline/unit tests and 19 browser fixture tests passed (87 total).
The fixture media checks run real FFmpeg against direct audio, unencrypted HLS and
DASH; they do not establish a live Whisper success. New tests cover keeping cached
roots after a failed probe in both event modes, generated-only stopping, company
limits, and skipping official transcript/caption sources without artifact creation.

The EXEL regression was an empty provider root list following a failed root probe.
Verified roots are now registered before probing. The September 8 live search
retained EXEL identity through HTTP 403, performed no fresh EXEL search, and found
13 events through public official resources.

Generated transcripts require configurable minimum segment/character counts,
valid increasing timestamps, saved JSON with segments, and a valid PDF with
selectable transcript text. Logs record FFmpeg version/result, normalized format,
Whisper model/device/compute type/language, and the first/last three timestamp ranges.
Neither encrypted media nor access-control barriers are bypassed.

Provider observations are persisted in `webcast_provider_capabilities.json`. Generated-only
search uses repeated protected/blocked outcomes to lower a provider's priority while
leaving normal production ordering unchanged. `--max-media-attempts` bounds per-event
candidate attempts.

Live outcome: in progress. Automatic approval review rejected the registration-enabled
search because of profile submission to additional providers. The safer command
above was approved and started with registration disabled. No live generated
transcript success is claimed until its artifacts have been inspected.
