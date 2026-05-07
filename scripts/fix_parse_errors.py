"""Reset or ignore messages that failed with a parse error.

Usage:
    python scripts/fix_parse_errors.py [--cutoff-days N] [--dry-run] [data.json path]

The script reads data.json (from the HA add-on snapshot or a local copy),
then for every message where parse_error is set:

  - Age > cutoff (default 90 days): mark review_state="ignored", keep parsed=True
    (disappears from the Review tab permanently)
  - Age <= cutoff: reset parsed=False, parse_error=None
    (add-on worker will retry on next restart)

After running, upload the patched file back to the HA host:
    scp data.json root@homeassistant.local:/mnt/data/addons/local_cleaning-tracker/data/data.json
Or paste it into the add-on's /data/data.json via the HA file editor.

If you pass the path to a ha_snapshot.json pulled by reconcile_pull.py,
the script reads from snapshot["data"] and writes a patched copy.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default="data.json",
                    help="Path to data.json or ha_snapshot.json (default: data.json)")
    ap.add_argument("--cutoff-days", type=int, default=90,
                    help="Messages older than this many days are ignored rather than retried (default: 90)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change but don't write anything")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2

    raw = json.loads(path.read_text(encoding="utf-8"))

    # Support both bare data.json and ha_snapshot.json (which wraps under "data")
    is_snapshot = "data" in raw and "messages" not in raw
    data = raw["data"] if is_snapshot else raw

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=args.cutoff_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    retry = []
    ignore = []
    skipped = []

    for m in data.get("messages", []):
        if not m.get("parse_error"):
            continue
        ts = m.get("timestamp") or ""
        dt = parse_ts(ts)
        if dt is None:
            # No parseable timestamp — use raw string comparison as fallback
            old = ts < cutoff_str if ts else True
        else:
            old = dt < cutoff

        if old:
            ignore.append(m)
        else:
            retry.append(m)

    print(f"Cutoff: {args.cutoff_days} days ({cutoff_str})")
    print(f"  Will ignore (old):  {len(ignore)}")
    print(f"  Will retry (recent): {len(retry)}")

    if args.dry_run:
        print("\n-- DRY RUN: no changes written --")
        if ignore:
            print("\nSample ignore (first 5):")
            for m in ignore[:5]:
                print(f"  [{m.get('timestamp','')}] {m.get('sender','')} -- {str(m.get('text',''))[:60]}".encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
        if retry:
            print("\nSample retry (first 5):")
            for m in retry[:5]:
                print(f"  [{m.get('timestamp','')}] {m.get('sender','')} -- {str(m.get('text',''))[:60]}".encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
        return 0

    for m in ignore:
        m["review_state"] = "ignored"

    for m in retry:
        m["parsed"] = False
        m["parse_error"] = None

    if is_snapshot:
        raw["data"] = data
        out = raw
    else:
        out = data

    out_path = path.with_suffix(".patched.json") if not args.dry_run else path
    path.with_suffix(".patched.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nWrote patched file to: {path.with_suffix('.patched.json')}")
    print("\nNext steps:")
    print("  1. Review the patched file")
    print("  2. Copy it to the HA host as /data/data.json")
    print("  3. Restart the add-on — the worker will pick up the retry queue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
