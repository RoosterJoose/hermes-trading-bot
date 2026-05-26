#!/usr/bin/env python3
"""Daily cron: export trading data on day 10, 20, 30 milestones.

Checks paper_start_date.txt, calculates days since, and exports
if today matches a milestone day (10, 20, 30).
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timezone, date

TRADING_DIR = "/opt/data/hermes-trading"
EXPORT_SCRIPT = os.path.join(TRADING_DIR, "scripts", "trade_export.py")
EXPORT_DIR = os.path.join(TRADING_DIR, "exports")
PAPER_START_FILE = os.path.join(TRADING_DIR, "state", "paper_start_date.txt")

MILESTONES = {10, 20, 30}


def days_since_start() -> int | None:
    if not os.path.exists(PAPER_START_FILE):
        return None
    start_str = open(PAPER_START_FILE).read().strip()
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = date.today()
    return (today - start).days


def main():
    days = days_since_start()
    if days is None:
        print("ERROR: cannot determine paper start date")
        sys.exit(1)

    day_label = str(days)
    is_milestone = days in MILESTONES

    if not is_milestone:
        print(f"Day {days} — not a milestone ({sorted(MILESTONES)}), skipping")
        return

    print(f"*** DAY {days} MILESTONE — exporting ***")
    result = subprocess.run(
        [EXPORT_SCRIPT, "--day", day_label, "--out-dir", EXPORT_DIR],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"EXPORT FAILED (rc={result.returncode}): {result.stderr}")
        print(result.stdout)
        sys.exit(1)

    print(result.stdout)
    data = json.loads(result.stdout)
    print(f"\n✅ Exported {data['trade_count']} trades to:")
    for k in ["csv", "snapshot", "summary"]:
        print(f"   {data[k]} ({data.get('csv_size_kb', '?')} KB)")


if __name__ == "__main__":
    main()
