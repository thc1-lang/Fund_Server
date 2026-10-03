from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DataAudit, HistoricalObservation


class ValidationStore:
    def __init__(self, root: str | Path = "artifacts/empirical_validation"):
        self.root=Path(root)
    def write_json(self, relative: str, value: Any) -> Path:
        path=self.root/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value,sort_keys=True,indent=2,default=str)+"\n",encoding="utf-8"); return path
    def write_observations(self, observations: list[HistoricalObservation]) -> Path:
        path=self.root/"datasets"/"observations.jsonl"; path.parent.mkdir(parents=True,exist_ok=True); path.write_text("".join(json.dumps(o.to_dict(),sort_keys=True)+"\n" for o in observations),encoding="utf-8"); return path
    def write_audit(self, audit: DataAudit, leakage, markdown: str) -> dict[str,str]:
        return {"json":str(self.write_json("audits/data_audit.json",audit.to_dict())),"leakage":str(self.write_json("audits/leakage_audit.json",leakage.to_dict())),"report":str(self.write_json("VALIDATION_REPORT.md",markdown))}
