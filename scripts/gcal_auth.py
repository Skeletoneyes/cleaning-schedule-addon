"""Validate a Google Calendar service-account key and print setup instructions.

Run this on a laptop (or anywhere) after creating a service account in Google
Cloud Console and downloading its JSON key. This script only verifies the key
looks sane and reminds you what to do with it — there is no OAuth flow.

Usage:
    python scripts/gcal_auth.py [path/to/service-account.json]

If no path is given, scans scripts/ and GCalOauth/ for a *.json file.

Setup recap:
    1. Cloud Console → IAM & Admin → Service Accounts → Create service account.
    2. Keys tab → Add key → Create new key → JSON → download.
    3. In Google Calendar, open the target calendar's Settings and sharing,
       add the service account's email under "Share with specific people",
       permission "Make changes to events".
    4. Paste the JSON blob into the add-on option `gcal_service_account_json`,
       and the calendar ID into `gcal_calendar_id`.
"""

import json
import sys
from pathlib import Path


def find_key() -> Path | None:
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        *sorted((repo_root / "GCalOauth").glob("*.json")),
        *sorted((Path(__file__).parent).glob("*.json")),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def main():
    key_path = Path(sys.argv[1]) if len(sys.argv) > 1 else find_key()
    if not key_path or not key_path.exists():
        print("Missing service-account JSON. Pass path as arg or drop it in scripts/ or GCalOauth/.")
        sys.exit(1)
    print(f"Using {key_path}")

    with open(key_path) as f:
        info = json.load(f)

    if info.get("type") != "service_account":
        print(f"ERROR: expected type=service_account, got {info.get('type')!r}. Wrong file?")
        sys.exit(1)

    email = info.get("client_email", "")
    project = info.get("project_id", "")
    if not email:
        print("ERROR: no client_email in JSON")
        sys.exit(1)

    print()
    print("=" * 70)
    print("Service account looks good.")
    print("=" * 70)
    print(f"Project:        {project}")
    print(f"Service email:  {email}")
    print()
    print("Next steps:")
    print(f"  1. Share the target calendar with {email}")
    print("     (Google Calendar -> calendar settings -> Share with specific people ->")
    print("      Add people -> permission: 'Make changes to events')")
    print("  2. Copy the calendar ID from that same settings page.")
    print("  3. In the HA add-on options, set:")
    print("     gcal_enabled:              true")
    print("     gcal_calendar_id:          <calendar ID from step 2>")
    print("     gcal_service_account_json: <paste entire JSON blob below>")
    print("=" * 70)
    print()
    print("── JSON to paste into gcal_service_account_json ──")
    print(json.dumps(info))


if __name__ == "__main__":
    main()
