"""Document metadata and archive-item classification."""
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from dateutil import parser
from .models import Link
from .scoring import NOISE

DATE_PATTERN = re.compile(r"\b(?:20\d{2}|19\d{2})[-/]\d{1,2}[-/]\d{1,2}(?=\b|T)|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},?\s+\d{4}\b|\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{4}\b", re.I)

def extract_date(text: str) -> str | None:
    match = DATE_PATTERN.search(text)
    if match:
        try:
            return parser.parse(match.group()).date().isoformat()
        except (ValueError, OverflowError):
            return None
    return None

def is_pdf(link: Link) -> bool:
    return bool("/static-files/" in link.url or re.search(r"\.pdf(?:$|[?#])", link.url, re.I) or re.search(r"\bpdf\b", link.text, re.I))

def release_link(link: Link) -> bool:
    text = link.text.lower()
    path = urlsplit(link.url).path.lower()
    if any(n in text + " " + path for n in NOISE):
        return False
    detail = bool(re.search(r"news[-/]details|news-release-details|press-release-details|/releases?/\d|/news/\d{4}/|/news/[^/]{20,}|/press-releases/[^/]{15,}", path))
    if not detail and re.search(r"/(?:events?|presentations?|webcasts?)(?:/|$)", path):
        return False
    if re.fullmatch(r"(?:view all|all news|news archive|press release archive|news releases|press releases|archive)", text.strip()):
        return False
    detail = detail or bool(re.search(r"/(?:news|news-releases|press-releases|press)/[^/]{6,}", path) and len(text) >= 18 and "archive" not in path)
    dated = extract_date(link.context) is not None and len(text) > 25
    return detail or dated or (is_pdf(link) and len(text) > 12)

def metadata(html: str) -> tuple[str, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".field--name-field-nir-news-title, [itemprop=headline]") or soup.select_one("article h1, main h1, h1")
    title = heading.get_text(" ", strip=True) if heading else (soup.title.get_text(" ", strip=True) if soup.title else "Untitled release")
    for selector, attr in (('meta[property="article:published_time"]', "content"), ('meta[name="date"]', "content"), ("time[datetime]", "datetime")):
        element = soup.select_one(selector)
        if element:
            date = extract_date(str(element.get(attr, "")))
            if date:
                return title, date
    content = soup.select_one("article, main, [role=main]") or soup
    return title, extract_date(content.get_text(" ", strip=True)[:6000])
