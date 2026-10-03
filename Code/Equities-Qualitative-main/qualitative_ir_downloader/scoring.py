"""Central heuristics, deliberately inspectable and independently testable."""
import re
from urllib.parse import urlsplit
from .models import Candidate, Link, Stock
from .urls import rejected_domain
import tldextract

DOMAIN = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

NEWS_KEYWORDS = ("news", "press", "releases", "newsroom", "media releases")
REPORT_KEYWORDS = ("featured reports", "reports", "presentations", "financial information", "financials", "financial results", "quarterly results", "investor materials", "resources", "publications")
EVENT_KEYWORDS = ("events", "event calendar", "webcasts", "presentations", "conference calls", "earnings calls", "quarterly results")
IR_SIGNALS = ("investor relations", "investors", "press releases", "financial information", "annual reports", "stock information", "quarterly results", "sec filings")
NOISE = ("privacy", "cookie", "terms", "contact", "email alert", "subscribe", "careers")

def company_tokens(name: str) -> list[str]:
    from .company_identity import identify
    name = identify(name).normalized_company_name.casefold()
    return [t for t in re.findall(r"[a-z0-9]+", name)
            if t not in {"inc", "incorporated", "corporation", "corp", "plc", "ltd", "limited", "the", "company", "holdings", "group", "co"}]

def company_key(name: str) -> str:
    return " ".join(company_tokens(name))

def score_ir(url: str, title: str, body: str, stock: Stock, corporate_endorsement: bool = False) -> Candidate:
    reasons = []
    if rejected_domain(url):
        return Candidate(url, -100, ["rejected third-party information domain"])
    score = 0
    tokens = company_tokens(stock.company_name)
    title_low, body_low = title.lower(), body.lower()
    company_present = bool(tokens) and all(t in body_low for t in tokens)
    if not company_present:
        return Candidate(url, 0, ["company identity not confirmed in content"])
    score += 25
    reasons.append("+25 company identity in content")
    if all(t in title_low for t in tokens):
        score += 20
        reasons.append("+20 company in title")
    if "investor relations" in title_low or "investors" in title_low:
        score += 20
        reasons.append("+20 investor page title")
    host = urlsplit(url).hostname or ""
    if re.search(r"(^ir\.|investor)", host) or "investor" in urlsplit(url).path:
        score += 10
        reasons.append("+10 IR URL pattern")
    brand = DOMAIN(url).domain.lower().replace("-", "")
    branded = bool(tokens) and (brand in tokens or brand == "".join(tokens))
    if branded:
        score += 15
        reasons.append("+15 company-branded hostname")
    if re.search(r"\b" + re.escape(stock.ticker.lower()) + r"\b", body_low):
        score += 5
        reasons.append("+5 ticker in content")
    signals = [s for s in IR_SIGNALS if s in body_low]
    score += min(15, len(signals) * 3)
    reasons.append(f"+{min(15, len(signals) * 3)} navigation signals: {', '.join(signals)}")
    if corporate_endorsement:
        score += 25
        reasons.append("+25 linked by company-branded corporate page")
    if not branded and not corporate_endorsement:
        score = min(score, 64)
        reasons.append("confidence capped: unverified hosting/domain relationship")
    if re.search(r"news-release-details|news/details|stock-quote|investor-faq", url, re.I):
        score = min(score, 64)
        reasons.append("confidence capped: detail page rather than IR entry page")
    if len(signals) < 2:
        score = min(score, 50)
    return Candidate(url, score, reasons)

def section_score(link: Link, category: str) -> Candidate:
    keywords = NEWS_KEYWORDS if category == "news" else EVENT_KEYWORDS if category == "events" else REPORT_KEYWORDS
    if category == "events" and re.search(r"career|community|charity|job fair|employee|training|volunteer", link.text + " " + link.url, re.I):
        return Candidate(link.url, -50, ["unrelated corporate event"])
    text = link.text.lower()
    path = urlsplit(link.url).path.lower().replace("-", " ").replace("_", " ")
    reasons = []
    score = 0
    if any(n in text + " " + path for n in NOISE):
        return Candidate(link.url, -50, ["utility link"])
    for label, value, weight in (("anchor", text, 12), ("path", path, 7), ("context", link.context.lower(), 2)):
        matches = [k for k in keywords if k in value]
        if matches:
            score += weight
            reasons.append(f"+{weight} {label}: {', '.join(matches)}")
    strong = ("press releases", "news releases", "all news") if category == "news" else ("featured reports", "view all reports", "annual reports")
    if text.strip() in strong:
        score += 15
        reasons.append("+15 explicit archive navigation")
    if "archive" in text + path:
        score += 5
        reasons.append("+5 archive")
    if category == "reports" and "sec filing" in text + path and "report" not in text + path:
        score -= 30
    return Candidate(link.url, score, reasons)
