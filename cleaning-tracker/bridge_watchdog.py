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
import os
import threading

import requests

# `check()` is load-mutate-save on a whole file, and it has two callers in one
# process: the scheduler thread and `POST /internal/watchdog/check` on a Flask
# request thread. That route exists precisely to be hit by a human *while* the
# timer keeps ticking, and the timer now ticks every 5 minutes instead of 60.
# Unguarded, the loser of an interleave silently overwrites the winner's events
# — erasing entries from the incident record this module exists to make
# trustworthy. Counters could survive that; an audit trail cannot.
_STATE_LOCK = threading.RLock()

SUPERVISOR_BASE = "http://supervisor"

# A stopped add-on is the whole point, but "starting"/"unknown" are transient
# and should not trigger a heal on the first sighting — a restart mid-check
# would otherwise ping-pong against Supervisor.
HEALTHY_STATES = {"started"}
TRANSIENT_STATES = {"startup", "starting", "unknown"}

# Every check is recorded, not only the ones where something changed.
#
# The first cut of this logged transitions only, reasoning that a 5-minute poll
# would otherwise write 288 rows a day and bury the handful that matter. That
# was the wrong trade: a sparse log that is empty reads *identically* whether
# the bridge was rock solid or the watchdog never ran at all. Absence of
# incidents is not evidence of stability — it is the same ambiguity this module
# was built to end, one level up. Logging every pass makes uptime positively
# verifiable: 8,640 consecutive `started / none` rows is a claim with evidence
# behind it, where an empty file is only a claim.
#
# Retention is a trailing window rather than a row cap, because the question is
# "how stable has it been *recently*", which is a question about time.
CHECK_LOG_RETENTION_DAYS = 30
# Pruning rewrites the whole file, so it must not run on every pass. Once per
# ~day at a 5-minute interval.
PRUNE_EVERY_N_CHECKS = 288


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
    """Persist atomically — temp file then `os.replace`.

    A plain `write_text` truncates before it writes, so a container restart
    landing mid-write leaves a half-file; `load_state` then swallows the
    `ValueError` and silently resets to defaults, discarding the whole incident
    history. Add-on restarts are routine here (`ha addons update`, host reboot,
    the watchdog's own healing), which makes that a likely loss, not a
    theoretical one. Same temp+replace pattern the GCal and sync status
    sidecars in `app.py` already use.
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, p)
    except OSError as e:
        print(f"[watchdog] failed to persist state: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ── The check log (one line per pass, JSONL) ────────────────────────────────

def _log_check(log_path, record):
    """Append one line. JSONL and append-mode on purpose: at 5-minute polling a
    30-day window is ~8,640 records, and rewriting that whole file every pass
    would be ~700 KB of writes every 5 minutes on a Pi's SSD. An append is O(1)
    and costs one line regardless of how much history is behind it.

    Never raises — failing to record a check must not fail the check.

    Repairs a missing trailing newline before appending. Without that, a write
    torn by a container kill leaves a line with no terminator, and the *next*
    append concatenates onto it — so one interrupted write would destroy two
    records instead of one, and the casualty would be the newer one. Found by
    `test_a_torn_line_does_not_break_the_history`, not by reasoning.
    """
    try:
        with open(log_path, "a+") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end:
                f.seek(end - 1)
                if f.read(1) != "\n":
                    f.write("\n")
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"[watchdog] failed to append check log: {e}")


def read_checks(log_path, days=None, now=None):
    """Return check records (oldest first), optionally limited to a window.

    A malformed line is skipped rather than fatal — a torn final line from a
    kill mid-append must not make the whole history unreadable.
    """
    p = Path(log_path)
    if not p.exists():
        return []
    cutoff = None
    if days is not None:
        cutoff = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if cutoff and (rec.get("at") or "") < cutoff:
                    continue
                out.append(rec)
    except OSError as e:
        print(f"[watchdog] failed to read check log: {e}")
    return out


def prune_checks(log_path, days=CHECK_LOG_RETENTION_DAYS, now=None):
    """Drop records older than the retention window. Rewrites via temp+replace
    so an interrupted prune cannot truncate the history it is preserving."""
    p = Path(log_path)
    if not p.exists():
        return 0
    kept = read_checks(p, days=days, now=now)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            for rec in kept:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        os.replace(tmp, p)
    except OSError as e:
        print(f"[watchdog] failed to prune check log: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return len(kept)


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

def check(state_path, slug, token, last_message_at=None, now=None, heal=True,
          log_path=None):
    """One watchdog pass. Returns the updated state dict.

    `last_message_at` is the timestamp of the newest WhatsApp message the
    tracker has stored. It is used only to describe the blind window — the
    check itself never consults message traffic.

    `log_path`, when given, receives exactly ONE record per pass — including
    the passes where nothing happened, which are the ones that evidence
    stability. Written after the state save so a logged check is always a check
    that actually completed.

    Serialized on `_STATE_LOCK`: the whole load-mutate-save must be one
    critical section, not just the write.
    """
    with _STATE_LOCK:
        now = now or datetime.now()
        state, record = _check_locked(state_path, slug, token, last_message_at, now, heal)
        if log_path:
            _log_check(log_path, record)
            if int(state.get("checks") or 0) % PRUNE_EVERY_N_CHECKS == 0:
                prune_checks(log_path, now=now)
        return state


def _check_locked(state_path, slug, token, last_message_at, now, heal):
    """Returns `(state, record)`. Every exit path produces a record, so the log
    can never silently omit a pass — an omitted pass is indistinguishable from
    a watchdog that stopped running."""
    now_s = now.strftime("%Y-%m-%dT%H:%M:%S")
    state = load_state(state_path)
    state["last_check"] = now_s
    state["checks"] = int(state.get("checks") or 0) + 1
    previous = state.get("last_state")

    def _done(observed, action, **extra):
        save_state(state_path, state)
        record = {"at": now_s, "state": observed, "action": action}
        if previous is not None and observed != previous:
            record["from_state"] = previous
        record.update({k: v for k, v in extra.items() if v is not None})
        return state, record

    if not token:
        # Fail loud. A watchdog that silently never ran is precisely the
        # failure mode this whole module exists to end, so an unusable token
        # must produce a finding rather than a quiet no-op.
        state["probe_error"] = (
            "no SUPERVISOR_TOKEN — the add-on cannot ask Supervisor for the "
            "bridge's state. Check hassio_api/hassio_role in config.yaml."
        )
        return _done(None, "no_token", detail=state["probe_error"])

    try:
        current = _supervisor_get_state(slug, token)
        state["probe_error"] = None
    except Exception as e:
        state["probe_error"] = f"could not read bridge state: {e}"
        return _done(None, "probe_failed", detail=str(e))

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
            return _done(current, "recovered",
                         detail=f"blind window {window['from']} → {window['to']}",
                         restarts=window["restarts"])
        return _done(current, "none")

    if current in TRANSIENT_STATES:
        # Mid-transition. Record but do not act; the next pass decides.
        return _done(current, "waited")

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

    if not heal:
        return _done(current, "observed_down")

    try:
        _supervisor_start(slug, token)
        outage["restart_attempts"] = int(outage.get("restart_attempts") or 0) + 1
        outage["last_restart_at"] = now_s
        state["heals"] = int(state.get("heals") or 0) + 1
        print(f"[watchdog] started bridge (attempt {outage['restart_attempts']})")
        return _done(current, "restarted", attempt=outage["restart_attempts"])
    except Exception as e:
        outage["last_restart_error"] = str(e)
        print(f"[watchdog] start FAILED: {e}")
        return _done(current, "restart_failed", detail=str(e))


# ── Reporting ───────────────────────────────────────────────────────────────

def summary(state, log_path=None, now=None, days=CHECK_LOG_RETENTION_DAYS):
    """"How stable has the bridge been lately" — derived from the check log.

    Every figure here is a count of *observations*, not an inference from their
    absence: `checks` is how many times we actually looked, and `healthy_pct` is
    the share of those looks that found it running. That distinction is the
    whole point of logging every pass — a watchdog that stopped running shows up
    as missing checks rather than as apparent perfect health.

    ⚠️ `restarts_*` still undercounts, and `caveat` says so out loud rather than
    letting a reassuring number stand unqualified. The check is a poll, so a
    crash that Supervisor's own add-on watchdog repairs between two polls is
    never observed — the bridge reads `started` before and `started` after.
    What is counted is restarts *this watchdog performed*. Closing that gap
    needs the bridge to report its own process start, which is a change to the
    bridge add-on, not to this one.
    """
    now = now or datetime.now()
    records = read_checks(log_path, days=days, now=now) if log_path else []

    def _at(rec):
        try:
            return datetime.fromisoformat(rec["at"])
        except (KeyError, TypeError, ValueError):
            return None

    def _count(pred, within_days=None):
        cutoff = now - timedelta(days=within_days) if within_days else None
        n = 0
        for r in records:
            if not pred(r):
                continue
            if cutoff is not None:
                ts = _at(r)
                if ts is None or ts < cutoff:
                    continue
            n += 1
        return n

    is_restart = lambda r: r.get("action") == "restarted"
    # An episode starts on the transition INTO a non-healthy state, which is the
    # only record that carries a healthy `from_state`.
    is_episode = lambda r: (r.get("from_state") in HEALTHY_STATES
                            and r.get("state") not in HEALTHY_STATES)

    observed = [r for r in records if r.get("state")]
    healthy = [r for r in observed if r.get("state") in HEALTHY_STATES]
    restarts = [r for r in records if is_restart(r)]
    actions = {}
    for r in records:
        a = r.get("action") or "unknown"
        actions[a] = actions.get(a, 0) + 1

    return {
        "window_days": days,
        "checks_logged": len(records),
        "first_check": records[0].get("at") if records else None,
        "last_check": state.get("last_check"),
        "last_state": state.get("last_state"),
        "healthy": state.get("last_state") in HEALTHY_STATES and not state.get("outage"),
        "healthy_checks": len(healthy),
        "healthy_pct": round(100.0 * len(healthy) / len(observed), 2) if observed else None,
        "actions": actions,
        "restarts_24h": _count(is_restart, 1),
        "restarts_7d": _count(is_restart, 7),
        "restarts_30d": _count(is_restart, 30),
        "last_restart_at": restarts[-1].get("at") if restarts else None,
        "down_episodes_7d": _count(is_episode, 7),
        "down_episodes_30d": _count(is_episode, 30),
        "probe_failures_7d": _count(lambda r: r.get("action") in ("probe_failed", "no_token"), 7),
        "checks_lifetime": int(state.get("checks") or 0),
        "restarts_lifetime": int(state.get("heals") or 0),
        "open_outage": bool(state.get("outage")),
        "unacknowledged_blind_windows": len(
            [w for w in state.get("blind_windows", []) if not w.get("acknowledged")]
        ),
        "probe_error": state.get("probe_error"),
        "caveat": (
            "Counts restarts performed by this watchdog only. A crash repaired "
            "by Supervisor's own add-on watchdog between two polls is invisible "
            "here — the true restart count is >= this one."
        ),
    }


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
