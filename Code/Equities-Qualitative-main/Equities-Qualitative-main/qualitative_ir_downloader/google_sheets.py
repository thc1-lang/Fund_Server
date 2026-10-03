"""Read-only Google Sheets source with row-level validation."""
import logging
import time
from collections.abc import Iterable, Sequence
from .config import Config, WORKSHEETS
from .models import Stock

log = logging.getLogger(__name__)

def parse_rows(rows: Iterable[Sequence[str]], worksheet: str) -> list[Stock]:
    stocks = []
    for number, row in enumerate(rows, 4):
        ticker = str(row[0]).strip() if row else ""
        name = str(row[1]).strip() if len(row) > 1 else ""
        if not ticker and not name:
            continue
        if not ticker or not name:
            log.error("INVALID_SHEET_ROW worksheet=%s row=%s ticker=%r company=%r", worksheet, number, ticker, name)
            continue
        stocks.append(Stock(ticker, name, worksheet, number))
    return stocks

def read_stocks(config: Config, worksheet: str | None = None, ticker: str | None = None) -> list[Stock]:
    import json
    from dataclasses import asdict
    from .filesystem import atomic_json
    cache=config.download_root/'stock_metadata.json'
    cached={}
    try:
        stored=json.loads(cache.read_text(encoding='utf-8'))
        if stored.get('spreadsheet_id')==config.spreadsheet_id:cached=stored.get('stocks',{})
    except (OSError,ValueError):pass
    if ticker:
        match=cached.get(ticker.upper())
        if match and (not worksheet or match['worksheet']==worksheet):
            log.info('Stock metadata cache hit: %s',ticker)
            return [Stock(**match)]
        # Recover metadata from this application's existing default-workbook run.
        from .config import SPREADSHEET_ID
        if config.spreadsheet_id==SPREADSHEET_ID:
            for path in sorted(config.download_root.glob('*/manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True):
                try:
                    data=json.loads(path.read_text(encoding='utf-8'))
                    if data.get('ticker','').casefold()==ticker.casefold() and data.get('worksheet') in WORKSHEETS and (not worksheet or data['worksheet']==worksheet):
                        stock=Stock(**{k:data[k] for k in Stock.__dataclass_fields__})
                        log.info('Stock metadata recovered from prior run: %s',ticker)
                        return [stock]
                except (OSError,ValueError,KeyError,TypeError):continue
    import gspread
    session = gspread.service_account(filename=str(config.service_account_path), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    def retry(operation):
        for attempt in range(config.retries):
            try:
                return operation()
            except Exception:
                if attempt + 1 == config.retries:
                    raise
                log.warning("Sheets read failed; retry %s", attempt + 1, exc_info=True)
                time.sleep(2 ** attempt)
    book = retry(lambda: session.open_by_key(config.spreadsheet_id))
    stocks = []
    for name in WORKSHEETS:
        if worksheet and name != worksheet:
            continue
        log.info("Starting worksheet %s", name)
        rows = retry(lambda: book.worksheet(name).get("B4:C"))
        stocks.extend(parse_rows(rows, name))
        if ticker and any(s.ticker.casefold()==ticker.casefold() for s in stocks):break
    cached.update({s.ticker.upper():asdict(s) for s in stocks})
    atomic_json(cache,{'spreadsheet_id':config.spreadsheet_id,'stocks':cached})
    if ticker:return [s for s in stocks if s.ticker.casefold()==ticker.casefold()]
    return stocks
