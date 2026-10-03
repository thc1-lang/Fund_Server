from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import run_score
from .explainability import explain_factor
from .factor_registry import RegistryError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evidence- and coverage-aware qualitative scoring")
    parser.add_argument("--ticker", action="append", required=True)
    parser.add_argument("--profile", default="core_v1")
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--pillar")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--artifacts-root", default="artifacts")
    parser.add_argument("--config-root", default="config/scoring")
    parser.add_argument("--output-root", default="artifacts/scoring_engine")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile, factors, pillars, snapshots, run, counts = run_score(args.ticker, args.as_of_date, args.profile,
                                                                      artifacts_root=args.artifacts_root, config_root=args.config_root,
                                                                      output_root=args.output_root, write=not args.dry_run)
    except RegistryError as exc:
        print(str(exc)); return 2
    print(f"Ticker: {', '.join(dict.fromkeys(item.upper() for item in args.ticker))}")
    print(f"Profile: {profile.profile}")
    print(f"Profile version: {profile.profile_version}")
    print(f"Calibration status: {profile.calibration_status or 'UNSPECIFIED'}")
    print(f"As-of: {args.as_of_date}")
    for snapshot in snapshots:
        print(f"\nOverall score: {snapshot.overall_score}")
        print(f"Evidence coverage: {snapshot.overall_evidence_coverage:.4f}")
        print(f"Scoring coverage: {snapshot.overall_scoring_coverage:.4f}")
        print(f"Overall confidence: {snapshot.overall_confidence:.4f}")
        print(f"Status: {snapshot.score_status}")
        print("\nPillars:")
        for pillar in pillars:
            if pillar.ticker == snapshot.ticker:
                print(f"{pillar.pillar}: score={pillar.score} evidence_coverage={pillar.evidence_coverage:.4f} scoring_coverage={pillar.scoring_coverage:.4f} confidence={pillar.confidence:.4f} status={pillar.coverage_status}")
        print(f"Scored factors: {snapshot.scored_factor_count}")
        print(f"Unscored factors: {snapshot.unscored_factor_count}")
        print(f"Critical missing: {', '.join(snapshot.critical_missing_factors) or 'none'}")
        if snapshot.top_positive_contributors: print(f"Top positive contributors: {', '.join(snapshot.top_positive_contributors)}")
        if snapshot.top_negative_contributors: print(f"Top negative contributors: {', '.join(snapshot.top_negative_contributors)}")
        print("Coverage limitations:")
        for limitation in snapshot.coverage_limitations: print(f"- {limitation}")
        if args.explain:
            print("\nFactor audit:")
            for factor in factors:
                if factor.ticker == snapshot.ticker and (not args.pillar or factor.pillar == args.pillar):
                    print(json.dumps(explain_factor(factor), sort_keys=True))
    if args.dry_run:
        print("Dry run: score materializations were still computed but no external actions were performed.")
    else:
        print(f"Stored: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
