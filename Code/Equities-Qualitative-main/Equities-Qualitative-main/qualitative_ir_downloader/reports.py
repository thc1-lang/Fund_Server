"""Report section discovery facade for future collector extensions."""
from .sections import find_sections

async def find_reports_section(page, base_url, browser, scope=None, company_name=""):
    return await find_sections(page, base_url, "reports", browser, scope, company_name)
