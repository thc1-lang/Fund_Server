from __future__ import annotations
import json
from .models import CompanyResearchDossier

def render_json(dossier: CompanyResearchDossier, include_evidence: bool=False) -> str:
    return json.dumps(dossier.to_dict(include_evidence), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
