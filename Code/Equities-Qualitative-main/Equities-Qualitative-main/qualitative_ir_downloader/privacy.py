"""Redact ephemeral playback credentials at persistence/logging boundaries."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
SECRET = re.compile(r"token|signature|credential|authorization|password|secret|key-pair|policy|jwt|auth|expires|x-amz-|x-goog-|hdnts|hdnea|email|first.?name|last.?name|company|occupation|given|family", re.I)

def redact_url(url):
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port: host += ":" + str(p.port)
        if p.query and (host=='ssrweb.zoom.us' or host.endswith('.zoom.us') and any(SECRET.search(k) for k,v in parse_qsl(p.query))):
            return urlunsplit((p.scheme,host,p.path,'[signed-query-redacted]',''))
        query = [(k, "REDACTED" if SECRET.search(k) else v) for k,v in parse_qsl(p.query, keep_blank_values=True)]
        return urlunsplit((p.scheme,host,p.path,urlencode(query),p.fragment if p.fragment.startswith("event-") else ""))
    except ValueError:
        return "[invalid URL]"

def safe_text(text):
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: redact_url(m.group()), str(text))
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[configured email]", text)

def safe_record(value):
    if isinstance(value, dict): return {k:safe_record(v) for k,v in value.items()}
    if isinstance(value, list): return [safe_record(v) for v in value]
    if isinstance(value, str): return safe_text(value)
    return value
