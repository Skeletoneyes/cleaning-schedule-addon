"""Bridge liveness watchdog — container state, not message traffic.

Why this exists (2026-07-28 → 2026-08-02, five days blind):
The WhatsApp Bridge add-on flapped (four `405` disconnects in thirty minutes),
its reconnect path spawned overlapping sockets, and the container ended up
`stopped`. Supervisor's own watchdog was off, so nothing restarted it. The
tracker meanwhile looked completely healthy: iCal sync fine, calendar push
fine, reconcile fine. The only symptom was an *absence* of WhatsApp messages,
and the two detectors that watch for absence are threshold-based — whole-bridge
silence at 7 days, per-channel at 14. A cleaning got reassigned to a different
cleaner at a different time inside that window and the shared calendar never
heard about it.

The lesson is the design rule here: **absence of traffic is a lagging,
ambiguous signal.** A quiet chat and a dead pipe look identical, and you cannot
shorten the threshold to fix it without alarming every time nobody talks
overnight. So this module does not look at messages at all. It asks the
Supervisor what state the container is in, hourly, and that answer is
unambiguous within the hour.

Two things follow, and both are deliberate:

- **It heals without asking.** Starting a stopped add-on is reversible and is
  what a human would do anyway; making it wait for a human is what turned a
  crash into a five-day outage.
- **A restart is not a fix, and must never read like one.** Messages that
  arrived while the bridge was down are gone — WhatsApp does not replay them to
  a linked device, and the bridge's own reconnect logic discards anything
  timestamped before process start. So every outage leaves a `blind window`
  finding that keeps appearing in the nightly Telegram digest until it is
  explicitly dismissed. Recovery is silent; *loss* is not.
"""

from datetime import datetime, timedelta
from pathlib import Path
import json

import requests

SUPERVISOR_BASE = "http://supervisor"

# A stopped add-on is the whole point, but "starting"/"unknown" are transient
# and should not trigger a heal on the first sighting — a restart mid-check
# would otherwise ping-pong against Supervisor.
HEALTHY_STATES = {"started"}
TRANSIENT_STATES = {"startup", "starting", "unknown"}


# ── State persistence ───────────────────────────────────────────────────────

def _default_state():
    return {
        "last_check": None,
        "last_state": None,
        "outage": None,
        "blind_windows": [],
        "probe_error": None,
        "checks": 0,
        "heals": 0,
    }


def load_state(path):
    """Read the watchdog's own record. A corrupt file resets rather than
    raising: this runs on a timer thread, and a watchdog that dies on its own
    state file is worse than one that forgets."""
    p = Path(path)
    if not p.exists():
        return _default_state()
    try:
        state = json.loads(p.read_text())
        base = _default_state()
        base.update(state if isinstance(state, dict) else {})
        return base
    except (OSError, ValueError):
        return _default_state()


def save_state(path, state):
    try:
        Path(path).write_text(json.dumps(state, indent=2))
    except OSError as e:
        print(f"[watchdog] failed to persist state: {e}")


# ── Supervisor calls ────────────────────────────────────────────────────────

def _supervisor_get_state(slug, token, timeout=10):
    r = requests.get(
        f"{SUPERVISOR_BASE}/addons/{slug}/info",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if r.status_code // 100 != 2:
        raise RuntimeError(f"info HTTP {r.status_code}: {r.text[:160]}")
    return (r.json().get("data") or {}).get("state")


def _supervisor_start(slug, token, timeout=90):
    """Start a stopped add-on. `start` rather than `restart` on purpose — a
    restart of an already-stopped add-on is an error on some Supervisor
    versions, and we only ever call this when it is not running."""
    r = requests.post(
        f"{SUPERVISOR_BASE}/addons/{slug}/start",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if r.status_code // 100 != 2:
        raise RuntimeError(f"start HTTP {r.status_code}: {r.text[:160]}")
    return True


# ── The check ───────────────────────────────────────────────────────────────

def check(state_path, slug, token, last_message_at=None, now=None, heal=True):
    """One watchdog pass. Returns the updated state dict.

    `last_message_at` is the timestamp of the newest WhatsApp message the
    tracker has stored. It is used only to describe the blind window — the
    check itself never consults message traffic.
    """
    now = now or datetime.now()
    now_s = now.strftime("%Y-%m-%dT%H:%M:%S")
    state = load_state(state_path)
    state["last_check"] = now_s
    state["checks"] = int(state.get("checks") or 0) + 1

    if not token:
        # Fail loud. A watchdog that silently never ran is precisely the
        # failure mode this whole module exists to end, so an unusable token
        # must produce a finding rather than a quiet no-op.
        state["probe_error"] = (
            "no SUPERVISOR_TOKEN — the add-on cannot ask Supervisor for the "
            "bridge's state. Check hassio_api/hassio_role in config.yaml."
        )
        save_state(state_path, state)
        return state

    try:
        current = _supervisor_get_state(slug, token)
        state["probe_error"] = None
    except Exception as e:
        state["probe_error"] = f"could not read bridge state: {e}"
        save_state(state_path, state)
        return state

    state["last_state"] = current

    if current in HEALTHY_STATES:
        outage = state.get("outage")
        if outage:
            # Recovery. Close the blind window and keep it — the messages that
            # arrived during it are unrecoverable, so this is the only record
            # that they were ever missed.
            outage["recovered_at"] = now_s
            window = {
                "from": outage.get("last_message_at") or outage.get("detected_at"),
                "to": now_s,
                "detected_at": outage.get("detected_at"),
                "recovered_at": now_s,
                "restarts": int(outage.get("restart_attempts") or 0),
                "acknowledged": False,
            }
            state.setdefault("blind_windows", []).append(window)
            state["outage"] = None
            print(f"[watchdog] bridge recovered — blind window {window['from']} → {window['to']}")
        save_state(state_path, state)
        return state

    if current in TRANSIENT_STATES:
        # Mid-transition. Record but do not act; the next pass decides.
        save_state(state_path, state)
        return state

    # Not running.
    outage = state.get("outage")
    if not outage:
        outage = {
            "detected_at": now_s,
            "last_message_at": last_message_at,
            "restart_attempts": 0,
            "observed_state": current,
        }
        state["outage"] = outage
        print(f"[watchdog] bridge is '{current}' — outage opened")

    if heal:
        try:
            _supervisor_start(slug, token)
            outage["restart_attempts"] = int(outage.get("restart_attempts") or 0) + 1
            outage["last_restart_at"] = now_s
            state["heals"] = int(state.get("heals") or 0) + 1
            print(f"[watchdog] started bridge (attempt {outage['restart_attempts']})")
        except Exception as e:
            outage["last_restart_error"] = str(e)
            print(f"[watchdog] start FAILED: {e}")

    save_state(state_path, state)
    return state


# ── Findings ────────────────────────────────────────────────────────────────

def _age_days(iso_ts, today):
    try:
        return max(0, (today - datetime.fromisoformat(iso_ts)).days)
    except (TypeError, ValueError):
        return None


def findings(state, today_str, now=None):
    """Reconciler findings derived from watchdog state.

    These are ordinary findings, so they ride the existing nightly digest →
    Telegram path and the existing dismiss mechanism. A blind window persists
    until dismissed on purpose — that is the "keep telling me until it's
    fixed" requirement, implemented as data rather than as a second alerting
    system that could itself go quiet.
    """
    now = now or datetime.now()
    out = []

    if state.get("probe_error"):
        out.append({
            "id": "bridge_watchdog_error",
            "detector": "bridge_watchdog",
            "kind": "bridge_watchdog_error",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"The bridge watchdog cannot check whether the WhatsApp bridge is "
                f"running: {state['probe_error']} Until this is fixed the bridge "
                f"could be down without anything noticing."
            ),
            "evidence": [],
        })

    outage = state.get("outage")
    if outage:
        attempts = int(outage.get("restart_attempts") or 0)
        age = _age_days(outage.get("detected_at"), now)
        age_txt = f" (down for {age} day(s))" if age else ""
        err = outage.get("last_restart_error")
        tail = (
            f" Automatic restart failed: {err}"
            if err else
            f" Restarted automatically {attempts} time(s) and it is still not running."
            if attempts else ""
        )
        out.append({
            "id": "bridge_down",
            "detector": "bridge_watchdog",
            "kind": "bridge_down",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"The WhatsApp bridge add-on is '{outage.get('observed_state')}' and NOT "
                f"receiving messages{age_txt}.{tail} Anything the cleaners send right now "
                f"is being lost permanently."
            ),
            "evidence": [],
        })

    for w in state.get("blind_windows", []):
        if w.get("acknowledged"):
            continue
        start = (w.get("from") or "?")[:16].replace("T", " ")
        end = (w.get("to") or "?")[:16].replace("T", " ")
        age = _age_days(w.get("to"), now)
        age_txt = f", flagged {age} day(s) ago" if age else ""
        out.append({
            # Keyed on the window start so it is stable across nights — a
            # changing id would look like a brand-new finding every evening and
            # could never be dismissed.
            "id": f"bridge_blind:{w.get('from')}",
            "detector": "bridge_watchdog",
            "kind": "bridge_blind_window",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"WhatsApp was not being received between {start} and {end}"
                f"{age_txt}. The bridge was restarted automatically and is working now, "
                f"but messages sent during that window were never delivered and cannot "
                f"be recovered — check both cleaner chats on your phone for anything "
                f"agreed in that period, then dismiss this finding."
            ),
            "evidence": [],
        })

    return out


def acknowledge_window(state_path, window_from):
    """Mark one blind window acknowledged so it stops appearing nightly."""
    state = load_state(state_path)
    hit = False
    for w in state.get("blind_windows", []):
        if w.get("from") == window_from:
            w["acknowledged"] = True
            hit = True
    if hit:
        save_state(state_path, state)
    return hit
