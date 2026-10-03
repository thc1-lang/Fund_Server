"""Maintain identical relationship engines in both standalone project folders.

Developer utility only; normal execution has no dependency on the sibling project.
Run after editing the canonical relationship modules in us_complete_pipeline.
"""

from pathlib import Path
import argparse
import shutil

ROOT = Path(__file__).resolve().parents[1]
SHARED = (
    "config.py",
    "google_sheets.py",
    "pipeline.py",
    "momentum_pipeline.py",
    "calculation_helper.py",
    "validation.py",
    "spread.py",
    "correlation.py",
    "momentum.py",
    "source_freshness.py",
    "run_lock.py",
    "run_relationships.py",
)
SHARED += (
    "notifications/__init__.py",
    "notifications/telegram.py",
    "notifications/lifecycle.py",
    "notifications/changes.py",
)
SHARED_TESTS = (
    "test_calculations.py",
    "test_io.py",
    "test_momentum.py",
    "test_momentum_updates.py",
    "test_helpers_growth.py",
    "test_request_progress.py",
    "test_datasets.py",
)
WRAPPER = '''#!/usr/bin/env python3
"""Standalone entry point for coincident and leading relationship histories."""
from run_relationships import main

if __name__ == '__main__':
    raise SystemExit(main())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", type=Path, default=ROOT.parent / "us_spread_correlation_analysis"
    )
    parser.add_argument(
        "--check", action="store_true", help="Fail if the shared source files differ"
    )
    args = parser.parse_args()
    if not args.target.is_dir():
        raise ValueError("Target project folder does not exist")
    pairs = [(ROOT / name, args.target / name) for name in SHARED] + [
        (ROOT / "tests" / name, args.target / "tests" / name) for name in SHARED_TESTS
    ]
    mismatches = [
        str(dst.relative_to(args.target))
        for src, dst in pairs
        if not dst.exists() or src.read_bytes() != dst.read_bytes()
    ]
    if args.check:
        if mismatches:
            raise SystemExit("Shared files differ: " + ", ".join(mismatches))
        if (args.target / "main.py").read_text() != WRAPPER:
            raise SystemExit("Standalone entry point differs")
        print("Shared engines and tests are identical")
        return
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (args.target / "main.py").write_text(WRAPPER)
    print("Synced relationship engines and tests to", args.target)


if __name__ == "__main__":
    main()
