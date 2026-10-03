"""Offline ingestion of downloader manifests and their local artifacts."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .models import IngestionFailure, NormalizedDocument
from .normalization import build_normalized_document, classify_document, normalize_whitespace
from .document_store import DocumentStore


@dataclass
class IngestionResult:
    ticker: str | None = None
    documents_discovered: int = 0
    documents_normalized: int = 0
    documents_added: int = 0
    documents_updated: int = 0
    documents_unchanged: int = 0
    documents_failed: int = 0
    characters_ingested: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    fiscal_periods_detected: int = 0
    unknown_fiscal_periods: int = 0
    failures: list[IngestionFailure] = field(default_factory=list)
    sample: list[NormalizedDocument] = field(default_factory=list)
    documents: list[NormalizedDocument] = field(default_factory=list)

    def merge(self, other: "IngestionResult") -> None:
        for key in ("documents_discovered", "documents_normalized", "documents_added", "documents_updated", "documents_unchanged", "documents_failed", "characters_ingested", "fiscal_periods_detected", "unknown_fiscal_periods"):
            setattr(self, key, getattr(self, key) + getattr(other, key))
        for key, value in other.by_type.items():
            self.by_type[key] = self.by_type.get(key, 0) + value
        self.failures.extend(other.failures)
        self.documents.extend(other.documents)
        self.sample.extend(other.sample)


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _text_from_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    # Some public IR PDFs contain embedded fonts for which pypdf emits one
    # warning per page.  Keep the CLI diagnostic focused on ingestion failures.
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise ValueError("encrypted PDF requires a password")
            except Exception as exc:
                raise ValueError(f"encrypted PDF requires a password: {exc}") from exc
        pages = [(page.extract_text() or "") for page in reader.pages]
        metadata = {str(k).lstrip("/"): str(v) for k, v in (reader.metadata or {}).items() if v is not None}
    finally:
        logging.disable(previous_logging_level)
    return normalize_whitespace("\n\n".join(pages)), {"page_count": len(reader.pages), "pdf_metadata": metadata}


def _html_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.select("nav, footer, header, script, style, noscript, [role=navigation], .cookie, .cookie-banner, .consent"):
        node.decompose()
    return normalize_whitespace(html.unescape(soup.get_text("\n")))


def _transcript_text(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    segments = data.get("segments") or data.get("transcription", {}).get("segments") or []
    if not isinstance(segments, list):
        segments = []
    lines: list[str] = []
    clean_segments: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = normalize_whitespace(str(segment.get("text") or segment.get("content") or ""))
        if not text:
            continue
        entry = {key: segment[key] for key in ("start", "end", "speaker") if key in segment}
        entry["text"] = text
        clean_segments.append(entry)
        speaker = segment.get("speaker")
        lines.append(f"{speaker}: {text}" if speaker else text)
    if not lines and data.get("text"):
        lines = [str(data["text"])]
    transcription = data.get("transcription") if isinstance(data.get("transcription"), dict) else {}
    qa = data.get("transcript_qa") or data.get("qa") or {}
    metadata = {
        "segments": clean_segments,
        "transcription_model": transcription.get("model") or data.get("transcription_model"),
        "transcription_engine": transcription.get("engine") or data.get("transcription_engine"),
        "transcription_qa": qa,
        "source_method": data.get("transcript_method") or data.get("source", {}).get("method"),
        "generated_by_whisper": bool(data.get("generated_by_whisper")),
    }
    return normalize_whitespace("\n\n".join(lines)), metadata


def _record_path(root: Path, record: dict[str, Any], field: str) -> Path | None:
    value = record.get(field)
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else root / path


def _title_source_date(title: str) -> str | None:
    """Use an explicit IR title date only as a source-date fallback."""
    match = re.match(r"\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})\b", title or "")
    if not match:
        return None
    year = int(match.group(3))
    year += 2000 if year < 100 else 0
    return f"{year:04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"


def _iter_company_dirs(download_root: Path, ticker: str | None = None) -> list[Path]:
    if not download_root.exists():
        return []
    dirs = [p for p in download_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    if ticker:
        prefix = ticker.upper() + "_"
        dirs = [p for p in dirs if p.name.upper().startswith(prefix)]
    return sorted(dirs, key=lambda p: (p.name.split("_", 1)[0], p.stat().st_mtime, p.name), reverse=True)


def _latest_by_ticker(download_root: Path, ticker: str | None = None) -> list[Path]:
    dirs = _iter_company_dirs(download_root, ticker)
    chosen: dict[str, Path] = {}
    for path in dirs:
        code = path.name.split("_", 1)[0].upper()
        chosen.setdefault(code, path)
    return list(chosen.values())


def _records(manifest: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    # Downloaded records are authoritative; discovered-only records have no local
    # artifact and are useful only for diagnostics, so they are not normalized.
    for key, kind in (("news", "news"), ("reports", "reports"), ("events", "events")):
        values = manifest.get(key) or []
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    yield kind, value


def ingest_company(company_dir: Path, store: DocumentStore | None = None, *, document_type: str | None = None, dry_run: bool = False) -> IngestionResult:
    manifest_path = company_dir / "manifest.json"
    manifest = _json(manifest_path)
    ticker = company_dir.name.split("_", 1)[0].upper()
    result = IngestionResult(ticker=ticker)
    if not manifest:
        result.failures.append(IngestionFailure("INVALID_METADATA", "manifest.json is missing or invalid", ticker=ticker, local_path=str(manifest_path)))
        result.documents_failed += 1
        return result
    company_name = str(manifest.get("company_name") or company_dir.name.split("_", 2)[1] if "_" in company_dir.name else ticker)
    seen: set[tuple[str, str]] = set()
    for kind, record in _records(manifest):
        title = str(record.get("title") or "Untitled")
        local = _record_path(company_dir, record, "local_filename")
        json_local = _record_path(company_dir, record, "local_json")
        pdf_local = _record_path(company_dir, record, "local_pdf")
        if kind == "events":
            local = json_local or pdf_local or local
        candidate_type, _ = classify_document(record, title, str(local or ""))
        if kind == "events" and json_local and candidate_type == "other_report":
            candidate_type = "event_transcript"
        if document_type and document_type not in {candidate_type, str(record.get("category") or "").lower()}:
            continue
        result.documents_discovered += 1
        source_key = (str(record.get("source_url") or record.get("event_url") or record.get("official_ir_url") or ""), str(local or ""))
        if source_key in seen:
            continue
        seen.add(source_key)
        metadata = dict(record)
        metadata["manifest_path"] = str(manifest_path)
        metadata["artifact_root"] = str(company_dir)
        metadata["manifest_schema_version"] = manifest.get("schema_version")
        metadata["source_method"] = record.get("method") or record.get("transcript_method") or metadata.get("source_method")
        text = ""
        artifact_metadata: dict[str, Any] = {}
        try:
            if local and local.exists():
                if local.suffix.lower() == ".json":
                    data = _json(local)
                    if not data:
                        raise ValueError("invalid JSON transcript")
                    text, artifact_metadata = _transcript_text(data)
                    metadata.update({k: v for k, v in artifact_metadata.items() if v is not None})
                    metadata["transcript_json"] = data.get("source", {})
                elif local.suffix.lower() == ".pdf":
                    text, artifact_metadata = _text_from_pdf(local)
                elif local.suffix.lower() in {".txt", ".html", ".htm"}:
                    raw = local.read_text(encoding="utf-8", errors="replace")
                    text = _html_text(raw) if local.suffix.lower() in {".html", ".htm"} else normalize_whitespace(raw)
                else:
                    raise ValueError(f"unsupported extension {local.suffix}")
            elif record.get("content_html"):
                text = _html_text(str(record["content_html"]))
                local = None
            elif record.get("description"):
                text = normalize_whitespace(str(record["description"]))
                local = None
            else:
                raise FileNotFoundError("local artifact is missing")
            if not text:
                raise ValueError("extracted text is empty")
            metadata.update(artifact_metadata)
            publication_date = record.get("date") if kind != "events" else None
            event_date = record.get("date") if kind == "events" else None
            if not metadata.get("source_date"):
                derived = _title_source_date(title)
                if derived:
                    metadata["source_date"] = derived
                    metadata["source_date_method"] = "title_date"
            document = build_normalized_document(ticker=ticker, company_name=company_name, title=title, metadata=metadata, text=text, local_path=str(local) if local else None, publication_date=str(publication_date) if publication_date else None, event_date=str(event_date) if event_date else None)
            result.documents_normalized += 1
            result.characters_ingested += document.text_length
            result.by_type[document.document_type] = result.by_type.get(document.document_type, 0) + 1
            if document.period_label:
                result.fiscal_periods_detected += 1
            else:
                result.unknown_fiscal_periods += 1
            result.documents.append(document)
            if len(result.sample) < 5:
                result.sample.append(document)
        except FileNotFoundError as exc:
            result.documents_failed += 1
            result.failures.append(IngestionFailure("MISSING_ARTIFACT", str(exc), ticker=ticker, title=title, source_url=record.get("source_url") or record.get("event_url"), local_path=str(local) if local else None, document_type=candidate_type))
        except json.JSONDecodeError as exc:
            result.documents_failed += 1
            result.failures.append(IngestionFailure("INVALID_METADATA", str(exc), ticker=ticker, title=title, local_path=str(local) if local else None, document_type=candidate_type))
        except (OSError, ValueError, TypeError) as exc:
            result.documents_failed += 1
            failure_type = "TEXT_EXTRACTION_FAILED" if local and local.suffix.lower() == ".pdf" else ("EMPTY_TEXT" if "empty" in str(exc).lower() else "UNSUPPORTED_FORMAT")
            result.failures.append(IngestionFailure(failure_type, str(exc), ticker=ticker, title=title, source_url=record.get("source_url") or record.get("event_url"), local_path=str(local) if local else None, document_type=candidate_type))
        except Exception as exc:
            # Parser-specific exceptions (for example pypdf's PdfReadError)
            # must remain isolated to the document that caused them.
            result.documents_failed += 1
            failure_type = "TEXT_EXTRACTION_FAILED" if local and local.suffix.lower() == ".pdf" else "INVALID_METADATA"
            result.failures.append(IngestionFailure(failure_type, str(exc), ticker=ticker, title=title, source_url=record.get("source_url") or record.get("event_url"), local_path=str(local) if local else None, document_type=candidate_type))
    if store and not dry_run:
        counts = store.upsert_many(result.documents)
        result.documents_added = counts["added"]
        result.documents_updated = counts["updated"]
        result.documents_unchanged = counts["unchanged"]
    return result


def ingest(*, ticker: str | None = None, all_companies: bool = False, download_root: str | Path = Path.home() / "Downloads", store: DocumentStore | None = None, rebuild: bool = False, replace_ticker: bool = False, document_type: str | None = None, dry_run: bool = False) -> IngestionResult:
    root = Path(download_root)
    store = store or DocumentStore()
    if rebuild and not dry_run:
        store.rebuild()
    # A later downloader run can contain only a subset (for example, a new
    # event run may not repeat the older news archive).  Read every local
    # manifest for the ticker and deduplicate by stable document ID below.
    dirs = _iter_company_dirs(root, None if all_companies else ticker)
    result = IngestionResult(ticker=ticker.upper() if ticker else None)
    for company_dir in dirs:
        result.merge(ingest_company(company_dir, None, document_type=document_type, dry_run=True))
    unique: dict[str, NormalizedDocument] = {}
    for document in result.documents:
        unique.setdefault(document.document_id, document)
    result.documents = list(unique.values())
    # Recompute document-derived counters after cross-run deduplication.
    result.documents_normalized = len(result.documents)
    result.characters_ingested = sum(document.text_length for document in result.documents)
    result.by_type = {}
    result.fiscal_periods_detected = 0
    result.unknown_fiscal_periods = 0
    for document in result.documents:
        result.by_type[document.document_type] = result.by_type.get(document.document_type, 0) + 1
        if document.period_label:
            result.fiscal_periods_detected += 1
        else:
            result.unknown_fiscal_periods += 1
    unique_failures: dict[tuple[Any, ...], IngestionFailure] = {}
    for failure in result.failures:
        key = (failure.failure_type, failure.source_url or failure.local_path, failure.title)
        unique_failures.setdefault(key, failure)
    result.failures = list(unique_failures.values())
    result.documents_failed = len(result.failures)
    result.documents_discovered = result.documents_normalized + result.documents_failed
    if store and not dry_run:
        counts = store.replace_ticker(result.ticker or ticker or "", result.documents) if replace_ticker and (ticker or result.ticker) else store.upsert_many(result.documents)
        result.documents_added = counts["added"]
        result.documents_updated = counts["updated"]
        result.documents_unchanged = counts["unchanged"]
    result.sample = result.documents[:5]
    return result
