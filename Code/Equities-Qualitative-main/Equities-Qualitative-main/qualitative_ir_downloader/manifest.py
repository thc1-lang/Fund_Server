"""Incrementally persisted company provenance."""
import json
from dataclasses import asdict
from pathlib import Path
from .filesystem import atomic_json
from .models import Document, Issue, Stock

class Manifest:
    def __init__(self, folder: Path, stock: Stock, timestamp: str, resume: bool = False):
        self.path = folder / "manifest.json"
        self.data = {**asdict(stock), "schema_version":3, "run_timestamp":timestamp, "investor_relations_url":None, "discovery":[], "news":[], "reports":[], "errors":[], "status":"IN_PROGRESS", "archive_pages":{}}
        if resume and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("run_history", []).append({"run_timestamp": self.data.get("run_timestamp"), "status": self.data.get("status"), "errors": self.data.get("errors", []), "event_metrics": self.data.get("event_metrics", {}), "events": self.data.get("events", [])})
            self.data.update(asdict(stock))
            self.data.update(run_timestamp=timestamp, status="IN_PROGRESS", errors=[], access_events=[], event_metrics={})
        self.data["schema_version"] = 4
        self.data.setdefault("events", [])
        self.write()

    def write(self):
        atomic_json(self.path, self.data)

    def error(self, issue: Issue):
        self.data["errors"].append(asdict(issue))
        self.write()

    def document(self, document: Document):
        records = self.data[document.category]
        record = asdict(document)
        index = next((i for i, value in enumerate(records) if value["source_url"] == document.source_url), None)
        if index is None:
            records.append(record)
        else:
            records[index] = record
        self.write()

    def event(self, event):
        record=event.record()
        records=self.data.setdefault("events",[])
        index=next((i for i,r in enumerate(records) if r["event_url"]==record["event_url"] and r.get("date")==record.get("date")),None)
        if index is None:records.append(record)
        else:records[index]=record
        self.write()
