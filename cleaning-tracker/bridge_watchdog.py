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
        "link": None,
        "health": None,
        "schema": 2,
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

DEFAULT_LINK_DOWN_ALERT_MIN = 60
DEFAULT_HEARTBEAT_STALE_SEC = 300
DEFAULT_MAX_HEAL_ATTEMPTS = 5
# Decrypt failures inside an allowlisted group. This is a COMPLETENESS fault,
# not a liveness one: the socket is wide open and healthy while one person's
# messages silently fail to decrypt. Daria was muted this way for three months.
# No heartbeat can see it, which is why it stays a separate verdict.
DECRYPT_ALERT_THRESHOLD = 5

# What link monitoring does NOT cover. Stated here on purpose: believing you
# are covered when you are not is worse than knowing you are not.
#
#   - Per-sender sender-key breakage. The socket is open, everything reports
#     healthy, and ONE participant's messages silently never decrypt. Daria was
#     muted this way for three months while other members forwarded fine.
#   - A wrong `group_allowlist`. Messages reach the bridge, are filtered before
#     forwarding, and the link is genuinely up the whole time.
#
# Both are completeness failures, not liveness failures. Liveness is a
# necessary condition for completeness, never the same thing.

def _link_verdict(heartbeat, now, stale_sec):
    """Is the WhatsApp link actually carrying, right now?

    This replaces two signals that both failed on 2026-09-05. Container state
    said `started` while the process crash-looped against a revoked
    registration. Message absence said nothing, because a quiet chat and a dead
    pipe are the same observation.

    Silence is a verdict here, not an abstention: no beat means DOWN. A deadman
    that answers "unknown" when it hears nothing is not a deadman.
    """
    if not heartbeat:
        return {"verdict": "never_seen",
                "reason": "the bridge has never reported its link state"}

    received = heartbeat.get("received_at")
    if not isinstance(received, (int, float)):
        return {"verdict": "down",
                "reason": "heartbeat carries no usable received_at"}

    age = now.timestamp() - received
    # Direction check, not merely a NaN check. A negative age means the stored
    # timestamp is in the future — corruption, or a clock that moved. Reading
    # that as "0 seconds old, all good" is the fail-open that makes a deadman
    # useless, so it is DOWN and it says why.
    if age < 0:
        return {"verdict": "down", "age_sec": age,
                "reason": f"heartbeat timestamp is {abs(int(age))}s in the "
                          f"future — corrupt or a clock moved; refusing to "
                          f"read it as fresh"}

    if age > stale_sec:
        return {"verdict": "down", "age_sec": age,
                "reason": f"no heartbeat for {int(age)}s (stale after "
                          f"{stale_sec}s) — the bridge is not reporting"}

    conn = heartbeat.get("connection")
    if conn != "open":
        return {"verdict": "down", "age_sec": age,
                "reason": f"bridge is reporting connection '{conn}'"}

    return {"verdict": "up", "age_sec": age,
            "reason": "bridge reports an open WhatsApp connection"}


def check(state_path, slug, token, last_message_at=None, now=None, heal=True,
          log_path=None, heartbeat=None,
          link_down_alert_min=DEFAULT_LINK_DOWN_ALERT_MIN,
          heartbeat_stale_sec=DEFAULT_HEARTBEAT_STALE_SEC,
          max_heal_attempts=DEFAULT_MAX_HEAL_ATTEMPTS,
          alert_cb=None):
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
        state, record = _check_locked(
            state_path, slug, token, last_message_at, now, heal,
            heartbeat, link_down_alert_min, heartbeat_stale_sec,
            max_heal_attempts, alert_cb)
        if log_path:
            _log_check(log_path, record)
            if int(state.get("checks") or 0) % PRUNE_EVERY_N_CHECKS == 0:
                prune_checks(log_path, now=now)
        return state


def _maybe_alert_health(state, now, now_s, alert_cb):
    """Fire the one alert, at most once per unhealthy episode.

    Josh, 2026-09-06: *"I want to strip it way back to a single chain of
    notifications."* This is that chain's only sender. Everything that used to
    alert independently — the bridge's five, the watchdog's four, the two
    silence detectors — now contributes a sentence to `_health_reasons` and
    nothing more.
    """
    health = state.get("health") or {}
    reasons = _health_reasons(state, now)
    current = {cls for cls, _ in reasons}

    # ONE alert per incident. An incident opens when health goes from clear to
    # unhealthy and closes when it returns to clear.
    #
    # A first cut keyed this on the SET of active fault classes so that a newly
    # appearing fault would speak again. That produced three phone buzzes for
    # one broken bridge — the link, then the capped restarts, then the wedged
    # consumer, as each crossed its threshold — which is precisely the noise
    # this whole change exists to remove. The alert's job is "the chain is
    # broken, go look"; a second buzz adds nothing a human can act on. The
    # evolving detail belongs in the finding, which regenerates every pass.
    #
    # (The set-keyed version was really compensating for a bug: the 24-hour
    # cumulative-downtime reason used to stay true after recovery and latch the
    # boolean shut. Once cumulative was scoped to a currently-down link, the
    # simple guard became correct again.)
    if current and not health.get("alerted_at"):
        if not health.get("since"):
            health["since"] = now_s
        health["alerted_at"] = now_s
        if alert_cb:
            try:
                alert_cb("bridge_health",
                         "WhatsApp is not reaching the cleaning app",
                         " ".join(t for _, t in reasons))
            except Exception as e:
                print(f"[watchdog] health alert failed: {e}")
    if not current:
        health["since"] = None
        health["alerted_at"] = None
    health["faults"] = sorted(current)
    state["health"] = health
    return reasons


def _check_locked(state_path, slug, token, last_message_at, now, heal,
                  heartbeat=None,
                  link_down_alert_min=DEFAULT_LINK_DOWN_ALERT_MIN,
                  heartbeat_stale_sec=DEFAULT_HEARTBEAT_STALE_SEC,
                  max_heal_attempts=DEFAULT_MAX_HEAL_ATTEMPTS,
                  alert_cb=None):
    """Returns `(state, record)`. Every exit path produces a record, so the log
    can never silently omit a pass — an omitted pass is indistinguishable from
    a watchdog that stopped running."""
    now_s = now.strftime("%Y-%m-%dT%H:%M:%S")
    state = load_state(state_path)

    # The Pi carries state written before 1.4.0, including the open 2026-09-05
    # episode. Left alone, first boot would compute ~30 hours of downtime and
    # fire the phone immediately — or, worse, find the alert guard already set
    # and stay silent through a real fault. The durable record (blind windows,
    # heal counts, check history) is preserved; only the volatile verdict is
    # rebuilt from the first heartbeat.
    if int(state.get("schema") or 0) < 2:
        state["schema"] = 2
        state["link"] = None
        state["health"] = None
        print("[watchdog] migrated state to schema 2 — link verdict rebuilt "
              "from the next heartbeat; blind windows and counts preserved")

    state["last_check"] = now_s
    state["checks"] = int(state.get("checks") or 0) + 1
    previous = state.get("last_state")

    def _done(observed, action, **extra):
        # The single alert. Evaluated here because every exit path returns
        # through `_done`, including the early ones (no token, probe failed) —
        # an alert that only fires on the happy path is the failure this
        # module exists to end.
        _maybe_alert_health(state, now, now_s, alert_cb)
        save_state(state_path, state)
        record = {"at": now_s, "state": observed, "action": action}
        if previous is not None and observed != previous:
            record["from_state"] = previous
        record.update({k: v for k, v in extra.items() if v is not None})
        return state, record

    # ── Link state ─────────────────────────────────────────────────────────
    # Evaluated on every pass, before and independent of the Supervisor probe.
    # The container answer and the link answer are different questions and the
    # link one is the one that matters: a container can be perfectly `started`
    # while the WhatsApp socket is dead, which is precisely how 2026-09-05 went
    # unnoticed for 22 hours.
    verdict = _link_verdict(heartbeat, now, heartbeat_stale_sec)
    link = state.get("link") or {
        "verdict": None, "down_since": None, "alerted_at": None, "episodes": []
    }
    link["verdict"] = verdict["verdict"]
    link["reason"] = verdict["reason"]
    link["checked_at"] = now_s
    if verdict.get("age_sec") is not None:
        link["age_sec"] = int(verdict["age_sec"])
    link["bridge_version"] = (heartbeat or {}).get("bridge_version")
    link["alert_after_min"] = link_down_alert_min
    # Counters are the presence-based half of the signal. Josh ruled that
    # absence proves nothing — a quiet cleaner group is not evidence. A ratio
    # between two things the bridge WATCHED happen is not an absence.
    link["counters"] = {k: (heartbeat or {}).get(k) for k in (
        "socket_events_seen", "allowlist_matched", "forwarded_ok",
        "forward_failed", "decrypt_failures", "last_forward_ok_at", "boot_id")}

    if verdict["verdict"] == "up":
        if link.get("down_since"):
            # Close the episode and KEEP it. Recovery is silent; loss is not —
            # the same rule the blind windows above follow.
            link.setdefault("episodes", []).append({
                "down_since": link["down_since"],
                "recovered_at": now_s,
                "alerted": bool(link.get("alerted_at")),
            })
            link["episodes"] = link["episodes"][-50:]
        link["down_since"] = None
        link["alerted_at"] = None
    else:
        if not link.get("down_since"):
            link["down_since"] = now_s
        try:
            down_for = (now - datetime.strptime(
                link["down_since"], "%Y-%m-%dT%H:%M:%S")).total_seconds() / 60.0
        except (ValueError, TypeError):
            down_for = 0.0
        # This box is a Raspberry Pi with no RTC; it steps its clock at boot.
        # A BACKWARD step makes this negative, and a negative duration never
        # reaches the threshold — the alert simply never fires, silently, which
        # is the one direction a deadman must not fail in. Clamp and restamp so
        # the clock cannot buy the fault more quiet time. (A forward step fires
        # early instead: noisy, but safe.)
        if down_for < 0:
            print(f"[watchdog] clock moved backwards — down_since "
                  f"{link['down_since']} is in the future; restamping")
            link["down_since"] = now_s
            down_for = 0.0
        link["down_for_min"] = int(down_for)
        # One alert per episode. `alerted_at` is the guard: without it this
        # fires on every poll for as long as the fault lasts, and an alert that
        # repeats every five minutes is one you learn to swipe away.
        # No alert is raised here any more. The verdict is assembled once, in
        # `_maybe_alert_health`, so that a link fault and a container fault and
        # a wedged consumer produce ONE message naming all of them rather than
        # three racing each other.
        if down_for >= link_down_alert_min and not link.get("alerted_at"):
            link["alerted_at"] = now_s
    state["link"] = link

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

    # A restart cannot repair every fault, and pretending otherwise is what
    # made 2026-09-05 look like an outage under active repair for 22 hours:
    # 263 identical restarts, five minutes apart, against a WhatsApp
    # registration the server had revoked. The attempt count was already being
    # written down — nothing ever read it back. Past the cap, stop, say so
    # once, and report `unhealable` rather than `restarted` so every downstream
    # surface stops describing a dead bridge as one being fixed.
    #
    # The counter lives on the outage, and recovery clears the outage, so a
    # later genuinely-restartable fault still heals automatically.
    attempts_so_far = int(outage.get("restart_attempts") or 0)
    if attempts_so_far >= max_heal_attempts:
        if not outage.get("escalated_at"):
            outage["escalated_at"] = now_s
        return _done(current, "unhealable", attempt=attempts_so_far)

    try:
        # Count the ATTEMPT, before the outcome is known. This used to
        # increment only after `_supervisor_start` returned, so a start that
        # always failed — a permanent "Another job is already in progress", a
        # permissions problem — left the counter pinned at zero and the
        # watchdog retrying forever with nothing to show it had tried. Any cap
        # keyed on a success-only counter can never fire on the failure it
        # most needs to stop. `heals` still counts successes only, because
        # that is what it is reported as.
        outage["restart_attempts"] = attempts_so_far + 1
        outage["last_restart_at"] = now_s
        _supervisor_start(slug, token)
        state["heals"] = int(state.get("heals") or 0) + 1
        print(f"[watchdog] started bridge (attempt {outage['restart_attempts']})")
        return _done(current, "restarted", attempt=outage["restart_attempts"])
    except Exception as e:
        outage["last_restart_error"] = str(e)
        print(f"[watchdog] start FAILED: {e}")
        return _done(current, "restart_failed", detail=str(e),
                     attempt=outage.get("restart_attempts"))


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


# Cumulative downtime and flap thresholds. A single continuous-downtime rule
# cannot see flapping: a bridge cycling down-3-up-5 is sampled `up` at most
# polls and never accumulates 60 continuous minutes, yet loses messages in
# every gap. That is the shape of the 2026-07-21 outage.
CUMULATIVE_DOWN_ALERT_MIN = 60
FLAP_EPISODES_24H = 4


def _episodes_since(link, now, hours=24):
    cutoff = now - timedelta(hours=hours)
    out = []
    for e in (link.get("episodes") or []):
        try:
            start = datetime.strptime(e["down_since"], "%Y-%m-%dT%H:%M:%S")
            end = datetime.strptime(e["recovered_at"], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            continue          # corrupt or clock-stepped; never negative time
        if end >= cutoff:
            out.append((max(start, cutoff), end))
    return out


def _cumulative_down_min(link, now, hours=24):
    """Total minutes the link was down in the window, closed episodes plus the
    one currently open. Continuous downtime is the wrong measure on its own —
    see CUMULATIVE_DOWN_ALERT_MIN."""
    total = sum((e - s).total_seconds() for s, e in _episodes_since(link, now, hours))
    if link.get("down_since"):
        try:
            start = datetime.strptime(link["down_since"], "%Y-%m-%dT%H:%M:%S")
            total += max(0.0, (now - start).total_seconds())
        except (TypeError, ValueError):
            pass
    return total / 60.0


def _fmt_dur(minutes):
    """4320 minutes is not a readable outage. Days once it is days."""
    m = int(minutes)
    if m < 90:
        return f"{m} minute(s)"
    if m < 60 * 48:
        return f"{m // 60} hour(s)"
    return f"{m // 1440} day(s)"


def _health_reasons(state, now):
    """Every input to the one verdict, most-decisive first.

    Each of these used to be its own alert with its own channel and its own
    latency. On 2026-09-05 five of them fired or would have fired for a single
    fault: `logged_out` instantly to a panel nobody reads, `bridge_down` to the
    08:00 digest twenty hours later, `bridge_silent` four days later, and the
    blind-window record after the fact. Twelve alerts did not make the system
    observable; they made it noisy enough to ignore. They are inputs now.
    """
    reasons = []
    link = state.get("link") or {}
    counters = link.get("counters") or {}
    alert_after = link.get("alert_after_min", DEFAULT_LINK_DOWN_ALERT_MIN)

    # 1. The evaluator itself. Categorically different from "the bridge is
    #    down": it means every other verdict here is untrustworthy, so it is
    #    stated first and never silently skipped.
    if state.get("probe_error"):
        reasons.append(("probe", f"The watchdog cannot check the bridge: {state['probe_error']}"))

    # 2. The link — the primary signal.
    down_min = int(link.get("down_for_min") or 0)
    if link.get("verdict") == "never_seen" and down_min >= alert_after:
        reasons.append(("link", 
            "The bridge has never reported its link state — it is running a "
            "build older than 1.4.0, or it cannot reach the tracker at all."))
    elif link.get("verdict") == "down" and down_min >= alert_after:
        reasons.append(("link", 
            f"No working WhatsApp connection for {_fmt_dur(down_min)}: "
            f"{link.get('reason')}."))

    # 3. Cumulative and flap. What a continuous-downtime rule cannot see.
    cum = _cumulative_down_min(link, now)
    episodes = len(_episodes_since(link, now))
    # Only while the link is DOWN RIGHT NOW but has not yet been down long
    # enough for the continuous rule. That is exactly the flapping case: each
    # individual drop is too short to trip the hour, but they add up.
    #
    # It must not speak while the link is healthy. A 24-hour lookback that
    # reports on a recovered bridge keeps the verdict unhealthy for a whole
    # day, and since the alert guard keys on the set of active faults, that
    # would silently suppress the alert for the NEXT outage inside that day.
    # What the earlier outage cost belongs in the blind-window record, which
    # is durable and dismissible; it does not belong in a live verdict.
    if link.get("down_since") and down_min < alert_after:
        if cum >= CUMULATIVE_DOWN_ALERT_MIN:
            reasons.append(("link", 
                f"The link has been down for {int(cum)} minutes in total over "
                f"the last 24 hours, in {episodes} separate episode(s) — no "
                f"single one long enough to notice, all of them losing messages."))
        elif episodes >= FLAP_EPISODES_24H:
            reasons.append(("link", 
                f"The link has dropped and recovered {episodes} times in 24 "
                f"hours. Messages arriving in the gaps are lost."))

    # 4. Container state — kept as a cross-check so the heartbeat is not a
    #    single point of failure, but no longer an alert of its own.
    outage = state.get("outage")
    if outage:
        try:
            out_min = (now - datetime.strptime(
                outage.get("detected_at") or "", "%Y-%m-%dT%H:%M:%S")).total_seconds() / 60.0
        except (TypeError, ValueError):
            out_min = 0.0
        if outage.get("escalated_at"):
            reasons.append(("unhealable", 
                f"Restarting is not fixing it: "
                f"{int(outage.get('restart_attempts') or 0)} automatic restarts "
                f"since {outage.get('detected_at')} all failed. This needs "
                f"hands — uninstall + install the add-on and re-scan the QR."))
        elif out_min >= alert_after:
            err = outage.get("last_restart_error")
            # Gated like the link. The watchdog restarts a stopped container
            # within five minutes, and paging a phone for something already
            # being fixed is how an alert becomes background noise.
            reasons.append(("container", 
                f"The add-on container is '{outage.get('observed_state')}' — "
                f"down for {_fmt_dur(out_min)}, since {outage.get('detected_at')}."
                + (f" Automatic restart failed: {err}" if err else "")))

    # 5. Consumer wedged. Positive evidence, not an absence: WhatsApp handed us
    #    messages for a cleaner group and none of them reached the tracker. A
    #    reconnect that fails to re-attach the messages.upsert handler looks
    #    exactly like this, and the connection reports open throughout.
    matched = int(counters.get("allowlist_matched") or 0)
    ok = int(counters.get("forwarded_ok") or 0)
    failed = int(counters.get("forward_failed") or 0)
    if matched > 0 and ok == 0:
        reasons.append(("consumer", 
            f"The bridge saw {matched} message(s) in a cleaner group and "
            f"forwarded none of them. This is not a quiet chat — it is a "
            f"broken consumer."))
    elif failed > 0 and ok == 0:
        reasons.append(("consumer", 
            f"All {failed} attempt(s) to hand messages to the tracker failed."))

    return reasons


def findings(state, today_str, now=None):
    """ONE health verdict for "is WhatsApp reaching the cleaning app", plus the
    record of what was lost while it wasn't.

    Before 2026-09-06 this returned up to four separate kinds and the bridge
    raised five more of its own straight to a Home Assistant panel, while
    `reconcile._channel_silence` raised two more. Twelve alerts, three
    channels, one question — and the 22-hour outage they were all watching was
    found by a human looking at his phone.

    The rule now: **many inputs, one verdict, one alert, one channel.**
    Redundant detection is defensive; redundant alerting is noise. Every
    former alert is an input to `_health_reasons`, and its detail lives in
    this finding's `why`.

    The id is a CONSTANT. It must not encode which fault is active: the digest
    diffs finding ids between nights (`app.py` `current_ids - baseline_ids`),
    so an id that changed with the fault mode would announce itself as brand
    new and mark the previous one resolved every time the symptom shifted.
    """
    now = now or datetime.now()
    out = []

    reasons = _health_reasons(state, now)
    if reasons:
        out.append({
            "id": "bridge_health",
            "detector": "bridge_watchdog",
            "kind": "bridge_health",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": ("WhatsApp is not reaching the cleaning app. "
                    + " ".join(t for _, t in reasons)),
            "evidence": [],
        })

    # The second and last verdict: completeness. Deliberately NOT folded into
    # bridge_health — "the pipe is down" and "the pipe is up and quietly
    # dropping one person" need different answers from a human, and merging
    # them would make the common case bury the rare one.
    dec = int(((state.get("link") or {}).get("counters") or {}).get("decrypt_failures") or 0)
    if dec >= DECRYPT_ALERT_THRESHOLD:
        out.append({
            "id": "completeness:decrypt",
            "detector": "bridge_watchdog",
            "kind": "bridge_completeness",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"{dec} message(s) in a cleaner group failed to decrypt. The "
                f"connection is fine — this is libsignal session corruption, "
                f"the failure that silently dropped three months of Daria's "
                f"messages while everything reported healthy. Check the Log tab "
                f"first and confirm the failures are still arriving in an "
                f"allowlisted group; re-pairing costs the auth state, so it is "
                f"the last step, not the first."
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
