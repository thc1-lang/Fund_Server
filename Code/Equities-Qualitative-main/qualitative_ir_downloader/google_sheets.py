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
    # A clean production rebuild may start with the entire download root
    # removed.  Create it before the atomic cache write below.
    cache.parent.mkdir(parents=True, exist_ok=True)
    cached={}
    full_fetched_at=0.0
    try:
        stored=json.loads(cache.read_text(encoding='utf-8'))
        if stored.get('spreadsheet_id')==config.spreadsheet_id and isinstance(stored.get('stocks'),dict):
            cached=stored['stocks']
            full_fetched_at=float(stored.get('full_fetched_at') or 0)
    except (OSError,ValueError):pass
    if ticker and time.time()-full_fetched_at < 300:
        matches=cached.get(ticker.upper())
        matches=matches if isinstance(matches,list) else [matches] if isinstance(matches,dict) else []
        selected=[match for match in matches if isinstance(match,dict) and (not worksheet or match.get('worksheet')==worksheet)]
        if selected:
            log.info('Stock metadata cache hit: %s',ticker)
            return [Stock(**match) for match in selected]
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
    if worksheet is None:
        cached={}
    else:
        for key, previous in list(cached.items()):
            previous=previous if isinstance(previous,list) else [previous] if isinstance(previous,dict) else []
            remaining=[entry for entry in previous if isinstance(entry,dict) and entry.get('worksheet')!=worksheet]
            if remaining:cached[key]=remaining
            else:cached.pop(key,None)
    updated=set()
    for stock in stocks:
        key=stock.ticker.upper()
        if key not in updated:
            previous=cached.get(key,[])
            cached[key]=previous if isinstance(previous,list) else [previous] if isinstance(previous,dict) else []
        cached[key].append(asdict(stock))
        updated.add(key)
    atomic_json(cache,{'spreadsheet_id':config.spreadsheet_id,
                       'full_fetched_at':time.time() if worksheet is None else full_fetched_at,
                       'stocks':cached})
    if ticker:return [s for s in stocks if s.ticker.casefold()==ticker.casefold()]
    return stocks
