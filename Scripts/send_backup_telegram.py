"""Deliver minimal Fund Server backup outcomes through the existing Telegram bot."""

import argparse
import sys
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_ROOT = SERVER_ROOT / "Code" / "us_complete_pipeline"
sys.path.insert(0, str(PIPELINE_ROOT))

from notifications.telegram import send_telegram  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", choices=("success", "failure"), required=True)
    parser.add_argument("--commit", default="")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.status == "success":
        message = "Fund Server is backed up to GitHub."
        if args.commit:
            message += f"\nCommit: {args.commit}"
        else:
            message += "\nNo changes were needed; main is already synchronized."
        delivered = send_telegram(message, "Fund Server backup")
    else:
        delivered = send_telegram(
            "Fund Server GitHub backup failed. Check C:\\Fund_Server\\Logs\\github_backup.log.",
            "Fund Server backup failed",
        )

    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
