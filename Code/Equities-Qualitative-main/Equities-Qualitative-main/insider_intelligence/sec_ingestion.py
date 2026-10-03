"""Official SEC EDGAR source/provider for insider filings.

The provider uses SEC machine-readable endpoints only. Network access is
isolated behind a small transport interface so all parser and cache behavior
can be tested offline.
"""

from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from .models import IssuerIdentity, SECIngestionResult, SECParsedFiling, SECFiling
from .sec_parser import parse_ownership_xml
from .source_registry import SourceRegistry


SEC_DATA = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_USER_AGENT = "Allen & Cooper Insider Intelligence nicholaslallen1@gmail.com"
SEC_ACCEPT = "application/json,text/xml,application/xml,text/html,*/*"
SEC_ACCEPT_ENCODING = "gzip, deflate"
DEFAULT_FORMS = ("3", "4", "5")
SUPPORTED_FORMS = {"3", "4", "5", "DEF 14A", "DEFA14A", "10-K", "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"}


def is_ownership_form(form_type: str | None) -> bool:
    """Return whether a filing is a Form 3/4/5, including amendments."""
    return str(form_type or "").upper().split("/", 1)[0] in {"3", "4", "5"}


@dataclass
class SECResponse:
    status: int
    body: bytes
    headers: dict[str, str]


class SECRequestError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str = "SEC_REQUEST_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


def _decode_content(body: bytes, content_encoding: str | None) -> bytes:
    """Decode HTTP content encodings in reverse application order."""
    encodings = [item.strip().lower() for item in (content_encoding or "").split(",") if item.strip()]
    decoded = body
    for encoding in reversed(encodings):
        if encoding in {"identity", ""}:
            continue
        if encoding == "gzip":
            decoded = gzip.decompress(decoded)
        elif encoding == "deflate":
            try:
                decoded = zlib.decompress(decoded)
            except zlib.error:
                decoded = zlib.decompress(decoded, -zlib.MAX_WBITS)
        else:
            raise SECRequestError(f"Unsupported SEC content encoding: {encoding}")
    return decoded


class SECTransport(Protocol):
    def get(self, url: str, headers: dict[str, str], timeout: float) -> SECResponse: ...


class UrllibSECTransport:
    def get(self, url: str, headers: dict[str, str], timeout: float) -> SECResponse:
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=timeout) as response:
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            body = _decode_content(response.read(), response_headers.get("content-encoding"))
            response_headers.pop("content-encoding", None)
            return SECResponse(response.status, body, response_headers)


def normalize_cik(value: str | int) -> str:
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        raise ValueError("CIK is required")
    return digits.zfill(10)


def accession_directory(accession: str) -> str:
    return accession.replace("-", "")


def extract_ownership_xml(body: bytes) -> bytes | None:
    """Extract the structured ownershipDocument from an SEC raw submission."""
    start_match = re.search(br"<ownershipDocument(?:\s|>)", body, flags=re.IGNORECASE)
    if start_match is None:
        return None
    end_match = re.search(br"</ownershipDocument\s*>", body[start_match.start():], flags=re.IGNORECASE)
    if end_match is None:
        return None
    end = start_match.start() + end_match.end()
    return body[start_match.start():end]


class SECSourceProvider:
    """Resolve issuers, discover official filings, cache XML, and parse Forms 3/4/5."""

    provider_name = "sec-edgar"
    source_authority = "sec_structured"

    def __init__(
        self,
        cache_root: str | Path = "artifacts/insider_intelligence/sec",
        *,
        user_agent: str = SEC_USER_AGENT,
        transport: SECTransport | None = None,
        min_interval: float | None = None,
        max_requests_per_second: float = 5.0,
        retries: int = 3,
        forbidden_retries: int = 1,
        backoff_base: float = 1.0,
        forbidden_delay: float = 2.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        debug: bool = False,
        diagnostic_hook: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.transport = transport or UrllibSECTransport()
        if max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be positive")
        self.max_requests_per_second = float(max_requests_per_second)
        self.min_interval = 1.0 / self.max_requests_per_second if min_interval is None else max(0.0, min_interval)
        self.retries = max(0, retries)
        self.forbidden_retries = max(0, forbidden_retries)
        self.backoff_base = max(0.0, backoff_base)
        self.forbidden_delay = max(0.0, forbidden_delay)
        self.timeout = timeout
        self.sleep = sleep
        self._last_request = 0.0
        self._limiter_lock = threading.Lock()
        self.debug = debug
        self.diagnostic_hook = diagnostic_hook
        self.diagnostics: list[dict[str, Any]] = []
        registry_root = self.cache_root.parent if self.cache_root.name == "sec" else self.cache_root
        self.registry = SourceRegistry(registry_root)

    def _emit_diagnostic(self, url: str, *, status: int | None, attempt: int, cache_hit: bool) -> None:
        parsed = urlparse(url)
        event = {
            "url": url,
            "host": parsed.hostname or "",
            "status": status,
            "attempt": attempt,
            "cache_hit": cache_hit,
            "cache": "hit" if cache_hit else "miss",
            "declared_user_agent": "yes" if self.user_agent.strip() else "no",
        }
        self.diagnostics.append(event)
        if self.diagnostic_hook is not None:
            self.diagnostic_hook(dict(event))
        elif self.debug:
            print(
                f"SEC request: {event['host']} status={event['status']} attempt={event['attempt']} "
                f"cache={event['cache']} declared_user_agent={event['declared_user_agent']} URL={event['url']}"
            )

    def _wait_for_slot(self) -> None:
        """Reserve a request slot so concurrent provider calls share one limiter."""
        with self._limiter_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval:
                self.sleep(self.min_interval - elapsed)
            self._last_request = time.monotonic()

    def _request(self, url: str, *, accept: str = SEC_ACCEPT) -> SECResponse:
        headers = {"User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": SEC_ACCEPT_ENCODING}
        attempt = 0
        while True:
            attempt += 1
            self._wait_for_slot()
            try:
                try:
                    response = self.transport.get(url, headers, self.timeout)
                except TypeError:
                    response = self.transport.get(url, headers)  # type: ignore[misc]
                response.headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                response.body = _decode_content(response.body, response.headers.get("content-encoding"))
                response.headers.pop("content-encoding", None)
                self._emit_diagnostic(url, status=response.status, attempt=attempt, cache_hit=False)
                if response.status == 429 or response.status >= 500:
                    raise SECRequestError(f"SEC request failed with HTTP {response.status}: {url}", response.status)
                if response.status == 403:
                    raise SECRequestError(f"SEC_ACCESS_FORBIDDEN URL={url} status=403 attempt={attempt} declared_user_agent={'yes' if self.user_agent.strip() else 'no'}", 403, "SEC_ACCESS_FORBIDDEN")
                if response.status >= 400:
                    raise SECRequestError(f"SEC request failed with HTTP {response.status}: {url}", response.status)
                return response
            except (HTTPError, URLError, SECRequestError) as exc:
                status = getattr(exc, "status", None)
                if status is None:
                    status = getattr(exc, "code", None)
                if isinstance(exc, (HTTPError, URLError)):
                    self._emit_diagnostic(url, status=status, attempt=attempt, cache_hit=False)
                if status == 403:
                    if attempt > self.forbidden_retries:
                        if isinstance(exc, SECRequestError) and exc.code == "SEC_ACCESS_FORBIDDEN":
                            raise
                        raise SECRequestError(f"SEC_ACCESS_FORBIDDEN URL={url} status=403 attempt={attempt} declared_user_agent={'yes' if self.user_agent.strip() else 'no'}", 403, "SEC_ACCESS_FORBIDDEN") from exc
                    self.sleep(self.forbidden_delay)
                    continue
                retryable = status in {429, 500, 502, 503, 504}
                if not retryable or attempt > self.retries:
                    if isinstance(exc, SECRequestError):
                        raise
                    raise SECRequestError(str(exc), status) from exc
                delay = min(30.0, self.backoff_base * float(2 ** (attempt - 1)))
                if isinstance(exc, HTTPError):
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after and retry_after.isdigit():
                        delay = min(60.0, float(retry_after))
                self.sleep(delay)

    def _json(self, url: str, *, cache_path: Path | None = None, force: bool = False) -> dict[str, Any]:
        if cache_path is not None and cache_path.exists() and not force:
            try:
                value = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    self._emit_diagnostic(url, status=None, attempt=0, cache_hit=True)
                    return value
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        response = self._request(url)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SECRequestError(f"SEC endpoint did not return JSON: {url}") from exc
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        return value

    def resolve_issuer(self, ticker: str, *, force: bool = False) -> IssuerIdentity:
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required")
        cache = self.cache_root / "company_tickers.json"
        data = self._json(SEC_TICKERS, cache_path=cache, force=force)
        matches = []
        values = data.values() if isinstance(data, dict) else data
        for item in values:
            if str(item.get("ticker", "")).upper() == symbol:
                matches.append(item)
        if not matches:
            raise LookupError(f"SEC issuer not found for ticker {symbol}")
        item = matches[0]
        cik = normalize_cik(item.get("cik_str") or item.get("cik"))
        return IssuerIdentity(symbol, str(item.get("title") or item.get("name") or symbol), cik, SEC_TICKERS)

    @staticmethod
    def _date_allowed(value: str | None, from_date: str | None, to_date: str | None) -> bool:
        if not value:
            return True
        if from_date and value < from_date:
            return False
        if to_date and value > to_date:
            return False
        return True

    def _submissions(self, identity: IssuerIdentity, *, force: bool = False) -> dict[str, Any]:
        cache = self.cache_root / identity.ticker / "submissions.json"
        url = f"{SEC_DATA}/submissions/CIK{identity.issuer_cik}.json"
        return self._json(url, cache_path=cache, force=force)

    def _filings_from_payload(self, identity: IssuerIdentity, payload: dict[str, Any], forms: set[str], from_date: str | None, to_date: str | None) -> list[SECFiling]:
        recent = payload.get("filings", {}).get("recent", {})
        values: list[SECFiling] = []
        length = len(recent.get("accessionNumber", []))
        for index in range(length):
            form = str(recent.get("form", [""] * length)[index]).upper()
            if not any(form == wanted or form.startswith(wanted + "/") for wanted in forms):
                continue
            filing_date = recent.get("filingDate", [None] * length)[index]
            if not self._date_allowed(filing_date, from_date, to_date):
                continue
            accession = str(recent["accessionNumber"][index])
            primary = str(recent.get("primaryDocument", [""] * length)[index] or "")
            values.append(SECFiling(identity.ticker, identity.issuer_cik, form, accession, filing_date, recent.get("reportDate", [None] * length)[index], primary, self._archive_url(identity.issuer_cik, accession, primary)))
        return values

    def discover_filings(
        self,
        ticker: str,
        *,
        forms: tuple[str, ...] = DEFAULT_FORMS,
        from_date: str | None = None,
        to_date: str | None = None,
        force: bool = False,
        include_historical: bool = False,
    ) -> list[SECFiling]:
        identity = self.resolve_issuer(ticker, force=force)
        wanted = {form.upper() for form in forms}
        unsupported = wanted.difference(SUPPORTED_FORMS)
        if unsupported:
            raise ValueError(f"unsupported SEC forms: {sorted(unsupported)}")
        payload = self._submissions(identity, force=force)
        filings = self._filings_from_payload(identity, payload, wanted, from_date, to_date)
        recent_dates = [value for value in payload.get("filings", {}).get("recent", {}).get("filingDate", []) if value]
        needs_archived_metadata = bool(from_date and recent_dates and from_date < min(recent_dates))
        if include_historical or needs_archived_metadata:
            for file_info in payload.get("filings", {}).get("files", []) or []:
                name = file_info.get("name") if isinstance(file_info, dict) else None
                if not name:
                    continue
                url = name if str(name).startswith("http") else f"{SEC_DATA}/submissions/{name}"
                historical = self._json(url, cache_path=self.cache_root / identity.ticker / Path(str(name)).name, force=force)
                filings.extend(self._filings_from_payload(identity, historical, wanted, from_date, to_date))
        unique: dict[str, SECFiling] = {item.accession_number: item for item in filings}
        return sorted(unique.values(), key=lambda item: (item.filing_date or "", item.accession_number))

    @staticmethod
    def _archive_url(cik: str, accession: str, primary: str) -> str:
        name = primary or "ownership.xml"
        return f"{SEC_ARCHIVES}/{int(cik)}/{accession_directory(accession)}/{name}"

    def _filing_dir(self, filing: SECFiling) -> Path:
        return self.cache_root / filing.ticker.upper() / "filings" / filing.accession_number

    def download_filing(self, filing: SECFiling, *, force: bool = False) -> tuple[bytes, SECFiling]:
        directory = self._filing_dir(filing)
        directory.mkdir(parents=True, exist_ok=True)
        metadata_path = directory / "filing_metadata.json"
        ownership_form = is_ownership_form(filing.form_type)
        target_name = "ownership.xml" if ownership_form else re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filing.primary_document or "filing.bin").name)
        target_path = directory / target_name
        # Older component builds cached amendment XML under the primary document
        # name. Reuse that unchanged local artifact instead of re-requesting it.
        cache_path = target_path
        if ownership_form and not cache_path.exists():
            legacy_xml = sorted(directory.glob("*.xml"))
            if len(legacy_xml) == 1:
                cache_path = legacy_xml[0]
        if cache_path.exists() and metadata_path.exists() and not force:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                body = cache_path.read_bytes()
                if not body:
                    raise ValueError("empty cached filing")
                cached = SECFiling.from_dict({**filing.to_dict(), **metadata, "local_source_path": str(cache_path), "downloaded": True, "cache_hit": True})
                self.registry.upsert(cached)
                self._emit_diagnostic(cached.source_url, status=None, attempt=0, cache_hit=True)
                return body, cached
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        base = f"{SEC_ARCHIVES}/{int(filing.issuer_cik)}/{accession_directory(filing.accession_number)}"
        candidates: list[str] = []
        if filing.primary_document.lower().endswith(".xml"):
            candidates.append(filing.source_url)
        if ownership_form:
            candidates.append(f"{base}/ownership.xml")
            candidates.append(f"{base}/{filing.accession_number}.txt")
        if filing.source_url not in candidates:
            candidates.append(filing.source_url)
        last_error: Exception | None = None
        body: bytes | None = None
        used_url = filing.source_url
        for url in dict.fromkeys(candidates):
            try:
                response = self._request(url)
                candidate_body = extract_ownership_xml(response.body) if ownership_form else response.body
                if ownership_form and candidate_body is None:
                    last_error = SECRequestError(f"SEC filing does not contain structured ownership XML: {url}", response.status)
                    continue
                body, used_url = candidate_body if candidate_body is not None else response.body, url
                break
            except SECRequestError as exc:
                last_error = exc
                if exc.status not in {403, 404}:
                    break
        if body is None:
            raise SECRequestError(f"Unable to download SEC filing {filing.accession_number}: {last_error}")
        target_path.write_bytes(body)
        metadata = {
            "accession_number": filing.accession_number,
            "form_type": filing.form_type,
            "issuer_cik": filing.issuer_cik,
            "filing_date": filing.filing_date,
            "report_date": filing.report_date,
            "primary_document": filing.primary_document,
            "source_url": used_url,
            "content_hash": hashlib.sha256(body).hexdigest(),
            "downloaded_at": time.time(),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        updated = SECFiling.from_dict({**filing.to_dict(), "source_url": used_url, "local_source_path": str(target_path), "downloaded": True, "cache_hit": False})
        self.registry.upsert(updated)
        return body, updated

    def acquire(
        self,
        ticker: str,
        *,
        forms: tuple[str, ...] = DEFAULT_FORMS,
        from_date: str | None = None,
        to_date: str | None = None,
        force: bool = False,
        dry_run: bool = False,
        include_historical: bool = False,
    ) -> SECIngestionResult:
        identity = self.resolve_issuer(ticker, force=force)
        filings = self.discover_filings(ticker, forms=forms, from_date=from_date, to_date=to_date, force=force, include_historical=include_historical)
        result = SECIngestionResult(ticker=identity.ticker, issuer_cik=identity.issuer_cik, filings_discovered=len(filings), filings=filings)
        if dry_run:
            return result
        for filing in filings:
            try:
                body, downloaded = self.download_filing(filing, force=force)
                result.filings[result.filings.index(filing)] = downloaded
                if downloaded.cache_hit:
                    result.filings_reused += 1
                if is_ownership_form(downloaded.form_type):
                    parsed = parse_ownership_xml(body, downloaded)
                    result.parsed_filings.append(parsed)
                result.filings_processed += 1
            except Exception as exc:
                result.warnings.append(f"{filing.accession_number}: {exc}")
        return result


__all__ = ["DEFAULT_FORMS", "SUPPORTED_FORMS", "SEC_ACCEPT", "SEC_ACCEPT_ENCODING", "SEC_ARCHIVES", "SEC_DATA", "SECRequestError", "SECResponse", "SECSourceProvider", "SECTransport", "SEC_TICKERS", "SEC_USER_AGENT", "UrllibSECTransport", "extract_ownership_xml", "is_ownership_form", "normalize_cik"]
