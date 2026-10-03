"""Faithful local faster-whisper segments; no rewriting or guessed speakers."""
import logging
import os
import time
from dataclasses import asdict
from .event_models import TranscriptSegment
from .filesystem import atomic_json
from .models import CollectionError
log=logging.getLogger(__name__)

def validate_segments(segments, config):
    import math
    if not isinstance(segments,list) or len(segments)<config.min_transcript_segments:
        raise CollectionError("TRANSCRIPT_INVALID","Insufficient transcript segments")
    previous=-1.0
    for segment in segments:
        try:
            start,end=segment["start"],segment["end"]
            if not isinstance(segment["text"],str) or not segment["text"].strip(): raise ValueError()
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (start,end)): raise ValueError()
            if start<previous or start<0 or end<start or end>config.max_duration: raise ValueError()
            previous=start
        except (KeyError,TypeError,ValueError):
            raise CollectionError("TRANSCRIPT_INVALID","Invalid or non-monotonic saved timestamps") from None
    if sum(len(s["text"].strip()) for s in segments)<config.min_transcript_characters:
        raise CollectionError("TRANSCRIPT_INVALID","Insufficient transcript text")

def validate_artifacts(pdf, json_path, config):
    import json
    from pypdf import PdfReader
    from .pdf_utils import validate_pdf
    record=json.loads(json_path.read_text(encoding="utf-8"))
    validate_segments(record.get("segments"),config)
    validate_pdf(pdf.read_bytes())
    text=" ".join(p.extract_text() or "" for p in PdfReader(pdf).pages)
    # Check recognized words occur in sequence, not just PDF character count.
    import re
    expected=re.findall(r"\w+", " ".join(s["text"] for s in record["segments"]).casefold())
    actual=iter(re.findall(r"\w+",text.casefold()))
    matched=0
    for word in expected:
        if any(token==word for token in actual): matched+=1
        else: break
    if matched < len(expected)*0.98:
        raise CollectionError("PDF_FAILED","PDF does not contain the saved transcript text")
    log.info("Transcript artifacts validated: method=%s; %s segments; %s transcript characters",record.get("source",{}).get("method"),len(record["segments"]),sum(len(s["text"]) for s in record["segments"]))

def choose_device(config):
    import ctranslate2
    if config.device=="cpu":return "cpu","int8"
    try:
        if ctranslate2.get_cuda_device_count()>0:
            supported=ctranslate2.get_supported_compute_types("cuda")
            return "cuda","int8_float16" if "int8_float16" in supported else "float16"
    except (RuntimeError,ValueError):pass
    if config.device=="cuda":log.warning("CUDA unavailable; using CPU int8")
    return "cpu","int8"

def transcribe(audio,config,event,checkpoint,cancel=None):
    from faster_whisper import WhisperModel
    from .transcription_profiles import resolve, fingerprint
    from .transcript_qa import assess, performance
    settings=resolve(config)
    model_name=settings['model']
    model_location=model_name
    from faster_whisper.utils import _MODELS
    if model_name in _MODELS:
        from huggingface_hub import try_to_load_from_cache
        cached=try_to_load_from_cache(_MODELS[model_name],'model.bin',cache_dir=str(config.model_cache))
        if isinstance(cached,str):model_location=str(__import__('pathlib').Path(cached).parent)
    device,compute=choose_device(config)
    started=time.perf_counter()
    log.info("Transcription configuration: %s; device=%s compute=%s",settings,device,compute)
    try:
        model=WhisperModel(model_location,device=device,compute_type=compute,cpu_threads=settings['cpu_threads'],num_workers=1,download_root=str(config.model_cache))
    except (RuntimeError,ValueError) as exc:
        if device!="cuda":raise CollectionError("TRANSCRIPTION_FAILED",f"Whisper initialization failed: {type(exc).__name__}") from exc
        log.warning("CUDA initialization failed; falling back to CPU int8")
        device="cpu";compute="int8";model=WhisperModel(model_location,device=device,compute_type=compute,cpu_threads=settings['cpu_threads'],num_workers=1,download_root=str(config.model_cache))
    event.timings['model_load']=time.perf_counter()-started
    settings.update(device=device,compute_type=compute)
    event.transcription_config={**settings,'fingerprint':fingerprint(settings)}
    history_path=config.model_cache/'performance_history.json'
    history={}
    try:
        import json
        history=json.loads(history_path.read_text(encoding='utf-8'))
        prior=history.get(event.transcription_config['fingerprint'])
        if prior and event.duration_seconds:
            log.info('Estimated transcription: profile=%s historical RTF=%.3f audio=%.1fm estimated compute=%.1fm (estimate only)',settings['profile'],prior['rtf'],event.duration_seconds/60,prior['rtf']*event.duration_seconds/60)
    except (OSError,ValueError):pass
    event.device=device;event.transcription_engine="faster-whisper";event.transcription_model=model_name
    kwargs={k:settings[k] for k in ('language','beam_size','best_of','temperature','vad_filter','vad_parameters','condition_on_previous_text','word_timestamps','initial_prompt')}
    engine=model
    if settings['batch_size']>1:
        from faster_whisper import BatchedInferencePipeline
        engine=BatchedInferencePipeline(model=model)
        kwargs['batch_size']=settings['batch_size']
    started=time.perf_counter()
    segments,info=engine.transcribe(str(audio),**kwargs)
    event.language=info.language
    log.info("Detected language: %s; audio duration: %.1f seconds",info.language,info.duration)
    result=[];last_percent=-10;last_log=time.monotonic()
    def save(complete=False):
        atomic_json(checkpoint,{'complete':complete,'segments':result,'model':model_name,'language':info.language,
            'configuration':event.transcription_config,'last_timestamp':result[-1]['end'] if result else 0,
            'resume_supported':False,'interrupted':bool(cancel and cancel.is_set())})
    try:
        for segment in segments:
            if cancel and cancel.is_set():raise InterruptedError('Transcription cancelled; diagnostic checkpoint retained')
            if not segment.text.strip():continue
            if segment.start<0 or segment.end<segment.start:raise CollectionError("TRANSCRIPTION_FAILED","Invalid transcript timestamps")
            result.append(asdict(TranscriptSegment(round(segment.start,3),round(segment.end,3),segment.text.strip())))
            percent=min(100,int(100*segment.end/max(event.duration_seconds or info.duration,1)))
            if percent>=last_percent+10 or time.monotonic()-last_log>45:
                log.info("Transcript progress: %s%% (%s segments)",percent,len(result));last_percent=percent;last_log=time.monotonic()
                save()
        if cancel and cancel.is_set():raise InterruptedError('Transcription cancelled')
    finally:
        save()
        event.timings['transcription']=time.perf_counter()-started
    event.transcript_qa=assess(result,event.duration_seconds or info.duration)
    if len(result)<config.min_transcript_segments or sum(len(s["text"]) for s in result)<config.min_transcript_characters:raise CollectionError("TRANSCRIPTION_FAILED","Transcript contains insufficient speech")
    if any(b["start"]<a["start"] for a,b in zip(result,result[1:])):raise CollectionError("TRANSCRIPTION_FAILED","Non-monotonic transcript timestamps")
    log.info("Transcript boundary validation: first three %s; last three %s",[(s["start"],s["end"],len(s["text"])) for s in result[:3]],[(s["start"],s["end"],len(s["text"])) for s in result[-3:]])
    validate_segments(result,config)
    event.transcript_qa=assess(result,event.duration_seconds or info.duration)
    event.performance=performance(event.timings['transcription'],event.duration_seconds or info.duration)
    if event.transcript_qa['status']=='PASS':
        history[event.transcription_config['fingerprint']]={**event.performance,'qa_status':'PASS'}
        try:atomic_json(history_path,history)
        except OSError:log.info('Performance history unavailable; transcript artifacts remain valid')
    log.info('Transcript QA: %s',event.transcript_qa)
    log.info('Transcription performance: %s',event.performance)
    save(True)
    log.info("Transcript complete: %s segments; %s characters; language=%s; duration=%.1fs",len(result),sum(len(s["text"]) for s in result),info.language,info.duration)
    return result
