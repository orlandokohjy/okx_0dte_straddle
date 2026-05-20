"""
One-shot migration of legacy ``session`` values in ``state/trade_log.csv``.

Background
----------
Until 2026-05-20 the algo had two sessions named ``afternoon`` and
``morning``. The 4-session pct_equity rebuild renamed them to canonical
``utc_HHMM`` form (``utc_1330`` and ``utc_0100`` respectively) and
added two new sessions (``utc_0900`` and ``utc_2330``).

Reports auto-canonicalise legacy names at read time, so historical
rows render correctly even before this migration runs. But analytics
that group by ``session`` directly (Excel pivots, ad-hoc SQL) will see
both forms side-by-side until the CSV is rewritten. This script does
that rewrite, in place, with a timestamped ``.bak`` backup.

Usage
-----
    python tools/migrate_session_names.py            # rewrite live trade_log.csv
    python tools/migrate_session_names.py --dry-run  # show counts only

The script is idempotent: re-running on an already-migrated file is a
no-op (no rows have ``session in {morning, afternoon}``).
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Make the script runnable from any cwd by adding the package root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config


LEGACY_TO_CANONICAL = {
    "morning": "utc_0100",
    "afternoon": "utc_1330",
}


def _backup_path(src: str) -> str:
    """Return a timestamped backup path next to ``src``."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{src}.{stamp}.bak"


def migrate(csv_path: str, *, dry_run: bool = False) -> dict[str, int]:
    """Rewrite ``session`` column legacy → canonical. Returns row counts.

    Counts: ``{rewritten_rows, untouched_rows, total_rows}``.
    """
    if not os.path.exists(csv_path):
        print(f"[migrate] {csv_path} does not exist — nothing to do.")
        return {"rewritten_rows": 0, "untouched_rows": 0, "total_rows": 0}

    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "session" not in reader.fieldnames:
            print(
                f"[migrate] {csv_path} has no 'session' column "
                f"({reader.fieldnames}) — nothing to migrate."
            )
            return {"rewritten_rows": 0, "untouched_rows": 0, "total_rows": 0}
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    rewritten = 0
    for row in rows:
        sess = (row.get("session") or "").strip()
        if sess in LEGACY_TO_CANONICAL:
            row["session"] = LEGACY_TO_CANONICAL[sess]
            rewritten += 1

    counts = {
        "rewritten_rows": rewritten,
        "untouched_rows": len(rows) - rewritten,
        "total_rows": len(rows),
    }

    print(
        f"[migrate] file={csv_path} total_rows={counts['total_rows']} "
        f"rewritten={counts['rewritten_rows']} "
        f"untouched={counts['untouched_rows']}"
    )

    if dry_run:
        print("[migrate] --dry-run set; no files modified.")
        return counts

    if rewritten == 0:
        print("[migrate] No rows to migrate. File left untouched.")
        return counts

    backup = _backup_path(csv_path)
    shutil.copy2(csv_path, backup)
    print(f"[migrate] backup written to {backup}")

    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    os.replace(tmp, csv_path)
    print(f"[migrate] rewrote {csv_path} ({rewritten} rows)")

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite legacy session names in trade_log.csv."
    )
    parser.add_argument(
        "--csv",
        default=config.TRADE_LOG_FILE,
        help=f"Path to trade-log CSV (default: {config.TRADE_LOG_FILE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be rewritten but do not modify the file.",
    )
    args = parser.parse_args(argv)

    counts = migrate(args.csv, dry_run=args.dry_run)
    if counts["rewritten_rows"] > 0 and not args.dry_run:
        print(
            "[migrate] DONE. Verify with: "
            f"head -n 1 {args.csv} && "
            f"awk -F, '{{print $2}}' {args.csv} | sort | uniq -c"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
