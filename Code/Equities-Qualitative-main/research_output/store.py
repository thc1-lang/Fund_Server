from __future__ import annotations
import json
from pathlib import Path
from .models import CompanyResearchDossier
from .renderer_json import render_json
from .renderer_markdown import render_markdown

def output_paths(d: CompanyResearchDossier, root: str|Path="artifacts/research_output") -> tuple[Path,Path]:
    folder=Path(root)/d.ticker; folder.mkdir(parents=True,exist_ok=True)
    stem=f"{d.ticker}_{d.as_of_date}_{d.profile_version}_qualitative_analysis"
    return folder/(stem+".md"),folder/(stem+".json")
def _strip_runtime(x):
    if isinstance(x,dict): return {k:_strip_runtime(v) for k,v in x.items() if k not in {"generated_at"}}
    if isinstance(x,list): return [_strip_runtime(v) for v in x]
    return x
def write_dossier(d: CompanyResearchDossier, root: str|Path="artifacts/research_output", fmt: str="both", detail: str="standard", include_evidence: bool=False, explain_scores: bool=False, force: bool=False) -> dict:
    md,jp=output_paths(d,root); outputs={}; unchanged=True
    if fmt in ("markdown","both"):
        content=render_markdown(d,detail,include_evidence,explain_scores); unchanged &= md.exists() and md.read_text(encoding="utf8")==content
        if force or not unchanged or not md.exists(): md.write_text(content,encoding="utf8")
        outputs["markdown"]=str(md)
    if fmt in ("json","both"):
        content=render_json(d,include_evidence); unchanged &= jp.exists() and _strip_runtime(json.loads(jp.read_text(encoding="utf8")))==_strip_runtime(json.loads(content))
        if force or not unchanged or not jp.exists(): jp.write_text(content,encoding="utf8")
        outputs["json"]=str(jp)
    # registry is an upsert keyed by stable dossier identity, so repeated runs do not duplicate entries
    registry=Path(root)/"dossiers.jsonl"; rows=[]
    if registry.exists():
        for line in registry.read_text(encoding="utf8").splitlines():
            if line.strip():
                try: rows.append(json.loads(line))
                except json.JSONDecodeError: pass
    entry={"dossier_id":d.dossier_id,"ticker":d.ticker,"as_of_date":d.as_of_date,"profile_version":d.profile_version,"input_fingerprint":d.input_fingerprint,"markdown_path":str(md),"json_path":str(jp)}
    found=False
    for i,r in enumerate(rows):
        if r.get("dossier_id")==d.dossier_id: rows[i]=entry; found=True; break
    if not found: rows.append(entry)
    rows=sorted(rows,key=lambda r:(r.get("ticker",""),r.get("as_of_date",""),r.get("dossier_id","")))
    registry.write_text("".join(json.dumps(r,sort_keys=True)+"\n" for r in rows),encoding="utf8")
    return {"outputs":outputs,"unchanged":unchanged,"registry_entries":len(rows),"fingerprint":d.input_fingerprint}

class DossierStore:
    """Small public store facade used by integrations and tests."""
    def __init__(self, root: str|Path = "artifacts/research_output"):
        self.root = Path(root)
    def save(self, dossier: CompanyResearchDossier, **kwargs) -> dict:
        return write_dossier(dossier, self.root, **kwargs)
