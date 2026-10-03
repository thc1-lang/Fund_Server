"""Explicit local decoding configuration and reproducible hardware fingerprints."""
import hashlib
import json
import os
import platform
import subprocess
from functools import lru_cache
from importlib.metadata import version

@lru_cache(maxsize=1)
def hardware():
    logical=os.cpu_count() or 1
    physical=None
    name=platform.processor()
    if os.name=='nt':
        try:
            result=subprocess.run(['powershell','-NoProfile','-Command',
                'Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress'],
                capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            data=json.loads(result.stdout)
            if isinstance(data,dict):physical=data['NumberOfCores'];name=data['Name']
        except (OSError,ValueError,subprocess.TimeoutExpired):pass
    return dict(name=name,physical_cores=physical,logical_cores=logical)

def resolve(config):
    # Profile choices are finalized with the local benchmark report.
    profiles={'fast':('small.en',1),'balanced':('medium.en',1),'maximum_accuracy':('large-v3',5)}
    model,beam=profiles[config.transcription_profile]
    if config.language not in ('en','en-US','en-GB') and model.endswith('.en'):
        model=model[:-3]  # Keep automatic/non-English recognition available.
    cpu=hardware()
    settings=dict(profile=config.transcription_profile,model=config.model or model,
        beam_size=config.beam_size or beam,best_of=1,temperature=0.0,
        word_timestamps=False,condition_on_previous_text=False,vad_filter=True,
        vad_parameters={'min_silence_duration_ms':500},language=config.language,
        initial_prompt=None,cpu_threads=config.cpu_threads or min(cpu['physical_cores'] or cpu['logical_cores'],4),
        num_workers=1,batch_size=config.batch_size or (2 if config.transcription_profile=='balanced' else 1),hardware=cpu,
        faster_whisper_version=version('faster-whisper'),ctranslate2_version=version('ctranslate2'))
    return settings

def fingerprint(settings):
    return hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()
