"""Batched Sheets REST I/O using the existing external Google credentials."""

import gzip
import json
import logging
import time
import threading
from contextlib import contextmanager
from urllib.parse import quote
from google.auth import load_credentials_from_file
from google.auth.transport.requests import AuthorizedSession
import requests
from config import WORKBOOKS

LOG = logging.getLogger(__name__)


@contextmanager
def request_progress(label, interval=15):
    """Report waits without moving network I/O off the interruptible main thread."""
    finished = threading.Event()
    started = time.monotonic()

    def report():
        while not finished.wait(interval):
            LOG.info(
                "%s: still waiting for Google Sheets (%.0fs elapsed)",
                label,
                time.monotonic() - started,
            )

    worker = threading.Thread(target=report, name="sheets-progress", daemon=True)
    worker.start()
    try:
        yield
    finally:
        finished.set()
        worker.join()


def a1(tab, cells=""):
    return "'" + tab.replace("'", "''") + "'" + ("!" + cells if cells else "")


def column_name(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


class Sheets:
    def __init__(self, credential_file, write_ids=None):
        self.write_ids = (
            {b.spreadsheet_id for b in WORKBOOKS}
            if write_ids is None
            else set(write_ids)
        )
        creds, _ = load_credentials_from_file(
            str(credential_file),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.session = AuthorizedSession(creds)
        self.writes_attempted = False

    def request(self, method, book_id, suffix="", **kwargs):
        if method != "GET" and book_id not in getattr(self, "write_ids", set()):
            raise ValueError("Writes to upstream/source workbooks are prohibited")
        if method != "GET":
            self.writes_attempted = True
        label = next(
            (b.key for b in WORKBOOKS if b.spreadsheet_id == book_id), "source workbook"
        )
        url = "https://sheets.googleapis.com/v4/spreadsheets/" + book_id + suffix
        for attempt in range(6):
            try:
                with request_progress(
                    f'{label} {method} {suffix or "metadata"} (attempt {attempt+1}/6)'
                ):
                    response = self.session.request(
                        method, url, timeout=(15, 180), **kwargs
                    )
                    if response.status_code not in (429, 500, 502, 503, 504):
                        response.raise_for_status()
                        return response.json()
                    error = f"Google Sheets HTTP {response.status_code}"
            except (requests.Timeout, requests.ConnectionError) as exc:
                error = type(exc).__name__
            if attempt == 5:
                raise RuntimeError(
                    f"{book_id}: {method} {suffix}: {error}; retries exhausted"
                )
            delay = min(2**attempt, 32)
            LOG.warning("%s; retrying in %ss", error, delay)
            time.sleep(delay)

    def metadata(self, book_id):
        return self.request(
            "GET",
            book_id,
            params={
                "fields": "spreadsheetId,properties(title,locale),sheets(properties,protectedRanges,merges)"
            },
        )

    def values(self, book_id, ranges, formulas=False):
        started = time.monotonic()
        LOG.info(
            "Downloading %s: %s",
            "formulas" if formulas else "values",
            ", ".join(ranges),
        )
        obj = self.request(
            "GET",
            book_id,
            "/values:batchGet",
            params={
                "ranges": ranges,
                "valueRenderOption": "FORMULA" if formulas else "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER",
            },
        )
        result = [r.get("values", []) for r in obj["valueRanges"]]
        LOG.info(
            "Download complete in %.1fs; rows per range: %s",
            time.monotonic() - started,
            [len(r) for r in result],
        )
        return result

    def batch(self, book_id, requests_):
        return self.request(
            "POST", book_id, ":batchUpdate", json={"requests": requests_}
        )

    def write_values(self, book_id, ranges):
        return self.request(
            "POST",
            book_id,
            "/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": ranges},
        )

    def backup(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, allow_nan=False)

    def publish(self, book, properties, matrix, backup_path, snapshot):
        self.backup(backup_path, snapshot)
        grid = properties["gridProperties"]
        last_col = column_name(grid["columnCount"])
        clear = a1(book.output_tab, f'B3:{last_col}{grid["rowCount"]}')
        self.request(
            "POST", book.spreadsheet_id, "/values:batchClear", json={"ranges": [clear]}
        )
        written = 0
        # At most ~50k cells per request, independent of total history length.
        block_rows = max(1, 50000 // max(1, len(matrix[0])))
        for start in range(0, len(matrix), block_rows):
            block = matrix[start : start + block_rows]
            last = column_name(len(block[0]) + 1)
            target = a1(book.output_tab, f"B{start+3}:{last}{start+2+len(block)}")
            self.write_values(book.spreadsheet_id, [{"range": target, "values": block}])
            written += sum(map(len, block))
            LOG.info(
                "%s: wrote rows %s:%s (%s/%s matrix cells)",
                book.key,
                start + 3,
                start + 2 + len(block),
                written,
                len(matrix) * len(matrix[0]),
            )
        return written
