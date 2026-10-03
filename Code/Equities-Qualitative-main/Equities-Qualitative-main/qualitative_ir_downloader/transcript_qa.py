"""Conservative speech plausibility warnings; never rewrite recognized facts."""
from collections import Counter
from difflib import SequenceMatcher
import re
import statistics

def assess(segments, duration):
    minutes=max(float(duration or 0)/60,1/60)
    texts=[' '.join(re.findall(r"\w+",s['text'].casefold())) for s in segments]
    words=[w for text in texts for w in text.split()]
    counts=Counter(texts)
    lengths=[max(0,s['end']-s['start']) for s in segments]
    grams=Counter(tuple(words[i:i+5]) for i in range(max(0,len(words)-4)))
    overlaps=sum(b['start']<a['end']-.001 for a,b in zip(segments,segments[1:]))
    backwards=sum(b['start']<a['start'] for a,b in zip(segments,segments[1:]))
    chars=sum(len(s['text']) for s in segments)
    local_repetition=[i for i,text in enumerate(texts) if re.search(r'\b(\w+)(?:\s+\1\b){9,}',text)]
    result=dict(duration_minutes=minutes,segment_count=len(segments),segments_per_minute=len(segments)/minutes,
        word_count=len(words),words_per_minute=len(words)/minutes,character_count=chars,characters_per_minute=chars/minutes,
        average_segment_duration=statistics.mean(lengths) if lengths else 0,median_segment_duration=statistics.median(lengths) if lengths else 0,
        duplicate_text_ratio=sum(n-1 for n in counts.values())/max(1,len(texts)),
        repeated_ngram_ratio=sum(n-1 for n in grams.values())/max(1,sum(grams.values())),
        consecutive_duplicate_ratio=sum(a==b for a,b in zip(texts,texts[1:]))/max(1,len(texts)-1),
        near_duplicate_adjacent_count=sum(a!=b and SequenceMatcher(None,a,b,autojunk=False).ratio()>.9 for a,b in zip(texts,texts[1:])),
        overlapping_timestamp_count=overlaps,non_monotonic_timestamp_count=backwards,
        first_timestamp=segments[0]['start'] if segments else None,last_timestamp=max((s['end'] for s in segments),default=0),
        local_repetition_segment_indices=local_repetition)
    warnings=[]
    if local_repetition: warnings.append('local_word_repetition_review_required')
    # Warnings, not speech edits or hard rejection. Rates above these generous
    # ceilings merit review; repetition requires substantial evidence.
    if len(words)>=100:
        if result['words_per_minute']>300: warnings.append('speech_rate_above_300_wpm')
        if result['characters_per_minute']>2000: warnings.append('character_rate_above_2000_per_minute')
        if result['duplicate_text_ratio']>.30: warnings.append('repeated_segment_text')
        if result['repeated_ngram_ratio']>.60: warnings.append('repeated_phrase_loop')
    if len(segments)>=300 and result['segments_per_minute']>45 and result['median_segment_duration']<=1.2:
        warnings.append('dense_short_segments')
    if overlaps: warnings.append('overlapping_timestamps')
    if backwards: warnings.append('non_monotonic_timestamps')
    if duration and result['last_timestamp']>duration+2: warnings.append('timestamps_beyond_audio')
    result.update(status='TRANSCRIPT_QA_WARNING' if warnings else 'PASS',warnings=warnings)
    return result

def performance(runtime, duration):
    return {'transcription_runtime_seconds':runtime,'audio_duration_seconds':duration,
            'rtf':runtime/duration if duration else None,'speed_x':duration/runtime if runtime else None}
