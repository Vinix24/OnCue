"""Cron-friendly data-retention auto-purge.

Reads ``DATA_RETENTION_DAYS`` and purges all sessions older than the retention
window via the existing ``purge_before_date``. No-op (exit 0) when retention is
disabled. Default is a dry run; pass ``--confirm`` to actually delete.

Usage:
    .venv/bin/python scripts/retention_purge.py [--confirm]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from purge_session import purge_before_date  # noqa: E402

from sales_copilot.core.retention import (  # noqa: E402
    resolve_retention_days,
    retention_cutoff,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Data-retention auto-purge")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete (default: dry run)",
    )
    args = parser.parse_args()

    cutoff = retention_cutoff()
    if cutoff is None:
        print(
            f"Retention disabled (DATA_RETENTION_DAYS={resolve_retention_days()}); "
            "nothing to purge."
        )
        return 0

    mode = "[LIVE]" if args.confirm else "[DRY-RUN]"
    print(f"{mode} Retention purge: sessions started before {cutoff.isoformat()}")
    totals = purge_before_date(cutoff, dry_run=not args.confirm)
    for key, count in totals.items():
        print(f"  {key}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
