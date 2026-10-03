from __future__ import annotations
import argparse
from .assembler import assemble_dossier
from .store import write_dossier

def build_parser():
    p=argparse.ArgumentParser(description="Generate a deterministic research dossier from frozen artifacts")
    p.add_argument("--ticker",required=True); p.add_argument("--as-of-date",required=True); p.add_argument("--profile",default="core_v1")
    p.add_argument("--format",choices=("markdown","json","both"),default="both"); p.add_argument("--detail",choices=("concise","standard","full"),default="standard")
    p.add_argument("--include-evidence",action="store_true"); p.add_argument("--explain-scores",action="store_true"); p.add_argument("--force",action="store_true")
    p.add_argument("--artifacts-root",default="artifacts"); p.add_argument("--output-root",default="artifacts/research_output")
    return p
def main(argv=None):
    a=build_parser().parse_args(argv); d=assemble_dossier(a.ticker,a.as_of_date,a.profile,a.artifacts_root)
    result=write_dossier(d,a.output_root,a.format,a.detail,a.include_evidence,a.explain_scores,a.force)
    print(f"Generated {d.ticker} dossier ({d.dossier_id})")
    print(f"Input fingerprint: {d.input_fingerprint}")
    print(f"Unchanged: {result['unchanged']}; registry entries: {result['registry_entries']}")
    for k,v in result["outputs"].items(): print(f"{k}: {v}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
