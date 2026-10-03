"""URL identity and constrained navigation policy."""
import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, urljoin

BLOCKED_DOMAINS = ("reddit.com", "github.com", "twitter.com", "quartr.com", "alphaspread.com", "financialfilings.com", "marketchameleon.com", "fool.com", "yahoo.com", "marketwatch.com", "nasdaq.com", "tradingview.com", "marketscreener.com", "stockanalysis.com", "seekingalpha.com", "reuters.com", "bloomberg.com", "wikipedia.org", "sec.gov", "google.com", "facebook.com", "linkedin.com", "x.com")

def canonicalize_url(url: str, base: str = "") -> str:
    try:
        parts = urlsplit(urljoin(base, url))
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return ""
        host = parts.hostname.lower()
        port = parts.port
        netloc = f"[{host}]" if ":" in host else host
        if port and (parts.scheme, port) not in (("http", 80), ("https", 443)):
            netloc += f":{port}"
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid", "msclkid"}]
        return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))
    except ValueError:
        return ""

def public_url(url: str) -> bool:
    host = urlsplit(canonicalize_url(url)).hostname or ""
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host

def rejected_domain(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)

class Scope:
    """HTML stays on explicitly established hosts; linked assets may use CDNs."""
    def __init__(self, urls: list[str]):
        self.hosts = {urlsplit(u).hostname for u in urls}

    def allows(self, url: str) -> bool:
        return public_url(url) and urlsplit(url).hostname in self.hosts and not rejected_domain(url)
