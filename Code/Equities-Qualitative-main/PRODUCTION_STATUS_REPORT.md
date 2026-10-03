# Production status and incremental cache

Company acquisition state is now separated into `ir_status`, `news_status`,
`reports_status`, `events_status`, and `overall_status`.  The legacy `status`
field remains as a compatibility alias; production summaries use
`overall_status`.

`overall_status` is one of `SUCCESS`, `SUCCESS_WITH_LIMITATIONS`, `PARTIAL`,
or `FAILED`.  Protected or unavailable webcast media is retained as an event
outcome and produces `SUCCESS_WITH_LIMITATIONS` when other usable artifacts
exist.  A processing or discovery failure in one module produces `PARTIAL`
when another module produced useful material.  `FAILED` is reserved for a run
that produced no meaningful acquisition.

News and reports expose an explicit completeness state: `SUCCESS`,
`NO_DOCUMENTS_FOUND`, `CACHE_HIT_EMPTY`, `DISCOVERY_FAILED`, `ACCESS_BLOCKED`,
`NOT_APPLICABLE`, or `NOT_SCANNED`.  Each category also reports discovered,
newly saved, cached/reused, blocked, and failed counts.

Existing documents are reused only when the canonical source URL, category,
publication date (when available), local path, and stored SHA-256 (when
available) validate.  The run manifest records `artifact_cache_hits`,
`documents_skipped_existing`, `event_cache_hits`, and
`network_requests_avoided`.  Event-specific counters continue to expose media
failures, protected media, transcript reuse, and Whisper runs.

The structured `completeness` object keeps IR, news, reports, events, and
transcript availability separate and records nonfatal limitations such as
`event_media_protected` and `event_media_unavailable`.

## Validation

The bounded rerun for EXEL, ZM, QLYS, CARG, INCY, DAVE, PLTR, and AFRM exited
successfully.  It produced zero Whisper retranscriptions and zero newly saved
documents.  EXEL retained `CACHED_EVENT_MEDIA_PROTECTED`; ZM and INCY reused
generated transcripts; QLYS and CARG reused official transcript artifacts;
DAVE retained `EVENT_ALREADY_COMPLETE`; PLTR retained `MEDIA_NOT_FOUND` with
`SUCCESS_WITH_LIMITATIONS`; and AFRM retained its cached protected event.

The subsequent normal rerun processed 19 unique companies in about 1,343
seconds (the prior population run took about 9,923 seconds).  It discovered
1,484 news items and 148 reports, saved 2 new news artifacts, reused 1,482
news artifacts and 128 reports, ran Whisper 0 times, used 1 media attempt,
recorded 1,620 artifact cache hits, skipped 1,610 existing documents, and
avoided 1,635 network requests.  A final PLTR pass after the explicit event
failure classification confirmed `MEDIA_NOT_FOUND` and
`SUCCESS_WITH_LIMITATIONS`.

The full run exited with code 2 because seven companies had no usable
acquisition and three had genuine partial discovery failures.  Those results
remain visible in their module statuses; access blocking and event-media
limitations are not converted into hidden global successes.
