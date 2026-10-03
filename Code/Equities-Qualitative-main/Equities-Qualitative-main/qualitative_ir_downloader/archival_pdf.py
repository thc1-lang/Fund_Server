"""Deterministic, selectable-text copies of verified first-party content."""
import html
from bs4 import BeautifulSoup
from .pdf_utils import validate_pdf
from .models import CollectionError

def clean_body(body):
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select("script, style, iframe, object, embed, form, img, link, meta"):
        node.decompose()
    for node in soup.find_all(True):
        if node.name not in {"p","div","span","br","strong","b","em","i","u","ul","ol","li","table","thead","tbody","tr","td","th","h2","h3","h4","blockquote","a"}:
            node.unwrap()
            continue
        node.attrs = {}
    return str(soup)

async def render_archival(context, document, ticker, company, body):
    if len(BeautifulSoup(body, "html.parser").get_text(" ", strip=True)) < 150:
        raise CollectionError("HTML_TO_PDF_FAILED", "Archival body too short")
    esc = html.escape
    source = document.official_ir_url or document.source_url
    footer = f"Original official IR URL: {esc(source)}"
    if document.feed_url:
        footer += f"<br>Retrieved via official IR RSS feed: {esc(document.feed_url)}"
    if document.wire_source_url and document.method == "verified_wire_source_html_to_pdf":
        footer += f"<br>Explicitly linked wire source: {esc(document.wire_source_url)}"
    markup = f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @page {{ size:A4; margin:18mm; }} body {{font:11pt Arial,sans-serif;line-height:1.5;color:#172333}}
    h1 {{font-size:20pt;line-height:1.2;margin-bottom:14pt}} h2 {{font-size:14pt}}
    .company {{font-size:12pt;color:#39516a}} .meta {{font-size:10pt;color:#596777}}
    footer {{margin-top:24pt;border-top:1px solid #abb7c4;padding-top:12pt;font-size:8pt;overflow-wrap:anywhere}}
    table {{border-collapse:collapse;width:100%;font-size:9pt}} td,th {{padding:4pt;vertical-align:top}}
    p {{orphans:3;widows:3}} a {{overflow-wrap:anywhere}}
    </style></head><body><div class="company">{esc(ticker)} - {esc(company)}</div>
    <p class="meta">Press Release | {esc(document.date or "Date not provided")}</p>
    <h1>{esc(document.title)}</h1><main>{clean_body(body)}</main>
    <footer>Source: {esc(company)} Investor Relations<br>{footer}</footer></body></html>"""
    page = await context.new_page()
    try:
        await page.route("**/*", lambda route: route.abort())
        await page.set_content(markup, wait_until="domcontentloaded")
        data = await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        validate_pdf(data)
        return data
    finally:
        await page.close()
