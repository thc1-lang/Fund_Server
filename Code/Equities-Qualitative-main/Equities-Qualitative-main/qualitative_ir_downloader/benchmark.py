"""Bounded local-audio benchmark; no Sheets, browser, or webcast acquisition."""
import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path
from .event_models import Event
from .filesystem import atomic_json
from .transcription import transcribe
from .webcast_media import run_ffmpeg, media_info

async def benchmark(config,audio,seconds,output,models=None):
    from faster_whisper.utils import available_models
    if not Path(audio).is_file():raise ValueError('Benchmark requires an existing local audio file')
    if seconds<=0 or seconds>300:raise ValueError('Benchmark duration must be 1..300 seconds')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    clip=output/'benchmark.wav'
    await run_ffmpeg(config,['-i',str(Path(audio).resolve()),'-t',str(seconds),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(clip)])
    duration=media_info(clip)['duration']
    if duration>seconds+.1:raise ValueError('Benchmark clip exceeds duration budget')
    rows=[]
    for name in models or ['small.en','medium.en','distil-large-v3','large-v3']:
        if name not in available_models() and not Path(name).is_dir():raise ValueError('Unsupported installed model: '+name)
        label=Path(name).name
        trial=replace(config,model=name,device='cpu',min_transcript_segments=1,min_transcript_characters=1)
        event=Event('Local benchmark',None,'local benchmark',duration_seconds=duration)
        started=time.perf_counter()
        segments=await asyncio.to_thread(transcribe,clip,trial,event,output/(label+'.checkpoint.json'))
        row={'model':name,'configuration':event.transcription_config,'timings':event.timings,
             'performance':event.performance,'qa':event.transcript_qa,'total_seconds':time.perf_counter()-started,'segments':segments}
        row['process_peak_memory_mib']=peak_memory_mib()
        atomic_json(output/(label+'.json'),row);rows.append(row)
        atomic_json(output/'report.json',rows)
        logging.getLogger(__name__).info('BENCHMARK %s RTF=%.3f speed=%.2fx runtime=%.1fs QA=%s',name,event.performance['rtf'],event.performance['speed_x'],event.timings['transcription'],event.transcript_qa['status'])
    return rows

def peak_memory_mib():
    """Process lifetime high-water mark, not an isolated per-model allocation."""
    import os
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in ('peak','working','paged_peak','paged','nonpaged_peak','nonpaged','pagefile','pagefile_peak')]
        counters=Counters();counters.cb=ctypes.sizeof(counters)
        api=ctypes.windll.psapi.GetProcessMemoryInfo
        api.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        if api(wintypes.HANDLE(-1),ctypes.byref(counters),counters.cb):return counters.peak/1024**2
    return None
