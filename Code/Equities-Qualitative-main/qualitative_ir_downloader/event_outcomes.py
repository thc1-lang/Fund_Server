"""Semantic event outcomes and a single company status precedence policy."""
PROTECTED = {"DRM_PROTECTED", "HLS_ENCRYPTED", "EVENT_MEDIA_PROTECTED"}
GATES = {"REGISTRATION_APPROVAL_REQUIRED", "REGISTRATION_REQUIRED", "AUTH_REQUIRED", "CAPTCHA_REQUIRED", "EMAIL_VERIFICATION_REQUIRED"}
SKIPS = {"SKIPPED_OFFICIAL_TRANSCRIPT", "SKIPPED_OFFICIAL_CAPTIONS", "SKIPPED_COMPLETED_GENERATED", "MEDIA_ATTEMPT_LIMIT", "CANDIDATE_SCANNED"}
NO_REPLAY = {"NO_WEBCAST", "NO_REPLAY_AVAILABLE", "FUTURE_EVENT_NO_REPLAY"}

def outcome(event):
    if event.status == "CACHED_EVENT_MEDIA_PROTECTED":return event.status
    if event.resumed and event.method=="generated_transcript":return "GENERATED_TRANSCRIPT_ALREADY_AVAILABLE"
    if event.status == "TRANSCRIBED":
        if event.transcript_qa.get('status')=='TRANSCRIPT_QA_WARNING':return 'TRANSCRIPT_QA_WARNING'
        if event.resumed:return 'EVENT_ALREADY_COMPLETE'
        return {"generated_transcript":"GENERATED_TRANSCRIPT_SUCCESS", "official_transcript":"OFFICIAL_TRANSCRIPT_USED", "official_captions":"OFFICIAL_CAPTIONS_USED"}.get(event.method, "TRANSCRIPT_READY")
    if event.protected_media_type or event.status in PROTECTED:
        return "EVENT_MEDIA_PROTECTED"
    return event.status

def company_outcome(events, fallback):
    outcomes = [outcome(e) for e in events]
    priority = ["GENERATED_TRANSCRIPT_ALREADY_AVAILABLE", "CACHED_EVENT_MEDIA_PROTECTED", "GENERATED_TRANSCRIPT_SUCCESS", "OFFICIAL_TRANSCRIPT_USED", "OFFICIAL_CAPTIONS_USED", "TRANSCRIPT_QA_WARNING", "EVENT_ALREADY_COMPLETE",
                "SKIPPED_OFFICIAL_TRANSCRIPT", "SKIPPED_OFFICIAL_CAPTIONS", "EVENT_MEDIA_PROTECTED",
                "IR_VERIFIED_ACCESS_BLOCKED", "MEDIA_NOT_FOUND",
                "REGISTRATION_APPROVAL_REQUIRED", "EMAIL_VERIFICATION_REQUIRED", "AUTH_REQUIRED", "CAPTCHA_REQUIRED",
                "REGISTRATION_FAILED", "REGISTRATION_REQUIRED", "PLAYER_HTTP_403", "PLAYER_BLOCKED", "EVENT_DISCOVERY_FAILED"]
    for status in priority:
        if status in outcomes: return status
    return next((s for s in outcomes if s not in {"DISCOVERED", "SITE_BLOCKED"}), fallback)

def metrics(events, selected, generated_only=False):
    outcomes = [outcome(e) for e in selected]
    expected = {"GENERATED_TRANSCRIPT_ALREADY_AVAILABLE","CACHED_EVENT_MEDIA_PROTECTED","CANDIDATE_CACHED"} | SKIPS | GATES | NO_REPLAY | {"EVENT_MEDIA_PROTECTED", "REGISTRATION_DESTINATION_DISCOVERED", "REGISTRATION_NOT_REQUIRED"}
    # Explicit found/saved counters are the public interface. Legacy plural
    # counters retain artifact semantics for integrations that use them to stop.
    transcript_found = sum(e.official_transcript_discovered or bool(e.official_transcript_url) or e.status == "SKIPPED_OFFICIAL_TRANSCRIPT" or e.method == "official_transcript" for e in selected)
    captions_found = sum(e.official_captions_discovered or e.status == "SKIPPED_OFFICIAL_CAPTIONS" or e.method == "official_captions" for e in selected)
    transcript_saved = sum(e.official_transcript_saved or outcome(e) == "OFFICIAL_TRANSCRIPT_USED" for e in selected)
    captions_saved = sum(e.official_captions_saved or outcome(e) == "OFFICIAL_CAPTIONS_USED" for e in selected)
    transcript_reused = sum(e.resumed and e.method == "official_transcript" for e in selected)
    captions_reused = sum(e.resumed and e.method == "official_captions" for e in selected)
    generated_created = sum(e.generated_transcript_created and not e.resumed for e in selected)
    return {"events_discovered":len(events), "events_with_webcasts":sum(bool(e.webcast_url) for e in events),
            "events_processed":len(selected), "events_resumed":sum(e.resumed for e in selected),
            "official_transcript_discovered":transcript_found, "official_transcript_saved":transcript_saved,
            "official_captions_discovered":captions_found, "official_captions_saved":captions_saved,
            "official_transcript_reused":transcript_reused,
            "official_transcript_saved_new":max(0, transcript_saved - transcript_reused),
            "official_captions_reused":captions_reused,
            "official_captions_saved_new":max(0, captions_saved - captions_reused),
            "generated_transcript_created":generated_created,
            "generated_transcript_created_this_run":generated_created,
            "generated_transcript_reused":sum(e.resumed and e.method=='generated_transcript' for e in selected),
            "generated_transcript_available":sum(e.method=='generated_transcript' and (e.generated_transcript_created or e.resumed) for e in selected),
            "media_attempts_used":sum(e.media_attempted for e in selected),
            "media_attempts_skipped_cached":sum(e.resumed for e in selected),
            "whisper_runs":sum(e.whisper_runs for e in selected),
            "media_downloads":sum(e.media_downloads for e in selected),
            "official_transcripts":transcript_saved, "official_captions":captions_saved,
            "generated_transcripts":sum(outcome(e)=="GENERATED_TRANSCRIPT_SUCCESS" and (not generated_only or not e.resumed) for e in selected),
            "integration_skipped_official":sum(s in {"SKIPPED_OFFICIAL_TRANSCRIPT","SKIPPED_OFFICIAL_CAPTIONS"} for s in outcomes),
            "events_no_replay":sum(e.status in NO_REPLAY for e in events),
            "registration_blocked":sum(s in GATES for s in outcomes),
            "registration_approval_required":outcomes.count("REGISTRATION_APPROVAL_REQUIRED"),
            "media_blocked":outcomes.count("EVENT_MEDIA_PROTECTED")+outcomes.count("CACHED_EVENT_MEDIA_PROTECTED"),
            "media_not_found":outcomes.count("MEDIA_NOT_FOUND"),
            "media_failures":outcomes.count("MEDIA_NOT_FOUND") + outcomes.count("PLAYER_HTTP_403") + outcomes.count("PLAYER_BLOCKED"),
            "transcription_failures":outcomes.count("TRANSCRIPTION_FAILED"),
            "event_failures":sum(s not in expected and e.status!="TRANSCRIBED" for e,s in zip(selected,outcomes)),
            # Discovery failure is reserved for an empty reconciled event set.
            "event_outcome":company_outcome(selected, "EVENTS_FOUND" if events else "EVENT_DISCOVERY_FAILED")}
