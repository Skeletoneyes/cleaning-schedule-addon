"""Fault-injection probe for the 1.25.0 GCal push-failure alarm (ISC-81..84).

An untested alarm is indistinguishable from no alarm. This deliberately breaks
the Google Calendar push, walks the pipeline, and asserts the failure reaches
the Telegram digest — then restores the real config and asserts recovery.

Injection method is chosen to leave NO residue: it points the add-on at a
non-existent calendar id, so every Calendar API call 404s at the *list* step.
No write ever reaches the real shared calendar, so Michelle and the cleaners
never see a probe artifact.

Usage (from the repo root, with .secrets/urls.json populated):
    python3 scripts/probe_push_alarm.py break
    python3 scripts/probe_push_alarm.py check
    python3 scripts/probe_push_alarm.py restore

`break` and `restore` print the exact SSH command to run — they never touch the
Supervisor themselves, because a script that can silently rewrite add-on options
is a worse hazard than the bug it is testing.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URLS = ROOT / ".secrets" / "urls.json"
ADDON = "27cbea7f_cleaning-tracker"
BAD_CALENDAR_ID = "cleaning-tracker-probe-nonexistent@group.calendar.google.com"


def _cfg():
    if not URLS.exists():
        sys.exit(f"error: {URLS} not found")
    return json.loads(URLS.read_text())


def _base():
    cfg = _cfg()
    url = cfg["ha_snapshot_url"]
    return url.rsplit("/internal/snapshot", 1)[0], cfg.get("ha_shared_secret", "")


def _get(path):
    base, secret = _base()
    req = urllib.request.Request(base + path, headers={"X-Shared-Secret": secret})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _post(path):
    base, secret = _base()
    req = urllib.request.Request(base + path, data=b"", method="POST",
                                 headers={"X-Shared-Secret": secret})
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read()
    return json.loads(body) if body.strip().startswith(b"{") else {"status": r.status}


def _options_cmd(calendar_id):
    """Print the merge-safe Supervisor options POST.

    Merge-safe matters: a partial options body wipes unrelated keys (the API
    key, the iCal URL). This fetches the full current options, replaces one
    field, and posts the complete set back.
    """
    return (
        f'ssh -p 22 -i ~/.ssh/id_ed25519_ha root@192.168.0.95 '
        f'\'OPTS=$(curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" '
        f'http://supervisor/addons/{ADDON}/info | sed -n "s/.*\\"options\\"://p"); '
        f'echo "fetch options, set gcal_calendar_id to {calendar_id}, POST the merged set, '
        f'then: ha addons restart {ADDON}"\''
    )


def cmd_break():
    print("STEP 1 — point the add-on at a non-existent calendar (no real writes occur):")
    print(f"  gcal_calendar_id -> {BAD_CALENDAR_ID}")
    print(_options_cmd(BAD_CALENDAR_ID))
    print("\nSTEP 2 — restart, then run: python3 scripts/probe_push_alarm.py check")


def cmd_check():
    print("→ triggering a push via POST /gcal/sync")
    try:
        _post("/gcal/sync")
    except Exception as e:
        print(f"  (push route returned {e} — expected while broken)")

    snap = _get("/internal/snapshot")
    status = snap.get("gcal_push_status")
    print(f"\ngcal_push_status: {json.dumps(status, indent=2)}")
    assert status is not None, "FAIL ISC-50: no status persisted"
    assert status.get("ok") is False, "FAIL ISC-50: broken push recorded as ok"
    assert status.get("error"), "FAIL ISC-50: no error string recorded"
    print("PASS ISC-50 — failed push persisted with an error string")

    print("\n→ running a fresh reconcile")
    _post("/reconcile/run")
    res = _get("/reconcile/last")
    kinds = {f["kind"] for f in res["findings"]}
    print(f"counts: {res['counts']}")
    assert "gcal_push_failed" in kinds, f"FAIL ISC-81: no gcal_push_failed finding (got {kinds})"
    print("PASS ISC-81 — gcal_push_failed finding present")
    assert "gcal_missing_event" not in kinds, "FAIL ISC-69: drift findings not absorbed"
    print("PASS ISC-69 — calendar-content findings absorbed into the root cause")
    assert res["counts"]["total"] == len(res["findings"]), "FAIL ISC-64: counts disagree"
    print("PASS ISC-64 — counts agree with findings")

    failed = [f for f in res["findings"] if f["kind"] == "gcal_push_failed"][0]
    print(f"\nwhy: {failed['why']}")

    print("\n→ running the digest (this sends a real Telegram message)")
    print(json.dumps(_post("/digest/run"), indent=2))
    print("\nNow check the VPS journal for delivery, then run: "
          "python3 scripts/probe_push_alarm.py restore")


def cmd_restore():
    print("STEP 1 — restore the real calendar id (value is in the HA UI / your notes;")
    print("         it is deliberately NOT stored in this public repo):")
    print(_options_cmd("<REAL_CALENDAR_ID>"))
    print("\nSTEP 2 — restart, then verify:")
    print("  python3 scripts/probe_push_alarm.py verify")


def cmd_verify():
    print("→ triggering a push")
    _post("/gcal/sync")
    snap = _get("/internal/snapshot")
    status = snap.get("gcal_push_status")
    print(f"gcal_push_status: {json.dumps(status, indent=2)}")
    assert status.get("ok") is True, "FAIL ISC-83: push did not recover"
    print("PASS ISC-83 — push recovered, ok: true")

    _post("/reconcile/run")
    res = _get("/reconcile/last")
    kinds = {f["kind"] for f in res["findings"]}
    print(f"counts: {res['counts']}")
    assert "gcal_push_failed" not in kinds, "FAIL ISC-83: finding did not clear"
    assert "stale_push" not in kinds, "FAIL ISC-83: staleness did not clear"
    print("PASS ISC-83 — push-health findings cleared")


COMMANDS = {"break": cmd_break, "check": cmd_check,
            "restore": cmd_restore, "verify": cmd_verify}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(COMMANDS)}}}")
    COMMANDS[sys.argv[1]]()
