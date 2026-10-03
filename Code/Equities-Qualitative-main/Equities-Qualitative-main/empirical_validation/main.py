from __future__ import annotations

import argparse
import json
from pathlib import Path

from .audit import audit_markdown, run_data_audit
from .calibration import calibration_refusal
from .config import ValidationConfig, load_config
from .factor_matrix import factor_matrix
from .leakage_audit import LeakageAuditor
from .market_data import NullMarketDataProvider
from .observation_builder import build_observations
from .store import ValidationStore
from .universe import PilotUniverse
from .walk_forward import chronological_split


def parser():
    p=argparse.ArgumentParser(description="Point-in-time empirical validation architecture")
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("audit-data","build-dataset","diagnose","walk-forward","calibrate"):
        q=sub.add_parser(name); q.add_argument("--config",required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv); config=load_config(args.config); observations=build_observations(config, PilotUniverse.from_tickers(config.tickers,config.universe_version)); store=ValidationStore(config.output_root)
    if args.command=="audit-data":
        audit,leakage=run_data_audit(observations,config,NullMarketDataProvider.status); paths=store.write_observations(observations); paths2=store.write_audit(audit,leakage,audit_markdown(audit,leakage)); print("POINT-IN-TIME DATA AUDIT"); print(json.dumps(audit.to_dict(),sort_keys=True,indent=2)); print(f"observations: {paths}"); print(json.dumps(paths2,sort_keys=True)); return 0
    if args.command=="build-dataset":
        path=store.write_observations(observations); print(f"Built {len(observations)} observations: {path}"); return 0
    if args.command=="diagnose":
        print(json.dumps(factor_matrix(observations),sort_keys=True,indent=2)); return 0
    if args.command=="walk-forward":
        split=chronological_split(observations); print(json.dumps({"status":split.status,"train":len(split.train),"validation":len(split.validation),"holdout":len(split.holdout),"reason":split.reason},sort_keys=True,indent=2)); return 0
    if args.command=="calibrate":
        refusal=calibration_refusal(observations,config); print(json.dumps(refusal,sort_keys=True,indent=2)); return 0
    return 0


if __name__=="__main__": raise SystemExit(main())
