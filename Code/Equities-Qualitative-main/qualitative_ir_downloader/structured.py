"""Shared first-party HTML/XML parsing and document identity."""
import re
from datetime import datetime
from urllib.parse import urlsplit, urljoin, urlencode, parse_qsl
from bs4 import BeautifulSoup
from .models import Document, Link
from .extraction import extract_date, is_pdf, release_link
from .urls import canonicalize_url
from .scoring import company_tokens

def document_keys(d):
    keys = {("url", canonicalize_url(d.source_url))}
    if d.guid:
        keys.add(("guid", d.guid))
    if d.title and d.date:
        keys.add(("title_date", re.sub(r"\W+", "", d.title.casefold()), d.date))
    return keys

def merge_documents(documents):
    result, identities = [], {}
    for document in documents:
        found = next((identities[k] for k in document_keys(document) if k in identities), None)
        if found is None:
            found = document
            result.append(found)
        else:
            for field in ("feed_url", "guid", "description", "content_html", "pdf_source_url", "wire_source_url", "wire_provenance_url", "source_family", "discovered_from", "artifact_url"):
                if not getattr(found, field):
                    setattr(found, field, getattr(document, field))
            found.content_complete = found.content_complete or document.content_complete
            found.issuer_verified = found.issuer_verified or document.issuer_verified
            found.retrieved_at = found.retrieved_at or document.retrieved_at
            found.provenance.extend(p for p in document.provenance if p not in found.provenance)
            if document.pdf_source_url and document.pdf_source_url != found.pdf_source_url and document.pdf_source_url not in found.alternate_pdf_urls:
                found.alternate_pdf_urls.append(document.pdf_source_url)
        for key in document_keys(document):
            identities[key] = found
    return result

def soup_links(soup, base):
    for a in soup.select("a[href]"):
        url = canonicalize_url(a.get("href", ""), base)
        if not url:
            continue
        parent = a.find_parent(["article", "li", "tr"]) or a.parent
        yield Link(url, a.get_text(" ", strip=True), parent.get_text(" ", strip=True)[:1500])

def page_matches(soup, company, category):
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    content = soup.get_text(" ", strip=True)
    tokens = company_tokens(company)
    topic = r"press|news|release" if category == "news" else r"report|financial|sustainability|resources"
    return bool(tokens) and all(t in (title + " " + content).lower() for t in tokens) and bool(re.search(topic, title, re.I))

def report_documents(soup, url):
    docs = []
    for link in soup_links(soup, url):
        if is_pdf(link) or "/static-files/" in link.url:
            if not re.search(r"report|sustainability|presentation", link.text + " " + link.context, re.I):
                continue
            date = extract_date(link.context)
            year = re.search(r"\b(?:19|20)\d{2}\b", link.text)
            size = re.search(r"\b\d+(?:\.\d+)?\s*(?:KB|MB|GB)\b", link.context, re.I)
            docs.append(Document(link.text, date or (year.group() if year else None), link.url, "reports", link.url, linked_from_url=url, official_ir_url=url, file_size=size.group() if size else None))
    return merge_documents(docs)

def archive_next_urls(soup, url, category, scope):
    targets = []
    for link in soup_links(soup, url):
        if not scope.allows(link.url) or release_link(link) or is_pdf(link) or "/static-files/" in link.url:
            continue
        if re.fullmatch(r"next(?: page)?|older|[>\u00bb\u203a]+|\d{1,4}|(?:page )\d+", link.text, re.I) or re.search(r"[?&]page=", link.url):
            targets.append(link.url)
    for select in soup.select("select[name]"):
        form = select.find_parent("form")
        if not form or form.get("method", "get").lower() != "get":
            continue
        action = canonicalize_url(form.get("action") or url, url)
        if not scope.allows(action):
            continue
        hidden = [(i.get("name"), i.get("value", "")) for i in form.select('input[type="hidden"][name]')]
        for option in select.select("option"):
            if re.fullmatch(r"(19|20)\d{2}", option.get_text(strip=True)):
                parsed = urlsplit(action)
                # Current form controls replace matching action-query values.
                # Retaining both duplicates hidden widget/form fields on every year visit.
                control_names = {select["name"], *(k for k, _ in hidden)}
                pairs = [(k,v) for k,v in parse_qsl(parsed.query) if k not in control_names]
                query = urlencode(pairs + hidden + [(select["name"], option.get("value", option.get_text()))])
                targets.append(canonicalize_url(parsed._replace(query=query).geturl()))
    return list(dict.fromkeys(targets))

WIRE_HOSTS = ("businesswire.com", "globenewswire.com", "prnewswire.com")

def wire_link(soup, official_url):
    for a in soup.select("a[href]"):
        url = canonicalize_url(a["href"], official_url)
        host = urlsplit(url).hostname or ""
        # Tracking links and navigation are not original wire releases.
        if any(host == d or host == "www." + d for d in WIRE_HOSTS) and re.search(r"/news/home/|/news-release/|/news-releases/", url):
            return url
    return None
