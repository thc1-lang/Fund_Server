"""PDF integrity validation, hashing, and complete-page Chromium printing."""
import hashlib
import io
import logging
from pypdf import PdfReader
from .models import CollectionError

log = logging.getLogger(__name__)

def validate_pdf(data: bytes) -> str:
    if not data.lstrip().startswith(b"%PDF-"):
        raise CollectionError("PDF_DOWNLOAD_FAILED", "Response is not a PDF (missing PDF signature)")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Investor-relations archives sometimes mark PDFs with an owner
            # password while leaving the user password empty.  They are still
            # public documents and pypdf can read them after an empty-password
            # decrypt.  Password-protected documents that reject this remain
            # invalid and are reported as such.
            try:
                decrypted = reader.decrypt("")
            except Exception:
                decrypted = 0
            if not decrypted:
                raise ValueError("Encrypted PDF requires a password")
        if not len(reader.pages):
            raise ValueError("Empty PDF")
    except Exception as exc:
        raise CollectionError("PDF_DOWNLOAD_FAILED", f"Invalid PDF structure: {exc}") from exc
    return hashlib.sha256(data).hexdigest()

async def render_release(page, browser) -> bytes:
    # Trigger bounded lazy loading, then restore the top before printing.
    height = await page.evaluate("() => document.body.scrollHeight")
    for y in range(0, min(height, 100000), 900):
        await page.evaluate("(y) => window.scrollTo(0,y)", y)
        await page.wait_for_timeout(70)
    if height > 100000:
        raise CollectionError("SAFETY_LIMIT_REACHED", "Release exceeds lazy-scroll height limit")
    await browser.settle(page)
    await page.evaluate("() => window.scrollTo(0,0)")
    await page.emulate_media(media="screen")
    # Preserve the full document tree: no guessed article extraction that could truncate disclosures.
    await page.add_style_tag(content="""
        @page { size: A4; margin: 16mm; }
        @media print {
          html, body { height:auto !important; overflow:visible !important; }
          article, main, [role=main] { height:auto !important; max-height:none !important; overflow:visible !important; }
          img, table { max-width:100% !important; }
          p { orphans:3; widows:3; }
        }
        nav, [role=navigation], button, [role=dialog], .cookie-banner { display:none !important; }
        html, body, article, main, [role=main] { overflow:visible !important; height:auto !important; max-height:none !important; }
        * { -webkit-print-color-adjust:exact; }
    """)
    data = await page.pdf(format="A4", print_background=True, margin={"top":"16mm","bottom":"16mm","left":"14mm","right":"14mm"}, prefer_css_page_size=True)
    validate_pdf(data)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    if len(text.strip()) < 80:
        raise CollectionError("HTML_TO_PDF_FAILED", "Rendered PDF lacks extractable release text")
    return data
