"""Pull all four data sources for reconciliation.

Reads config from .secrets/urls.json at the repo root. Writes the raw
responses to .secrets/pulls/<ISO-timestamp>/ and prints the directory path
as the last line of stdout so callers (skill, scheduled task) can find it.

Sources:
    1. HA add-on snapshot (data.json + non-secret options) via /internal/snapshot
    2. Airbnb iCal (URL taken from snapshot or .secrets/urls.json override)
    3. Google Calendar iCal (secret URL from .secrets/urls.json)
    4. WhatsApp message archive — included in the HA snapshot under data.messages
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"
URLS_FILE = SECRETS / "urls.json"
PULLS_DIR = SECRETS / "pulls"


def _fetch(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def main() -> int:
    if not URLS_FILE.exists():
        print(f"error: {URLS_FILE} not found. Copy urls.json.example and fill in.", file=sys.stderr)
        return 2

    cfg = json.loads(URLS_FILE.read_text())
    ha_url = cfg.get("ha_snapshot_url")
    ha_secret = cfg.get("ha_shared_secret", "")
    airbnb_override = cfg.get("airbnb_ical_url") or ""
    gcal_url = cfg.get("gcal_ical_url") or ""

    if not ha_url:
        print("error: ha_snapshot_url missing in urls.json", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = PULLS_DIR / stamp
    out.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []

    # 1. HA snapshot (includes WhatsApp archive under data.messages)
    try:
        headers = {"X-Shared-Secret": ha_secret} if ha_secret else {}
        raw = _fetch(ha_url, headers=headers)
        (out / "ha_snapshot.json").write_bytes(raw)
        snapshot = json.loads(raw)
    except Exception as e:
        errors.append(f"ha_snapshot: {e}")
        snapshot = {}

    # 2. Airbnb iCal
    airbnb_url = airbnb_override or snapshot.get("options", {}).get("ical_url", "")
    if airbnb_url:
        try:
            (out / "airbnb.ics").write_bytes(_fetch(airbnb_url))
        except Exception as e:
            errors.append(f"airbnb_ical: {e}")
    else:
        errors.append("airbnb_ical: no URL (not in snapshot, no override)")

    # 3. Google Calendar iCal
    if gcal_url:
        try:
            (out / "gcal.ics").write_bytes(_fetch(gcal_url))
        except Exception as e:
            errors.append(f"gcal_ical: {e}")
    else:
        errors.append("gcal_ical: no URL in urls.json")

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": sorted(p.name for p in out.iterdir()),
        "errors": errors,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    for e in errors:
        print(f"warn: {e}", file=sys.stderr)

    # Last line of stdout is the pull directory — skills/scripts parse this.
    print(str(out))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
