"""
Airbnb Cleaning Schedule Tracker
A simple web app to manage cleaning schedules from Airbnb bookings.
Paste WhatsApp chat logs to verify cleaner confirmations.
"""

import calendar
import hashlib
import json
import os
import queue
import re
import socket
import threading
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
from flask import (Flask, render_template_string, request, redirect, jsonify, abort,
                   Response, has_request_context)

import bridge_watchdog as watchdog_mod
import facts as facts_mod
import notify_ack as notify_ack_mod
import gcal as gcal_mod
import clock as clock_mod
import reconcile as reconcile_mod

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
# When running as an HA add-on, options are in /data/options.json and
# persistent storage is /data/. When running locally, fall back to defaults.

OPTIONS_FILE = Path("/data/options.json")
DATA_DIR = Path("/data") if OPTIONS_FILE.exists() else Path(__file__).parent

def load_options():
    if OPTIONS_FILE.exists():
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    return {}

OPTIONS = load_options()
ICAL_URL = OPTIONS.get("ical_url", os.environ.get("ICAL_URL", ""))
ANTHROPIC_API_KEY = OPTIONS.get("anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", ""))
CLEANERS = OPTIONS.get("cleaners", [])
DATA_FILE = DATA_DIR / "data.json"
# Presence means "this install has written data at least once" — see load_data().
INIT_MARKER = DATA_DIR / ".data-initialized"
RECONCILER_LAST_FILE = DATA_DIR / "reconciler_last.json"

GCAL_ENABLED = bool(OPTIONS.get("gcal_enabled", False))
GCAL_CALENDAR_ID = OPTIONS.get("gcal_calendar_id", "")
GCAL_SERVICE_ACCOUNT_JSON = OPTIONS.get("gcal_service_account_json", "")

# Persisted outcome of the last GCal push attempt (D2/D3). Lets the async
# fire-and-forget push (save_data()) and the synchronous nightly retry
# (_nightly_gcal_push) share one durable record the reconciler can read
# instead of a print() that journald truncates within a day.
GCAL_STATUS_FILE = DATA_DIR / "gcal_push_status.json"
SYNC_STATUS_FILE = DATA_DIR / "sync_status.json"
# Operator actions on the live system — deliberate human interventions, not
# scheduled machine work. The ISA records what was *built*; nothing recorded
# what was *done*, so a repair like "pasted a WhatsApp transcript to refill a
# blind window" left no trace but a `source: backfill` tag on the rows it
# created. Reconstructing intent from side effects is exactly the failure this
# codebase already learned about elsewhere (see CLAUDE.md § GCal push health).
OPS_LOG_FILE = DATA_DIR / "ops_log.json"
OPS_LOG_CAP = 500
OPS_LOG_LOCK = threading.Lock()
PUSH_STALE_HOURS = 26
NIGHTLY_PUSH_BUDGET_S = 240
# How long the nightly repair path will WAIT for gcal.py's _SYNC_LOCK instead
# of racing it. Without this, _nightly_gcal_push() runs milliseconds behind
# the async push sync_ical() -> save_data() just spawned, loses the race for
# the lock every time, gets {"skipped": 1} back, and _classify_push correctly
# (but wrongly, in this context) reports that as a failure — manufacturing a
# false "gcal_push_failed" alarm nearly every single night. Waiting lets the
# nightly pass either do the work itself or converge behind the concurrent
# push and then run its own fast, idempotent, honestly-"ok" pass. Must stay
# comfortably below NIGHTLY_PUSH_BUDGET_S so a lock wait alone can't consume
# the whole nightly budget and mask a genuinely wedged push.
NIGHTLY_LOCK_WAIT_S = 120

WHATSAPP_SHARED_SECRET = OPTIONS.get(
    "whatsapp_shared_secret", os.environ.get("WHATSAPP_SHARED_SECRET", "")
)

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DIGEST_LAST_FILE = DATA_DIR / "digest_last.json"
# ISC-358: every outgoing VPS payload, one JSON line each, 30-day trailing
# retention. This is the replay record the 14-night measurement (ISC-359)
# reads — before it existed, nothing retained past payloads and no causality
# claim about the digest could ever be checked against what actually crossed.
DIGEST_ARCHIVE_FILE = DATA_DIR / "digest_archive.jsonl"
DIGEST_ARCHIVE_RETENTION_DAYS = 30
# Serializes the archive's read-filter-rewrite: the manual /digest/run route
# (Flask thread) and the scheduler thread can push concurrently, and two
# unlocked rewrites through the same tmp name would silently drop a line from
# the replay record (code-review 2026-08-21).
_ARCHIVE_LOCK = threading.Lock()
DIGEST_ENABLED = bool(OPTIONS.get("digest_enabled", False))
DIGEST_TIME = OPTIONS.get("digest_time", "08:00")

# How near a dated finding must be before it repeats in the nightly digest.
# Three weeks is roughly the window in which a cleaner can still be found and
# a calendar entry still matters; beyond it, repeating is noise. See the
# comment at the `persisting` filter for why this exists.
REPEAT_HORIZON_DAYS = int(OPTIONS.get("repeat_horizon_days", 21))

# Rollback lever for the outreach detector (1.41.1). The detector reads host
# schedule_assertions at 0.5 on purpose (questions score ~0.75); set this to
# 0.85 to ignore questions, or above 1.0 to switch the detector off — both
# without a redeploy. Pure module constant, overridden at startup.
reconcile_mod.OUTREACH_MIN_CONFIDENCE = float(
    OPTIONS.get("outreach_min_confidence", reconcile_mod.OUTREACH_MIN_CONFIDENCE))

# How far past a review item's subject date it stays in the queue awaiting a
# human decision. Conflicts already self-suppress after RECONCILER STALE_DAYS
# (5); the review queue had no equivalent and simply accumulated.
REVIEW_EXPIRY_DAYS = int(OPTIONS.get("review_expiry_days", 7))

# Whether verbatim WhatsApp text may cross to the VPS Telegram bot. Off by
# default — the payload allowlist has always excluded quotes, and a reporting
# feature should not quietly dissolve a privacy boundary. Turn on to get the
# quoted message in Telegram instead of only in the app.
DIGEST_INCLUDE_QUOTES = bool(OPTIONS.get("digest_include_quotes", False))

# Nightly digest push to the VPS Telegram bot. The Pi initiates (the VPS is
# egress-locked and cannot pull). Payload is built by ALLOWLIST — finding ids,
# dates, severities, cleaner first names and the generated `why` line only.
# Never guest names, WhatsApp text, quotes/evidence, tokens, or secrets.
VPS_PUSH_ENABLED = bool(OPTIONS.get("vps_push_enabled", False))
VPS_PUSH_URL = OPTIONS.get("vps_push_url", "")
VPS_PUSH_SECRET = OPTIONS.get("vps_push_secret", "")

# How long to wait for the bot's reply. This MUST exceed the bot's own triage
# budget, because the bot deliberately does not answer until it has run the
# findings through its triage model AND sent the Telegram message — so its 200
# carries the strong claim "Josh has been told", not the weak one "bytes
# arrived". Keeping that claim means waiting for the work behind it.
#
# It was 20s against a 60s budget, and on 2026-08-06 the two finally crossed:
# the bot logged `digest received 15:00:04.530Z` / `message sent 15:00:25.806Z`
# — 21.3s — so the Pi hung up 1.3s early, and Josh got the digest AND a
# "delivery failed" alarm for the same successful delivery. The cost of that is
# not the one false alarm; it is that every future REAL failure now arrives in
# a channel he has learned to disbelieve.
#
# 90s = the bot's 60s triage budget (`sdkTriageTimeoutMs`,
# ~/dev/pai-telegram-bot/src/cleaning.ts) + the Telegram round trip + margin.
# ⚠️ If that budget is ever raised, raise this with it — they are one number
# split across two machines, and nothing but this comment links them.
VPS_PUSH_TIMEOUT_S = 90

# Escalation channel for alerts that must not wait for someone to open Home
# Assistant. A persistent notification is a place the host *visits*; this is a
# place a message *finds* him — and critically it does not route through the
# VPS or Telegram, so it still works when the thing that failed IS the bot.
# Never hardcode the service name: this repo is public (same rule as
# vps_status_url). Empty = no phone escalation, panel notification only.
PHONE_NOTIFY_SERVICE = (OPTIONS.get("phone_notify_service", "") or "").strip()

# Channel-silence ("WhatsApp going dark") detection. Complements the bridge's
# error-burst health alarms, which cannot see a quiet per-group mute (the Daria
# failure: 3 months of silently-dropped messages, no error to count).
DEAD_CHANNEL_ENABLED = bool(OPTIONS.get("dead_channel_enabled", True))
DEAD_CHANNEL_DAYS = int(OPTIONS.get("dead_channel_days", 14))
BRIDGE_SILENT_DAYS = int(OPTIONS.get("bridge_silent_days", 7))
DEAD_CHANNEL_MIN_MSGS = int(OPTIONS.get("dead_channel_min_msgs", 10))

# Bridge liveness watchdog. Complements the silence detectors above rather than
# replacing them: those infer a dead pipe from *absent traffic*, which is
# lagging (7/14-day thresholds) and ambiguous (a quiet chat looks identical).
# This asks Supervisor for the container's state on an hourly timer, which is
# unambiguous, and restarts it without asking. See bridge_watchdog.py for the
# 2026-07-28 five-day outage that motivated it.
BRIDGE_WATCHDOG_ENABLED = bool(OPTIONS.get("bridge_watchdog_enabled", True))
BRIDGE_WATCHDOG_SLUG = (OPTIONS.get("bridge_watchdog_slug", "") or "").strip()
BRIDGE_WATCHDOG_INTERVAL_MIN = int(OPTIONS.get("bridge_watchdog_interval_min", 60))
BRIDGE_WATCHDOG_FILE = DATA_DIR / "bridge_watchdog.json"
BRIDGE_HEARTBEAT_FILE = DATA_DIR / "bridge_heartbeat.json"
# How long the link may be down before it buzzes a phone. Josh, 2026-09-06:
# "Warn me if it's down for even 1 hour."
BRIDGE_LINK_DOWN_ALERT_MIN = int(OPTIONS.get("bridge_link_down_alert_min", 60))
# Silence that long counts as DOWN. At a 60s beat this is five missed beats —
# long enough to ride out a restart, short enough that it is not a new blind
# spot of its own.
BRIDGE_HEARTBEAT_STALE_SEC = int(OPTIONS.get("bridge_heartbeat_stale_sec", 300))
BRIDGE_MAX_HEAL_ATTEMPTS = int(OPTIONS.get("bridge_max_heal_attempts", 5))
# One JSONL line per liveness check, including the uneventful ones — that is
# what makes a stable stretch evidence rather than an absence. Trailing 30-day
# window; see bridge_watchdog.CHECK_LOG_RETENTION_DAYS.
BRIDGE_CHECK_LOG = DATA_DIR / "bridge_checks.jsonl"

# Append-only record of every booking mutation applied from WhatsApp. Exists so
# the nightly digest can answer "what did you change while I wasn't looking"
# without anyone opening the app — the thing that, before 2026-08-02, required
# a human to come and ask.
CHANGE_LOG_FILE = DATA_DIR / "change_log.json"
CHANGE_LOG_MAX = 500

# Footer VPS status widget. Host/URL is NEVER hardcoded (this add-on's code is
# on GitHub) — it's a config option, empty by default, same as ical_url.
VPS_STATUS_ENABLED = bool(OPTIONS.get("vps_status_enabled", False))
VPS_STATUS_URL = (OPTIONS.get("vps_status_url", "") or "").strip()
VPS_STATUS_LABEL = OPTIONS.get("vps_status_label", "VPS") or "VPS"
VPS_STATUS_TTL = 45  # seconds — cache probe result; footer polls every 60s
_VPS_STATUS_CACHE = {"result": None, "at": 0.0}


def _classify_push(stats, err, exc=None):
    """Pure. Classify a sync_to_gcal() outcome into an honest verdict.

    A skip (gcal.py's non-blocking lock was already held) and a real
    failure both come back from sync_to_gcal() as "no error", which is why
    the old caller logged a skip as a success. Precedence: exc -> failed;
    err -> failed; stats with truthy "skipped" -> skipped; stats truthy ->
    ok; stats falsy/None with no err -> failed ("no stats returned").

    Returns {"ok": bool, "outcome": "ok"|"skipped"|"failed", "error": str|None}.
    """
    if exc is not None:
        return {"ok": False, "outcome": "failed", "error": str(exc)}
    if err:
        return {"ok": False, "outcome": "failed", "error": str(err)}
    if stats and stats.get("skipped"):
        return {"ok": False, "outcome": "skipped", "error": "another sync already running"}
    if stats:
        return {"ok": True, "outcome": "ok", "error": None}
    return {"ok": False, "outcome": "failed", "error": "no stats returned"}


def _read_gcal_status():
    """Return the persisted GCal push status dict, or None if absent/unreadable.

    Never raises — a corrupt or missing status file just means "we don't
    know", not a crash.
    """
    try:
        if not GCAL_STATUS_FILE.exists():
            return None
        with open(GCAL_STATUS_FILE) as f:
            status = json.load(f)
        return status if isinstance(status, dict) else None
    except Exception as e:
        print(f"[gcal] failed to read push status: {e}")
        return None


def _read_ops_log():
    """Return the operator-action log (oldest first), or [] if absent."""
    try:
        if not OPS_LOG_FILE.exists():
            return []
        with open(OPS_LOG_FILE) as f:
            entries = json.load(f)
        return entries if isinstance(entries, list) else []
    except Exception as e:
        print(f"[ops] failed to read ops log: {e}")
        return []


def _log_op(action, **detail):
    """Append one operator action. Never raises — logging an action must never
    be able to fail the action itself.

    Locked because this is a read-modify-write on a whole file: the watchdog
    timer thread and a Flask request thread can both reach it, and an interleave
    would truncate the log to whatever the loser read. A corrupt file degrades
    to `[]` on the next read, which loses the history silently — the exact
    failure this log exists to prevent.
    """
    try:
        with OPS_LOG_LOCK:
            entries = _read_ops_log()
            entries.append({
                "at": datetime.now().isoformat(timespec="seconds"),
                "action": action,
                **{k: v for k, v in detail.items() if v is not None},
            })
            if len(entries) > OPS_LOG_CAP:
                del entries[:len(entries) - OPS_LOG_CAP]
            # Atomic, same as the gcal/sync sidecars: a kill mid-write would
            # otherwise truncate the file and _read_ops_log would degrade it
            # to [] on the next read, losing the history silently.
            tmp = OPS_LOG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(entries, f, indent=2)
            os.replace(tmp, OPS_LOG_FILE)
        print(f"[ops] {action} {detail}")
    except Exception as e:
        print(f"[ops] failed to log '{action}': {e}")


def _actor():
    """Who is making this write: a human at the HA UI, an API caller, or the
    system itself.

    That distinction is the whole point of the write log. Once a CLI exists —
    and through it, a model — "Josh pressed Assign" and "something called
    /assign" must be separable after the fact, because that is the only
    question anyone will ever ask of this record. Derived from the same three
    signals `_require_local_or_secret` gates on, so the log cannot disagree
    with the door.
    """
    if not has_request_context():
        return "system"
    if request.headers.get("X-Ingress-Path"):
        return "human:ha-ui"
    if request.headers.get("X-Shared-Secret"):
        return "api:shared-secret"
    return f"api:{request.remote_addr or '?'}"


def _log_write(op, booking_uid=None, **detail):
    """Record one mutation of the schedule: who, what, and which booking it
    resolved to.

    Separate from `_record_change`, which is the nightly "what I changed"
    REPORT and is deliberately narrow: it watches four fields, returns early
    when none of them moved, is only ever reached from
    `_apply_booking_change`, and is projected to the VPS. This is the audit
    trail — every write path, including the ones that resolve to nothing,
    because "a delete was issued against a uid that does not exist" is exactly
    the entry a bad automation leaves behind.

    Structured fields only, and never message text: `_log_op` prints its detail
    to the add-on log and the ops log is read by the UI, so a quote here would
    cross the same boundary `_record_change`'s docstring exists to defend.
    """
    _log_op(op, actor=_actor(), booking_uid=booking_uid, **detail)


def _write_gcal_status(status):
    """Persist the GCal push status dict to GCAL_STATUS_FILE.

    Never raises — a failure here must not corrupt data.json nor propagate
    into the push path. Writes to a temp file in the same directory and
    os.replace()s it into place so a crash mid-write can't leave a partial
    (unparseable) status file behind.
    """
    try:
        tmp_path = GCAL_STATUS_FILE.with_name(f"{GCAL_STATUS_FILE.name}.tmp{os.getpid()}")
        with open(tmp_path, "w") as f:
            json.dump(status, f, indent=2, default=str)
        os.replace(tmp_path, GCAL_STATUS_FILE)
    except Exception as e:
        print(f"[gcal] failed to persist push status: {e}")


def _gcal_push(data, lock_timeout_s=0):
    """Run the GCal projection, classify the outcome honestly, persist it,
    and return the status dict. Never raises. Returns None if GCAL_ENABLED
    is false (unchanged early exit) — no status is written in that case,
    since a disabled projection has nothing to report.

    Distinguishes "skipped" (another sync already running) from a genuine
    failure — before this, both surfaced identically as a swallowed
    exception or a mislabeled "synced" log line, and a stuck GCal push could
    go unnoticed indefinitely because journald truncates within a day.

    lock_timeout_s defaults to 0 (non-blocking — race the lock, skip if
    busy), which is exactly the prior fire-and-forget behaviour and is what
    save_data()'s async dispatch still uses. Only _nightly_gcal_push passes
    a positive value, because racing an async push it just triggered a
    moment earlier is a false-alarm generator, not a health signal.
    """
    if not GCAL_ENABLED:
        return None

    prev = _read_gcal_status()
    stats, err, exc = None, None, None
    try:
        stats, err = gcal_mod.sync_to_gcal(
            data, GCAL_SERVICE_ACCOUNT_JSON, GCAL_CALENDAR_ID,
            lock_timeout_s=lock_timeout_s,
        )
    except Exception as e:
        exc = e

    verdict = _classify_push(stats, err, exc)
    at = datetime.now().isoformat(timespec="seconds")
    prev_ok = (prev or {}).get("ok")
    attempt = 1 if (prev is None or prev_ok is True) else (prev or {}).get("attempt", 0) + 1
    last_ok_at = at if verdict["ok"] else (prev or {}).get("last_ok_at")

    status = {
        "ok": verdict["ok"],
        "outcome": verdict["outcome"],
        "at": at,
        "error": verdict["error"],
        "attempt": attempt,
        "last_ok_at": last_ok_at,
        # Carried forward, never cleared by a success. A push that blows its
        # nightly budget every night but eventually finishes would otherwise
        # have its timeout record clobbered by its own late writer, and a
        # chronically wedging push would read as permanently healthy — a
        # last-writer-wins failure in the quiet direction. (Advisor finding,
        # 2026-08-01.) The reconciler ages this out on its own.
        "last_timeout_at": (prev or {}).get("last_timeout_at"),
        "stats": stats,
    }
    _write_gcal_status(status)

    if verdict["outcome"] == "skipped":
        print("[gcal] SKIPPED — another sync already running")
    elif verdict["outcome"] == "failed":
        print(f"[gcal] push FAILED: {verdict['error']}")
    else:
        print(f"[gcal] synced: {stats}")

    return status


def _should_retry_push(status):
    """Pure. True when status is None or status.get("ok") is not True."""
    return status is None or status.get("ok") is not True


def _nightly_gcal_push():
    """Run the GCal projection synchronously-with-a-budget, for the nightly
    digest path only.

    Builds an annotated snapshot the same way /gcal/sync does (load_data(),
    then set b["_needs_notify"] = needs_notify(b) on every booking), runs
    _gcal_push in a background thread and join()s it with a
    NIGHTLY_PUSH_BUDGET_S deadline. If the thread is still alive at the
    deadline, log loudly and return WITHOUT waiting further — the digest
    must proceed regardless; a wedged push must never hang the nightly job.
    (The push thread is a daemon and keeps running in the background; it
    will still persist its own status via _write_gcal_status when it
    eventually finishes.) Never raises.

    Passes lock_timeout_s=NIGHTLY_LOCK_WAIT_S into _gcal_push (unlike every
    async caller, which uses the default non-blocking 0). This call runs
    moments after _digest_scheduler's sync_ical() -> save_data(), which just
    fired its own async push on a separate thread — without a wait, this
    nightly push would race that in-flight push for gcal.py's _SYNC_LOCK,
    lose almost every time, get back {"skipped": 1}, and _classify_push
    would correctly-but-wrongly record that as a failed nightly push: a
    fabricated alarm nearly every night. Waiting up to NIGHTLY_LOCK_WAIT_S
    (kept well under NIGHTLY_PUSH_BUDGET_S, the outer join() deadline) lets
    this pass either do the work itself or converge behind the concurrent
    push and then run its own fast, idempotent, honestly-"ok" pass.
    """
    try:
        with DATA_LOCK:
            data = load_data()
        for b in data.get("bookings", {}).values():
            b["_needs_notify"] = needs_notify(b)

        thread = threading.Thread(
            target=_gcal_push, args=(data,),
            kwargs={"lock_timeout_s": NIGHTLY_LOCK_WAIT_S}, daemon=True,
        )
        thread.start()
        thread.join(NIGHTLY_PUSH_BUDGET_S)
        if thread.is_alive():
            # Record the timeout as a first-class not-ok fact. Without this the
            # previous ok:true record stands, _run_full_reconcile reads it, and
            # a wedged push presents as a healthy one for up to PUSH_STALE_HOURS
            # — the exact "positive-looking failure" this release exists to end,
            # smuggled back in through the timeout branch. (Cross-vendor audit
            # finding, gpt-5.5, 2026-08-01.)
            #
            # The thread is still running and will write its own status if it
            # ever finishes; that later write legitimately wins, because a push
            # that eventually succeeded really is ok.
            prev = _read_gcal_status() or {}
            _write_gcal_status({
                "ok": False,
                "outcome": "timeout",
                "at": datetime.now().isoformat(timespec="seconds"),
                "error": (
                    f"nightly push exceeded its {NIGHTLY_PUSH_BUDGET_S}s budget and "
                    "was still running; the calendar may not have converged"
                ),
                "attempt": prev.get("attempt", 0) + 1,
                "last_ok_at": prev.get("last_ok_at"),
                "last_timeout_at": datetime.now().isoformat(timespec="seconds"),
                "stats": None,
            })
            print(
                f"[gcal] nightly push still running after {NIGHTLY_PUSH_BUDGET_S}s "
                "budget — NOT blocking the digest further; recorded outcome=timeout "
                "so the reconciler cannot read this as healthy"
            )
            return
        print("[gcal] nightly push finished within budget")
    except Exception as e:
        print(f"[gcal] nightly push wrapper error: {e}")


def ingress_prefix():
    """Get the HA ingress path prefix from the request header."""
    return request.headers.get("X-Ingress-Path", "")


# ── Data persistence ─────────────────────────────────────────────────────────

class DataVanished(RuntimeError):
    """`data.json` is gone but this install has written it before.

    The default-empty branch below is correct exactly once, on a fresh install.
    Every other time it is a catastrophe wearing a fresh install's clothes: an
    fsck that moved the file to lost+found, a failed volume mount, a botched
    restore. Returning `{}` there is not a degraded read — the very next
    `save_data` writes that emptiness back as authoritative, and `sync_ical`
    then repopulates every booking from the feed, so the dashboard returns
    looking healthy with every cleaning present, every one unassigned, and
    every `cleaner_commitment` gone. Nothing downstream can tell that state
    apart from a genuine first boot.

    `INIT_MARKER` is what distinguishes them. It is written next to the data on
    the first successful save and never removed, so its presence means "this
    install has had data" — and if the data is missing while the marker is not,
    we refuse to serve emptiness and fail loudly instead.
    """


def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            data = json.load(f)
    elif INIT_MARKER.exists():
        raise DataVanished(
            f"{DATA_FILE} is missing but {INIT_MARKER.name} exists — this install "
            "has written data before. Refusing to initialise an empty store, which "
            "would be committed on the next save. Restore via POST /internal/restore "
            f"(a snapshot may exist at {DATA_FILE}.bak), or delete "
            f"{INIT_MARKER} to force a genuine fresh start."
        )
    else:
        data = {"bookings": {}, "last_sync": None}
    for uid, b in data.get("bookings", {}).items():
        if "type" not in b:
            b["type"] = "manual_cleaning" if uid.startswith("manual-") else "airbnb"
        # Lazy backfill of a time that only ever reached free text.
        #
        # `_parse_clean_time` was written for exactly this and then never
        # called — 17 live bookings carry `clean_time: null` beside a notes
        # string like "Time: 5:00 PM" that it parses cleanly. That is what a
        # nullable scalar does to the humans around it: when the typed slot
        # cannot hold the answer, the answer goes into the untyped one, and
        # the recovery function gets written and never wired up.
        #
        # Done here rather than as a migration script because a one-shot
        # rewrite of completed bookings changes nothing anyone reads, while a
        # reader on the load path also heals whatever arrives next. All 17
        # current cases are `complete`; the value is the guarantee, not the
        # backfill. Only fills when the typed field is empty — a real
        # `clean_time` always wins over prose.
        if not b.get("clean_time"):
            recovered = _parse_clean_time(b.get("notes") or "")
            if recovered:
                b["clean_time"] = recovered
    data.setdefault("messages", [])
    data.setdefault("cleaner_jids", {})
    data.setdefault("host_jids", [])
    data.setdefault("group_labels", {})
    data.setdefault("message_facts", {})
    return data


def save_data(data):
    # Write-to-temp-then-rename, with an fsync the sidecars skip.
    #
    # `open(DATA_FILE, "w")` is O_TRUNC: the old content is destroyed the
    # instant the call opens, before a byte of the new content exists, and
    # nothing is on disk until writeback (up to ~30s later on ext4). Add-on
    # restarts are routine here and this is a Raspberry Pi, so power loss is
    # routine too — the exposure is every save, not a theoretical window.
    #
    # Every sidecar in this add-on (ops log, gcal status, sync status, watchdog
    # state) already writes this way; `bridge_watchdog.save_state` carries the
    # docstring explaining why. That reasoning was applied to a restart counter
    # and not to the one file holding every booking, message and commitment.
    # `os.replace` is atomic, and under ext4's default data=ordered the temp
    # file's data is forced out before the rename metadata commits — so a
    # reader sees old-or-new, never neither.
    tmp = DATA_FILE.with_name(f"{DATA_FILE.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if not INIT_MARKER.exists():
        try:
            INIT_MARKER.write_text(datetime.now().isoformat(timespec="seconds"))
        except OSError as e:
            print(f"[data] could not write init marker: {e}")
    if GCAL_ENABLED:
        # Snapshot the data so the worker thread doesn't race with further
        # mutations by the caller. Annotate bookings with drift state so gcal
        # can flag them without re-deriving the logic.
        snapshot = json.loads(json.dumps(data, default=str))
        for b in snapshot.get("bookings", {}).values():
            b["_needs_notify"] = needs_notify(b)
        threading.Thread(target=_gcal_push, args=(snapshot,), daemon=True).start()


# ── Data lock ────────────────────────────────────────────────────────────────
# Serializes reads/writes against data.json. The parse worker mutates messages
# and bookings concurrently with Flask request handlers.

DATA_LOCK = threading.RLock()


# ── Cleaner config helpers ───────────────────────────────────────────────────
# CLEANERS from config.yaml is a list of strings today. We also support an
# object form {"name": "...", "whatsapp": ["jid", ...]} for forward-compat.

def cleaner_names():
    """Return the list of cleaner display names, regardless of config shape."""
    names = []
    for c in CLEANERS:
        if isinstance(c, str):
            names.append(c)
        elif isinstance(c, dict) and c.get("name"):
            names.append(c["name"])
    return names


def cleaner_jid_map(data):
    """Merge JIDs from config.yaml (if present) with runtime data.cleaner_jids.

    Returns {cleaner_name: [jid, ...]}. Runtime data wins on conflict.
    """
    merged = {}
    for c in CLEANERS:
        if isinstance(c, dict) and c.get("name") and c.get("whatsapp"):
            merged[c["name"]] = list(c["whatsapp"])
    for name, jids in data.get("cleaner_jids", {}).items():
        merged.setdefault(name, [])
        for jid in jids:
            if jid not in merged[name]:
                merged[name].append(jid)
    return merged


def lookup_cleaner_by_jid(data, jid):
    """Return the cleaner name mapped to this JID, or None."""
    for name, jids in cleaner_jid_map(data).items():
        if jid in jids:
            return name
    return None


def group_label(data, jid):
    """Human-friendly label for a group JID, or the JID itself if unlabeled."""
    return data.get("group_labels", {}).get(jid) or jid


# ── Cleaner color ─────────────────────────────────────────────────────────────

def _parse_clean_time(notes: str):
    """Return 'HH:MM:SS' parsed from a notes string like 'Time: 11:00 AM | ...'."""
    if not notes:
        return None
    m = re.search(r'Time:\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?)', notes, re.IGNORECASE)
    if not m:
        return None
    ts = m.group(1).strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(ts, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def cleaner_color(name: str) -> str:
    """Return a stable #RRGGBB color derived from the cleaner's name."""
    if not name:
        return "#9ca3af"
    digest = hashlib.md5(name.encode()).hexdigest()
    hue = int(digest[:4], 16) % 360
    s, l = 0.65, 0.55
    # HSL → RGB (C = chroma, X = intermediate, m = offset)
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = l - c / 2
    sector = int(hue / 60)
    rgb_f = [
        (c, x, 0), (x, c, 0), (0, c, x),
        (0, x, c), (x, 0, c), (c, 0, x),
    ][sector]
    r, g, b = (round((v + m) * 255) for v in rgb_f)
    return f"#{r:02x}{g:02x}{b:02x}"


# ── iCal sync ────────────────────────────────────────────────────────────────

def sync_ical():
    """Fetch Airbnb iCal and merge into local data.

    Records the per-attempt outcome to `sync_status.json` on every exit path.
    That record lives HERE rather than in the nightly scheduler so it covers
    every caller — the manual button, the startup sync and the nightly job all
    prove the same way. `last_sync` advances only on success, so it answers
    "when did a sync last work", never "did the most recent attempt work", and
    those diverge exactly when it matters.

    The feed is fetched BEFORE the lock; only the merge runs inside it. This
    whole function was unlocked until 1.36.0 while the WhatsApp worker pool
    held DATA_LOCK on the same file — a read-modify-write race whose losing
    write vanishes without a trace. Wrapping it wholesale would trade that for
    a different fault: holding the lock across a 15-second network call stalls
    every inbound message behind the sync.
    """
    if not ICAL_URL:
        err = "No iCal URL configured. Set it in the add-on options."
        _write_sync_status(False, err)
        with DATA_LOCK:
            return load_data(), err

    try:
        resp = requests.get(ICAL_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        _write_sync_status(False, e)
        with DATA_LOCK:
            return load_data(), str(e)

    # Parse and merge are inside the status handler: a crash in either used to
    # leave the PREVIOUS success standing in sync_status.json, so a broken feed
    # read as a healthy sync for up to 26h.
    try:
        cal = __import__("icalendar").Calendar.from_ical(resp.text)
        with DATA_LOCK:
            return _merge_ical_events(cal)
    except SuspiciousFeed as e:
        # Deliberately NOT a phone escalation of its own. `_digest_scheduler`
        # already raises on any non-empty sync error and escalates that to the
        # phone, so notifying here would double-alert on one event — and the
        # phone tier is reserved, by test, for the four cases where nobody
        # would otherwise find out. A refused feed is not one of them: the
        # schedule is frozen, not lost, and the stale-sync sentinel says so
        # within 26h even if the nightly notification is missed.
        _write_sync_status(False, e)
        with DATA_LOCK:
            return load_data(), str(e)
    except Exception as e:
        _write_sync_status(False, e)
        with DATA_LOCK:
            return load_data(), str(e)


# A feed that would cancel this many future bookings at once, and more than
# this fraction of them, is treated as broken rather than believed.
MASS_CANCEL_MIN = 4
MASS_CANCEL_RATIO = 0.5


class SuspiciousFeed(RuntimeError):
    """The iCal feed parsed cleanly but describes an implausible world."""


def _merge_ical_events(cal):
    """Merge parsed feed events into data.json. Caller holds DATA_LOCK."""
    data = load_data()
    seen_uids = set()

    for event in cal.walk("VEVENT"):
        summary = str(event.get("SUMMARY", ""))
        if summary != "Reserved":
            continue

        uid = str(event.get("UID", ""))
        dtstart = event.get("DTSTART").dt
        dtend = event.get("DTEND").dt

        start_str = dtstart.strftime("%Y-%m-%d") if hasattr(dtstart, "strftime") else str(dtstart)
        end_str = dtend.strftime("%Y-%m-%d") if hasattr(dtend, "strftime") else str(dtend)
        seen_uids.add(uid)

        if uid in data["bookings"]:
            b = data["bookings"][uid]
            b["start"] = start_str
            b["end"] = end_str
            b["status"] = "active"
        else:
            data["bookings"][uid] = {
                "start": start_str,
                "end": end_str,
                "cleaner": None,
                "paid": False,
                "status": "active",
                "confirmed": False,
                "notes": "",
            }

    today = date.today()

    # ── Mass-cancellation guard (2026-08-20) ────────────────────────────────
    # The sweep below marks any airbnb booking absent from the feed as
    # cancelled. There was no floor on how many, so a 200 response containing
    # zero `Reserved` events — listing snoozed, feed URL rotated, export
    # toggled off, an Airbnb-side hiccup — cancelled the entire forward
    # schedule in one pass. Everything downstream then agreed it was a good
    # night: the GCal push deleted the events, `last_sync` advanced,
    # `sync_status` recorded success, the >26h freshness sentinel stayed quiet,
    # and the digest reported the vanished findings as "N previously flagged
    # finding(s) resolved." Total loss of the schedule, delivered as good news.
    #
    # A cancellation is a normal event; four at once on future dates is not.
    # Bail BEFORE `save_data` so a suspicious feed writes nothing at all —
    # including its additions. Failing closed costs one stale sync, which the
    # freshness sentinel already alarms on; failing open costs the schedule.
    future_active = [
        uid for uid, b in data["bookings"].items()
        if b.get("type", "airbnb") == "airbnb"
        and b.get("status") == "active"
        and (b.get("end") or "") >= today.isoformat()
    ]
    to_cancel = [uid for uid in future_active if uid not in seen_uids]
    # Logged on EVERY poll, not only when it trips: a guard that is silent
    # until it fires cannot be distinguished from one that never evaluated.
    print(f"[sync] feed carried {len(seen_uids)} reservation(s); "
          f"{len(to_cancel)} of {len(future_active)} future booking(s) absent")
    if to_cancel and (
        not seen_uids
        # A TOTAL wipe is suspicious at any count. Without this clause the
        # ratio+floor test is inert exactly where this household lives: at
        # three future bookings, a feed cancelling all three sails under the
        # >=4 floor and takes the whole schedule with it.
        or len(to_cancel) == len(future_active)
        or (len(to_cancel) >= MASS_CANCEL_MIN and len(to_cancel) > MASS_CANCEL_RATIO * len(future_active))
    ):
        raise SuspiciousFeed(
            f"iCal feed returned {len(seen_uids)} reservation(s) and would cancel "
            f"{len(to_cancel)} of {len(future_active)} future active booking(s). "
            "Refusing to apply — nothing was written. Check the Airbnb export URL "
            "and listing status, then re-run the sync."
        )

    for uid, b in data["bookings"].items():
        if b.get("type", "airbnb") != "airbnb":
            continue
        if uid not in seen_uids:
            end_dt = datetime.strptime(b["end"], "%Y-%m-%d").date()
            if end_dt < today:
                b["status"] = "complete"
            else:
                b["status"] = "cancelled"

    data["last_sync"] = datetime.now().isoformat()
    save_data(data)
    _write_sync_status(True)
    return data, None


# ── Inbound WhatsApp: message parsing with chat context ─────────────────────



def _msg_local_day(msg, default=None):
    """The LOCAL calendar day a message was sent, or `default`.

    Was a near-duplicate of `_msg_day` with its own inline ZoneInfo. The two
    genuinely differed once, because `_msg_day` sliced `ts[:10]` off the raw
    string — which is wrong for any evening message, since 9pm in Vancouver is
    already tomorrow in UTC. Now that `_msg_day` parses properly they are the
    same question, so both go through clock.py and the timezone is constructed
    in exactly one place.
    """
    ts = msg.get("timestamp") if isinstance(msg, dict) else msg
    day = clock_mod.local_day(ts)
    if day is not None:
        return day
    if isinstance(ts, str) and len(ts) >= 10:
        try:
            return date.fromisoformat(ts[:10])
        except ValueError:
            return default
    return default
def _date_header(msg, today=None):
    """The dating preamble every model prompt in this pipeline opens with.

    Two dates, and keeping them distinct is the whole point. TODAY tells the
    model what is past and what is upcoming — without it, "Monday" and a bare
    "the 3rd" are anchored to nothing and the year has to be guessed. The
    MESSAGE timestamp is what relative terms actually resolve against, and it
    is NOT today whenever a pasted transcript is being ingested: a January
    message reprocessed in August must still read "tomorrow" as January.
    Stating only today would silently re-date the entire backfill.
    """
    today = today or date.today()
    # Local send day, not the raw UTC date slice — this value anchors the word
    # "today" for both the model and the candidate list, and those two must
    # agree or the whole date-agreement check below is comparing to a fiction.
    sent_day = _msg_local_day(msg)
    lines = [f"TODAY IS {today.isoformat()} ({today.strftime('%A')})."]
    if sent_day and sent_day != today:
        lines.append(
            f"This message was SENT ON {sent_day.isoformat()} ({sent_day.strftime('%A')}) — "
            f"it is being processed later than it was sent. Resolve every relative term "
            f"(\"today\", \"tomorrow\", \"Monday\", \"the 3rd\") against the SEND date, not "
            f"against today. Today is given only so you can tell which dates have already passed."
        )
    else:
        lines.append("Resolve relative terms (\"tomorrow\", \"Monday\", \"the 3rd\") against today.")
    return "\n".join(lines)




# ── Message queue + worker ──────────────────────────────────────────────────
# Single module-level queue; a worker thread drains it and calls Haiku. This
# keeps the Flask request handler fast and bounds Anthropic API concurrency.
# Pool size 2 is deliberately small — burst traffic in one group shouldn't
# fan out unbounded requests.

MESSAGE_QUEUE = queue.Queue()
_WORKERS_STARTED = False
_WORKERS_LOCK = threading.Lock()


def enqueue_message(msg_id):
    MESSAGE_QUEUE.put(msg_id)


def _message_worker():
    while True:
        msg_id = MESSAGE_QUEUE.get()
        try:
            process_message(msg_id)
        except Exception as e:
            # Worker must never die — log and continue.
            print(f"[worker] error processing {msg_id}: {e}")
        finally:
            MESSAGE_QUEUE.task_done()


def ensure_workers_started(pool_size=2):
    global _WORKERS_STARTED
    with _WORKERS_LOCK:
        if _WORKERS_STARTED:
            return
        for i in range(pool_size):
            t = threading.Thread(target=_message_worker, daemon=True, name=f"wa-worker-{i}")
            t.start()
        _WORKERS_STARTED = True


# ── Credit-exhaustion circuit breaker ───────────────────────────────────────
# Anthropic returns HTTP 400 "credit balance is too low" when the prepaid
# balance hits zero. Unhandled, inbound messages fail silently and sit pending
# (this happened 2026-06-10 and hid a same-day scheduling change for ~17h).
# Behaviour: detect the signature → post ONE HA notification (6h cooldown) →
# defer the message id → spin a probe thread that requeues everything and
# notifies once credits return. 400s are rejected pre-billing, so this costs
# no tokens while exhausted; the recovery probe is a single max_tokens=1 ping.

_CREDIT_LOCK = threading.Lock()
_CREDIT_STATE = {
    "exhausted": False,
    "since": None,
    "last_notified": None,
    "deferred": set(),
    "recovery_running": False,
}
_CREDIT_NOTIFY_COOLDOWN = timedelta(hours=6)
_CREDIT_PROBE_INTERVAL = 600  # seconds between recovery probes


def _is_low_balance_error(err):
    """True if an extract/parse error string is the Anthropic out-of-credit 400."""
    if not err:
        return False
    e = str(err).lower()
    return "credit balance is too low" in e or ("400" in e and "billing" in e)


def _credit_probe_ok():
    """One tiny Haiku ping to test whether credits are back. True only on 200."""
    if not ANTHROPIC_API_KEY:
        return False
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ok"}],
            },
            timeout=20,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _credit_recovery_loop():
    """Spun up on first credit-exhausted failure. Probes every
    _CREDIT_PROBE_INTERVAL; on recovery, resets + requeues deferred messages
    and notifies. Exits once recovered."""
    while True:
        time.sleep(_CREDIT_PROBE_INTERVAL)
        if not _credit_probe_ok():
            continue
        with _CREDIT_LOCK:
            deferred = list(_CREDIT_STATE["deferred"])
            _CREDIT_STATE.update(
                exhausted=False, since=None, deferred=set(), recovery_running=False,
            )
        ensure_workers_started()
        with DATA_LOCK:
            data = load_data()
            for mid in deferred:
                m = _find_message(data, mid)
                if m:
                    m["parsed"] = False
                    m["parse_error"] = None
            save_data(data)
        for mid in deferred:
            enqueue_message(mid)
        _post_ha_notification(
            "Cleaning tracker: Anthropic credits restored",
            f"Live WhatsApp parsing resumed. Re-queued {len(deferred)} message(s) "
            "that failed while the balance was empty.",
            notification_id="cleaning_credit",
        )
        return


def _flag_credit_exhausted(msg_id):
    """Record an out-of-credit failure: defer the message, notify once per
    cooldown, and ensure the recovery probe is running."""
    notify = False
    start_recovery = False
    now = datetime.now()
    with _CREDIT_LOCK:
        _CREDIT_STATE["deferred"].add(msg_id)
        if not _CREDIT_STATE["exhausted"]:
            _CREDIT_STATE["exhausted"] = True
            _CREDIT_STATE["since"] = now.isoformat(timespec="seconds")
        last = _CREDIT_STATE["last_notified"]
        if last is None or (now - datetime.fromisoformat(last)) > _CREDIT_NOTIFY_COOLDOWN:
            _CREDIT_STATE["last_notified"] = now.isoformat(timespec="seconds")
            notify = True
        if not _CREDIT_STATE["recovery_running"]:
            _CREDIT_STATE["recovery_running"] = True
            start_recovery = True
    if notify:
        _post_ha_notification(
            "Cleaning tracker: Anthropic credits exhausted",
            "Live WhatsApp parsing is PAUSED — the API returned 'credit balance "
            "is too low'. Top up at console.anthropic.com (Plans & Billing). "
            "Queued messages auto-reprocess once credits return; no action needed "
            "in the add-on.",
            notification_id="cleaning_credit",
        )
    if start_recovery:
        threading.Thread(
            target=_credit_recovery_loop, daemon=True, name="credit-recovery",
        ).start()


# Facts extraction prompts carry recent context so the model can resolve
# "yes", "that date", and spot re-posted lists. Unbounded history was fine
# at the 33-message test corpus but blows up in bulk backfill (quadratic
# tokens, TPM ceilings, 429 backoff storms). Window to the 30 most recent
# same-group messages strictly preceding the target.
FACTS_HISTORY_WINDOW = 30
FACTS_HISTORY_DAYS = 45
FACTS_HISTORY_MAX = 120

# Cross-chat facts digest handed to fact extraction. Bounded in both
# directions: a little history (a date agreed last week may still be being
# renegotiated) and a long forward horizon (schedules are agreed months out —
# the Aug 3 commitment was made on Mar 30).
CROSS_FACTS_BACK_DAYS = 7
CROSS_FACTS_FWD_DAYS = 150
CROSS_FACTS_MAX_LINES = 80


# One clock: event time, UTC, everywhere. The reasoning, and the three-clock
# bug this replaced, are documented in clock.py — including why a naive value
# is read as local rather than UTC.
_ts_utc = clock_mod.ts_utc
_utc_iso = clock_mod.utc_iso


def _migrate_timestamps_to_utc():
    """One-shot: convert stored naive-local timestamps to UTC. Idempotent.

    Backfilled rows were written with the naive wall time straight out of the
    WhatsApp export, while live rows were UTC — so the two sat seven to eight
    hours apart in the same list, and every comparison across them was wrong.
    Anything already carrying an offset or a `Z` is left alone, so this is safe
    to run repeatedly and safe to run on a store that is already clean.

    ⚠️ Message IDs are deliberately NOT touched. A backfill id is
    `sha1(timestamp|sender|text)`, so the ids of already-stored rows were
    derived from the naive value. Recomputing them would mean rewriting every
    `message_facts` key in the same breath, on live data, to fix a re-paste
    case that the structured write path is going to remove anyway. The cost of
    leaving them: re-pasting a transcript that was ingested BEFORE this
    migration will not dedupe against those old rows. Trim the paste to the
    window you actually need and it does not arise.
    """
    with DATA_LOCK:
        data = load_data()
        if data.get("timestamps_utc_migrated"):
            return 0
        changed = 0
        for m in data.get("messages", []) or []:
            raw = m.get("timestamp")
            if not isinstance(raw, str) or not raw:
                continue
            if clock_mod.has_zone(raw):
                continue          # already carries a zone
            dt = _ts_utc(raw)     # naive is read as local, which is what it is
            if dt is None:
                continue
            m["timestamp"] = _utc_iso(dt)
            changed += 1
        data["timestamps_utc_migrated"] = True
        save_data(data)
        return changed


def _msg_day(m):
    """Local calendar day for a message, or None.

    This used to slice the first ten characters off the stored string. That is
    wrong for any evening message: 9pm in Vancouver is stored as the NEXT day
    in UTC. See clock.py.
    """
    ts = m.get("timestamp") if isinstance(m, dict) else m
    day = clock_mod.local_day(ts)
    if day is not None:
        return day
    # Last resort for a value too malformed to parse at all.
    if isinstance(ts, str) and len(ts) >= 10:
        try:
            return date.fromisoformat(ts[:10])
        except ValueError:
            return None
    return None


def _window_by_count_or_days(prior, count, days, hard_max, target):
    """Most recent `count` messages OR everything within `days` — whichever is
    larger — capped at `hard_max`.

    A pure message count is the wrong unit and was backwards in practice: 30
    messages of Daria's chat (96 total since March) reaches back months, while
    50 of Itzel's (513 total) covers about a fortnight. So the busiest thread —
    the one most likely to hold a superseding decision — had the SHORTEST
    memory. Taking the larger of the two windows fixes the quiet case without
    shrinking the busy one; hard_max keeps the token cost bounded, which is why
    the original caps existed (bulk backfill hit the org rate limit).
    """
    prior = sorted(prior, key=lambda m: m.get("timestamp") or "")
    by_count = prior[-count:]
    tgt_day = _msg_day(target)
    if tgt_day is None:
        return by_count[-hard_max:]
    cutoff = tgt_day - timedelta(days=days)
    by_days = [m for m in prior if (_msg_day(m) or date.min) >= cutoff]
    chosen = by_days if len(by_days) > len(by_count) else by_count
    return chosen[-hard_max:]


def _facts_history(messages, target):
    """Same-chat context for fact extraction.

    Stays same-chat on purpose: fact extraction runs on every message, so
    doubling its raw context doubles the token bill on the hot path. Cross-chat
    awareness is supplied instead by _cross_chat_facts() — already-extracted,
    structured, and a fraction of the size.
    """
    tgt_group = target.get("group")
    tgt_ts = target.get("timestamp") or ""
    same = [
        m for m in messages
        if m.get("group") == tgt_group and (m.get("timestamp") or "") < tgt_ts
    ]
    return _window_by_count_or_days(
        same, FACTS_HISTORY_WINDOW, FACTS_HISTORY_DAYS, FACTS_HISTORY_MAX, target,
    )


def _sender_roles(data):
    """Exact {jid: "cleaner:Name" | "host"} map for fact extraction.

    Built from `cleaner_jids` and `host_jids`, which the system has maintained
    all along and which the facts prompt was not consulting — it guessed the
    speaker's role by looking for a cleaner's name inside the sender label
    instead. That guess fails for every sender shape actually stored (JIDs
    live, phone numbers from pasted exports), so it silently mislabelled
    cleaners as the host.
    """
    roles = {}
    for jid in data.get("host_jids") or []:
        if jid:
            roles[jid] = "host"
    for name, jids in (data.get("cleaner_jids") or {}).items():
        for jid in jids or []:
            if jid:
                roles[jid] = f"cleaner:{name}"
    return roles


def _cross_chat_facts(data, target, now=None):
    """Compact digest of what OTHER chats have already established.

    The problem this solves: scheduling a single cleaning routinely spans both
    threads — one cleaner is released in her chat while another is asked in
    hers — and fact extraction could only ever see one side. It therefore
    recorded "Daria confirmed Aug 3" with no idea Itzel was ever involved.

    Feeding the other chat's raw messages in would be the obvious fix and the
    wrong one: it doubles tokens on every message, and both history windows
    were capped precisely because unbounded context hit the rate limit during
    backfill. Facts that have ALREADY been extracted are structured, tiny, and
    are exactly the thing needed to notice two cleaners claiming one date.
    """
    now = now or datetime.now()
    tgt_group = target.get("group")
    tgt_day = _msg_day(target) or now.date()
    lo = (tgt_day - timedelta(days=CROSS_FACTS_BACK_DAYS)).isoformat()
    hi = (tgt_day + timedelta(days=CROSS_FACTS_FWD_DAYS)).isoformat()

    group_of = {}
    said_at = {}
    for m in data.get("messages", []) or []:
        if m.get("id"):
            group_of[m["id"]] = m.get("group")
            said_at[m["id"]] = m.get("timestamp") or ""
    labels = data.get("group_labels", {}) or {}

    # Keyed on (date, cleaner, kind) keeping the most recently stated — an old
    # confirm that was later superseded must not outrank the newer one.
    best = {}
    for msg_id, rec in (data.get("message_facts", {}) or {}).items():
        grp = group_of.get(msg_id)
        if not grp or grp == tgt_group:
            continue
        # WHEN IT WAS SAID, not when we got round to reading it.
        #
        # This was `rec["extracted_at"]` — the moment facts extraction ran.
        # For live traffic that tracks the send time closely enough to hide the
        # bug. For a backfill it does not: every statement in a transcript
        # pasted today gets today's `extracted_at`, so a cleaner's OLD message
        # outranks her newer live one and overwrites the correct answer. The
        # message's own timestamp is the only defensible ordering key, and it
        # was sitting in the row this loop already joins against for `group`.
        stated = said_at.get(msg_id) or rec.get("extracted_at") or ""
        # Compare PARSED instants, never the raw strings. Until the migration
        # below has run everywhere, this store holds both `...Z` UTC and naive
        # local values, and comparing those as text orders them by their
        # spelling rather than by when they happened.
        order = _ts_utc(stated) or datetime.min.replace(tzinfo=timezone.utc)
        for f in rec.get("facts", []) or []:
            tgt_date = f.get("target_date")
            kind = f.get("kind")
            cleaner = f.get("cleaner")
            if not tgt_date or not kind or kind == "unclear":
                continue
            if not (lo <= tgt_date <= hi):
                continue
            key = (tgt_date, cleaner, kind)
            if key not in best or order > best[key][0]:
                best[key] = (order, {
                    "date": tgt_date,
                    "cleaner": cleaner,
                    "kind": kind,
                    "time": f.get("target_time"),
                    "chat": labels.get(grp) or grp,
                    "stated": (_msg_day({"timestamp": stated}) or date.min).isoformat(),
                })

    # Truncate by PROXIMITY to the message's own date, then present
    # chronologically. Sorting by date and slicing would have dropped the
    # nearest commitments first — the live corpus produced 41 rows against a
    # cap of 40 on the day this shipped, so the boundary is real, not
    # theoretical, and getting it backwards would have silently discarded
    # exactly the dates being negotiated.
    rows = [v[1] for v in best.values()]
    rows.sort(key=lambda r: (abs((date.fromisoformat(r["date"]) - tgt_day).days),
                             r["date"], r["cleaner"] or ""))
    kept = rows[:CROSS_FACTS_MAX_LINES]
    kept.sort(key=lambda r: (r["date"], r["cleaner"] or ""))
    return kept




# ── Routing: which booking does a message touch? ────────────────────────────
# Decided in CODE, from the facts the model extracted — never by asking the
# model to name a booking.
#
# Until 2026-08-20 a second Sonnet call was shown a list of candidate cleanings,
# each tagged with its 56-character uid, and asked to copy one back. On
# 2026-08-20 Itzel wrote "Yes Sept 10 I can do it at 11:00"; the model returned
# the right action, the right date, the right cleaner and 0.90 confidence — and
# the uid without its `@airbnb.com` suffix. `bookings.get()` returned None, the
# write was refused, and because the branch that EXPLAINS a refusal also needs
# the booking it is explaining about, the hold was recorded with no reason at
# all. 79 of 81 uids in the archive resolved; one was truncated and one was
# invented outright.
#
# The uid was never worth asking for. `target_date` is already in the facts, it
# is human-meaningful, it can be checked against the data, and across the whole
# two-year archive exactly one date carries two bookings. So resolve the date
# here: zero matches is not actionable, one match applies, two or more is a
# question for a human. That is a branch you can unit-test; a transcribed key
# is not. The model is no longer shown the booking list at all, which is why
# `upcoming_booking_list` is gone — the 2026-08-06 wrong-row bug (16 of 48
# auto-applied confirmations landed on the stay that CHECKED IN that day,
# because 53% of cleanings share a date with the next check-in) is now
# structurally impossible rather than guarded against.

ROUTE_CONFIDENCE = 0.85


def _routable_bookings_by_date(bookings):
    """{cleaning_date: [uid, ...]} over bookings a cleaner could be sent to."""
    by_date = {}
    for uid, b in bookings.items():
        if b.get("status") != "active" or b.get("type") == "custom_stay":
            continue
        d = cleaning_date_for(b)
        if d:
            by_date.setdefault(d, []).append(uid)
    return by_date


def _route_from_facts(facts_list, bookings, sender_cleaner, known_cleaners, today_str):
    """Which bookings may this message write to, and why not for the rest.

    Returns `(decisions, blocks)`.
      decisions: [{uid, action, target_date, confidence, evidence}]
      blocks:    ["human-readable reason", ...] for facts that expressed a real
                 scheduling intent but could not be routed.

    Pure — no data access, no clock — so the rule is testable directly rather
    than through a copy of itself (the ISC-192 lesson, kept).

    A message may legitimately decide MANY bookings: the dominant real-chat
    pattern here is a cleaner re-posting a dated list. One call, N decisions.
    """
    if not facts_list:
        return [], []
    if not sender_cleaner or sender_cleaner not in known_cleaners:
        # Host messages and unmapped senders never write. The host's own
        # schedule assertions are the reconciler's business, not the write
        # path's — he is stating a plan, not accepting one.
        return [], []

    by_date = _routable_bookings_by_date(bookings)
    decisions, blocks = [], []
    seen = {}

    for f in facts_list:
        kind = (f.get("kind") or "").lower()
        if kind not in ("confirm", "decline"):
            continue
        tgt = f.get("target_date")
        if not tgt or tgt < today_str:
            continue
        if f.get("tentative"):
            blocks.append(f"{sender_cleaner} was tentative about {tgt}")
            continue
        # She speaks for herself. "Itzel told me she's taking the 17th" is
        # testimony about a third party, and the facts prompt attributes it to
        # the SUBJECT — so without this check a cleaner could confirm on behalf
        # of someone who never spoke.
        if f.get("cleaner") and f["cleaner"] != sender_cleaner:
            blocks.append(
                f"message names {f['cleaner']} for {tgt} but was sent by {sender_cleaner}"
            )
            continue
        if float(f.get("confidence") or 0.0) < ROUTE_CONFIDENCE:
            blocks.append(f"low confidence on {tgt}")
            continue

        matches = by_date.get(tgt, [])
        if not matches:
            blocks.append(f"no active cleaning on {tgt}")
            continue
        if len(matches) > 1:
            blocks.append(
                f"{len(matches)} cleanings share {tgt} — which one is a question for a human"
            )
            continue

        uid = matches[0]
        prior = seen.get(uid)
        if prior and prior != kind:
            # One message that both accepts and declines the same cleaning.
            # Drop both rather than let ordering decide.
            decisions[:] = [d for d in decisions if d["uid"] != uid]
            blocks.append(f"message both confirms and declines {tgt}")
            seen[uid] = "contradiction"
            continue
        if seen.get(uid) == "contradiction":
            continue
        seen[uid] = kind
        decisions.append({
            "uid": uid,
            "action": kind,
            "target_date": tgt,
            "confidence": float(f.get("confidence") or 0.0),
            "evidence": f.get("evidence") or "",
        })

    return decisions, blocks


def _hold_destructive_on_blocks(decisions, blocks):
    """A decline auto-applies only when the whole message was understood.

    Pure, so the rule is testable directly rather than through a copy of itself.

    A decline is the only destructive decision in this pipeline — it clears a
    booked cleaner. Without this, "can't do Sept 8, can I come Sept 9?"
    half-lands: Sept 9 is not a checkout, so the confirm is held, while the
    decline sails through and strips the cleaner off a booking nobody has
    replaced. Ending in a worse state than doing nothing is the one outcome a
    partial apply must never produce.

    Confirms stay additive and still apply, so a nine-date re-posted list
    carrying one unrecognised date still books the eight it resolved.
    """
    if not blocks or not any(d["action"] == "decline" for d in decisions):
        return decisions, blocks
    held = [d for d in decisions if d["action"] == "decline"]
    kept = [d for d in decisions if d["action"] != "decline"]
    return kept, list(blocks) + [
        "held the cancellation of "
        + ", ".join(d["target_date"] for d in held)
        + " because the rest of this message could not be routed"
    ]



def _synthesize_result(decisions, blocks, sender_cleaner):
    """A `haiku_result`-shaped record, built from the routed decisions.

    The Review tab, `/review/accept` and `_review_subject_date` all read this
    field. Keeping its shape means the routing rewrite does not drag the whole
    UI with it — and every uid in it now came from `_route_from_facts`, so it
    always resolves.
    """
    if not decisions:
        return {
            "action": "none",
            "booking_uid": None,
            "cleaning_date": None,
            "cleaner": sender_cleaner,
            "confidence": 0.0,
            "reason": blocks[0] if blocks else "no scheduling statement in this message",
            "source": "facts",
        }
    primary = decisions[0]
    extra = "" if len(decisions) == 1 else f" (+{len(decisions) - 1} more in the same message)"
    return {
        "action": primary["action"],
        "booking_uid": primary["uid"],
        "cleaning_date": primary["target_date"],
        "cleaner": sender_cleaner,
        "confidence": primary["confidence"],
        "reason": f"{sender_cleaner} {primary['action']}ed {primary['target_date']}{extra}",
        "source": "facts",
    }


def process_message(msg_id):
    """Extract facts from one inbound message, then route them to bookings in code."""
    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg:
            return
        if msg.get("parsed"):
            return
        # Snapshot everything we need, then release the lock while we call
        # the API. Re-acquire on write.
        all_messages = [m for m in data["messages"] if m.get("id") != msg_id]
        known = cleaner_names()
        sender_cleaner = lookup_cleaner_by_jid(data, msg.get("sender"))
        labels = dict(data.get("group_labels", {}))
        cross_facts = _cross_chat_facts(data, msg)
        roles = _sender_roles(data)

    # ONE model call. It answers "what did this message say", which is the only
    # question a language model is better at than code. Routing — "which row
    # does that touch" — is `_route_from_facts`, below the lock re-acquire.
    #
    # There used to be a second call whose job was to pick a booking, and it
    # was the only one allowed to write. It had no time field, so a revised
    # hour was extracted correctly by this call and then discarded; it had no
    # honest bucket for "thank you", so gratitude was rounded to `confirm` and
    # applied; and it carried a uid it could mistype. An empty facts list is a
    # valid result (chitchat) — only `facts_err` means retry via reprocess.
    facts_list, facts_err = facts_mod.extract_facts(
        ANTHROPIC_API_KEY, msg, _facts_history(all_messages, msg), known, labels,
        cross_facts=cross_facts, roles=roles, date_header=_date_header(msg),
    )

    # Out-of-credit (HTTP 400 "balance too low") is not a per-message failure —
    # it's an account-wide outage. Don't bury it as a pending parse_error;
    # alert + defer so it auto-reprocesses when credits return.
    if _is_low_balance_error(facts_err):
        _flag_credit_exhausted(msg_id)
        with DATA_LOCK:
            data = load_data()
            m = _find_message(data, msg_id)
            if m:
                m["parsed"] = False  # eligible for recovery requeue
                m["parse_error"] = facts_err
                m["review_state"] = "pending"
                save_data(data)
        return

    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg:
            return
        # Re-check under the re-acquired lock. The queue does not dedup and
        # three paths re-enqueue (sender mapping, credit recovery, the
        # parse-error sweeper), so two workers could both pass the entry guard
        # above, both spend a call, and both write — on a decline, the second
        # clearing a cleaner the first just set.
        if msg.get("parsed"):
            return
        msg["parsed"] = True
        msg["parse_error"] = facts_err

        if facts_list is not None:
            data.setdefault("message_facts", {})[msg_id] = facts_mod.build_record(
                facts_list, msg.get("sender") or "",
            )

        if facts_err:
            # Held, and now VISIBLE: `_unread_messages` in reconcile.py turns
            # this into a finding dated to the cleaning it concerns. Before
            # 2026-08-20 nothing anywhere watched this field, and 60 un-retried
            # rate limits sat here unseen.
            msg["review_state"] = "pending"
            msg["haiku_result"] = None
            save_data(data)
            return

        decisions, blocks = _route_from_facts(
            facts_list, data.get("bookings", {}), sender_cleaner, known,
            (_msg_local_day(msg) or date.today()).isoformat(),
        )
        msg["haiku_result"] = _synthesize_result(decisions, blocks, sender_cleaner)
        if blocks and not decisions:
            msg["auto_block"] = "Held for review: " + "; ".join(blocks[:3])
        else:
            msg.pop("auto_block", None)

        decisions, blocks = _hold_destructive_on_blocks(decisions, blocks)

        if decisions:
            for d in decisions:
                _apply_booking_change(data, d["uid"], sender_cleaner, d["action"], msg,
                                      facts_list=facts_list)
            msg["review_state"] = "auto"
            msg["applied_uid"] = decisions[0]["uid"]
            # One re-posted list confirms many dates; `applied_uid` is singular
            # and predates that, so the full set rides alongside it.
            msg["applied_uids"] = [d["uid"] for d in decisions]
        elif blocks:
            msg["review_state"] = "pending"
        else:
            msg["review_state"] = "ignored"

        save_data(data)




def _find_message(data, msg_id):
    for m in data.get("messages", []):
        if m.get("id") == msg_id:
            return m
    return None


def _record_change(before, after, booking_uid, action, msg, auto):
    """Append one applied-change record for the nightly "what I changed" report.

    Deliberately stores *derived fields only* — date, cleaner, time, confirmed —
    and never the WhatsApp text that caused the change. This record is projected
    to the VPS Telegram bot, and the payload allowlist there exists precisely to
    keep message content on the Pi. Storing the quote here would launder it past
    that boundary through a field nobody thought to check.
    """
    watched = ("cleaner", "clean_time", "confirmed", "end")
    changed = {
        f: {"from": before.get(f), "to": after.get(f)}
        for f in watched
        if before.get(f) != after.get(f)
    }
    if not changed:
        return
    entry = {
        "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "booking_uid": booking_uid,
        "cleaning_date": after.get("end"),
        "action": action,
        "source": "whatsapp-auto" if auto else "whatsapp-review",
        "message_id": (msg or {}).get("id"),
        "changed": changed,
    }
    try:
        log = []
        if CHANGE_LOG_FILE.exists():
            log = json.loads(CHANGE_LOG_FILE.read_text())
            if not isinstance(log, list):
                log = []
        log.append(entry)
        CHANGE_LOG_FILE.write_text(json.dumps(log[-CHANGE_LOG_MAX:], indent=2))
    except (OSError, ValueError) as e:
        # Never let bookkeeping break the booking write itself.
        print(f"[changelog] failed to record change: {e}")


def _read_change_log_tail(limit=100):
    """Most recent change records, newest last. Never raises.

    Read by `/internal/snapshot`, which is the off-host reconciliation lifeline
    and must never 500 — same discipline as `_read_ops_log` (ISC-167).
    """
    try:
        if not CHANGE_LOG_FILE.exists():
            return []
        log = json.loads(CHANGE_LOG_FILE.read_text())
        return log[-limit:] if isinstance(log, list) else []
    except (OSError, ValueError):
        return []


def _recent_changes(hours=24, now=None):
    """Applied changes inside the last `hours`. Used by the nightly digest."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=hours)
    try:
        if not CHANGE_LOG_FILE.exists():
            return []
        log = json.loads(CHANGE_LOG_FILE.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for e in log if isinstance(log, list) else []:
        try:
            if datetime.fromisoformat(e["at"]) >= cutoff:
                out.append(e)
        except (KeyError, TypeError, ValueError):
            continue
    return out


_CHANGE_LABELS = {
    "cleaner": "cleaner",
    "clean_time": "time",
    "confirmed": "confirmed",
    "end": "cleaning date",
}


def _change_findings(today_str, hours=24, now=None, bookings=None):
    """Turn the last day's applied changes into digest findings.

    Rides the findings channel rather than a parallel one so it inherits the
    delivery, allowlist and dismissal machinery that already exists — and so a
    night with no problems but real changes still produces a Telegram message.
    Severity is informational: these are reports of work done, not alarms.
    """
    out = []
    for e in _recent_changes(hours=hours, now=now):
        bits = []
        for field, delta in (e.get("changed") or {}).items():
            label = _CHANGE_LABELS.get(field, field)
            before = delta.get("from")
            after = delta.get("to")
            bits.append(f"{label} {before if before not in (None, '') else '—'} → "
                        f"{after if after not in (None, '') else '—'}")
        if not bits:
            continue
        when = (e.get("cleaning_date") or "?")
        src = "auto-applied" if e.get("source") == "whatsapp-auto" else "applied after review"
        out.append({
            "id": f"applied:{e.get('message_id')}:{e.get('at')}",
            "detector": "change_log",
            "kind": "applied_change",
            "severity": "informational",
            "booking_uid": e.get("booking_uid"),
            # The booking's REAL cleaner. This was hardcoded to None until
            # 1.36.0, and the VPS bot rendered a null cleaner as the literal
            # word "unassigned" — so a change report on a booking Itzel has
            # held since April arrived on Josh's phone as "no cleaner
            # assigned; verify intended and assign if needed", twice. A null
            # meaning "not applicable to this finding type" must never travel
            # as the assertion "this booking has no cleaner".
            "cleaner": ((bookings or {}).get(e.get("booking_uid")) or {}).get("cleaner"),
            "date": today_str,
            "why": f"{when} cleaning — {'; '.join(bits)} ({src} from WhatsApp).",
            "evidence": [],
        })
    return out


# Fact kinds that may set a cleaning time. `schedule_assertion` is HOST-only by
# the role-tagged facts prompt and is the single largest bucket of timed facts in
# the live archive (84 of 235) — writing from it would let Josh's own plan
# masquerade as the cleaner's agreement, which is the whole distinction this
# system exists to keep. `unclear` carries a time 5 times in the archive and by
# definition means the extractor could not tell what was meant.
CLEANER_TIME_KINDS = ("confirm", "time_proposal")

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _stated_clean_time(facts_list, cleaning_date):
    """The time the CLEANER named for `cleaning_date` in one message.

    Pure — takes the facts extracted from a single message and returns
    `(clean_time, unusable_reason)`, exactly one of which is set, or
    `(None, None)` when the message says nothing about the time at all. That
    last case is the overwhelmingly common one and is not a problem.

    The `unusable_reason` case is the interesting one and is why this returns a
    pair rather than an Optional: it means she said something about the time
    that we must NOT act on. The caller has to know the difference, because
    "she named no time" and "she named a time I could not use" call for
    opposite handling of the acknowledgement — see `_apply_booking_change`.
    """
    times = {
        f.get("target_time")
        for f in (facts_list or [])
        if isinstance(f, dict)
        and f.get("kind") in CLEANER_TIME_KINDS
        and not f.get("tentative")
        and f.get("target_date") == cleaning_date
        and f.get("target_time")
    }
    if not times:
        return None, None
    if len(times) > 1:
        # Real case from the archive: "anytime after 11am and before 3pm"
        # extracts as 11:00 AND 15:00. That is a range, not a time, and
        # choosing either end would invent an agreement nobody made.
        listed = ", ".join(sorted(str(t) for t in times))
        return None, f"names {len(times)} different times ({listed})"
    stated = str(times.pop())
    if not _HHMM_RE.match(stated):
        # 235 of 235 archive samples parse clean. That is evidence about the
        # model's habit, not a guarantee about the next one.
        return None, f"unparseable time {stated!r}"
    return f"{stated}:00", None


def _apply_booking_change(data, booking_uid, cleaner_name, action, msg,
                          auto=True, facts_list=None):
    """Apply a confirm/decline to a booking. Caller holds DATA_LOCK."""
    booking = data["bookings"].get(booking_uid)
    if not booking:
        # Logged, not silently dropped: a write aimed at a uid that no longer
        # exists is the signature of a stale automation, and it is invisible
        # everywhere else.
        _log_write("booking_write_unresolved", booking_uid, attempted=action,
                   cleaner=cleaner_name, auto=auto)
        return
    before = dict(booking)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if action == "confirm":
        if cleaner_name and not booking.get("cleaner"):
            booking["cleaner"] = cleaner_name
            booking["cleaner_since"] = now
        booking["confirmed"] = True

        # The time she named in THIS message, if she named one. Until 1.36.0
        # the confirm path wrote `confirmed` and nothing else, so "see you on
        # Monday at 11:00 am" was extracted correctly by the facts layer,
        # stored, and then discarded — the booking kept a 5:00pm agreed back in
        # March and the shared calendar showed it for a cleaning happening at
        # 11. A revision wins over a standing agreement (Josh's ruling
        # 2026-08-09): being wrong on the calendar for two days is the failure
        # being fixed, so this applies rather than queueing for review.
        stated, unusable = _stated_clean_time(facts_list, cleaning_date_for(booking))
        if stated:
            booking["clean_time"] = stated
        if unusable:
            booking["time_note"] = unusable
        else:
            booking.pop("time_note", None)

        # Ratify only what we can stand behind. `ack_notified` stamps CURRENT
        # truth into the commitment, so calling it after a message that named a
        # time we could not use would record an agreement she did not make —
        # and because commitment would then equal truth, the notify queue would
        # go silent on precisely the booking that just became doubtful.
        if not unusable:
            ack_notified(booking, via="whatsapp")
    elif action == "decline":
        # Clear the cleaner so the booking surfaces as "needs cleaner" again.
        # Preserve notes so the history of what the cleaner said is visible.
        if booking.get("cleaner") == cleaner_name:
            booking["cleaner"] = None
            booking["cleaner_since"] = None
            booking.pop("cleaner_commitment", None)
        booking["confirmed"] = False
    # Record the message id that last mutated this booking.
    booking["last_wa_msg_id"] = msg.get("id")
    _log_write(f"booking_{action}", booking_uid,
               cleaning_date=cleaning_date_for(booking), cleaner=cleaner_name,
               auto=auto, message_id=(msg or {}).get("id"))
    _record_change(before, booking, booking_uid, action, msg, auto)


# ── Commitment / review queue ───────────────────────────────────────────────

def cleaning_date_for(b):
    """The date a cleaner would come, or None for custom stays."""
    if b.get("type") == "custom_stay":
        return None
    return b.get("end")


def _truth_tuple(b):
    """Current (cleaner, date, clean_time) snapshot, or None if not a cleaning."""
    d = cleaning_date_for(b)
    if not d:
        return None
    return (b.get("cleaner"), d, b.get("clean_time"))


def _commit_tuple(c):
    if not c:
        return None
    return (c.get("cleaner"), c.get("date"), c.get("clean_time"))


def review_item(uid, b):
    """Diff description for one booking, or None if it's settled."""
    if b.get("type") == "custom_stay":
        return None
    end_str = b.get("end")
    if not end_str:
        return None
    try:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
    except ValueError:
        return None

    today = date.today()
    commitment = b.get("cleaner_commitment")
    status = b.get("status", "active")

    if status == "cancelled":
        if not commitment:
            return None
        if end_dt < today - timedelta(days=1):
            return None
        return {
            "uid": uid, "kind": "cancelled",
            "cleaner": commitment.get("cleaner") or b.get("cleaner"),
            "booking": b, "date": end_str,
            "was": _commit_tuple(commitment), "now": None,
        }

    if end_dt < today:
        return None

    cleaner = b.get("cleaner")
    if not cleaner:
        if b.get("type", "airbnb") == "airbnb":
            return {
                "uid": uid, "kind": "unassigned", "cleaner": None,
                "booking": b, "date": end_str, "was": None,
                "now": _truth_tuple(b),
            }
        return None

    if not commitment:
        return {
            "uid": uid, "kind": "new", "cleaner": cleaner, "booking": b,
            "date": end_str, "was": None, "now": _truth_tuple(b),
        }

    if _commit_tuple(commitment) == _truth_tuple(b):
        return None

    return {
        "uid": uid, "kind": "changed", "cleaner": cleaner, "booking": b,
        "date": end_str,
        "was": _commit_tuple(commitment), "now": _truth_tuple(b),
    }


def needs_notify(b):
    """True if this booking has unresolved drift (used by gcal signalling)."""
    return review_item(None, b) is not None


def review_queue(data):
    """(buckets, unassigned) where buckets = [{cleaner, items}, ...]."""
    by_cleaner = {}
    unassigned = []
    for uid, b in data.get("bookings", {}).items():
        item = review_item(uid, b)
        if not item:
            continue
        if item["kind"] == "unassigned":
            unassigned.append(item)
        else:
            by_cleaner.setdefault(item["cleaner"], []).append(item)
    unassigned.sort(key=lambda x: x["date"])
    buckets = []
    for cleaner, items in sorted(by_cleaner.items(), key=lambda kv: (kv[0] or "")):
        items.sort(key=lambda x: x["date"])
        buckets.append({"cleaner": cleaner, "items": items})
    return buckets, unassigned


def ack_notified(booking, via):
    """Stamp cleaner_commitment to match current truth. For cancelled bookings,
    remove the commitment (the cleaner now knows it's off)."""
    if booking.get("status") == "cancelled":
        booking.pop("cleaner_commitment", None)
        return
    cleaner = booking.get("cleaner")
    d = cleaning_date_for(booking)
    if not cleaner or not d:
        return
    booking["cleaner_commitment"] = {
        "cleaner": cleaner,
        "date": d,
        "clean_time": booking.get("clean_time"),
        "communicated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "communicated_via": via,
    }


# ── HTML Templates ───────────────────────────────────────────────────────────

# Shared CSS used by FOCUS_TEMPLATE and other pages (add/edit/print).
_SHARED_STYLES = """
  :root {
    --green: #d4edda; --red: #ffcccb; --yellow: #fff3cd;
    --blue: #cce5ff; --gray: #f8f9fa; --dark: #212529;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--gray); color: var(--dark); padding: 12px; max-width: 960px; margin: 0 auto;
  }
  h1 { font-size: 1.3rem; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 0.85rem; margin-bottom: 16px; }
  .sync-bar {
    display: flex; gap: 8px; align-items: center; margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .sync-bar form { display: inline; }
  button, .btn {
    padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer;
    font-size: 0.9rem; font-weight: 500; text-decoration: none; display: inline-block;
  }
  .btn-primary { background: #0d6efd; color: #fff; }
  .btn-primary:hover { background: #0b5ed7; }
  .btn-secondary { background: #6c757d; color: #fff; }
  .btn-sm { padding: 4px 10px; font-size: 0.8rem; }
  .btn-success { background: #198754; color: #fff; }
  .btn-outline { background: transparent; border: 1px solid #dee2e6; color: #333; }
  .btn-outline:hover { background: #e9ecef; }
  .btn-danger { background: #dc3545; color: #fff; }
  .btn-warning { background: #ffc107; color: #000; }

  .tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 2px solid #dee2e6; }
  .tab {
    padding: 8px 16px; cursor: pointer; border: none; background: none;
    font-size: 0.95rem; border-bottom: 2px solid transparent; margin-bottom: -2px;
  }
  .tab.active { border-bottom-color: #0d6efd; font-weight: 600; color: #0d6efd; }

  .panel { display: none; }
  .panel.active { display: block; }

  .card {
    background: #fff; border-radius: 10px; padding: 14px; margin-bottom: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #dee2e6;
  }
  .card.needs-cleaner { border-left-color: #dc3545; }
  .card.assigned { border-left-color: #ffc107; }
  .card.confirmed { border-left-color: #198754; }
  .card.complete { border-left-color: #198754; background: var(--green); }
  .card.cancelled { border-left-color: #999; background: var(--red); opacity: 0.6; }
  .card.conflicted { border-left-color: #fd7e14; background: #fff8f0; }
  .card.urgent { animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { box-shadow: 0 1px 3px rgba(0,0,0,0.08); } 50% { box-shadow: 0 0 12px rgba(220,53,69,0.3); } }

  .card-header { display: flex; justify-content: space-between; align-items: start; }
  .dates { font-weight: 600; font-size: 1.05rem; }
  .cleaning-date { color: #0d6efd; font-size: 0.85rem; margin-top: 2px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
  }
  .badge-active { background: var(--blue); color: #004085; }
  .badge-complete { background: var(--green); color: #155724; }
  .badge-cancelled { background: var(--red); color: #721c24; }

  .card-actions {
    display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; align-items: center;
  }

  .assign-form select {
    padding: 4px 8px; border-radius: 4px; border: 1px solid #ccc; font-size: 0.85rem;
  }

  .whatsapp-box {
    width: 100%; min-height: 150px; border: 1px solid #ccc; border-radius: 6px;
    padding: 10px; font-size: 0.85rem; font-family: inherit; resize: vertical;
  }

  .wa-results { margin-top: 12px; }
  .wa-match {
    background: #fff; border-radius: 8px; padding: 10px; margin-bottom: 6px;
    border-left: 3px solid #dee2e6;
  }
  .wa-match.confirmed { border-left-color: #198754; background: #d4edda; }
  .wa-match.declined { border-left-color: #dc3545; background: #ffcccb; }
  .wa-match.unclear { border-left-color: #ffc107; background: #fff3cd; }
  .wa-match .wa-date { font-weight: 600; }
  .wa-match .wa-note { font-size: 0.85rem; color: #555; margin-top: 2px; }
  .wa-summary {
    background: #e2e3e5; border-radius: 8px; padding: 10px; margin-bottom: 12px;
    font-size: 0.9rem;
  }

  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
    gap: 8px; margin-bottom: 16px;
  }
  .stat {
    background: #fff; border-radius: 8px; padding: 12px; text-align: center;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  .stat-num { font-size: 1.8rem; font-weight: 700; }
  .stat-label { font-size: 0.75rem; color: #666; text-transform: uppercase; }

  .empty { text-align: center; color: #999; padding: 40px; }

  .config-warning {
    background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px;
    padding: 12px; margin-bottom: 16px; font-size: 0.9rem;
  }
  .error-box {
    background: #ffcccb; border: 1px solid #dc3545; border-radius: 8px;
    padding: 12px; margin-bottom: 12px; font-size: 0.9rem;
  }

  @media (max-width: 500px) {
    body { padding: 8px; }
    .card { padding: 10px; }
    .dates { font-size: 0.95rem; }
  }
"""

_REVIEW_PANEL = """
  <h2 style="font-size:1.05rem;margin-bottom:8px;">WhatsApp Review Queue</h2>
  <p class="subtitle" style="margin-bottom:12px;">
    Inbound messages parsed by Haiku that need a human decision.
  </p>

  {% if groups %}
  <h3 style="font-size:0.95rem;margin:14px 0 8px;">Groups</h3>
  <p class="subtitle" style="font-size:0.8rem;margin-bottom:8px;">
    Human-friendly names shown to the LLM instead of opaque JIDs.
  </p>
  {% for g in groups %}
  <div class="card" style="padding:10px;">
    <form action="{{ prefix }}/review/label_group" method="POST" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
      <input type="hidden" name="jid" value="{{ g.jid }}">
      <span style="font-size:0.75rem;color:#666;font-family:monospace;flex:1;min-width:180px;word-break:break-all;">{{ g.jid }}</span>
      <span style="font-size:0.75rem;color:#999;">{{ g.count }} msg{{ 's' if g.count != 1 }}</span>
      <input type="text" name="label" value="{{ g.label }}" placeholder="label (e.g. Maria group)" style="padding:4px 8px;border-radius:4px;border:1px solid #ccc;font-size:0.85rem;">
      <button type="submit" class="btn btn-sm btn-outline">Save</button>
    </form>
  </div>
  {% endfor %}
  {% endif %}

  {% if unmapped_senders %}
  <h3 style="font-size:0.95rem;margin:14px 0 8px;color:#fd7e14;">Unmapped senders</h3>
  {% for u in unmapped_senders %}
  <div class="card" style="border-left-color:#fd7e14;">
    <div style="font-size:0.85rem;color:#666;">{{ u.jid }} · in {{ u.group_label }} · {{ u.timestamp }}</div>
    <div style="margin:6px 0;font-size:0.9rem;">{{ u.first_text }}</div>
    <form action="{{ prefix }}/review/map" method="POST" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px;">
      <input type="hidden" name="jid" value="{{ u.jid }}">
      <select name="cleaner" style="padding:4px 8px;border-radius:4px;border:1px solid #ccc;">
        <option value="">-- map to existing cleaner --</option>
        {% for c in cleaners %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
      </select>
      <span style="font-size:0.85rem;color:#666;">or</span>
      <input type="text" name="new_cleaner" placeholder="new cleaner name" style="padding:4px 8px;border-radius:4px;border:1px solid #ccc;font-size:0.85rem;">
      <button type="submit" class="btn btn-sm btn-primary">Save mapping</button>
    </form>
    <form action="{{ prefix }}/review/ignore-sender" method="POST" style="margin-top:4px;">
      <input type="hidden" name="jid" value="{{ u.jid }}">
      <button type="submit" class="btn btn-sm btn-secondary">Not a cleaner (ignore)</button>
    </form>
  </div>
  {% endfor %}
  {% endif %}

  <h3 style="font-size:0.95rem;margin:14px 0 8px;">Pending messages</h3>
  {% if not pending %}
  <div class="empty">No messages pending review.</div>
  {% endif %}
  {% for m in pending %}
  <div class="card">
    <div style="font-size:0.8rem;color:#666;">
      {{ m.timestamp }} · from {{ m.sender_cleaner or m.sender }}
      {% if not m.sender_cleaner %}<span style="color:#fd7e14;"> · unmapped</span>{% endif %}
    </div>
    <div style="margin:6px 0;font-size:0.95rem;white-space:pre-wrap;">{{ m.text }}</div>
    <div style="font-size:0.85rem;color:#555;background:#f8f9fa;padding:6px 8px;border-radius:6px;margin-top:6px;">
      {% if m.parse_error %}
        <strong>Parse error:</strong> {{ m.parse_error }}
      {% elif m.haiku_action == 'none' or not m.haiku_action %}
        <strong>Haiku:</strong> not actionable{% if m.haiku_reason %} — {{ m.haiku_reason }}{% endif %}
      {% else %}
        <strong>Haiku suggests:</strong> {{ m.haiku_action }}
        {% if m.haiku_booking_label %} for {{ m.haiku_booking_label }}{% endif %}
        {% if m.haiku_cleaner %} by {{ m.haiku_cleaner }}{% endif %}
        {% if m.haiku_confidence is not none %} (conf {{ '%.0f' | format(m.haiku_confidence * 100) }}%){% endif %}
        {% if m.haiku_reason %}<div style="font-size:0.8rem;color:#666;margin-top:2px;">{{ m.haiku_reason }}</div>{% endif %}
      {% endif %}
      {% if m.auto_block %}<div style="font-size:0.8rem;color:#a94442;background:#f9e4e4;border-radius:4px;padding:4px 6px;margin-top:6px;">⚠️ {{ m.auto_block }}</div>{% endif %}
    </div>
    <form action="{{ prefix }}/review/accept/{{ m.id }}" method="POST" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px;">
      <select name="booking_uid" style="padding:4px 8px;border-radius:4px;border:1px solid #ccc;font-size:0.85rem;">
        <option value="">-- booking --</option>
        {% for opt in booking_options %}
        <option value="{{ opt.uid }}" {{ 'selected' if opt.uid == m.haiku_booking_uid }}>{{ opt.label }}</option>
        {% endfor %}
      </select>
      <select name="action" style="padding:4px 8px;border-radius:4px;border:1px solid #ccc;font-size:0.85rem;">
        <option value="confirm" {{ 'selected' if m.haiku_action == 'confirm' }}>confirm</option>
        <option value="decline" {{ 'selected' if m.haiku_action == 'decline' }}>decline</option>
      </select>
      <select name="cleaner" style="padding:4px 8px;border-radius:4px;border:1px solid #ccc;font-size:0.85rem;">
        <option value="">-- cleaner --</option>
        {% for c in cleaners %}
        <option value="{{ c }}" {{ 'selected' if (m.haiku_cleaner or m.sender_cleaner) == c }}>{{ c }}</option>
        {% endfor %}
      </select>
      <button type="submit" class="btn btn-sm btn-success">Accept</button>
    </form>
    <form action="{{ prefix }}/review/ignore/{{ m.id }}" method="POST" style="display:inline-block;margin-top:4px;">
      <button type="submit" class="btn btn-sm btn-outline">Ignore</button>
    </form>
  </div>
  {% endfor %}
"""


FOCUS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cleaning Schedule</title>
<style>
""" + _SHARED_STYLES + """
  body { max-width: 560px; }
  h1 { text-align: center; }
  .subtitle { text-align: center; }
  .sync-bar { justify-content: center; }
  .tabs { justify-content: center; }
  .focus-pager {
    display: flex; justify-content: space-between; align-items: center;
    font-size: 0.85rem; color: #666; margin-bottom: 12px;
  }
  .focus-card {
    background: #fff; border-radius: 14px; padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 16px;
  }
  .focus-name { font-size: 1.5rem; font-weight: 700; margin-bottom: 14px; }
  .diff-list { list-style: none; padding: 0; margin: 0 0 18px 0; }
  .diff-item { padding: 10px 0; border-bottom: 1px solid #eef1f4; font-size: 0.95rem; }
  .diff-item:last-child { border-bottom: none; }
  .kind {
    display: inline-block; font-size: 0.68rem; font-weight: 700;
    padding: 2px 7px; border-radius: 10px; text-transform: uppercase;
    margin-right: 6px; vertical-align: 1px; letter-spacing: 0.03em;
  }
  .kind.new { background: #cce5ff; color: #004085; }
  .kind.changed { background: #fff3cd; color: #856404; }
  .kind.cancelled { background: #ffcccb; color: #721c24; }
  .diff-detail { color: #666; font-size: 0.82rem; margin-top: 3px; }
  .focus-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .focus-actions .btn { flex: 1 1 auto; min-width: 140px; text-align: center; }
  .empty-state { text-align: center; padding: 32px 12px; color: #666; }
  .empty-state .check { font-size: 2.4rem; color: #198754; margin-bottom: 10px; }
  .unassigned-card {
    background: #fff; border-radius: 14px; padding: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 16px;
    border-left: 4px solid #dc3545;
  }
  .unassigned-header { font-weight: 700; margin-bottom: 10px; font-size: 0.95rem; }
  .unassigned-row {
    display: flex; gap: 8px; align-items: center;
    padding: 8px 0; border-bottom: 1px solid #f0f0f0; flex-wrap: wrap;
  }
  .unassigned-row:last-child { border-bottom: none; padding-bottom: 0; }
  .unassigned-row .date { font-weight: 600; flex: 0 0 90px; font-size: 0.9rem; }
  .unassigned-row form { display: flex; gap: 4px; flex: 1; flex-wrap: wrap; }
  .unassigned-row select {
    flex: 1; min-width: 120px; padding: 5px 8px;
    border-radius: 4px; border: 1px solid #ccc; font-size: 0.85rem;
  }
  .pager-link {
    background: transparent; border: 1px solid #dee2e6; color: #333;
    padding: 5px 12px; border-radius: 6px; text-decoration: none;
    font-size: 0.85rem;
  }
  .pager-link.disabled { opacity: 0.35; pointer-events: none; }
  .conflict-card {
    background: #fff; border-radius: 10px; padding: 12px 14px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.06); margin-bottom: 10px;
    border-left: 4px solid #ccc;
  }
  .conflict-card.conflict-needs-attention { border-left-color: #dc3545; }
  .conflict-card.conflict-suggest { border-left-color: #fd7e14; }
  .conflict-card.conflict-informational { border-left-color: #6c757d; }
  .conflict-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
  .sev {
    font-size: 0.65rem; font-weight: 700; letter-spacing: 0.04em;
    padding: 2px 7px; border-radius: 10px; text-transform: uppercase;
  }
  .sev-needs-attention { background: #f8d7da; color: #721c24; }
  .sev-suggest { background: #ffe4c4; color: #7a4a00; }
  .sev-informational { background: #e9ecef; color: #495057; }
  .conflict-kind { font-family: ui-monospace, monospace; font-size: 0.78rem; color: #555; }
  .conflict-date { margin-left: auto; font-size: 0.82rem; color: #666; }
  .conflict-why { font-size: 0.93rem; margin-bottom: 4px; }
  .conflict-quote { font-size: 0.82rem; color: #666; font-style: italic; margin-bottom: 8px; }
  .conflict-actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .conflict-actions .btn { font-size: 0.82rem; padding: 4px 10px; }
</style>
</head>
<body>

<h1>Cleaning Schedule</h1>
<p class="subtitle">
  Last synced: {{ last_sync or "Never" }}
  {% if error %}<br><span style="color:red">Sync error: {{ error }}</span>{% endif %}
</p>

{% if no_ical %}
<div class="config-warning">
  No iCal URL configured. Go to <strong>Settings &gt; Add-ons &gt; Cleaning Schedule Tracker &gt; Configuration</strong> and set your Airbnb calendar URL.
</div>
{% endif %}

<div class="sync-bar">
  <form action="{{ prefix }}/sync" method="POST">
    <button type="submit" class="btn btn-primary">Sync Airbnb</button>
  </form>
  <a href="{{ prefix }}/add" class="btn btn-outline">+ Add</a>
  <a href="{{ prefix }}/print" class="btn btn-outline">Print</a>
  {% if gcal_enabled %}
  <form action="{{ prefix }}/gcal/sync" method="POST">
    <button type="submit" class="btn btn-outline">Sync GCal</button>
  </form>
  {% endif %}
</div>

<div class="tabs">
  <button class="tab active" onclick="showTab('notify-tab', this)" id="notify-tab-btn">
    Notify{% if total_count %} <span style="background:#fd7e14;color:#fff;border-radius:10px;padding:1px 8px;font-size:0.75rem;margin-left:4px;">{{ total_count }}</span>{% endif %}
  </button>
  <button class="tab" onclick="showTab('review-tab', this)" id="review-tab-btn">
    WhatsApp{% if pending_count %} <span style="background:#dc3545;color:#fff;border-radius:10px;padding:1px 8px;font-size:0.75rem;margin-left:4px;">{{ pending_count }}</span>{% endif %}
  </button>
  <button class="tab" onclick="showTab('conflicts-tab', this)" id="conflicts-tab-btn">
    Conflicts{% if conflicts_attn %} <span style="background:#dc3545;color:#fff;border-radius:10px;padding:1px 8px;font-size:0.75rem;margin-left:4px;">{{ conflicts_attn }}</span>{% endif %}
  </button>
  {% if bridge_enabled %}
  <button class="tab" onclick="showTab('bridge-tab', this)" id="bridge-tab-btn">
    Bridge <span title="{{ 'Running' if bridge_up else 'NOT running' }}" style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-left:5px;vertical-align:middle;background:{{ '#28a745' if bridge_up else '#dc3545' }};"></span>
  </button>
  {% endif %}
</div>

<div id="notify-tab" class="panel active">
  {% if unassigned %}
  <div class="unassigned-card">
    <div class="unassigned-header">Unassigned bookings ({{ unassigned|length }}) · <a href="{{ prefix }}/admin/ingest" style="font-weight:500;font-size:0.85rem;">Ingest transcript</a></div>
    {% for item in unassigned %}
    <div class="unassigned-row">
      <span class="date">{{ item.date_fmt }}</span>
      <form action="{{ prefix }}/assign/{{ item.uid }}" method="POST">
        <select name="cleaner" required>
          <option value="">-- pick cleaner --</option>
          {% for c in cleaners %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
        </select>
        <button type="submit" class="btn btn-sm btn-primary">Assign</button>
      </form>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if current_bucket %}
  <div class="focus-pager">
    <a class="pager-link {{ 'disabled' if prev_index is none }}"
       href="{% if prev_index is not none %}{{ prefix }}/?i={{ prev_index }}{% else %}#{% endif %}">← Prev</a>
    <span>Cleaner {{ current_index + 1 }} of {{ total_cleaners }}</span>
    <a class="pager-link {{ 'disabled' if next_index is none }}"
       href="{% if next_index is not none %}{{ prefix }}/?i={{ next_index }}{% else %}#{% endif %}">Skip →</a>
  </div>
  <div class="focus-card">
    <div class="focus-name">{{ current_bucket.cleaner }}</div>
    <ul class="diff-list">
      {% for item in current_bucket['items'] %}
      <li class="diff-item">
        <span class="kind {{ item.kind }}">{{ item.kind }}</span>
        {{ item.line }}
        {% if item.detail %}<div class="diff-detail">{{ item.detail }}</div>{% endif %}
        <div style="margin-top:4px;"><a href="{{ prefix }}/edit/{{ item.uid }}" style="font-size:0.8rem;color:#0d6efd;">Edit details</a></div>
      </li>
      {% endfor %}
    </ul>
    <div class="focus-actions">
      <form action="{{ prefix }}/review/notify/{{ current_bucket.cleaner_slug }}" method="POST" style="flex:1 1 auto;">
        <input type="hidden" name="i" value="{{ current_index }}">
        <button type="submit" class="btn btn-success">Mark notified</button>
      </form>
    </div>
  </div>
  {% elif not unassigned %}
  <div class="focus-card empty-state">
    <div class="check">✓</div>
    <div style="font-weight:700;font-size:1.1rem;margin-bottom:6px;">All cleaners up to date</div>
    <div style="font-size:0.9rem;color:#666;">Nothing to notify. Changes will appear here when Airbnb or a cleaner updates.</div>
  </div>
  {% endif %}
</div>

<div id="review-tab" class="panel">
""" + _REVIEW_PANEL + """
</div>

<div id="conflicts-tab" class="panel">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
    <div style="font-size:0.85rem;color:#666;">
      {% if conflicts_generated_at %}Last run: {{ conflicts_generated_at }}{% else %}Never run{% endif %}
      {% if conflicts_total is not none %} · {{ conflicts_total }} open{% endif %}
      {% if conflicts_dismissed %} · {{ conflicts_dismissed }} dismissed{% endif %}
    </div>
    <form action="{{ prefix }}/reconcile/run" method="POST" style="display:inline;">
      <button type="submit" class="btn btn-sm btn-primary">Re-run</button>
    </form>
    {% if digest_enabled %}
    <form action="{{ prefix }}/digest/run" method="POST" style="display:inline;margin-left:6px;">
      <button type="submit" class="btn btn-sm btn-outline">Run digest</button>
    </form>
    {% endif %}
  </div>
  {% if digest_enabled and digest_run_at is defined %}
  <div style="font-size:0.8rem;color:#888;margin-bottom:10px;">
    Digest last ran: {{ digest_run_at }}
    {% if digest_title %} — {{ digest_title }}{% endif %}
    {% if digest_notified is defined %}
      {% if digest_notified %}
        <span style="color:#2a7;margin-left:4px;">✓ HA notified</span>
      {% else %}
        <span style="color:#c33;margin-left:4px;">✗ HA notification failed — check add-on logs</span>
      {% endif %}
    {% endif %}
  </div>
  {% endif %}

  {% if conflicts_findings %}
    {% for f in conflicts_findings %}
    <div class="conflict-card conflict-{{ f.severity }}">
      <div class="conflict-head">
        <span class="sev sev-{{ f.severity }}">{{ f.severity }}</span>
        <span class="conflict-kind">{{ f.kind }}</span>
        {% if f.date %}<span class="conflict-date">{{ f.date }}</span>{% endif %}
      </div>
      <div class="conflict-why">{{ f.why }}</div>
      {% if f.quote %}<div class="conflict-quote">&ldquo;{{ f.quote }}&rdquo;</div>{% endif %}
      <div class="conflict-actions">
        {% if f.decision == 'approve' and f.booking_uid and f.cleaner %}
        <form action="{{ prefix }}/assign/{{ f.booking_uid }}" method="POST" style="display:inline;">
          <input type="hidden" name="cleaner" value="{{ f.cleaner }}">
          <button type="submit" class="btn btn-sm btn-success">Assign {{ f.cleaner }}</button>
        </form>
        {% endif %}
        {% if f.booking_uid %}
        <a href="{{ prefix }}/edit/{{ f.booking_uid }}" class="btn btn-sm btn-outline">Edit booking</a>
        {% endif %}
        <form action="{{ prefix }}/reconcile/dismiss" method="POST" style="display:inline;">
          <input type="hidden" name="finding_id" value="{{ f.id }}">
          <button type="submit" class="btn btn-sm btn-outline">Dismiss</button>
        </form>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="focus-card empty-state">
      <div class="check">✓</div>
      <div style="font-weight:700;font-size:1.1rem;margin-bottom:6px;">No open conflicts</div>
      <div style="font-size:0.9rem;color:#666;">Re-run to refresh.</div>
    </div>
  {% endif %}
</div>

{% if bridge_enabled %}
<div id="bridge-tab" class="panel">
  <style>
    .bw-hero { display:flex; align-items:center; gap:12px; padding:14px 16px; border-radius:8px;
               margin-bottom:14px; border:1px solid #e3e3e3; }
    .bw-hero.up   { background:#eef9f1; border-color:#c3e6cd; }
    .bw-hero.down { background:#fdecee; border-color:#f5c2c7; }
    .bw-dot { width:14px; height:14px; border-radius:50%; flex:none; }
    .bw-stats { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; }
    .bw-stat { flex:1 1 130px; border:1px solid #e6e6e6; border-radius:8px; padding:10px 12px; background:#fff; }
    .bw-stat .n { font-size:1.4rem; font-weight:700; line-height:1.1; }
    .bw-stat .l { font-size:0.75rem; color:#777; margin-top:2px; }
    .bw-strip { display:flex; gap:3px; margin:6px 0 4px; }
    .bw-day { flex:1 1 0; height:34px; border-radius:3px; min-width:6px; }
    .bw-day.ok      { background:#28a745; }
    .bw-day.partial { background:#28a745; opacity:.42; }
    .bw-day.warn    { background:#fd7e14; }
    .bw-day.bad     { background:#dc3545; }
    .bw-day.nodata  { background:#e0e0e0; }
    .bw-legend { font-size:0.75rem; color:#777; display:flex; flex-wrap:wrap; gap:12px; margin-top:6px; }
    .bw-legend span::before { content:''; display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:4px; }
    .bw-tbl { width:100%; border-collapse:collapse; font-size:0.85rem; }
    .bw-tbl th { text-align:left; font-size:0.72rem; text-transform:uppercase; color:#888; padding:4px 6px; }
    .bw-tbl td { padding:5px 6px; border-top:1px solid #eee; }
    .bw-act { font-weight:600; }
    .bw-act.restarted, .bw-act.restart_failed, .bw-act.observed_down { color:#dc3545; }
    .bw-act.recovered { color:#28a745; }
    .bw-act.probe_failed, .bw-act.no_token { color:#fd7e14; }
  </style>

  {% if bridge_error %}
    <div class="focus-card empty-state"><div style="color:#dc3545;">Could not read watchdog state: {{ bridge_error }}</div></div>
  {% else %}
  <div class="bw-hero {{ 'up' if bridge_up else 'down' }}">
    <div class="bw-dot" style="background:{{ '#28a745' if bridge_up else '#dc3545' }};"></div>
    <div>
      <div style="font-weight:700;font-size:1.05rem;">
        WhatsApp bridge is {{ 'running' if bridge_up else 'NOT running' }}
      </div>
      <div style="font-size:0.82rem;color:#666;">
        Last checked {{ bridge.last_check or 'never' }} · every {{ bridge.interval_min or '?' }} min
        {% if bridge.probe_error %} · <span style="color:#dc3545;">{{ bridge.probe_error }}</span>{% endif %}
      </div>
    </div>
  </div>

  <div class="bw-stats">
    <div class="bw-stat">
      <div class="n">{% if bridge.healthy_pct is not none %}{{ bridge.healthy_pct }}%{% else %}—{% endif %}</div>
      <div class="l">checks found it up</div>
    </div>
    <div class="bw-stat">
      <div class="n">{{ bridge.restarts_7d if bridge.restarts_7d is not none else '—' }}</div>
      <div class="l">restarts, last 7 days</div>
    </div>
    <div class="bw-stat">
      <div class="n">{{ bridge.restarts_30d if bridge.restarts_30d is not none else '—' }}</div>
      <div class="l">restarts, last 30 days</div>
    </div>
    <div class="bw-stat">
      <div class="n">{{ bridge.down_episodes_30d if bridge.down_episodes_30d is not none else '—' }}</div>
      <div class="l">times it went down</div>
    </div>
    <div class="bw-stat">
      <div class="n">{{ bridge.checks_logged or 0 }}</div>
      <div class="l">checks recorded</div>
    </div>
  </div>

  <div style="font-weight:600;font-size:0.9rem;margin-bottom:2px;">Last 30 days</div>
  <div class="bw-strip">
    {% for d in bridge_days %}<div class="bw-day {{ d.cls }}" title="{{ d.label }}"></div>{% endfor %}
  </div>
  <div style="display:flex;justify-content:space-between;font-size:0.72rem;color:#999;">
    <span>{{ bridge_days[0].date if bridge_days else '' }}</span><span>today</span>
  </div>
  <div class="bw-legend">
    <span style="--c:#28a745;"><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#28a745;margin-right:4px;"></i>all healthy</span>
    <span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#28a745;opacity:.42;margin-right:4px;"></i>fewer checks than expected</span>
    <span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#fd7e14;margin-right:4px;"></i>seen unhealthy</span>
    <span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#dc3545;margin-right:4px;"></i>restarted</span>
    <span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:#e0e0e0;margin-right:4px;"></i>no checks recorded</span>
  </div>

  <div style="font-weight:600;font-size:0.9rem;margin:18px 0 4px;">
    Things that happened{% if bridge_actions %} ({{ bridge_actions|length }}){% endif %}
  </div>
  {% if bridge_actions %}
  <table class="bw-tbl">
    <tr><th>When</th><th>State</th><th>Action</th><th>Detail</th></tr>
    {% for a in bridge_actions %}
    <tr>
      <td style="white-space:nowrap;">{{ a.at|replace('T',' ') }}</td>
      <td>{{ a.state or '—' }}{% if a.from_state %} <span style="color:#999;">(was {{ a.from_state }})</span>{% endif %}</td>
      <td class="bw-act {{ a.action }}">{{ a.action|replace('_',' ') }}</td>
      <td style="color:#666;">{{ a.detail or '' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div style="font-size:0.85rem;color:#666;padding:10px 0;">
    Nothing but healthy checks in the window — no restarts, no outages, no failed probes.
  </div>
  {% endif %}

  <div style="font-size:0.75rem;color:#888;margin-top:16px;line-height:1.5;border-top:1px solid #eee;padding-top:10px;">
    ⚠️ Restart counts are the ones <em>this</em> watchdog performed. Home Assistant's own
    add-on watchdog is also enabled on the bridge and reacts faster than this 5-minute
    check, so a crash it repairs in between is never seen here — the real number is at
    least this one, possibly higher. A grey day means no checks were recorded at all,
    which is a gap in the evidence rather than a healthy day.
  </div>
  {% endif %}
</div>
{% endif %}

<script>
function showTab(id, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  history.replaceState(null, '', '#' + id.replace('-tab',''));
}
if (location.hash === '#review') {
  document.addEventListener('DOMContentLoaded', function() {
    showTab('review-tab', document.getElementById('review-tab-btn'));
  });
} else if (location.hash === '#conflicts') {
  document.addEventListener('DOMContentLoaded', function() {
    showTab('conflicts-tab', document.getElementById('conflicts-tab-btn'));
  });
} else if (location.hash === '#bridge') {
  document.addEventListener('DOMContentLoaded', function() {
    showTab('bridge-tab', document.getElementById('bridge-tab-btn'));
  });
}
</script>

<style>
  .app-footer { margin: 28px auto 12px; text-align: center; }
  .vps-widget { display: inline-flex; align-items: center; gap: 7px;
    font-size: 0.8rem; color: #666; padding: 5px 12px; border: 1px solid #e3e3e3;
    border-radius: 14px; background: #fafafa; }
  .vps-dot { width: 9px; height: 9px; border-radius: 50%; background: #bbb;
    flex: 0 0 auto; }
  .vps-dot.up { background: #28a745; }
  .vps-dot.down { background: #dc3545; }
  .vps-dot.checking { background: #f0ad4e; }
</style>
<footer class="app-footer">
  <span id="vps-widget" class="vps-widget" style="display:none;">
    <span id="vps-dot" class="vps-dot checking"></span>
    <span id="vps-text">checking…</span>
  </span>
</footer>
<script>
(function() {
  var prefix = "{{ prefix }}";
  function render(d) {
    var w = document.getElementById('vps-widget');
    var dot = document.getElementById('vps-dot');
    var txt = document.getElementById('vps-text');
    if (!d || !d.enabled) { w.style.display = 'none'; return; }
    w.style.display = 'inline-flex';
    var label = d.label || 'VPS';
    if (d.reachable) {
      dot.className = 'vps-dot up';
      var lat = (d.latency_ms != null) ? (' · ' + d.latency_ms + 'ms') : '';
      var code = d.http_status ? (' · HTTP ' + d.http_status) : '';
      txt.textContent = label + ' online' + lat + code;
    } else {
      dot.className = 'vps-dot down';
      txt.textContent = label + ' unreachable' + (d.error ? ' (' + d.error + ')' : '');
    }
    if (d.checked_at) txt.title = 'last checked ' + d.checked_at;
  }
  function poll() {
    fetch(prefix + '/vps/status')
      .then(function(r) { return r.json(); })
      .then(render)
      .catch(function() {
        var dot = document.getElementById('vps-dot');
        var txt = document.getElementById('vps-text');
        if (dot) dot.className = 'vps-dot down';
        if (txt) txt.textContent = 'VPS check failed';
      });
  }
  poll();
  setInterval(poll, 60000);
})();
</script>
</body>
</html>
"""



ADD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add Entry</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; padding: 20px; max-width: 500px; margin: 0 auto; }
  label { display: block; margin: 12px 0 4px; font-weight: 600; }
  input, select, textarea { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 1rem; }
  button { margin-top: 16px; padding: 10px 24px; background: #0d6efd; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 1rem; }
  a { color: #0d6efd; }
  .type-radio { display: flex; gap: 16px; margin-bottom: 16px; }
  .type-radio label { display: flex; align-items: center; gap: 6px; font-weight: 600; cursor: pointer; margin: 0; }
  .type-radio input[type=radio] { width: auto; }
</style>
</head>
<body>
<h2>Add Entry</h2>
<form action="{{ prefix }}/add" method="POST">
  <div class="type-radio">
    <span style="font-weight:600;margin-right:4px;">Type:</span>
    <label><input type="radio" name="entry_type" value="cleaning" checked onchange="toggleType()"> Cleaning</label>
    <label><input type="radio" name="entry_type" value="stay" onchange="toggleType()"> Stay</label>
  </div>

  <div id="cleaning-fields">
    <label>Cleaning Date</label>
    <input type="date" name="date" value="{{ prefill_date or '' }}">
    <label>Cleaner</label>
    <select name="cleaner">
      <option value="">-- Select --</option>
      {% for c in cleaners %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
    </select>
  </div>

  <div id="stay-fields" style="display:none;">
    <label>Start Date</label>
    <input type="date" name="start_date" value="{{ prefill_date or '' }}">
    <label>End Date</label>
    <input type="date" name="end_date">
  </div>

  <label>Notes</label>
  <textarea name="notes" rows="2" placeholder="e.g., Mom visiting, deep clean"></textarea>
  <br>
  <button type="submit">Add</button>
  <a href="{{ prefix }}/" style="margin-left:12px;">Cancel</a>
</form>
<script>
function toggleType() {
  var val = document.querySelector('input[name=entry_type]:checked').value;
  document.getElementById('cleaning-fields').style.display = val === 'cleaning' ? '' : 'none';
  document.getElementById('stay-fields').style.display = val === 'stay' ? '' : 'none';
}
</script>
</body>
</html>
"""

EDIT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit Booking</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; padding: 20px; max-width: 500px; margin: 0 auto; }
  h2 { margin-bottom: 4px; }
  .meta { color: #666; font-size: 0.9rem; margin-bottom: 20px; }
  .section { margin-bottom: 20px; padding: 14px; background: #f8f9fa; border-radius: 8px; }
  .section h3 { font-size: 1rem; margin-bottom: 10px; }
  label { display: block; margin: 10px 0 4px; font-weight: 600; font-size: 0.9rem; }
  select, input[type=text] { width: 100%; padding: 7px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.95rem; }
  button { padding: 8px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.9rem; font-weight: 500; }
  .btn-primary { background: #0d6efd; color: #fff; }
  .btn-success { background: #198754; color: #fff; }
  .btn-warning { background: #ffc107; color: #000; }
  .btn-danger { background: #dc3545; color: #fff; }
  .btn-outline { background: transparent; border: 1px solid #ccc; color: #333; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  a { color: #0d6efd; font-size: 0.9rem; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
  .delete-zone { margin-top: 24px; padding: 12px; border: 1px solid #dc3545; border-radius: 8px; }
  .delete-zone h3 { color: #dc3545; font-size: 0.95rem; margin-bottom: 8px; }
</style>
</head>
<body>
<a href="{{ prefix }}/">&larr; Back</a>
<h2 style="margin-top:12px;">
  {% if booking.type == 'manual_cleaning' %}Manual Cleaning
  {% elif booking.type == 'custom_stay' %}Custom Stay
  {% else %}Airbnb Booking{% endif %}
</h2>
<div class="meta">
  {{ booking.start }} &rarr; {{ booking.end }}
  &nbsp;&middot;&nbsp;
  <span class="badge" style="
    {% if booking.status == 'active' %}background:#cce5ff;color:#004085
    {% elif booking.status == 'complete' %}background:#d4edda;color:#155724
    {% else %}background:#ffcccb;color:#721c24{% endif %}
  ">{{ booking.status }}</span>
</div>

<!-- Assign cleaner -->
<div class="section">
  <h3>Cleaner Assignment</h3>
  <form action="{{ prefix }}/assign/{{ uid }}" method="POST">
    <label>Cleaner</label>
    <select name="cleaner">
      <option value="">-- None --</option>
      {% for c in cleaners %}
      <option value="{{ c }}" {{ 'selected' if booking.cleaner == c }}>{{ c }}</option>
      {% endfor %}
    </select>
    <label>Cleaning Time</label>
    <input type="time" name="clean_time" value="{{ booking.clean_time[:5] if booking.clean_time else '' }}" style="width:100%;padding:7px;border:1px solid #ccc;border-radius:6px;font-size:0.95rem;">
    <div class="actions">
      <button type="submit" class="btn-primary">Save</button>
    </div>
  </form>
</div>

<!-- Confirm / Pay -->
<div class="section">
  <h3>Status</h3>
  <div class="actions">
    {% if booking.cleaner and not booking.confirmed %}
    <form action="{{ prefix }}/confirm/{{ uid }}" method="POST" style="display:inline;">
      <button type="submit" class="btn-success">Mark Confirmed</button>
    </form>
    {% elif booking.confirmed %}
    <span class="badge" style="background:#d4edda;color:#155724">Confirmed</span>
    {% endif %}
    {% if not booking.paid %}
    <form action="{{ prefix }}/pay/{{ uid }}" method="POST" style="display:inline;">
      <button type="submit" class="btn-warning">Mark Paid</button>
    </form>
    {% else %}
    <span class="badge" style="background:#d4edda;color:#155724">Paid</span>
    {% endif %}
  </div>
  {% if booking.notes %}
  <div style="margin-top:10px;font-size:0.85rem;color:#555;">Notes: {{ booking.notes }}</div>
  {% endif %}
</div>

<!-- Dismiss (cancelled bookings) -->
{% if booking.status == 'cancelled' %}
<div class="delete-zone">
  <h3>Dismiss</h3>
  <p style="font-size:0.85rem;color:#666;margin-bottom:10px;">Remove this cancelled booking from the calendar.</p>
  <form action="{{ prefix }}/delete/{{ uid }}" method="POST" onsubmit="return confirm('Dismiss this cancelled booking?');">
    <button type="submit" class="btn-danger">Dismiss</button>
  </form>
</div>
{% elif deletable %}
<div class="delete-zone">
  <h3>Delete</h3>
  <p style="font-size:0.85rem;color:#666;margin-bottom:10px;">Permanently removes this entry. Cannot be undone.</p>
  <form action="{{ prefix }}/delete/{{ uid }}" method="POST" onsubmit="return confirm('Delete this entry?');">
    <button type="submit" class="btn-danger">Delete</button>
  </form>
</div>
{% endif %}
</body>
</html>
"""


PRINT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ month_label }} — Print View</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 11px; background: #fff; color: #000; }
  .nav-bar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: #f8f9fa; border-bottom: 1px solid #dee2e6; flex-wrap: wrap; }
  .nav-bar a { color: #0d6efd; text-decoration: none; font-size: 0.9rem; }
  .nav-bar a:hover { text-decoration: underline; }
  .nav-bar h2 { font-size: 1.1rem; flex: 1; text-align: center; }
  .print-btn { padding: 6px 14px; background: #0d6efd; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-size: 0.85rem; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  th { background: #212529; color: #fff; text-align: center; padding: 4px 2px; font-size: 10px; font-weight: 700; border: 1px solid #000; }
  td { border: 1px solid #ccc; vertical-align: top; height: 80px; padding: 2px 3px; width: 14.285%; }
  td.other-month { background: #f5f5f5; }
  .day-num { font-weight: 700; font-size: 11px; margin-bottom: 2px; }
  .stay-bar {
    display: block; font-size: 9px; padding: 1px 3px; border-radius: 2px; margin-bottom: 1px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  .cleaning-line { font-size: 9px; font-weight: 700; margin-top: 2px; padding: 1px 2px; border-radius: 2px; }
  .cleaning-unassigned { color: #dc3545; }
  @media print {
    .nav-bar { display: none !important; }
    body { font-size: 10px; }
    td { height: 70px; border: 1px solid #000; }
    th { border: 1px solid #000; }
    @page { size: landscape; margin: 0.5in; }
  }
</style>
</head>
<body>

<div class="nav-bar">
  <a href="{{ prefix }}/print?month={{ prev_month }}">&laquo; Prev</a>
  <h2>{{ month_label }}</h2>
  <a href="{{ prefix }}/print?month={{ next_month }}">Next &raquo;</a>
  <a href="{{ prefix }}/">Back to app</a>
  <button class="print-btn" onclick="window.print()">Print this page</button>
</div>

<table>
  <thead>
    <tr>
      <th>Sun</th><th>Mon</th><th>Tue</th><th>Wed</th><th>Thu</th><th>Fri</th><th>Sat</th>
    </tr>
  </thead>
  <tbody>
    {% for week in weeks %}
    <tr>
      {% for cell in week %}
      <td class="{{ '' if cell.in_month else 'other-month' }}">
        {% if cell.day %}
        <div class="day-num">{{ cell.day }}</div>
        {% for stay in cell.stays %}
        <span class="stay-bar" style="background:{{ stay.color }};">
          {% if stay.is_start %}&#9654; {% endif %}{{ stay.title }}{% if stay.is_end %} &#9664;{% endif %}
        </span>
        {% endfor %}
        {% for cl in cell.cleanings %}
        {% if cl.cleaner %}
        <div class="cleaning-line" style="color:{{ cl.color }};">&#9986; {{ cl.cleaner }}{% if cl.confirmed %} &#10003;{% endif %}</div>
        {% else %}
        <div class="cleaning-line cleaning-unassigned">&#9986; ??</div>
        {% endif %}
        {% endfor %}
        {% endif %}
      </td>
      {% endfor %}
    </tr>
    {% endfor %}
  </tbody>
</table>

</body>
</html>
"""



# ── Print-view helper ────────────────────────────────────────────────────────

def build_print_data(month_str: str, bookings: dict) -> dict:
    """Build the data structure for the /print month-grid view."""
    month_dt = datetime.strptime(month_str, "%Y-%m")
    year = month_dt.year
    month = month_dt.month

    month_label = month_dt.strftime("%B %Y")

    # Prev/next month strings
    first_of_month = date(year, month, 1)
    prev_first = first_of_month - timedelta(days=1)
    prev_month = prev_first.strftime("%Y-%m")
    last_day_num = calendar.monthrange(year, month)[1]
    last_of_month = date(year, month, last_day_num)
    next_first = last_of_month + timedelta(days=1)
    next_month = next_first.strftime("%Y-%m")

    # Grid: Sun=0 ... Sat=6.  Find first Sunday at-or-before day 1.
    # date.weekday(): Mon=0..Sun=6  →  Sunday offset = (weekday + 1) % 7
    sun_offset = (first_of_month.weekday() + 1) % 7
    grid_start = first_of_month - timedelta(days=sun_offset)
    # Last Saturday at-or-after last day of month
    sat_offset = (5 - last_of_month.weekday()) % 7  # days until Saturday (weekday 5)
    grid_end = last_of_month + timedelta(days=sat_offset)

    # Build a dict: iso_date -> cell
    cells = {}
    cur = grid_start
    while cur <= grid_end:
        cells[cur.isoformat()] = {
            "day": cur.day if cur.month == month else None,
            "iso": cur.isoformat(),
            "in_month": cur.month == month,
            "stays": [],
            "cleanings": [],
        }
        cur += timedelta(days=1)

    # Populate stays and cleanings
    for uid, b in bookings.items():
        btype = b.get("type", "airbnb")
        status = b.get("status", "active")
        if status == "cancelled":
            continue

        b_start = date.fromisoformat(b["start"])
        b_end = date.fromisoformat(b["end"])

        # Stay bars for airbnb and custom_stay
        if btype in ("airbnb", "custom_stay"):
            stay_color = "#cfe2ff" if btype == "airbnb" else "#d1e7dd"
            title = b.get("notes") or ("Airbnb" if btype == "airbnb" else "Custom stay")
            # Iterate every day of the stay that falls in the grid
            d = max(b_start, grid_start)
            end_iter = min(b_end - timedelta(days=1), grid_end)  # stay end is exclusive checkout
            while d <= end_iter:
                if d.isoformat() in cells:
                    cells[d.isoformat()]["stays"].append({
                        "title": title,
                        "color": stay_color,
                        "is_start": d == b_start,
                        "is_end": d == b_end - timedelta(days=1),
                    })
                d += timedelta(days=1)

        # Cleaning annotations
        if btype == "custom_stay":
            continue
        # For airbnb: cleaning on checkout (b_end). For manual_cleaning: b_end == b_start.
        clean_date = b_end
        if clean_date.isoformat() in cells:
            cleaner = b.get("cleaner")
            cells[clean_date.isoformat()]["cleanings"].append({
                "cleaner": cleaner,
                "confirmed": b.get("confirmed", False),
                "color": cleaner_color(cleaner) if cleaner else "#dc3545",
            })

    # Arrange into weeks
    weeks = []
    ordered = sorted(cells.values(), key=lambda c: c["iso"])
    for i in range(0, len(ordered), 7):
        weeks.append(ordered[i:i + 7])

    return {
        "month_label": month_label,
        "prev_month": prev_month,
        "next_month": next_month,
        "weeks": weeks,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

def _fmt_date_short(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().strftime("%b %d")
    except (ValueError, TypeError):
        return s


def _fmt_time_12h(t):
    if not t:
        return None
    try:
        return datetime.strptime(t, "%H:%M:%S").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return t


def _describe_item(item):
    """Turn a review_queue item into {line, detail} strings for the focus view."""
    kind = item["kind"]
    date_fmt = _fmt_date_short(item["date"])
    now = item.get("now")
    was = item.get("was")

    if kind == "new":
        time_fmt = _fmt_time_12h(now[2]) if now else None
        line = f"{date_fmt}"
        if time_fmt:
            line += f" at {time_fmt}"
        return {"line": line, "detail": "New cleaning — first-time notify."}

    if kind == "changed":
        parts = []
        if was and now:
            if was[0] != now[0]:
                parts.append(f"cleaner: {was[0]} → {now[0]}")
            if was[1] != now[1]:
                parts.append(f"date: {_fmt_date_short(was[1])} → {_fmt_date_short(now[1])}")
            if was[2] != now[2]:
                parts.append(
                    f"time: {_fmt_time_12h(was[2]) or '—'} → {_fmt_time_12h(now[2]) or '—'}"
                )
        time_fmt = _fmt_time_12h(now[2]) if now else None
        line = f"{date_fmt}" + (f" at {time_fmt}" if time_fmt else "")
        return {"line": line, "detail": "; ".join(parts) or "Details changed."}

    if kind == "cancelled":
        was_time = _fmt_time_12h(was[2]) if was else None
        line = f"{date_fmt}" + (f" at {was_time}" if was_time else "")
        return {"line": line, "detail": "Cleaning cancelled — tell the cleaner."}

    # unassigned never reaches this path — handled separately
    return {"line": date_fmt, "detail": None}


def _cleaner_slug(name):
    """URL-safe slug for a cleaner name. Handles unicode by lowercasing ASCII and
    replacing anything non-alphanumeric with a dash."""
    if not name:
        return "none"
    out = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return out or "none"


def build_focus_context(data, requested_index):
    """Build the full template context for the focus view."""
    buckets, unassigned = review_queue(data)

    # Annotate items with display strings.
    for bk in buckets:
        bk["cleaner_slug"] = _cleaner_slug(bk["cleaner"])
        for it in bk["items"]:
            it.update(_describe_item(it))
    for it in unassigned:
        it["date_fmt"] = _fmt_date_short(it["date"])

    total_cleaners = len(buckets)
    try:
        idx = max(0, int(requested_index))
    except (TypeError, ValueError):
        idx = 0
    if total_cleaners == 0:
        idx = 0
        current_bucket = None
        prev_index = None
        next_index = None
    else:
        idx = min(idx, total_cleaners - 1)
        current_bucket = buckets[idx]
        prev_index = idx - 1 if idx > 0 else None
        next_index = idx + 1 if idx < total_cleaners - 1 else None

    total_count = sum(len(bk["items"]) for bk in buckets) + len(unassigned)

    last_sync = data.get("last_sync")
    if last_sync:
        try:
            last_sync = datetime.fromisoformat(last_sync).strftime("%b %d, %I:%M %p")
        except (ValueError, TypeError):
            pass

    return {
        "buckets": buckets,
        "unassigned": unassigned,
        "current_bucket": current_bucket,
        "current_index": idx,
        "prev_index": prev_index,
        "next_index": next_index,
        "total_cleaners": total_cleaners,
        "total_count": total_count,
        "last_sync": last_sync,
        "cleaners": cleaner_names(),
        "prefix": ingress_prefix(),
        "no_ical": not ICAL_URL,
        "gcal_enabled": GCAL_ENABLED,
    }


@app.route("/")
def index():
    data = load_data()
    ctx = build_focus_context(data, request.args.get("i", 0))
    review = _build_review_context(data)
    conflicts = _build_conflicts_context()
    bridge = _build_bridge_context()
    return render_template_string(
        FOCUS_TEMPLATE,
        error=request.args.get("error"),
        digest_enabled=DIGEST_ENABLED,
        **ctx,
        **review,
        **conflicts,
        **bridge,
    )


def _build_bridge_context():
    """Bridge stability for the home page.

    This exists because the check log was built API-only, and an operator who
    will never run a curl command has no way to see it — a health signal nobody
    looks at is the same as no health signal, which is the mistake this whole
    subsystem exists to correct. So it renders where the rest of the app lives.

    The per-day strip is the point: one cell per day for 30 days. A day with no
    checks renders GREY rather than green, because "we never looked" and "it was
    fine" are the two things this must never conflate.
    """
    if not (BRIDGE_WATCHDOG_ENABLED and BRIDGE_WATCHDOG_SLUG):
        return {"bridge_enabled": False, "bridge_days": [], "bridge_actions": [],
                "bridge": {}, "bridge_up": None}
    try:
        state = watchdog_mod.load_state(BRIDGE_WATCHDOG_FILE)
        summary = watchdog_mod.summary(state, log_path=BRIDGE_CHECK_LOG)
        records = watchdog_mod.read_checks(BRIDGE_CHECK_LOG, days=30)
    except Exception as e:
        print(f"[bridge-ui] context failed: {e}")
        return {"bridge_enabled": True, "bridge_error": str(e), "bridge_days": [],
                "bridge_actions": [], "bridge": {}, "bridge_up": None}

    per_day = {}
    for r in records:
        day = (r.get("at") or "")[:10]
        if not day:
            continue
        bucket = per_day.setdefault(day, {"total": 0, "healthy": 0, "restarts": 0, "bad": 0})
        bucket["total"] += 1
        if r.get("state") in watchdog_mod.HEALTHY_STATES:
            bucket["healthy"] += 1
        else:
            bucket["bad"] += 1
        if r.get("action") in ("restarted", "restart_failed"):
            bucket["restarts"] += 1

    # A full day at the configured interval. Anything well short of it means the
    # watchdog itself was not running, which is a gap in the evidence, not health.
    per_day_expected = max(1, int(24 * 60 / max(5, BRIDGE_WATCHDOG_INTERVAL_MIN)))
    now = datetime.now()
    today = date.today()
    days = []
    for offset in range(29, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        b = per_day.get(d)
        # Today is only partly over, so pro-rate what "a full day" means for it.
        # Without this the cell the operator looks at most — today's — is
        # permanently amber-ish and the strip cries wolf every single day.
        if offset == 0:
            elapsed = max(0.04, (now.hour * 60 + now.minute) / 1440.0)
        else:
            elapsed = 1.0
        expected = max(1, int(per_day_expected * elapsed))

        if not b or b["total"] == 0:
            cls, label = "nodata", "no checks recorded"
        elif b["restarts"]:
            cls, label = "bad", f"{b['restarts']} restart(s), {b['total']} checks"
        elif b["bad"]:
            cls, label = "warn", f"{b['bad']} unhealthy of {b['total']} checks"
        elif b["total"] < expected * 0.9:
            cls, label = "partial", f"only {b['total']} of ~{expected} expected checks — watchdog gap"
        else:
            cls, label = "ok", f"{b['total']} checks, all healthy"
        days.append({"date": d, "cls": cls, "label": f"{d}: {label}"})

    actions = [r for r in records if r.get("action") != "none"]
    actions = list(reversed(actions[-25:]))

    return {
        "bridge_enabled": True,
        "bridge_error": None,
        "bridge": summary,
        "bridge_up": bool(summary.get("healthy")),
        "bridge_days": days,
        "bridge_actions": actions,
        "bridge_expected_per_day": expected,
    }


def _build_conflicts_context():
    """Read the cached reconciler_last.json. Does not recompute — the Re-run
    button / POST /reconcile/run refreshes the cache."""
    if not RECONCILER_LAST_FILE.exists():
        return {
            "conflicts_findings": [],
            "conflicts_total": None,
            "conflicts_attn": 0,
            "conflicts_dismissed": 0,
            "conflicts_generated_at": None,
        }
    try:
        result = json.loads(RECONCILER_LAST_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "conflicts_findings": [], "conflicts_total": None,
            "conflicts_attn": 0, "conflicts_dismissed": 0,
            "conflicts_generated_at": None,
        }
    findings = result.get("findings", [])
    counts = result.get("counts", {})
    gen = result.get("generated_at", "")
    try:
        gen = datetime.fromisoformat(gen).strftime("%b %d, %I:%M %p")
    except (ValueError, TypeError):
        pass
    digest_info = {}
    if DIGEST_LAST_FILE.exists():
        try:
            dl = json.loads(DIGEST_LAST_FILE.read_text())
            digest_run_at = dl.get("run_at", "")
            try:
                digest_run_at = datetime.fromisoformat(digest_run_at).strftime("%b %d, %I:%M %p")
            except (ValueError, TypeError):
                pass
            digest_info = {
                "digest_run_at": digest_run_at,
                "digest_title": dl.get("last_title"),
                "digest_notified": dl.get("last_notified"),
            }
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "conflicts_findings": findings,
        "conflicts_total": counts.get("total", len(findings)),
        "conflicts_attn": counts.get("needs-attention", 0),
        "conflicts_dismissed": counts.get("dismissed", 0),
        "conflicts_generated_at": gen,
        **digest_info,
    }


@app.route("/sync", methods=["POST"])
def sync():
    _, error = sync_ical()
    prefix = ingress_prefix()
    if error:
        return redirect(prefix + "/?error=" + error)
    return redirect(prefix + "/")


@app.route("/gcal/sync", methods=["POST"])
def gcal_sync():
    prefix = ingress_prefix()
    if not GCAL_ENABLED:
        return redirect(prefix + "/?error=Google+Calendar+is+not+enabled")
    data = load_data()
    for b in data.get("bookings", {}).values():
        b["_needs_notify"] = needs_notify(b)
    stats, error = gcal_mod.sync_to_gcal(
        data, GCAL_SERVICE_ACCOUNT_JSON, GCAL_CALENDAR_ID,
    )
    if error:
        return redirect(prefix + "/?error=" + error.replace(" ", "+"))
    return redirect(prefix + "/")


@app.route("/assign/<path:uid>", methods=["POST"])
def assign(uid):
    cleaner = request.form.get("cleaner", "").strip()
    clean_time_raw = request.form.get("clean_time", "").strip()
    with DATA_LOCK:
        data = load_data()
        if uid in data["bookings"]:
            data["bookings"][uid]["cleaner"] = cleaner or None
            data["bookings"][uid]["cleaner_since"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") if cleaner else None
            if not cleaner:
                data["bookings"][uid]["confirmed"] = False
            # input type="time" gives "HH:MM"; store as "HH:MM:SS"
            if clean_time_raw:
                data["bookings"][uid]["clean_time"] = clean_time_raw + ":00"
            else:
                data["bookings"][uid]["clean_time"] = None
            # A human setting the time resolves any ambiguity the cleaner's
            # wording left behind. Without this the `time_ambiguous` finding
            # would outlive its own cause and sit in the digest forever —
            # a finding that cannot resolve is how a monitor becomes noise.
            data["bookings"][uid].pop("time_note", None)
            _log_write("booking_assign", uid,
                       cleaning_date=cleaning_date_for(data["bookings"][uid]),
                       cleaner=cleaner or None, clean_time=clean_time_raw or None)
            save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/confirm/<path:uid>", methods=["POST"])
def confirm(uid):
    with DATA_LOCK:
        data = load_data()
        if uid in data["bookings"]:
            data["bookings"][uid]["confirmed"] = True
            _log_write("booking_confirm_manual", uid,
                       cleaning_date=cleaning_date_for(data["bookings"][uid]),
                       cleaner=data["bookings"][uid].get("cleaner"))
            save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/pay/<path:uid>", methods=["POST"])
def pay(uid):
    with DATA_LOCK:
        data = load_data()
        if uid in data["bookings"]:
            data["bookings"][uid]["paid"] = True
            _log_write("booking_paid", uid,
                       cleaning_date=cleaning_date_for(data["bookings"][uid]),
                       cleaner=data["bookings"][uid].get("cleaner"))
            save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/edit/<path:uid>")
def edit(uid):
    data = load_data()
    booking = data["bookings"].get(uid)
    if not booking:
        return redirect(ingress_prefix() + "/")
    return render_template_string(
        EDIT_TEMPLATE,
        uid=uid,
        booking=booking,
        cleaners=cleaner_names(),
        prefix=ingress_prefix(),
        deletable=booking.get("type") in ("custom_stay", "manual_cleaning"),
    )


@app.route("/delete/<path:uid>", methods=["POST"])
def delete_booking(uid):
    with DATA_LOCK:
        data = load_data()
        booking = data["bookings"].get(uid)
        if booking and (booking.get("type") in ("custom_stay", "manual_cleaning") or booking.get("status") == "cancelled"):
            _log_write("booking_delete", uid,
                       cleaning_date=cleaning_date_for(booking),
                       booking_type=booking.get("type"),
                       booking_status=booking.get("status"))
            del data["bookings"][uid]
            save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "GET":
        prefill_date = request.args.get("date", "")
        return render_template_string(
            ADD_TEMPLATE, cleaners=cleaner_names(), prefix=ingress_prefix(),
            prefill_date=prefill_date,
        )

    entry_type = request.form.get("entry_type", "cleaning")
    notes = request.form.get("notes", "").strip()
    with DATA_LOCK:
        return _add_booking(entry_type, notes)


def _add_booking(entry_type, notes):
    """Create a stay or cleaning from the /add form. Caller holds DATA_LOCK."""
    data = load_data()

    if entry_type == "stay":
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        if not start_date or not end_date:
            return redirect(ingress_prefix() + "/add")
        uid = f"custom-{start_date}-{len(data['bookings'])}"
        data["bookings"][uid] = {
            "start": start_date,
            "end": end_date,
            "cleaner": None,
            "paid": False,
            "status": "active",
            "confirmed": False,
            "notes": notes or "Custom stay",
            "type": "custom_stay",
        }
    else:
        cleaning_date = request.form.get("date", "").strip()
        cleaner = request.form.get("cleaner", "").strip()
        if not cleaning_date:
            return redirect(ingress_prefix() + "/add")
        uid = f"manual-{cleaning_date}-{len(data['bookings'])}"
        data["bookings"][uid] = {
            "start": cleaning_date,
            "end": cleaning_date,
            "cleaner": cleaner or None,
            "paid": False,
            "status": "active",
            "confirmed": bool(cleaner),
            "notes": notes or "Manual cleaning",
            "type": "manual_cleaning",
        }

    _log_write("booking_add", uid, entry_type=entry_type,
               cleaning_date=data["bookings"][uid].get("end"),
               cleaner=data["bookings"][uid].get("cleaner"))
    save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/review/notify/<slug>", methods=["POST"])
def review_notify(slug):
    """Mark every current review item for this cleaner as notified — i.e.
    rewrite cleaner_commitment to match current truth on each booking in
    that cleaner's bucket. Advances the focus pager to the next cleaner."""
    try:
        next_idx = max(0, int(request.form.get("i", 0)))
    except ValueError:
        next_idx = 0
    with DATA_LOCK:
        data = load_data()
        buckets, _unassigned = review_queue(data)
        target = None
        for bk in buckets:
            if _cleaner_slug(bk["cleaner"]) == slug:
                target = bk
                break
        if target:
            for item in target["items"]:
                b = data["bookings"].get(item["uid"])
                if b:
                    ack_notified(b, via="manual")
        save_data(data)
    # After writing, the current bucket collapses — stay on the same index so
    # the next cleaner slides into view.
    return redirect(ingress_prefix() + f"/?i={next_idx}")


@app.route("/internal/snapshot", methods=["GET"])
def internal_snapshot():
    """Return data.json plus non-secret option fields for off-host reconciliation.

    Same auth model as the WhatsApp inbound endpoint: loopback is open, remote
    callers must present X-Shared-Secret. API keys and the GCal service-account
    JSON are never returned; the Airbnb iCal URL is returned so the caller can
    pull the upstream feed itself. Also includes the persisted GCal push
    status so pipeline health is verifiable off-host, not just from journald
    (which truncates within a day).
    """
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1"):
        provided = request.headers.get("X-Shared-Secret", "")
        if not WHATSAPP_SHARED_SECRET or provided != WHATSAPP_SHARED_SECRET:
            abort(403)

    with DATA_LOCK:
        data = load_data()

    return jsonify({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "options": {
            "ical_url": ICAL_URL,
            "cleaners": CLEANERS,
            "gcal_enabled": GCAL_ENABLED,
            "gcal_calendar_id": GCAL_CALENDAR_ID,
        },
        "data": data,
        "gcal_push_status": _read_gcal_status(),
        "sync_status": _read_sync_status(),
        "bridge_watchdog": _watchdog_summary(),
        "ops_log": _read_ops_log(),
        # The nightly digest reports "here is what I changed" from this log,
        # and until 1.36.0 it was the one such record unreadable off-host —
        # so the digest could assert something about the system that no
        # investigator could audit without a deploy. That cost a real answer:
        # a `confirmed → False` write between Aug 5 and Aug 7 could not be
        # attributed. Tail only; the full log is capped at CHANGE_LOG_MAX and
        # the snapshot already carries every booking.
        "change_log": _read_change_log_tail(),
    })


@app.route("/internal/restore", methods=["POST"])
def internal_restore():
    """Overwrite data.json with a posted full snapshot (disaster recovery).

    Inverse of /internal/snapshot: used to repopulate /data after a host wipe
    or fresh install, since the add-on's /data volume is private and cannot be
    written from outside. Same auth model — loopback is open, remote callers
    must present X-Shared-Secret.

    Body is either the bare data object (a dict with a "bookings" map) or a
    {"data": {...}} wrapper (so a /internal/snapshot response can be replayed
    verbatim). The existing data.json is moved to data.json.bak first. Writes
    go through save_data(), so the GCal projection re-syncs as usual.
    """
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1"):
        provided = request.headers.get("X-Shared-Secret", "")
        if not WHATSAPP_SHARED_SECRET or provided != WHATSAPP_SHARED_SECRET:
            abort(403)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    data = payload["data"] if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data.get("bookings"), dict):
        return jsonify({"error": "missing bookings object"}), 400

    with DATA_LOCK:
        if DATA_FILE.exists():
            try:
                DATA_FILE.replace(DATA_FILE.with_name(DATA_FILE.name + ".bak"))
            except OSError:
                pass
        save_data(data)
        restored = load_data()

    b = restored.get("bookings", {})
    return jsonify({
        "ok": True,
        "bookings": len(b),
        "assigned": sum(1 for v in b.values() if v.get("cleaner")),
        "messages": len(restored.get("messages", [])),
        "message_facts": len(restored.get("message_facts", {})),
    })


@app.route("/internal/whatsapp/inbound", methods=["POST"])
def whatsapp_inbound():
    """Accept a single WhatsApp message from the Baileys sidecar.

    Auth: loopback requests are always allowed (same-host sidecar). Remote
    requests must present X-Shared-Secret matching WHATSAPP_SHARED_SECRET.
    Dedups on message id (Baileys replays on reconnect).
    """
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1"):
        provided = request.headers.get("X-Shared-Secret", "")
        if not WHATSAPP_SHARED_SECRET or provided != WHATSAPP_SHARED_SECRET:
            abort(403)

    payload = request.get_json(silent=True) or {}
    msg_id = (payload.get("id") or "").strip()
    text = payload.get("text") or ""
    sender = (payload.get("sender_jid") or "").strip()
    group = (payload.get("group_jid") or "").strip()
    ts = (payload.get("timestamp") or "").strip()

    if not msg_id or not text or not sender or not group:
        return jsonify({"error": "missing required fields"}), 400

    with DATA_LOCK:
        data = load_data()
        if _find_message(data, msg_id):
            return jsonify({"status": "duplicate", "id": msg_id})
        data["messages"].append({
            "id": msg_id,
            "timestamp": ts or _utc_iso(datetime.now(tz=timezone.utc)),
            "sender": sender,
            "group": group,
            "text": text,
            "parsed": False,
            "applied_uid": None,
            "review_state": "pending",
        })
        save_data(data)

    ensure_workers_started()
    enqueue_message(msg_id)
    return jsonify({"status": "queued", "id": msg_id})


def _require_local_or_secret():
    """Gate: loopback and HA ingress open, otherwise X-Shared-Secret must match.

    Ingress traffic arrives from the Supervisor's docker bridge (172.30.x.x)
    and carries an X-Ingress-Path header set by the proxy — we trust that
    as proof the caller went through HA's auth layer.
    """
    remote = request.remote_addr or ""
    if remote in ("127.0.0.1", "::1"):
        return
    if request.headers.get("X-Ingress-Path"):
        return
    provided = request.headers.get("X-Shared-Secret", "")
    if not WHATSAPP_SHARED_SECRET or provided != WHATSAPP_SHARED_SECRET:
        abort(403)


def _load_heartbeat():
    """Last link-state beat from the bridge. Missing/corrupt reads as absent,
    which the watchdog treats as DOWN — never as 'unknown, carry on'."""
    try:
        return json.loads(BRIDGE_HEARTBEAT_FILE.read_text())
    except (OSError, ValueError):
        return None


def _save_heartbeat(record):
    """Temp-then-replace, same reason as bridge_watchdog.save_state: this is
    written every 60s and a restart landing mid-write must not leave a partial
    file that reads back as 'no heartbeat ever' and fires a false outage."""
    tmp = BRIDGE_HEARTBEAT_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, BRIDGE_HEARTBEAT_FILE)
    except OSError as e:
        print(f"[heartbeat] failed to persist: {e}")


@app.route("/internal/whatsapp/heartbeat", methods=["POST"])
def whatsapp_heartbeat():
    """Accept the bridge's report of its own WhatsApp link state.

    Why this endpoint exists, and why it is not about message traffic:
    on 2026-09-05 the bridge went logged-out and nobody knew for 22 hours. The
    two signals available both failed. Container state said `started` — true,
    and useless, because the process was alive and crash-looping against a
    revoked registration. Message absence said nothing at all, because a quiet
    cleaner group and a dead pipe are indistinguishable; the chat genuinely had
    been quiet for three days before the container ever went unhealthy.

    The bridge is the only component that knows whether WhatsApp is actually
    talking to it. It runs no HTTP server, so it cannot be polled — it pushes
    here instead, on every connection change and on a fixed interval.

    ⚠️ `received_at` is stamped HERE, from this host's clock, and the bridge's
    own `sent_at` is kept for information only. Age is always computed from two
    readings of the same clock. A prior incident in this fleet compared a
    timestamp generated on one machine against another machine's clock; the
    skew produced a NEGATIVE age, which rendered as "0 hours old, all good"
    and fails open forever — in a deadman switch, the one direction that must
    never fail.
    """
    _require_local_or_secret()
    payload = request.get_json(silent=True) or {}
    record = {
        "received_at": int(time.time()),
        "connection": str(payload.get("connection") or "unknown"),
        "connected_since": payload.get("connected_since"),
        "last_close_code": payload.get("last_close_code"),
        "reconnect_attempts": payload.get("reconnect_attempts"),
        "bridge_version": payload.get("version"),
        "sent_at": payload.get("sent_at"),
    }
    _save_heartbeat(record)
    return jsonify({"ok": True, "received_at": record["received_at"]})


@app.route("/admin/facts", methods=["GET"])
def admin_facts():
    """Dump stored message_facts for inspection. Loopback / shared-secret only."""
    _require_local_or_secret()
    data = load_data()
    return jsonify({
        "prompt_version": facts_mod.FACTS_PROMPT_VERSION,
        "model_version": facts_mod.FACTS_MODEL,
        "count": len(data.get("message_facts", {})),
        "message_facts": data.get("message_facts", {}),
    })


@app.route("/admin/reprocess-facts", methods=["POST"])
def admin_reprocess_facts():
    """Re-extract facts for any message whose record is missing or stale.

    Idempotent: running repeatedly is safe.

    ⚠️ It is NOT free, and it is not safe in the way this docstring used to
    claim. The old text said "the reconciler only reads current-version facts,
    so a half-complete reprocess can't corrupt results." `reconcile.py` has
    never contained the string `prompt_version`; `run()` takes
    `data["message_facts"]` whole. The version gates which messages THIS route
    considers stale, and nothing else. A half-finished reprocess therefore
    feeds the detectors a corpus spanning two prompt generations whose kind
    labels mean different things — and the safety rationale for pressing the
    button was printed on the button.

    Cost gate added 2026-08-20, mirroring `/admin/ingest-transcript`'s: one
    version bump plus one POST re-extracts the entire archive, which at the
    current corpus is roughly 4.5M input tokens. Unconfirmed calls report the
    count and change nothing.
    """
    _require_local_or_secret()

    with DATA_LOCK:
        data = load_data()
        all_messages = list(data.get("messages", []))
        existing = dict(data.get("message_facts", {}))
        known = cleaner_names()
        labels = dict(data.get("group_labels", {}))

    stale = []
    for m in all_messages:
        msg_id = m.get("id")
        if not msg_id:
            continue
        rec = existing.get(msg_id)
        if rec is None or rec.get("prompt_version") != facts_mod.FACTS_PROMPT_VERSION:
            stale.append(m)

    # Oldest first, so the cross-chat digest each message sees is built from
    # facts that were established BEFORE it — the same order live processing
    # sees. Stored order is append order, which backfill inserts can violate.
    stale.sort(key=lambda m: m.get("timestamp") or "")

    # Confirm gate. A true no-op without `confirm=1`: counts, reports, writes
    # nothing. Same shape as the ingest route's, for the same reason.
    if stale and not _truthy(request.values.get("confirm")):
        return jsonify({
            "status": "needs_confirmation",
            "stale": len(stale),
            "prompt_version": facts_mod.FACTS_PROMPT_VERSION,
            "estimated_calls": len(stale),
            "message": (
                f"{len(stale)} message(s) would be re-extracted at 1 model call each. "
                "Nothing has been written. Re-POST with confirm=1 to proceed."
            ),
        }), 409

    extracted = 0
    errors = 0
    for m in stale:
        history = _facts_history(
            [h for h in all_messages if h.get("id") != m.get("id")], m,
        )
        # Re-read inside the loop: each message's facts are saved as we go, so
        # a later message in this pass can see what an earlier one established
        # in the other chat. Reprocessing oldest-first therefore converges the
        # same way live processing does, instead of every message seeing the
        # pre-reprocess snapshot.
        with DATA_LOCK:
            _snap = load_data()
            cross_facts = _cross_chat_facts(_snap, m)
            roles = _sender_roles(_snap)
        facts_list, err = facts_mod.extract_facts(
            ANTHROPIC_API_KEY, m, history, known, labels,
            cross_facts=cross_facts, roles=roles, date_header=_date_header(m),
        )
        if err or facts_list is None:
            errors += 1
            continue
        with DATA_LOCK:
            data = load_data()
            data.setdefault("message_facts", {})[m["id"]] = facts_mod.build_record(
                facts_list, m.get("sender") or "",
            )
            save_data(data)
        extracted += 1

    return jsonify({
        "stale": len(stale),
        "extracted": extracted,
        "errors": errors,
        "prompt_version": facts_mod.FACTS_PROMPT_VERSION,
    })


@app.route("/admin/fix-parse-errors", methods=["POST"])
def admin_fix_parse_errors():
    """Bulk-fix messages that have parse_error set.

    Messages older than cutoff_days (default 90) are silently ignored.
    Recent ones are reset so the worker retries them on next enqueue.
    Idempotent.
    """
    _require_local_or_secret()
    cutoff_days = int(request.json.get("cutoff_days", 90) if request.is_json else 90)
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=cutoff_days)

    ignored = 0
    reset = 0
    retry_ids = []

    with DATA_LOCK:
        data = load_data()
        for m in data.get("messages", []):
            if not m.get("parse_error"):
                continue
            # A naive value here is LOCAL, not UTC. Stamping it as UTC — which
            # this did — shifted every backfilled row seven hours earlier and
            # could age it past the cutoff a boundary case would depend on.
            ts_raw = m.get("timestamp") or ""
            ts = clock_mod.ts_utc(ts_raw)
            old = (ts < cutoff) if ts else (ts_raw < cutoff.strftime("%Y-%m-%d") if ts_raw else True)
            if old:
                m["review_state"] = "ignored"
                ignored += 1
            else:
                m["parsed"] = False
                m["parse_error"] = None
                retry_ids.append(m["id"])
                reset += 1
        save_data(data)

    ensure_workers_started()
    for mid in retry_ids:
        enqueue_message(mid)

    return jsonify({"ignored": ignored, "reset_for_retry": reset, "cutoff_days": cutoff_days})


def _fetch_ical_events():
    """Fetch + parse the Airbnb iCal into [{uid, start, end}] for detector 1.

    Raises on HTTP / parse failure — the /reconcile/run endpoint is meant to
    surface fetch problems, not hide them. Returns [] when no URL configured.
    """
    if not ICAL_URL:
        raise RuntimeError("No iCal URL configured. Set it in the add-on options.")
    resp = requests.get(ICAL_URL, timeout=15)
    resp.raise_for_status()
    cal = __import__("icalendar").Calendar.from_ical(resp.text)
    out = []
    for event in cal.walk("VEVENT"):
        if str(event.get("SUMMARY", "")) != "Reserved":
            continue
        uid = str(event.get("UID", ""))
        dtstart = event.get("DTSTART").dt
        dtend = event.get("DTEND").dt
        start_str = dtstart.strftime("%Y-%m-%d") if hasattr(dtstart, "strftime") else str(dtstart)
        end_str = dtend.strftime("%Y-%m-%d") if hasattr(dtend, "strftime") else str(dtend)
        out.append({"uid": uid, "start": start_str, "end": end_str})
    return out


_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")

def _parse_msg_ts(s):
    """Parse a message timestamp to a naive datetime for day-granularity age
    math. Messages are a mix of bridge UTC-with-Z (`...Z`) and naive-local
    fallback (`datetime.now().isoformat()`); at a 7–14 day threshold the ~8h
    tz skew is irrelevant, so we take the leading YYYY-MM-DD[THH:MM:SS] and
    ignore any offset / fractional seconds. Returns None on unparseable input."""
    if not s:
        return None
    m = _TS_RE.match(str(s).strip())
    if not m:
        return None
    y, mo, d = m.group(1).split("-")
    hh = m.group(2) or "0"
    mm = m.group(3) or "0"
    ss = m.group(4) or "0"
    try:
        return datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
    except ValueError:
        return None


def _compute_silence_input(data):
    """Pre-compute the channel-silence detector's input from the message log so
    reconcile._channel_silence stays a pure function. Per-group last-seen age +
    historical count, plus the newest message from any group (whole-bridge
    liveness)."""
    labels = data.get("group_labels", {}) or {}
    now = datetime.now()
    groups = {}          # jid -> {"last_dt": datetime, "count": int}
    last_any = None
    for msg in data.get("messages", []):
        ts = _parse_msg_ts(msg.get("timestamp"))
        if ts is None:
            continue
        if last_any is None or ts > last_any:
            last_any = ts
        gjid = msg.get("group")
        if not gjid:
            continue
        rec = groups.setdefault(gjid, {"last_dt": None, "count": 0})
        rec["count"] += 1
        if rec["last_dt"] is None or ts > rec["last_dt"]:
            rec["last_dt"] = ts

    def _age_days(dt):
        return (now - dt).total_seconds() / 86400 if dt else None

    group_out = {
        gjid: {
            "label": labels.get(gjid) or gjid,
            "age_days": _age_days(rec["last_dt"]),
            "count": rec["count"],
            "last_ts": rec["last_dt"].isoformat(timespec="seconds") if rec["last_dt"] else None,
        }
        for gjid, rec in groups.items()
    }
    return {
        "enabled": DEAD_CHANNEL_ENABLED,
        "bridge_days": BRIDGE_SILENT_DAYS,
        "dead_days": DEAD_CHANNEL_DAYS,
        "min_group_msgs": DEAD_CHANNEL_MIN_MSGS,
        "last_any_age_days": _age_days(last_any),
        "groups": group_out,
    }


def _latest_message_ts(data):
    """Newest stored WhatsApp message timestamp, or None. Used ONLY to describe
    an outage window after the fact — never to decide whether one is happening."""
    best = None
    for m in data.get("messages", []) or []:
        ts = m.get("timestamp")
        if ts and (best is None or ts > best):
            best = ts
    return best


def _bridge_health_line():
    """One line of proof-of-life for the WhatsApp chain. Never raises — a
    reporting helper must not be able to break the digest it rides on."""
    try:
        hb = _load_heartbeat()
        if not hb:
            return "WhatsApp link: NO HEARTBEAT — the bridge has never reported in."
        age = int(time.time()) - int(hb.get("received_at") or 0)
        conn = hb.get("connection")
        state = watchdog_mod.load_state(BRIDGE_WATCHDOG_FILE)
        faults = (state.get("health") or {}).get("faults") or []
        c = (state.get("link") or {}).get("counters") or {}
        fwd = c.get("forwarded_ok")
        tail = f"; {fwd} message(s) forwarded since the bridge started" if fwd is not None else ""
        if faults:
            return (f"WhatsApp link: DOWN ({', '.join(faults)}) — last heartbeat "
                    f"{age}s ago{tail}.")
        return (f"WhatsApp link: UP (connection {conn}, last heartbeat "
                f"{age}s ago){tail}.")
    except Exception as e:
        return f"WhatsApp link: could not be determined ({e})."


def _bridge_phone_alert(kind, title, message):
    """Escalate a bridge fault to the phone.

    Phone escalation is deliberately rare — `_post_ha_notification`'s own
    docstring reserves it for "the cases where nobody would otherwise find
    out: the pipeline itself being broken." Every prior call site was a
    tracker-side failure. A dead bridge was not among them, which is exactly
    how it stayed invisible for 22 hours while the panel quietly held an
    alarm nobody was looking at.
    """
    try:
        _post_ha_notification(
            title,
            message,
            notification_id=kind,
            to_phone=True,
        )
    except Exception as e:
        # A failing alerter must never take down the watchdog that called it.
        print(f"[watchdog] phone alert '{kind}' failed: {e}")


def _watchdog_check_now(heal=True):
    """One bridge liveness pass. Safe to call from the timer or a route."""
    if not (BRIDGE_WATCHDOG_ENABLED and BRIDGE_WATCHDOG_SLUG):
        return None
    with DATA_LOCK:
        last_msg = _latest_message_ts(load_data())
    return watchdog_mod.check(
        BRIDGE_WATCHDOG_FILE,
        BRIDGE_WATCHDOG_SLUG,
        SUPERVISOR_TOKEN,
        last_message_at=last_msg,
        heal=heal,
        log_path=BRIDGE_CHECK_LOG,
        heartbeat=_load_heartbeat(),
        link_down_alert_min=BRIDGE_LINK_DOWN_ALERT_MIN,
        heartbeat_stale_sec=BRIDGE_HEARTBEAT_STALE_SEC,
        max_heal_attempts=BRIDGE_MAX_HEAL_ATTEMPTS,
        alert_cb=_bridge_phone_alert,
    )


def _watchdog_summary():
    """Restart-frequency summary for /internal/snapshot. Never raises — a
    reporting helper must not be able to break the snapshot it rides on."""
    if not (BRIDGE_WATCHDOG_ENABLED and BRIDGE_WATCHDOG_SLUG):
        return {"enabled": False}
    try:
        state = watchdog_mod.load_state(BRIDGE_WATCHDOG_FILE)
        out = watchdog_mod.summary(state, log_path=BRIDGE_CHECK_LOG)
        out["enabled"] = True
        out["interval_min"] = BRIDGE_WATCHDOG_INTERVAL_MIN
        # Only a tail here — a 30-day log is ~8,600 records and the snapshot is
        # already half a megabyte. The full log is at /internal/watchdog/history.
        out["recent_checks"] = watchdog_mod.read_checks(BRIDGE_CHECK_LOG)[-20:]
        return out
    except Exception as e:
        print(f"[watchdog] summary failed: {e}")
        return {"enabled": True, "error": str(e)}


def _watchdog_scheduler():
    """Hourly bridge liveness loop.

    Runs unconditionally when enabled, including a pass at startup — a bridge
    that died while the tracker was also down should be found on the way back
    up, not an hour later.
    """
    interval = max(5, BRIDGE_WATCHDOG_INTERVAL_MIN) * 60
    print(f"[watchdog] started — checking '{BRIDGE_WATCHDOG_SLUG}' every {interval // 60} min")
    while True:
        try:
            state = _watchdog_check_now()
            if state:
                print(f"[watchdog] state={state.get('last_state')} outage={bool(state.get('outage'))}")
        except Exception as e:
            # Never let the loop die — a watchdog thread that exits silently is
            # indistinguishable from one reporting all-clear.
            print(f"[watchdog] loop error (continuing): {e}")
        time.sleep(interval)


def _review_subject_date(msg, bookings):
    """The date a pending review item is ABOUT, as an ISO string.

    Prefers the cleaning date of the booking the parser matched, because that
    is what "refers to a past date" means for a scheduling decision — a
    message sent in June about an August booking is not stale. Falls back to
    the message's own date for items that resolved to no booking ("Be there in
    5"), where the send date is the only thing it can be about.
    """
    uid = (msg.get("haiku_result") or {}).get("booking_uid")
    b = (bookings or {}).get(uid) if uid else None
    if b:
        d = cleaning_date_for(b) or b.get("end")
        if d:
            return str(d)[:10]
    # Local day, not a slice of the stored UTC string — an evening message is
    # already tomorrow in UTC and would be labelled with the wrong date.
    day = _msg_local_day(msg)
    return day.isoformat() if day else None


def expire_stale_reviews(today=None, days=REVIEW_EXPIRY_DAYS):
    """Retire pending review items whose subject date is well in the past.

    The review queue never aged anything out, so it accumulated: 16 items
    pending on 2026-08-02, 14 of them about dates already gone. A queue that
    only grows stops being a queue and becomes a wall, and the items that
    actually need a decision are the ones lost in it.

    Marks `review_state: "expired"` rather than deleting. The message, its
    parse result and its extracted facts all stay — the reconciler still reads
    facts from expired messages, so retiring an item removes a demand for
    attention without removing evidence.
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    expired = []
    with DATA_LOCK:
        data = load_data()
        bookings = data.get("bookings") or {}
        for m in data.get("messages", []) or []:
            if m.get("review_state") != "pending":
                continue
            subj = _review_subject_date(m, bookings)
            if subj and subj < cutoff:
                m["review_state"] = "expired"
                m["expired_at"] = today.isoformat()
                m["expired_reason"] = f"subject date {subj} is older than {days} days"
                expired.append({"id": m.get("id"), "subject_date": subj})
        if expired:
            save_data(data)
    if expired:
        print(f"[review] expired {len(expired)} stale review item(s) older than {cutoff}")
    return {"today": today.isoformat(), "cutoff": cutoff, "expired": len(expired),
            "items": expired}


def _group_of_cleaner(data):
    """{cleaner name: her group jid}, derived from which chat her JIDs post in."""
    out = {}
    jid_to_cleaner = {}
    for name, jids in (data.get("cleaner_jids") or {}).items():
        for j in jids or []:
            jid_to_cleaner[j] = name
    for m in data.get("messages", []) or []:
        name = jid_to_cleaner.get(m.get("sender"))
        if name and m.get("group") and name not in out:
            out[name] = m.get("group")
    # Fall back to the group label matching her name, for a cleaner who has
    # never sent a message the bridge saw.
    for jid, label in (data.get("group_labels") or {}).items():
        out.setdefault(label, jid)
    return out


def auto_ack_notifications(today=None, apply=True, include_quotes=True):
    """Clear notify items the host has already handled in WhatsApp.

    Runs before the nightly reconcile so the digest reflects the world after
    the acknowledgement, not a conflict that was resolved hours earlier.

    Returns the list of acknowledgements with their justifying message, and the
    list of near-misses with the reason each was NOT acted on — a rule that
    only reports its successes is impossible to trust or debug.
    """
    today = today or date.today()
    acked, skipped = [], []
    with DATA_LOCK:
        data = load_data()
        facts = data.get("message_facts") or {}
        messages_by_id = {m["id"]: m for m in data.get("messages", []) if m.get("id")}
        groups = _group_of_cleaner(data)
        changed = False

        for uid, b in (data.get("bookings") or {}).items():
            if not needs_notify(b):
                continue
            if b.get("status") == "cancelled":
                # A cancellation still needs a human: "she was told it moved"
                # and "she was told it is off entirely" are different messages,
                # and guessing between them is exactly the risk not worth taking.
                continue
            ev = notify_ack_mod.find_ack_evidence(b, facts, messages_by_id, groups)
            if ev["ok"]:
                reason = notify_ack_mod.describe(b.get("end"), ev, include_quotes=True)
                rec = {
                    "booking_uid": uid, "date": b.get("end"),
                    "cleaner": b.get("cleaner"),
                    "was": (b.get("cleaner_commitment") or {}).get("cleaner"),
                    "reason": reason,
                    "evidence": ev["sides"],
                }
                acked.append(rec)
                if apply:
                    ack_notified(b, via="whatsapp-host")
                    changed = True
                    print(f"[auto-ack] {b.get('end')} cleared — {reason}")
            elif ev["sides"] or ev["missing"]:
                skipped.append({"booking_uid": uid, "date": b.get("end"),
                                "missing": ev["missing"][:3]})

        if changed:
            save_data(data)

    if acked:
        _record_auto_acks(acked, today)
    return {"today": today.isoformat(), "acknowledged": acked, "not_acknowledged": skipped}


def _record_auto_acks(acked, today):
    """Log every automatic acknowledgement into the same change log the nightly
    'changes applied' section reads, so it is reported rather than merely done."""
    try:
        log = []
        if CHANGE_LOG_FILE.exists():
            log = json.loads(CHANGE_LOG_FILE.read_text())
            if not isinstance(log, list):
                log = []
        for a in acked:
            log.append({
                "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "booking_uid": a["booking_uid"],
                "cleaning_date": a["date"],
                "action": "auto-ack",
                "source": "whatsapp-host",
                "reason": a["reason"],
                "changed": {"notify": {"from": "pending", "to": "cleared"}},
            })
        CHANGE_LOG_FILE.write_text(json.dumps(log[-CHANGE_LOG_MAX:], indent=2))
    except (OSError, ValueError) as e:
        print(f"[auto-ack] failed to record: {e}")


def _run_full_reconcile():
    with DATA_LOCK:
        data = load_data()
    buckets, unassigned = review_queue(data)
    drift_items = [it for bk in buckets for it in bk["items"]] + unassigned
    ical_events = _fetch_ical_events()
    gcal_events = None
    gcal_read_error = None
    data_for_detectors = data
    if GCAL_ENABLED:
        # fetch_tagged_events() raises by design ("fail loudly, no fallbacks").
        # That was right when a GCal read failure meant one detector produced
        # nothing; it is wrong now. An unreachable calendar used to kill the
        # WHOLE reconcile, so the digest produced no findings, sent no Telegram
        # message, and went quiet — during exactly the outage the push-health
        # findings exist to announce. Caught 2026-08-01 by fault injection
        # (ISC-60): a bad calendar id made /reconcile/run return 500.
        #
        # Degrade instead: skip the calendar-content detector (we genuinely
        # cannot compare against a calendar we cannot read) and pass the read
        # error through so it becomes a finding. Loud, but not fatal.
        try:
            gcal_events = gcal_mod.fetch_tagged_events(GCAL_SERVICE_ACCOUNT_JSON, GCAL_CALENDAR_ID)
        except Exception as e:
            gcal_read_error = str(e)
            gcal_events = None
            print(f"[reconcile] GCal read FAILED, continuing without it: {e}")
        data_for_detectors = json.loads(json.dumps(data, default=str))
        for b in data_for_detectors.get("bookings", {}).values():
            b["_needs_notify"] = needs_notify(b)
    result = reconcile_mod.run(
        data_for_detectors, drift_items,
        ical_events=ical_events,
        gcal_events=gcal_events,
        silence=_compute_silence_input(data),
        gcal_status=_read_gcal_status(),
        gcal_read_error=gcal_read_error,
    )
    # Bridge liveness findings are merged here rather than inside reconcile.run()
    # because they come from Supervisor, not from `data` — reconcile.py stays a
    # pure function of the data it is handed, which is what makes it testable.
    if BRIDGE_WATCHDOG_ENABLED and BRIDGE_WATCHDOG_SLUG:
        try:
            wd_state = watchdog_mod.load_state(BRIDGE_WATCHDOG_FILE)
            wd_findings = watchdog_mod.findings(wd_state, date.today().isoformat())
            if wd_findings:
                # Both lists, and `findings_raw` FIRST — it is what
                # filter_and_sort() re-derives from on every dismiss, refilter
                # and digest run. Merging into `findings` alone would let the
                # next filter pass silently delete these again.
                # Stamp the decision here. Findings merged AFTER
                # `reconcile.run()` never pass through `resolve_subjects`, so
                # without this they reach the digest and the VPS payload with
                # `decision: null` — the one field the bot's triage prompt now
                # reasons from. Caught on the 1.37.1 deploy: 19 of 20 findings
                # carried a decision and the bridge one did not.
                wd_findings = [
                    dict(f, decision=reconcile_mod._decision_of(f["kind"]))
                    for f in wd_findings
                ]
                # Into `findings_raw` ONLY, then re-derive everything from it.
                #
                # Prepending to `findings` put these AHEAD of the decision
                # ranking they had just been stamped for, and past the
                # dismissed filter entirely — `filter_and_sort` had already
                # run inside reconcile.run(). Two consequences, both live on
                # 2026-08-20: `bridge_blind_window` held line 1 of every
                # digest above the one-tap actions despite ranking
                # `investigate`, and no dismissal could ever clear it because
                # the next run re-prepended it (ISC-236). Those were filed as
                # separate problems for two days; they are one.
                #
                # `counts` was hand-maintained here for the same reason and is
                # now derived too — a third copy of a rule that already exists
                # once. Every watchdog finding is dated `today_str` at its emit
                # site, so the STALE_DAYS filter cannot drop them; that is the
                # property the old comment here was protecting, and it holds
                # without a second list to protect it with.
                prior_raw = list(result.get("findings_raw") or result.get("findings") or [])
                result["findings_raw"] = wd_findings + prior_raw
                result = reconcile_mod.filter_and_sort(
                    result, data.get("dismissed_findings", {}) or {}
                )
        except Exception as e:
            print(f"[reconcile] watchdog findings failed (non-fatal): {e}")
    try:
        RECONCILER_LAST_FILE.write_text(json.dumps(result, indent=2))
    except OSError as e:
        print(f"[reconcile] failed to persist: {e}")
    return result


def _vps_ping(url):
    """Container-safe reachability probe for the footer widget. Raw ICMP isn't
    available in the add-on container, so 'ping' = a TCP connect to the URL's
    host:port (proves the box + port is up, times the handshake) plus a short
    HTTP HEAD for a status code. Any HTTP response — including 401/403 (the hub
    is Basic-Auth protected) — means the web server is up. Returns a dict; never
    raises."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = parsed.hostname
    scheme = parsed.scheme or "https"
    port = parsed.port or (443 if scheme == "https" else 80)
    out = {"reachable": False, "latency_ms": None, "http_status": None,
           "error": None, "checked_at": now_iso}
    if not host:
        out["error"] = "no host configured"
        return out
    t = time.time()
    try:
        socket.create_connection((host, port), timeout=4).close()
    except Exception as e:
        out["error"] = type(e).__name__
        return out
    out["reachable"] = True
    out["latency_ms"] = round((time.time() - t) * 1000)
    # Best-effort status code — reachability is already proven above.
    full = url if "://" in url else "https://" + url
    try:
        out["http_status"] = requests.head(full, timeout=4, allow_redirects=True).status_code
    except Exception:
        try:
            r = requests.get(full, timeout=4, stream=True)
            out["http_status"] = r.status_code
            r.close()
        except Exception:
            pass  # TCP up but HTTP unreadable — still 'reachable'
    return out


def _vps_status_cached():
    now = time.time()
    if _VPS_STATUS_CACHE["result"] is None or now - _VPS_STATUS_CACHE["at"] > VPS_STATUS_TTL:
        _VPS_STATUS_CACHE["at"] = now  # set first to blunt concurrent stampede
        _VPS_STATUS_CACHE["result"] = _vps_ping(VPS_STATUS_URL)
    return _VPS_STATUS_CACHE["result"]


@app.route("/vps/status")
def vps_status():
    # Ungated to match the home page (index() is LAN-reachable without auth);
    # this returns strictly less than that page — VPS reachability + latency,
    # never the configured host/URL. Cached (VPS_STATUS_TTL) so it can't be used
    # to hammer the target.
    if not VPS_STATUS_ENABLED or not VPS_STATUS_URL:
        return jsonify({"enabled": False})
    res = dict(_vps_status_cached())
    res["enabled"] = True
    res["label"] = VPS_STATUS_LABEL
    return jsonify(res)


@app.route("/reconcile/run", methods=["POST"])
def reconcile_run():
    _require_local_or_secret()
    result = _run_full_reconcile()
    if request.form:
        return redirect(ingress_prefix() + "/#conflicts")
    return jsonify(result)


@app.route("/internal/watchdog/check", methods=["POST"])
def watchdog_check():
    """Run one bridge liveness pass now instead of waiting for the hourly tick.

    Exists so the healing path can be *fault-injected* rather than reasoned
    about: stop the bridge, call this, confirm it comes back and that a blind
    window was recorded. Every serious bug in this add-on's history was found
    by doing that and none were found by reading the code, so the ability to
    trigger a check on demand is part of the feature, not a test hook.

    `heal=false` observes without restarting, for checking state safely.
    """
    _require_local_or_secret()
    heal = (request.args.get("heal", "true").lower() != "false")
    state = _watchdog_check_now(heal=heal)
    if state is None:
        return jsonify({"error": "watchdog disabled or slug not configured"}), 400
    return jsonify({
        "state": state.get("last_state"),
        "outage": state.get("outage"),
        "blind_windows": state.get("blind_windows"),
        "probe_error": state.get("probe_error"),
        "findings": watchdog_mod.findings(state, date.today().isoformat()),
    })


@app.route("/internal/watchdog/history", methods=["GET"])
def watchdog_history():
    """Every liveness check in the retention window, newest last.

    Separate from the snapshot on purpose: at 5-minute polling a 30-day window
    is ~8,600 records, which has no business riding a payload that already
    carries the whole booking set. `?days=N` narrows it; `?actions_only=1` drops
    the uneventful passes when you want the incidents rather than the evidence
    of stability.
    """
    _require_local_or_secret()
    try:
        days = int(request.args.get("days", watchdog_mod.CHECK_LOG_RETENTION_DAYS))
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400
    records = watchdog_mod.read_checks(BRIDGE_CHECK_LOG, days=days)
    if _truthy(request.args.get("actions_only")):
        records = [r for r in records if r.get("action") != "none"]
    state = watchdog_mod.load_state(BRIDGE_WATCHDOG_FILE)
    return jsonify({
        "summary": watchdog_mod.summary(state, log_path=BRIDGE_CHECK_LOG, days=days),
        "count": len(records),
        "checks": records,
    })


@app.route("/reconcile/last", methods=["GET"])
def reconcile_last():
    _require_local_or_secret()
    if not RECONCILER_LAST_FILE.exists():
        return jsonify({"error": "no run yet"}), 404
    return Response(RECONCILER_LAST_FILE.read_text(), mimetype="application/json")


@app.route("/reconcile/dismiss", methods=["POST"])
def reconcile_dismiss():
    """Mark a finding id dismissed so future reconciler runs filter it out.
    Body: {"finding_id": "...", "reason": "optional note"}"""
    _require_local_or_secret()
    payload = request.get_json(silent=True) or request.form or {}
    finding_id = payload.get("finding_id")
    reason = payload.get("reason") or ""
    if not finding_id:
        return jsonify({"error": "missing finding_id"}), 400
    # ISC-349: record WHICH BOOKING this dismissal adjudicates, so a later
    # finding about the same booking built entirely from evidence that
    # predates this moment is filtered even when detectors mint it a new id.
    # Resolved from the cached findings, not parsed from the id — changed_mind
    # ids carry no uid. Best-effort: a miss stores null and the legacy
    # parse-from-id fallback in reconcile._dismissal_subject still applies.
    booking_uid = None
    try:
        if RECONCILER_LAST_FILE.exists():
            cached = json.loads(RECONCILER_LAST_FILE.read_text())
            for f in cached.get("findings_raw") or []:
                # Match absorbed ids too (code-review 2026-08-21): the id Josh
                # read in a digest may since have been absorbed under a
                # different primary, and a changed_mind:* id embeds no uid for
                # the regex fallback to find.
                if f.get("id") == finding_id or finding_id in (f.get("absorbed") or []):
                    booking_uid = f.get("booking_uid")
                    break
    except Exception:
        booking_uid = None
    with DATA_LOCK:
        data = load_data()
        data.setdefault("dismissed_findings", {})[finding_id] = {
            "dismissed_at": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "booking_uid": booking_uid,
        }
        save_data(data)
    _log_op("finding_dismissed", finding_id=finding_id, reason=reason or None)
    # Re-run immediately so the cached findings reflect the dismissal.
    _rerun_reconcile_cached()
    if request.form:
        return redirect(ingress_prefix() + "/#conflicts")
    return jsonify({"dismissed": finding_id})


def _rerun_reconcile_cached():
    """Re-filter the cached findings against current dismissed_findings.

    Called after dismiss / undismiss. Does NOT re-run detectors or re-fetch
    iCal / GCal — those only happen on an explicit /reconcile/run. If the
    cache is missing, no-op.
    """
    try:
        if not RECONCILER_LAST_FILE.exists():
            return
        with DATA_LOCK:
            data = load_data()
        cached = json.loads(RECONCILER_LAST_FILE.read_text())
        dismissed = data.get("dismissed_findings", {}) or {}
        result = reconcile_mod.filter_and_sort(cached, dismissed)
        RECONCILER_LAST_FILE.write_text(json.dumps(result, indent=2))
    except Exception as e:
        print(f"[reconcile] re-run failed: {e}")


@app.route("/reconcile/undismiss", methods=["POST"])
def reconcile_undismiss():
    """Remove a finding id from dismissed_findings so it surfaces again."""
    _require_local_or_secret()
    payload = request.get_json(silent=True) or request.form or {}
    finding_id = payload.get("finding_id")
    if not finding_id:
        return jsonify({"error": "missing finding_id"}), 400
    with DATA_LOCK:
        data = load_data()
        removed = (data.get("dismissed_findings") or {}).pop(finding_id, None)
        save_data(data)
    _rerun_reconcile_cached()
    return jsonify({"undismissed": finding_id, "was_dismissed": bool(removed)})


def _post_phone_notification(title, message):
    """Escalate to the host's phone via a configured HA notify service.

    Deliberately best-effort and non-fatal: this is an *escalation* of a
    notification that has already been posted, so a failure here must never
    prevent or unwind the panel notification it was meant to amplify. Returns
    True only on a confirmed 2xx.

    Delivery rides Home Assistant (→ Nabu Casa → the phone's push service),
    which shares no infrastructure with the VPS or Telegram. That is the whole
    point: it is the one channel still standing when the failure being reported
    is the Telegram bot itself.
    """
    if not (PHONE_NOTIFY_SERVICE and SUPERVISOR_TOKEN):
        return False
    try:
        r = requests.post(
            f"http://supervisor/core/api/services/notify/{PHONE_NOTIFY_SERVICE}",
            headers={
                "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"title": title, "message": message},
            timeout=10,
        )
        r.raise_for_status()
        print(f"[notify] phone escalation sent via notify.{PHONE_NOTIFY_SERVICE}")
        return True
    except Exception as e:
        print(f"[notify] phone escalation FAILED via notify.{PHONE_NOTIFY_SERVICE}: {e}")
        return False


def _post_ha_notification(title, message, notification_id="cleaning_digest", to_phone=False):
    """Post a persistent notification; optionally also escalate to the phone.

    `to_phone` is opt-in per call rather than on by default — the panel is the
    right home for routine findings, and an alert that buzzes a phone every
    morning stops being an alert. Reserve it for the cases where nobody would
    otherwise find out: the pipeline itself being broken.
    """
    if to_phone:
        _post_phone_notification(title, message)
    if not SUPERVISOR_TOKEN:
        print("[digest] no SUPERVISOR_TOKEN — cannot post HA notification")
        return False
    try:
        r = requests.post(
            "http://supervisor/core/api/services/persistent_notification/create",
            headers={
                "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"title": title, "message": message, "notification_id": notification_id},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[digest] HA notification failed: {e}")
        return False


_QUOTE_RE = re.compile(r' — ".*?"(?=(;|$))')


def _redact_quotes_for_vps(findings):
    """Strip verbatim WhatsApp text before a finding crosses to the VPS.

    The payload allowlist has always excluded `quote` and `evidence`; this
    keeps that true for the `why` line as well, which is the one field that
    crosses and is now capable of carrying a quotation. The timestamp, the
    cleaner and the date all still cross — enough to know exactly which
    message was used, without the message.

    `digest_include_quotes` opts back in for a host who would rather have the
    text in Telegram than keep it on the Pi. Off by default: the wall was a
    deliberate decision and should not be dissolved by a formatting change.
    """
    if DIGEST_INCLUDE_QUOTES:
        return list(findings)
    out = []
    for f in findings:
        g = dict(f)
        why = _QUOTE_RE.sub("", g.get("why") or "")
        g["why"] = why + (" (open the app to read the message)" if why != g.get("why") else "")
        out.append(g)
    return out


def _digest_compute_and_notify():
    """Run reconcile, diff against baseline, post HA notification, save baseline.

    Returns a dict with new/resolved/total/notified/message. Safe to call from
    both the HTTP route and the background scheduler.

    Opens by establishing TODAY, once, and everything downstream is dated
    against that single value — the queue expiry, the reconciler's staleness
    cutoff, the repeat horizon, and the date header handed to every model
    prompt. Reading the clock separately in each stage is how a job that
    straddles midnight ends up reasoning about two different days.
    """
    today = date.today()
    try:
        # Before anything else reads the world: close notify items the host has
        # already handled in WhatsApp, so the digest reports the state after
        # those acknowledgements rather than conflicts resolved hours ago.
        acks = auto_ack_notifications(today=today)
        for a in acks["acknowledged"]:
            print(f"[digest] auto-cleared notify for {a['date']}: {a['reason']}")
    except Exception as e:
        print(f"[digest] auto-ack failed (non-fatal): {e}")
        acks = {"acknowledged": [], "not_acknowledged": []}

    try:
        expiry = expire_stale_reviews(today=today)
        if expiry["expired"]:
            print(f"[digest] retired {expiry['expired']} stale review item(s)")
    except Exception as e:
        # Housekeeping must never take the digest down with it.
        print(f"[digest] review expiry failed (non-fatal): {e}")

    try:
        result = _run_full_reconcile()
    except Exception as e:
        # A reconcile that dies used to take the whole digest with it: no push,
        # no heartbeat, and therefore SILENCE — which reads as a clean night
        # until the VPS dead-man eventually fires a day later with a vague
        # "nothing arrived". Attestation exists precisely so a broken stage can
        # announce itself, so send a degraded heartbeat saying reconcile failed
        # rather than saying nothing at all.
        print(f"[digest] reconcile FAILED, sending degraded heartbeat: {e}")
        _push_digest_to_vps([], 0, {}, reconcile_ok=False)
        _post_ha_notification(
            "Cleaning reconcile failed",
            f"The nightly reconcile could not complete: {e}. "
            "Conflicts are unmeasured until this is fixed — not absent.",
            notification_id="cleaning_reconcile_failed",
            to_phone=True,
        )
        raise

    # Dismissals must reach the digest, not just the web UI. Before 2026-08-02
    # `_run_full_reconcile()` returned unfiltered findings and only the HTTP
    # routes filtered, so a dismissed finding kept riding the nightly Telegram
    # message forever. That was survivable while the digest only reported NEW
    # findings; it is not survivable now that unresolved ones repeat nightly,
    # because dismissal is the only "I've dealt with this" switch there is.
    # The cached file stays unfiltered on purpose — /reconcile/refilter and
    # undismiss both depend on being able to re-derive from the full set.
    with DATA_LOCK:
        dismissed = (load_data().get("dismissed_findings") or {})
    result = reconcile_mod.filter_and_sort(result, dismissed)

    baseline = {}
    if DIGEST_LAST_FILE.exists():
        try:
            baseline = json.loads(DIGEST_LAST_FILE.read_text())
        except Exception:
            baseline = {}

    current_ids = {f["id"] for f in result["findings"]}
    baseline_ids = set(baseline.get("finding_ids", []))
    no_baseline = not baseline_ids and not baseline

    # Defined for both branches — the first-ever run has no baseline to diff
    # against, so nothing is "still open" yet, but the persisted first_seen map
    # must still be written or every finding looks brand new again tomorrow.
    repeated = []
    today_iso = today.isoformat()
    first_seen = {f["id"]: today_iso for f in result["findings"]}

    if no_baseline:
        title = "Cleaning Digest — initial baseline"
        counts = result["counts"]
        message = (
            f"Total: {counts.get('total', 0)} findings "
            f"({counts.get('needs-attention', 0)} needs-attention, "
            f"{counts.get('suggest', 0)} suggest, "
            f"{counts.get('informational', 0)} informational). "
            "Baseline saved — future runs will show new/resolved diff."
        )
        new_findings = []
        resolved_count = 0
    else:
        # One horizon, computed once, applied to BOTH halves of the diff.
        horizon = (today + timedelta(days=REPEAT_HORIZON_DAYS)).isoformat()
        resolved_count = len(baseline_ids - current_ids)

        # A finding is new to the READER only if it is also near enough to act
        # on. The repeat filter below has always understood that; the
        # new-finding path never did, so an Airbnb reservation landing eleven
        # months out pushed `drift_unassigned` to Josh's phone the night it
        # arrived — noise by his own rule ("only a problem if it's less than a
        # month from today"), and relentless, because far-future reservations
        # arrive constantly.
        #
        # Suppressed, not dropped. `finding_ids` below still records it, so it
        # never re-announces as new later; and it starts repeating on its own
        # once `_drift` promotes it back to needs-attention inside the window.
        # Dateless findings — bridge down, blind window — are about NOW and
        # always announce.
        new_findings = [
            f for f in result["findings"]
            if f["id"] in current_ids - baseline_ids
            and (not f.get("date") or f["date"] <= horizon)
        ]

        # Repeat-until-fixed. Reporting only what changed since last night meant
        # a real problem was announced once and then never again — the bridge
        # outage of 2026-07-28 got exactly one notification and then five silent
        # days. Anything still unresolved and still needs-attention rides every
        # nightly message until it is fixed or explicitly dismissed. Suggestions
        # and informational findings deliberately do NOT repeat: making
        # everything recur trains the reader to ignore the whole message.
        # …but only for things that are actually actionable soon. The first cut
        # of this repeated every open needs-attention finding, which on the
        # very first run meant fourteen unassigned bookings in Sep 2026 – Jul
        # 2027 arriving nightly forever. That is how a digest trains its reader
        # to skim past it, and a digest nobody reads fails exactly like the
        # silence it replaced. A booking with no cleaner nine months out is
        # true, unimportant today, and will start repeating on its own once it
        # comes inside the horizon. Findings with no date (bridge down, blind
        # window, watchdog broken) always repeat — they are about *now*.
        persisting = [
            f for f in result["findings"]
            if f["id"] in (current_ids & baseline_ids)
            and f.get("severity") == "needs-attention"
            and (not f.get("date") or f["date"] <= horizon)
        ]
        first_seen = dict(baseline.get("first_seen") or {})
        for f in result["findings"]:
            first_seen.setdefault(f["id"], today_iso)
        # Drop ids that no longer exist, so a finding that resolves and later
        # recurs is reported as new rather than as months old.
        first_seen = {k: v for k, v in first_seen.items() if k in current_ids}

        repeated = []
        for f in persisting:
            f = dict(f)
            try:
                days = (today - date.fromisoformat(first_seen[f["id"]])).days
            except (KeyError, ValueError):
                days = None
            if days:
                # The age goes in `why` rather than a new field on purpose: the
                # VPS bot validates findings against a fixed key set, so an
                # extra key would be dropped in transit and the reader would
                # never learn how long this has been broken.
                f["why"] = f"{f['why']} [unresolved for {days} day(s)]"
            repeated.append(f)

        title = (
            f"Cleaning Digest — {len(new_findings)} new, "
            f"{len(repeated)} still open, {resolved_count} resolved"
        )

        severity_order = ["needs-attention", "suggest", "informational"]
        grouped = {}
        for f in new_findings:
            grouped.setdefault(f["severity"], []).append(f)

        lines = []
        shown = 0
        for sev in severity_order:
            for f in grouped.get(sev, []):
                if shown >= 10:
                    break
                lines.append(f"• [{sev}] {f['why']}")
                shown += 1
            if shown >= 10:
                break

        if len(new_findings) > 10:
            lines.append(f"…and {len(new_findings) - 10} more")

        for f in repeated[:10]:
            lines.append(f"• [still open] {f['why']}")
        if len(repeated) > 10:
            lines.append(f"…and {len(repeated) - 10} more still open")

        if resolved_count:
            lines.append(f"{resolved_count} previously flagged finding(s) resolved.")

        counts = result["counts"]
        lines.append(
            f"Total: {counts.get('total', 0)} findings "
            f"({counts.get('needs-attention', 0)} needs-attention, "
            f"{counts.get('suggest', 0)} suggest, "
            f"{counts.get('informational', 0)} informational)."
        )
        message = "\n".join(lines) if lines else "No new findings."

    # Automatic acknowledgements are reported as findings so they ride the same
    # delivery path as everything else — an automatic change that only appears
    # in a log file has not been reported, it has been filed.
    ack_findings = []
    for a in acks.get("acknowledged", []):
        ack_findings.append({
            "id": f"auto_ack:{a['booking_uid']}:{today.isoformat()}",
            "detector": "notify_ack",
            "kind": "notify_auto_cleared",
            "severity": "informational",
            "booking_uid": a["booking_uid"],
            "cleaner": a.get("cleaner"),
            "date": a.get("date"),
            "why": (f"{a['date']} cleaning — I cleared the 'tell the cleaner' item "
                    f"by myself, because you already did: {a['reason']}"),
            "evidence": [],
        })

    with DATA_LOCK:
        bookings_now = dict(load_data().get("bookings", {}))
    # Suppress anything already reported on a previous night. A change record
    # sits inside the 24h window across two 08:00 runs whenever it lands in the
    # evening, so without this it is announced twice. Persisting the ids
    # without consulting them here would have been bookkeeping that looked like
    # deduplication and did nothing.
    in_window = _change_findings(today.isoformat(), bookings=bookings_now) + ack_findings
    already = set(baseline.get("reported_ids") or [])
    # Everything still inside the window stays suppressed; once it ages out of
    # `_recent_changes` it leaves this set on its own, so nothing accumulates.
    reported_ids = current_ids | {f["id"] for f in in_window}
    changes = [c for c in in_window
               if c["kind"] == "applied_change" and c["id"] not in already]
    ack_findings = [a for a in ack_findings if a["id"] not in already]

    if ack_findings:
        message = message + "\n" + "\n".join(f"• [auto-cleared] {a['why']}" for a in ack_findings[:10])
    if changes:
        message = message + "\n" + "\n".join(f"• [changed] {c['why']}" for c in changes[:10])
        if len(changes) > 10:
            message += f"\n…and {len(changes) - 10} more changes"

    # Positive confirmation, always, healthy or not.
    #
    # The old twelve-alert design failed by NOISE. This one can only fail by
    # SILENCE — and silence is the mode that has bitten this household twice
    # already (Daria's three quiet months, and the 2026-09-05 outage). "Nothing
    # arriving" is now indistinguishable from "everything is fine" unless the
    # system says so out loud. One line, in a message Josh already reads, is
    # also the cheapest possible deadman for the alert chain itself: if this
    # line stops appearing, the chain that would report a fault is the thing
    # that broke.
    message = message + "\n\n" + _bridge_health_line()

    notified = _post_ha_notification(title, message)
    _push_digest_to_vps(
        new_findings, resolved_count, result["counts"],
        extra_findings=repeated + changes + _redact_quotes_for_vps(ack_findings),
    )

    # TWO id sets, deliberately, because they answer different questions.
    #
    # `finding_ids` is the RECONCILER's set and drives the new/resolved diff.
    # `reported_ids` is everything actually sent, and drives suppression.
    #
    # The first cut of this merged them, and a cross-vendor audit caught what
    # that costs: change ids are never in the next night's reconciler output,
    # so `resolved_count = len(baseline_ids - current_ids)` would have counted
    # every one of them as a problem solved. One set doing two jobs invents a
    # phantom in the other job — the same shape as `ack_notified` encoding both
    # "she was told" and "we have a usable time", which is what this whole
    # release is about.
    DIGEST_LAST_FILE.write_text(json.dumps({
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "finding_ids": sorted(current_ids),
        "reported_ids": sorted(reported_ids),
        "first_seen": first_seen,
        "counts": result["counts"],
        "last_title": title,
        "last_message": message,
        "last_notified": notified,
    }, indent=2))

    return {
        "new": len(new_findings),
        "resolved": resolved_count,
        "total": result["counts"].get("total"),
        "notified": notified,
        "message": message,
    }


def _read_sync_status():
    """Per-attempt outcome of the last iCal sync. Never raises.

    Returns:
        None            — no record has ever been written (a fresh install or
                          an upgrade); the caller may fall back to a weaker
                          signal.
        {"unreadable"}  — a record EXISTS but could not be parsed. This is not
                          the same thing, and conflating them is dangerous: SD
                          wear is the common Pi death, and a corrupt record
                          that degrades into "fall back to freshness" can
                          report sync_ok:true off a stale-but-recent success.
                          A lying attestation is strictly worse than silence,
                          because it also satisfies the absence alarm.
                          (Advisor finding, 2026-08-01.)
    """
    try:
        if not SYNC_STATUS_FILE.exists():
            return None
        with open(SYNC_STATUS_FILE) as f:
            status = json.load(f)
        if not isinstance(status, dict):
            return {"unreadable": True}
        return status
    except Exception as e:
        print(f"[sync] sync status exists but is unreadable — failing closed: {e}")
        return {"unreadable": True}


def _write_sync_status(ok, error=None):
    """Record what THIS sync attempt did. Never raises.

    Separate from `last_sync` on purpose: `last_sync` advances only on success,
    so it answers "when did a sync last work", never "did the most recent
    attempt work". Those differ exactly when it matters most.
    """
    try:
        tmp = SYNC_STATUS_FILE.with_name(f"{SYNC_STATUS_FILE.name}.tmp{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump({
                "ok": bool(ok),
                "at": datetime.now().isoformat(timespec="seconds"),
                "error": (str(error)[:300] if error else None),
            }, f, indent=2)
        os.replace(tmp, SYNC_STATUS_FILE)
    except Exception as e:
        print(f"[sync] failed to persist sync status: {e}")


def _build_attestation(reconcile_ok):
    """Report what the pipeline actually DID, not merely that it phoned home.

    The heartbeat alone proves the Pi reached the VPS. A Pi whose sync throws,
    whose push is skipped and whose reconcile returns garbage — but which still
    completes the final POST — satisfies the dead-man indefinitely, degrading it
    from a liveness signal into a "TCP still works" signal. (Advisor finding,
    2026-08-01.)

    `sync_ok` and `push_outcome` are DERIVED from durable state rather than
    passed in by the caller. That is deliberate: an attestation a caller
    assembles is an attestation a caller can quietly omit a stage from, and the
    omission would look identical to success. Reading the same files the
    reconciler reads means this cannot claim work that left no trace.

    Only booleans and an enum string cross the wire, so the payload allowlist
    is unaffected — no guest data, no secrets, nothing new in kind.
    """
    def _fresh(ts):
        if not ts:
            return False
        try:
            age_h = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600
        except (ValueError, TypeError):
            return False
        # A future date is a broken clock, not freshness (the Pi has no RTC).
        return -1 <= age_h <= 26

    # Read the PER-ATTEMPT sync outcome, not merely how fresh the last success
    # is. Deriving sync_ok from `last_sync` age alone was wrong and shipped
    # briefly in 1.26.0: if tonight's sync throws but yesterday's succeeded,
    # last_sync is ~24h old, inside the window, and the attestation cheerfully
    # reports sync_ok:true for a stage that just failed — recreating the exact
    # "an old good fact masks a new failure" pattern this whole feature exists
    # to eliminate. (Cross-vendor audit finding, gpt-5.5, 2026-08-01.)
    try:
        sync_status = _read_sync_status()
        if sync_status is not None and sync_status.get("unreadable"):
            # Fail closed. We cannot prove the sync ran, so we must not claim it.
            sync_ok = False
        elif sync_status is not None:
            sync_ok = bool(sync_status.get("ok")) and _fresh(sync_status.get("at"))
        else:
            # No per-attempt record yet (first run after upgrade). Fall back to
            # freshness so the attestation degrades to the old, weaker signal
            # rather than reporting a hard false and alarming on nothing.
            sync_ok = _fresh(load_data().get("last_sync"))
    except Exception as e:
        print(f"[attest] sync_ok undeterminable, reporting False: {e}")
        sync_ok = False

    if not GCAL_ENABLED:
        push_outcome = "disabled"
    else:
        status = _read_gcal_status()
        push_outcome = (status or {}).get("outcome") or "never"

    return {
        "sync_ok": bool(sync_ok),
        "push_outcome": str(push_outcome),
        "reconcile_ok": bool(reconcile_ok),
    }


def _probe_bot_health():
    """Ask the VPS bot whether it is actually alive, not merely reachable.

    Derived from VPS_PUSH_URL rather than configured separately, so the health
    probe and the digest push can never drift onto different hosts.

    Returns (ok: bool, detail: str). A reachable-but-unhealthy bot is the case
    that matters — a crashed process behind a live web server looks identical to
    a healthy one from the outside, which is exactly what the footer status
    widget cannot see (ISC-16).
    """
    if not (VPS_PUSH_ENABLED and VPS_PUSH_URL and VPS_PUSH_SECRET):
        return True, "not configured — skipped"
    # Swap the PATH rather than substring-replacing, so the probe keeps working
    # if the route is ever mounted somewhere else. The old substring form
    # silently returned "healthy, skipped" on any unrecognised shape, which
    # meant a config drift could disable the monitor without ever saying so —
    # a fallback that switches off the very thing it is guarding.
    # (Cross-vendor audit finding, gpt-5.5, 2026-08-01.)
    try:
        parts = urlsplit(VPS_PUSH_URL)
        if not (parts.scheme and parts.netloc):
            raise ValueError("no scheme/host")
        base = parts.path.rsplit("/", 1)[0] if "/" in parts.path else ""
        url = urlunsplit((parts.scheme, parts.netloc, f"{base}/health", "", ""))
    except Exception as e:
        return False, f"cannot derive a health URL from the configured push URL ({e})"
    try:
        r = requests.get(url, headers={"X-Push-Secret": VPS_PUSH_SECRET}, timeout=15)
        if r.status_code // 100 != 2:
            return False, f"HTTP {r.status_code}"
        body = r.json()
        if not body.get("ok"):
            return False, f"bot reports not ok: {str(body)[:120]}"
        return True, f"uptime {body.get('uptime_s')}s, last digest {body.get('last_digest_age_s')}s ago"
    except Exception as e:
        return False, str(e)[:160]


def _archive_digest_payload(path, payload, delivered, now=None, retention_days=DIGEST_ARCHIVE_RETENTION_DAYS):
    """Append one archive line for an outgoing VPS payload (ISC-358).

    Read-filter-rewrite on every call, deliberately: at one payload a day the
    file is ~30 small lines, and rewriting through a tmp file + rename means a
    torn write can never destroy the history (the lesson `_log_check` learned
    the O(1)-append way). Lines older than `retention_days` age out here, so
    the file needs no separate pruner. A corrupt line is dropped, counted,
    and reported in the return rather than silently — a replay record that
    can quietly lose nights would undermine the very measurement (ISC-359)
    it exists to serve. Never raises: archiving must not take the push down.

    Returns {"kept": N, "dropped_corrupt": N, "ok": bool} for logging.
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    entry = {
        "archived_at": now.isoformat(timespec="seconds"),
        "delivered": bool(delivered),
        "payload": payload,
    }
    kept, dropped = [], 0
    try:
        with _ARCHIVE_LOCK:
            path = Path(path)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        old = json.loads(line)
                    except ValueError:
                        dropped += 1
                        continue
                    if (old.get("archived_at") or "") >= cutoff:
                        kept.append(line)
            kept.append(json.dumps(entry))
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
        return {"kept": len(kept), "dropped_corrupt": dropped, "ok": True}
    except Exception as e:
        print(f"[digest-archive] FAILED (non-fatal): {e}")
        return {"kept": len(kept), "dropped_corrupt": dropped, "ok": False}


def _push_digest_to_vps(new_findings, resolved_count, counts, reconcile_ok=True, extra_findings=None):
    """POST the nightly digest to the VPS Telegram bot (allowlist-built payload).

    Fires every night including clean ones — `heartbeat: true` is the VPS-side
    dead-man's-switch signal, so a quiet night must still produce a POST.
    Fail-loud: any failure posts an HA persistent notification.
    """
    if not (VPS_PUSH_ENABLED and VPS_PUSH_URL and VPS_PUSH_SECRET):
        return False
    # Freshness guard (fail-closed-loudly): a digest computed against a stale
    # world must not read as a healthy night. If the iCal sync hasn't
    # succeeded in >26h, inject a synthetic needs-attention finding so the
    # staleness rides the normal triage → Telegram path and breaks
    # quiet-when-clean. 26h > the 24h nightly cadence, < the 25h+1h dead-man
    # window on the VPS, so one missed sync alarms exactly once.
    new_findings = list(new_findings)
    counts = dict(counts)
    # Bookings for `booking_status` derivation in the projection (ISC-356).
    # Loaded once alongside last_sync; empty on any failure — the projection
    # then sends booking_status: null rather than blocking the heartbeat.
    bookings_for_status = {}
    try:
        _push_data = load_data()
        bookings_for_status = _push_data.get("bookings") or {}
        last_sync = _push_data.get("last_sync")
        age_h = None
        if last_sync:
            age_h = (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds() / 3600
        if age_h is None or age_h > 26:
            age_txt = f"{age_h:.0f} hours" if age_h is not None else "unknown (never synced)"
            new_findings.append({
                "id": "pipeline:stale-sync",
                "detector": "pipeline",
                "kind": "stale_sync",
                "severity": "needs-attention",
                "date": date.today().isoformat(),
                "cleaner": None,
                "why": f"Airbnb iCal sync is stale ({age_txt} old) — tonight's digest may be reconciling outdated bookings. Check the cleaning-tracker add-on.",
            })
            # Counts must agree with findings, or a downstream consumer keying
            # off counts reads the stale-sync night as healthy — reintroducing
            # the exact "looks fine" failure this sentinel exists to break.
            counts["total"] = counts.get("total", 0) + 1
            counts["needs-attention"] = counts.get("needs-attention", 0) + 1
    except Exception as e:
        print(f"[vps-push] freshness guard error (non-fatal): {e}")
    # Still-open problems and applied-change reports travel in `findings` but
    # are NOT counted as new — the bot decides whether to send on
    # findings.length, so this is what makes a recurring problem keep arriving,
    # while `new` stays honest about what actually changed tonight.
    outgoing = list(new_findings) + list(extra_findings or [])
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "heartbeat": True,
        "counts": {
            "total": counts.get("total", 0),
            "needs-attention": counts.get("needs-attention", 0),
            "suggest": counts.get("suggest", 0),
            "informational": counts.get("informational", 0),
        },
        "new": len(new_findings),
        "resolved": resolved_count,
        # Booleans and one enum string only — the crossing allowlist is
        # unchanged in kind, so this widens nothing.
        "attestation": _build_attestation(reconcile_ok),
        # Allowlist projection — NEVER pass findings through whole. `quote` and
        # `evidence` (raw WhatsApp text) must not cross to the VPS. The
        # allowlist itself lives in reconcile.project_finding_for_vps
        # (ISC-356) so it is pure and unit-tested: 8 identity fields plus
        # `booking_status` (derived here from the booking, closed vocabulary)
        # and `absorbed` (merged finding ids — corroboration the prose can
        # count).
        "findings": [
            reconcile_mod.project_finding_for_vps(f, bookings_for_status)
            for f in outgoing
        ],
    }
    delivered = False
    try:
        resp = requests.post(
            VPS_PUSH_URL,
            json=payload,
            headers={"X-Push-Secret": VPS_PUSH_SECRET},
            timeout=VPS_PUSH_TIMEOUT_S,
        )
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        delivered = True
        print(f"[vps-push] ok — {len(new_findings)} new, "
              f"{len(outgoing) - len(new_findings)} carried, heartbeat sent")
        return True
    except Exception as e:
        print(f"[vps-push] FAILED: {e}")
        _post_ha_notification(
            "Cleaning digest push failed",
            f"Could not deliver the nightly digest to the VPS bot: {e}. "
            "Telegram alerts will not fire until this is fixed.",
            # to_phone: this is the ONE alert Telegram cannot deliver, because
            # the thing that failed is the Telegram path. Without escalating to
            # a channel that doesn't route through the VPS, a dead bot is
            # indistinguishable from a quiet clean night — the exact recursion
            # the advisor flagged 2026-08-01 as the chain's unmonitored end.
            to_phone=True,
        )
        return False
    finally:
        # ISC-358: archive AFTER the attempt so the line records whether it
        # was delivered — a failed push is a night the measurement most needs
        # to see. In `finally`, so neither outcome can skip it; the archiver
        # itself never raises.
        rep = _archive_digest_payload(DIGEST_ARCHIVE_FILE, payload, delivered)
        print(f"[digest-archive] {'ok' if rep['ok'] else 'FAILED'} — "
              f"{rep['kept']} line(s) retained, {rep['dropped_corrupt']} corrupt dropped")


def _digest_scheduler():
    """Background thread: nightly maintenance at DIGEST_TIME.

    Order is strictly load-bearing, repair-then-detect, three steps:

      1. Sync the Airbnb iCal FIRST so the reconcile/digest runs against
         fresh world state ("reconciling stale data produces confident
         garbage").
      2. Retry the Google Calendar push, synchronously-with-a-budget
         (_nightly_gcal_push). This is the repair step: if the async push
         from the last save() failed or got skipped, the nightly job is the
         retry cadence (no queue, no backoff library) — and it runs BEFORE
         the reconciler reads GCal, closing the race where
         _digest_scheduler used to sync_ical() (which fires an async push)
         and immediately reconcile against Google Calendar while that push
         was potentially still in flight or still broken.
      3. Run the digest/reconcile, which now sees the freshest calendar
         state this pipeline can produce.

    Steps 1 and 2 both run even when the digest is disabled — before
    1.24.0, sync only ran at process startup, so a quiet deploy-free stretch
    left data.json days stale (the Oct 16-18 cancellation sat unapplied for
    3 days); the same "don't let a disabled digest silently disable upkeep"
    rationale extends to the GCal push repair added in 1.25.0.
    """
    try:
        hour, minute = map(int, DIGEST_TIME.split(":"))
    except Exception:
        hour, minute = 8, 0
    print(f"[digest] scheduler started — daily at {hour:02d}:{minute:02d} (sync then digest)")
    while True:
        now = datetime.now()
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        time.sleep((next_run - now).total_seconds())
        if ICAL_URL:
            try:
                _, sync_err = sync_ical()
                if sync_err:
                    raise RuntimeError(sync_err)
                print("[digest] nightly iCal sync ok")
            except Exception as e:
                # sync_ical() already recorded the per-attempt outcome.
                print(f"[digest] nightly iCal sync FAILED: {e}")
                _post_ha_notification(
                    "Cleaning iCal sync failed",
                    f"The nightly Airbnb calendar sync failed: {e}. "
                    "Bookings may be stale until this is fixed.",
                    # to_phone: everything downstream reconciles against these
                    # bookings, so a silent sync failure makes every later
                    # answer confidently wrong.
                    to_phone=True,
                )
        if GCAL_ENABLED:
            try:
                is_retry = _should_retry_push(_read_gcal_status())
                print(
                    "[gcal] nightly push starting"
                    + (" (retry — prior push was not ok)" if is_retry else "")
                )
                _nightly_gcal_push()
            except Exception as e:
                print(f"[digest] nightly gcal push wrapper error: {e}")
        # Probe the bot BEFORE the digest, inline — deliberately not a watcher
        # thread. A dedicated poller would be one more thing that dies quietly,
        # to reach a conclusion this already reaches. The push-failure path
        # catches an *unreachable* bot; this catches a bot that is listening but
        # not working, which from the outside looks identical to a healthy one.
        if VPS_PUSH_ENABLED:
            healthy, detail = _probe_bot_health()
            print(f"[bot-health] {'ok' if healthy else 'UNHEALTHY'} — {detail}")
            if not healthy:
                _post_ha_notification(
                    "Cleaning Telegram bot unhealthy",
                    f"The VPS bot did not report healthy: {detail}. "
                    "Tonight's digest may not reach Telegram — this message came "
                    "by a different route for that reason.",
                    notification_id="cleaning_bot_health",
                    to_phone=True,
                )

        if not DIGEST_ENABLED:
            continue
        try:
            r = _digest_compute_and_notify()
            print(f"[digest] ran: new={r['new']} resolved={r['resolved']} notified={r['notified']}")
        except Exception as e:
            print(f"[digest] scheduler error: {e}")


@app.route("/digest/run", methods=["POST"])
def digest_run():
    _require_local_or_secret()
    r = _digest_compute_and_notify()
    if request.form:
        return redirect(ingress_prefix() + "/#conflicts")
    return jsonify(r)


def _rename_cleaner_in_data(data, old, new):
    """Rewrite every stored occurrence of a cleaner's name. Caller holds lock.

    A cleaner's name is a join key in five separate places, and a partial
    rename is worse than none: the detectors compare `booking.cleaner` against
    `fact.cleaner` by string equality, so leaving one side as "Daria" while the
    other becomes "Darya" manufactures a contested-cleaner conflict on every
    booking she has ever touched. Returns per-field counts so a caller can see
    that all five moved together.

    Deliberately does NOT touch free-text `notes`: those are a human record of
    what was said at the time, not a join key, and rewriting history to match a
    later correction is how a record stops being evidence.
    """
    counts = {"bookings": 0, "commitments": 0, "cleaner_jids": 0,
              "group_labels": 0, "facts": 0}

    for b in (data.get("bookings") or {}).values():
        if b.get("cleaner") == old:
            b["cleaner"] = new
            counts["bookings"] += 1
        c = b.get("cleaner_commitment")
        if isinstance(c, dict) and c.get("cleaner") == old:
            c["cleaner"] = new
            counts["commitments"] += 1

    cj = data.get("cleaner_jids") or {}
    if old in cj:
        # Merge rather than overwrite: the new key may already exist if a
        # rename was half-applied before.
        cj[new] = list(dict.fromkeys((cj.get(new) or []) + (cj.pop(old) or [])))
        data["cleaner_jids"] = cj
        counts["cleaner_jids"] = 1

    gl = data.get("group_labels") or {}
    for jid, label in list(gl.items()):
        if label == old:
            gl[jid] = new
            counts["group_labels"] += 1

    for rec in (data.get("message_facts") or {}).values():
        for f in rec.get("facts") or []:
            if f.get("cleaner") == old:
                f["cleaner"] = new
                counts["facts"] += 1

    return counts


@app.route("/admin/auto-ack", methods=["POST"])
def admin_auto_ack():
    """Run the notify auto-acknowledgement now. `?apply=false` to preview.

    The preview mode matters more than the trigger: this is the one rule in the
    system that can tell you a human has been informed when she has not, so
    being able to see what it *would* clear, and why, before it clears anything
    is part of the feature.
    """
    _require_local_or_secret()
    apply = request.args.get("apply", "true").lower() != "false"
    return jsonify(auto_ack_notifications(apply=apply))


@app.route("/admin/expire-reviews", methods=["POST"])
def admin_expire_reviews():
    """Retire stale pending review items now. `?days=N` overrides the default.

    The nightly digest calls the same function; this exists so the backlog can
    be cleared without waiting for 08:00, and so the rule can be exercised
    against a chosen horizon before trusting it on a schedule.
    """
    _require_local_or_secret()
    try:
        days = int(request.args.get("days", REVIEW_EXPIRY_DAYS))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    return jsonify(expire_stale_reviews(days=days))


@app.route("/admin/rename-cleaner", methods=["POST"])
def admin_rename_cleaner():
    """Rename a cleaner everywhere at once. Body: {"old": "...", "new": "..."}

    The add-on's `cleaners` option must be updated separately (it lives in
    Supervisor config, not data.json) — the response says so rather than
    letting a half-rename look complete.
    """
    _require_local_or_secret()
    payload = request.get_json(silent=True) or request.form or {}
    old = (payload.get("old") or "").strip()
    new = (payload.get("new") or "").strip()
    if not old or not new:
        return jsonify({"error": "both 'old' and 'new' are required"}), 400
    if old == new:
        return jsonify({"error": "old and new are identical"}), 400

    with DATA_LOCK:
        data = load_data()
        counts = _rename_cleaner_in_data(data, old, new)
        save_data(data)

    return jsonify({
        "renamed": {"from": old, "to": new},
        "updated": counts,
        "reminder": ("Also update the add-on's `cleaners` option — it is "
                     "Supervisor config, not data.json, and this endpoint "
                     "cannot reach it."),
        "cleaners_option_now": cleaner_names(),
    })


@app.route("/admin/remap-group", methods=["POST"])
def admin_remap_group_route():
    """Bulk-rewrite `group` on messages and update `group_labels`.

    Body: {"mapping": {"old_jid": "new_jid", ...},
           "labels":  {"jid": "label", ...}}

    Used to promote placeholder group IDs (e.g. `itzel-group`) to the real
    WhatsApp JIDs once they're known. Safe against concurrent writes via
    DATA_LOCK.
    """
    _require_local_or_secret()
    payload = request.get_json(silent=True) or {}
    mapping = payload.get("mapping") or {}
    labels = payload.get("labels") or {}
    if not isinstance(mapping, dict) or not isinstance(labels, dict):
        return jsonify({"error": "mapping and labels must be objects"}), 400

    changed = 0
    with DATA_LOCK:
        data = load_data()
        for m in data.get("messages", []):
            g = m.get("group")
            if g in mapping:
                m["group"] = mapping[g]
                changed += 1
        gl = data.setdefault("group_labels", {})
        for jid, label in labels.items():
            gl[jid] = label
        save_data(data)

    return jsonify({"messages_updated": changed, "labels_set": len(labels)})


# ── Transcript ingest (historical backfill into facts layer) ────────────────
#
# /admin/ingest-transcript takes a pasted WhatsApp export and threads each
# line through the same pipeline the live sidecar uses — but with an `apply`
# switch. apply=false (historical catch-up): facts only, no parse, no
# auto-apply. apply=true (future bulk adds): full process_message path.

_TRANSCRIPT_LINE_RE = re.compile(
    r"^\[([^\]]+)\]\s+([^:]+?):\s?(.*)$"
)

# Android export: "2026-03-13, 9:58 a.m. - Sender: text" (no brackets, ` - `
# separator, dotted am/pm). System lines ("Michelle Groves added you") share
# the prefix but have no `Sender:` body — handled in _parse_whatsapp_transcript.
_ANDROID_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2},\s+\d{1,2}:\d{2}(?::\d{2})?\s+[aApP]\.?[mM]\.?)\s+-\s+(.+)$"
)
_ANDROID_BODY_RE = re.compile(r"^([^:]+?):\s?(.*)$")

_TS_FORMATS = [
    "%Y-%m-%d %I:%M:%S %p",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %I:%M %p",
    "%d/%m/%Y %I:%M %p",
]


def _parse_transcript_ts(bracket):
    """Try every known WhatsApp export timestamp layout. Handles both
    [YYYY-MM-DD, HH:MM:SS AM/PM] and [H:MM AM/PM, M/D/YYYY] (and variants
    with/without seconds, 24h, dashes/slashes).

    Returns a NAIVE datetime holding LOCAL wall time — that is what a WhatsApp
    export contains, and there is nothing in the file to say otherwise. The
    caller converts to UTC via `_utc_iso` before storing. Storing the naive
    value directly, which is what happened until 2026-09-06, put every
    backfilled message seven to eight hours away from every live one.
    """
    normalized = (bracket
                  .replace("a.m.", "AM").replace("p.m.", "PM")
                  .replace("A.M.", "AM").replace("P.M.", "PM"))
    parts = [p.strip() for p in normalized.split(",", 1)]
    if len(parts) != 2:
        return None
    a, b = parts
    for date_s, time_s in ((a, b), (b, a)):
        candidate = f"{date_s} {time_s.upper().replace('  ', ' ')}"
        for fmt in _TS_FORMATS:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None

INGEST_LOCK = threading.Lock()
INGEST_STATUS = {"running": False, "total": 0, "done": 0, "errors": 0, "apply": False}


def _truthy(v):
    return str(v or "").lower() in ("1", "true", "yes", "on")


def _parse_whatsapp_transcript(text):
    """Parse a WhatsApp export into [{timestamp, sender, text}, ...].

    Handles multi-line messages by appending continuation lines to the
    previous entry. Silently drops lines before the first timestamped entry
    (export headers, "Messages and calls are end-to-end encrypted", etc).
    """
    out = []
    for raw in text.splitlines():
        m = _TRANSCRIPT_LINE_RE.match(raw)
        if m:
            ts = _parse_transcript_ts(m.group(1))
            if ts:
                out.append({
                    "timestamp": _utc_iso(ts),
                    "sender": m.group(2).strip(),
                    "text": m.group(3),
                })
                continue
        m2 = _ANDROID_LINE_RE.match(raw)
        if m2:
            ts = _parse_transcript_ts(m2.group(1))
            if ts:
                body = _ANDROID_BODY_RE.match(m2.group(2))
                if body:
                    out.append({
                        "timestamp": _utc_iso(ts),
                        "sender": body.group(1).strip(),
                        "text": body.group(2),
                    })
                # No body match = system line ("X added you"); skip without
                # polluting the previous message.
                continue
        if out and raw.strip():
            out[-1]["text"] += "\n" + raw
    return out


def _resolve_sender_jid(data, sender_name):
    """Reverse-lookup cleaner_jids by name. Unknown senders get a stable
    backfill:<slug> placeholder so facts still attribute consistently."""
    for name, jids in cleaner_jid_map(data).items():
        if name.strip().lower() == sender_name.strip().lower() and jids:
            return jids[0]
    slug = re.sub(r"[^a-z0-9]+", "-", sender_name.lower()).strip("-") or "unknown"
    return f"backfill:{slug}"


def _ingest_msg_id(ts, sender, text):
    h = hashlib.sha1(f"{ts}|{sender}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"backfill-{h}"


def _ingest_facts_only(msg_id):
    """Facts extraction only — skip parse/auto-apply. Marks message parsed
    with a sentinel so Review tab never surfaces it and the live pipeline
    won't re-touch it."""
    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg or msg.get("parsed"):
            return
        history = _facts_history(
            [m for m in data["messages"] if m.get("id") != msg_id], msg,
        )
        known = cleaner_names()
        labels = dict(data.get("group_labels", {}))
        cross_facts = _cross_chat_facts(data, msg)
        roles = _sender_roles(data)

    facts_list, facts_err = facts_mod.extract_facts(
        ANTHROPIC_API_KEY, msg, history, known, labels,
        cross_facts=cross_facts, roles=roles, date_header=_date_header(msg),
    )

    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg:
            return
        msg["parsed"] = True
        msg["parse_error"] = None
        msg["haiku_result"] = {"action": "none", "backfill_ingest": True}
        msg["review_state"] = "ignored"
        if facts_list is not None:
            data.setdefault("message_facts", {})[msg_id] = facts_mod.build_record(
                facts_list, msg.get("sender") or "",
            )
        save_data(data)
    return facts_err


def _ingest_worker(msg_ids, apply):
    global INGEST_STATUS
    INGEST_STATUS = {"running": True, "total": len(msg_ids), "done": 0, "errors": 0, "apply": apply}
    try:
        for mid in msg_ids:
            try:
                if apply:
                    process_message(mid)
                else:
                    err = _ingest_facts_only(mid)
                    if err:
                        INGEST_STATUS["errors"] += 1
                        INGEST_STATUS["last_error"] = str(err)[:200]
            except Exception as e:
                print(f"[ingest] error on {mid}: {e}")
                INGEST_STATUS["errors"] += 1
                INGEST_STATUS["last_error"] = str(e)[:200]
            INGEST_STATUS["done"] += 1
            time.sleep(0.8)  # pace against Anthropic TPM limit
    finally:
        INGEST_STATUS["running"] = False


@app.route("/admin/ingest-transcript", methods=["POST"])
def admin_ingest_transcript():
    """Parse a pasted WhatsApp transcript into the messages log and extract
    facts. Body: {transcript, group_jid, apply}. Loopback or X-Shared-Secret.
    Returns immediately; progress at /admin/ingest-status."""
    _require_local_or_secret()
    payload = request.get_json(silent=True) or request.form
    transcript = (payload.get("transcript") or "").strip()
    group_jid = (payload.get("group_jid") or "backfill-group").strip()
    apply_flag = _truthy(payload.get("apply"))
    confirm_apply = _truthy(payload.get("confirm_apply"))

    if not transcript:
        return jsonify({"error": "missing transcript"}), 400
    if INGEST_LOCK.locked() or INGEST_STATUS.get("running"):
        return jsonify({"error": "ingest already running"}), 409

    entries = _parse_whatsapp_transcript(transcript)
    if not entries:
        return jsonify({"error": "no messages parsed from transcript"}), 400

    # Cost gate (ISC-9): apply=true runs full process_message — 2 Haiku calls
    # per NEW message (parse + facts) AND routes lines into the live review/
    # auto-apply queue. Historical backfill should be facts-only (apply off =
    # 1 call/msg, no routing). On 2026-06-10 a ticked box silently double-spent.
    # Require explicit confirmation that states the real cost. Count new
    # messages WITHOUT inserting, so an unconfirmed apply is a true no-op.
    if apply_flag and not confirm_apply:
        with DATA_LOCK:
            data = load_data()
            new_count = sum(
                1 for e in entries
                if not _find_message(
                    data, _ingest_msg_id(e["timestamp"], e["sender"], e["text"]))
            )
        warn = {
            "needs_confirmation": True,
            "apply": True,
            "parsed_entries": len(entries),
            "new_messages": new_count,
            "haiku_calls": new_count * 2,
            "why": ("apply=true runs full parse + facts (2 Haiku calls per new "
                    "message) and routes them into the live review/auto-apply "
                    "queue. For historical backfill, uncheck Apply: facts-only "
                    "is 1 call/message and never touches a booking."),
        }
        if request.is_json:
            return jsonify(warn), 409
        return render_template_string(
            _INGEST_CONFIRM_TEMPLATE, prefix=ingress_prefix(),
            shared_styles=_SHARED_STYLES, transcript=transcript,
            group_jid=group_jid, new_messages=new_count,
            haiku_calls=new_count * 2, parsed_entries=len(entries),
        ), 409

    inserted_ids = []
    skipped = 0
    with DATA_LOCK:
        data = load_data()
        for e in entries:
            mid = _ingest_msg_id(e["timestamp"], e["sender"], e["text"])
            if _find_message(data, mid):
                skipped += 1
                continue
            sender_jid = _resolve_sender_jid(data, e["sender"])
            data["messages"].append({
                "id": mid,
                "timestamp": e["timestamp"],
                "sender": sender_jid,
                "sender_name_raw": e["sender"],
                "group": group_jid,
                "text": e["text"],
                "parsed": False,
                "applied_uid": None,
                "review_state": "pending",
                "source": "backfill",
            })
            inserted_ids.append(mid)
        save_data(data)

    # The operator action, recorded as an action. Without this the only trace a
    # transcript repair leaves is a `source: backfill` tag on the rows, which
    # says a paste happened but never says when, how much, or which chat.
    _log_op(
        "transcript_ingested",
        group=group_jid,
        parsed_entries=len(entries),
        inserted=len(inserted_ids),
        skipped_duplicates=skipped,
        applied=apply_flag,
        earliest=entries[0]["timestamp"] if entries else None,
        latest=entries[-1]["timestamp"] if entries else None,
    )

    def _run():
        with INGEST_LOCK:
            if apply_flag:
                ensure_workers_started()
            _ingest_worker(inserted_ids, apply_flag)

    threading.Thread(target=_run, daemon=True, name="ingest-worker").start()

    return jsonify({
        "inserted": len(inserted_ids),
        "skipped": skipped,
        "parsed_entries": len(entries),
        "apply": apply_flag,
        "status_url": f"{ingress_prefix()}/admin/ingest-status",
    })


@app.route("/admin/ingest-status", methods=["GET"])
def admin_ingest_status():
    _require_local_or_secret()
    return jsonify(dict(INGEST_STATUS))


_INGEST_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset=\"utf-8\"><title>Transcript ingest</title>
<style>{{ shared_styles|safe }}
textarea { width: 100%; min-height: 320px; font-family: monospace; font-size: 12px; }
.row { margin: 12px 0; }
</style></head><body>
<div class=\"container\">
<h1>Transcript ingest</h1>
<p><a href=\"{{ prefix }}/\">← back</a></p>
<p>Paste a WhatsApp chat export. Each line
<code>[YYYY-MM-DD, HH:MM:SS AM/PM] Sender: text</code> becomes a message
in the log. Facts are extracted for every inserted message.</p>
<form method=\"POST\" action=\"{{ prefix }}/admin/ingest-transcript\">
  <div class=\"row\">
    <label>Group JID (optional tag): <input type=\"text\" name=\"group_jid\" value=\"backfill-group\" /></label>
  </div>
  <div class=\"row\">
    <label><input type=\"checkbox\" name=\"apply\" value=\"1\" /> Apply (run full parse + auto-apply — leave unchecked for historical backfill)</label>
  </div>
  <div class=\"row\"><textarea name=\"transcript\" placeholder=\"[2026-04-15, 10:23:00 AM] Itzel: si puedo\"></textarea></div>
  <div class=\"row\"><button type=\"submit\">Ingest</button></div>
</form>
<p>After submitting, poll <a href=\"{{ prefix }}/admin/ingest-status\">/admin/ingest-status</a> to watch progress.
Inspect extracted facts at <a href=\"{{ prefix }}/admin/facts\">/admin/facts</a>.</p>
</div>
</body></html>
"""


# Cost-gate interstitial (ISC-9). Shown when Apply is ticked but unconfirmed.
# Re-posts the same transcript with confirm_apply=1 so the cost is acknowledged.
_INGEST_CONFIRM_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset=\"utf-8\"><title>Confirm Apply ingest</title>
<style>{{ shared_styles|safe }}
.warn { background:#fff3cd; border:1px solid #ffc107; border-radius:6px; padding:14px; margin:14px 0; }
.big { font-size:1.4rem; font-weight:600; }
.muted { color:#666; font-size:0.9rem; }
</style></head><body>
<div class=\"container\">
<h1>⚠️ Confirm full-Apply ingest</h1>
<div class=\"warn\">
  <p class=\"big\">{{ new_messages }} new message(s) → {{ haiku_calls }} Haiku calls</p>
  <p>You ticked <strong>Apply</strong>. That runs the full parse + facts pipeline
  (<strong>2 Haiku calls per new message</strong>) and routes these lines into the
  live review / auto-apply queue — where a high-confidence parse can reassign a
  booking.</p>
  <p class=\"muted\">For a historical backfill this is almost always wrong. Going
  back and unchecking Apply makes it facts-only: 1 call/message, no queue routing,
  no booking changes. ({{ parsed_entries }} lines parsed; already-seen lines are skipped.)</p>
</div>
<form method=\"POST\" action=\"{{ prefix }}/admin/ingest-transcript\">
  <input type=\"hidden\" name=\"transcript\" value=\"{{ transcript }}\" />
  <input type=\"hidden\" name=\"group_jid\" value=\"{{ group_jid }}\" />
  <input type=\"hidden\" name=\"apply\" value=\"1\" />
  <input type=\"hidden\" name=\"confirm_apply\" value=\"1\" />
  <button type=\"submit\" style=\"background:#dc3545;\">Yes, run full Apply ({{ haiku_calls }} calls)</button>
</form>
<p style=\"margin-top:12px;\"><a href=\"{{ prefix }}/admin/ingest\">← Back (uncheck Apply for facts-only backfill)</a></p>
</div>
</body></html>
"""


@app.route("/admin/ingest", methods=["GET"])
def admin_ingest_form():
    _require_local_or_secret()
    return render_template_string(
        _INGEST_TEMPLATE, prefix=ingress_prefix(), shared_styles=_SHARED_STYLES,
    )


# ── Review queue routes ─────────────────────────────────────────────────────

def _build_review_context(data):
    """Gather pending messages + what would change if accepted."""
    bookings = data.get("bookings", {})
    labels = data.get("group_labels", {})
    jid_map = cleaner_jid_map(data)
    known_jids = set()
    for jids in jid_map.values():
        known_jids.update(jids)

    host_jids = set(data.get("host_jids", []))
    pending = []
    unmapped_senders = {}  # sender_jid -> first msg preview
    for m in data.get("messages", []):
        if m.get("review_state") != "pending":
            continue
        sender = m.get("sender") or ""
        if sender and sender not in known_jids and sender not in host_jids and sender not in unmapped_senders:
            grp = m.get("group")
            unmapped_senders[sender] = {
                "jid": sender,
                "first_text": m.get("text", "")[:200],
                "group": grp,
                "group_label": labels.get(grp) or grp,
                "timestamp": m.get("timestamp"),
            }
        res = m.get("haiku_result") or {}
        booking_uid = res.get("booking_uid")
        booking = bookings.get(booking_uid) if booking_uid else None
        booking_label = None
        if booking:
            try:
                s = datetime.strptime(booking["start"], "%Y-%m-%d").date()
                e = datetime.strptime(booking["end"], "%Y-%m-%d").date()
                booking_label = f"{s.strftime('%b %d')} → {e.strftime('%b %d')}"
            except (ValueError, KeyError):
                booking_label = booking_uid
        pending.append({
            "id": m.get("id"),
            "timestamp": m.get("timestamp"),
            "sender": sender,
            "sender_cleaner": lookup_cleaner_by_jid(data, sender),
            "group": m.get("group"),
            "text": m.get("text", ""),
            "haiku_action": res.get("action"),
            "haiku_cleaner": res.get("cleaner"),
            "haiku_confidence": res.get("confidence"),
            "haiku_reason": res.get("reason"),
            "haiku_booking_uid": booking_uid,
            "haiku_booking_label": booking_label,
            "parse_error": m.get("parse_error"),
            "auto_block": m.get("auto_block"),
        })

    # Build booking options for manual assignment in review UI
    today = date.today()
    options = []
    for uid, b in bookings.items():
        if b.get("status") != "active":
            continue
        try:
            end = datetime.strptime(b["end"], "%Y-%m-%d").date()
            start = datetime.strptime(b["start"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if end < today - timedelta(days=7):
            continue
        options.append({
            "uid": uid,
            "label": f"{start.strftime('%b %d')} → {end.strftime('%b %d')} ({b.get('cleaner') or 'unassigned'})",
            "end": b["end"],
        })
    options.sort(key=lambda x: x["end"])

    # Build the groups list for the label-editing UI. Every distinct group
    # that has ever sent a message shows up here with its current label (if
    # any) and a message count.
    group_counts = {}
    for m in data.get("messages", []):
        g = m.get("group")
        if g:
            group_counts[g] = group_counts.get(g, 0) + 1
    labels = data.get("group_labels", {})
    groups = [
        {"jid": jid, "label": labels.get(jid, ""), "count": n}
        for jid, n in sorted(group_counts.items(), key=lambda x: -x[1])
    ]

    return {
        "pending": pending,
        "pending_count": len(pending),
        "unmapped_senders": list(unmapped_senders.values()),
        "booking_options": options,
        "groups": groups,
    }


@app.route("/review/accept/<msg_id>", methods=["POST"])
def review_accept(msg_id):
    """Apply Haiku's suggestion (or a user-overridden version) to the booking."""
    override_uid = request.form.get("booking_uid", "").strip()
    override_action = request.form.get("action", "").strip()
    override_cleaner = request.form.get("cleaner", "").strip()
    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg:
            return redirect(ingress_prefix() + "/#review")
        res = msg.get("haiku_result") or {}
        booking_uid = override_uid or res.get("booking_uid")
        action = override_action or res.get("action") or "confirm"
        cleaner = override_cleaner or res.get("cleaner") or lookup_cleaner_by_jid(data, msg.get("sender"))
        if booking_uid and booking_uid in data.get("bookings", {}) and action in ("confirm", "decline"):
            # Same facts this message produced at ingest, so accepting from the
            # Review tab applies the stated time exactly as the auto path does.
            # An override_uid may point at a different booking than the model
            # chose; `_stated_clean_time` keys on that booking's own cleaning
            # date, so a mismatched date simply yields no time rather than
            # writing one message's time onto an unrelated day.
            stored_facts = (data.get("message_facts", {}).get(msg_id) or {}).get("facts")
            _apply_booking_change(data, booking_uid, cleaner, action, msg,
                                  auto=False, facts_list=stored_facts)
            msg["review_state"] = "auto"
            msg["applied_uid"] = booking_uid
            save_data(data)
    return redirect(ingress_prefix() + "/#review")


@app.route("/review/label_group", methods=["POST"])
def review_label_group():
    """Set a human-friendly label for a group JID."""
    jid = request.form.get("jid", "").strip()
    label = request.form.get("label", "").strip()
    if not jid:
        return redirect(ingress_prefix() + "/#review")
    with DATA_LOCK:
        data = load_data()
        labels = data.setdefault("group_labels", {})
        if label:
            labels[jid] = label
        else:
            labels.pop(jid, None)
        save_data(data)
    return redirect(ingress_prefix() + "/#review")


@app.route("/review/ignore/<msg_id>", methods=["POST"])
def review_ignore(msg_id):
    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if msg:
            msg["review_state"] = "ignored"
            save_data(data)
    return redirect(ingress_prefix() + "/#review")


@app.route("/review/map", methods=["POST"])
def review_map_sender():
    """Map a WhatsApp sender JID to a cleaner name. Either maps to an existing
    cleaner (from the dropdown) or records a new name in data.cleaner_jids.
    After mapping, re-queue any of this sender's pending messages for a fresh
    parse so the sender hint applies.
    """
    jid = request.form.get("jid", "").strip()
    cleaner = request.form.get("cleaner", "").strip()
    new_name = request.form.get("new_cleaner", "").strip()
    target = cleaner or new_name
    if not jid or not target:
        return redirect(ingress_prefix() + "/#review")
    with DATA_LOCK:
        data = load_data()
        jids = data.setdefault("cleaner_jids", {}).setdefault(target, [])
        if jid not in jids:
            jids.append(jid)
        # Re-queue this sender's pending messages
        requeue = []
        for m in data.get("messages", []):
            if m.get("sender") == jid and m.get("review_state") == "pending":
                m["parsed"] = False
                m["haiku_result"] = None
                m["parse_error"] = None
                requeue.append(m["id"])
        save_data(data)
    ensure_workers_started()
    for mid in requeue:
        enqueue_message(mid)
    return redirect(ingress_prefix() + "/#review")


@app.route("/review/ignore-sender", methods=["POST"])
def review_ignore_sender():
    """Mark a JID as a host/non-cleaner so it stops appearing in unmapped senders."""
    jid = request.form.get("jid", "").strip()
    if not jid:
        return redirect(ingress_prefix() + "/#review")
    with DATA_LOCK:
        data = load_data()
        host_jids = data.setdefault("host_jids", [])
        if jid not in host_jids:
            host_jids.append(jid)
        save_data(data)
    return redirect(ingress_prefix() + "/#review")






@app.route("/print")
def print_view():
    month_str = request.args.get("month", "")
    if not month_str:
        month_str = date.today().strftime("%Y-%m")
    try:
        datetime.strptime(month_str, "%Y-%m")
    except ValueError:
        month_str = date.today().strftime("%Y-%m")

    data = load_data()
    bookings = data.get("bookings", {})
    ctx = build_print_data(month_str, bookings)
    ctx["prefix"] = ingress_prefix()
    return render_template_string(PRINT_TEMPLATE, **ctx)


if __name__ == "__main__":
    if ICAL_URL:
        print("Syncing Airbnb calendar...")
        _, err = sync_ical()
        if err:
            print(f"Warning: sync failed: {err}")
        else:
            print("Sync complete!")
    else:
        print("No iCal URL configured — skipping initial sync.")

    migrated = _migrate_timestamps_to_utc()
    if migrated:
        print(f"[migrate] {migrated} backfilled message timestamp(s) converted "
              f"from local time to UTC")

    ensure_workers_started()
    print("WhatsApp parse workers started (pool=2).")

    # Always start — the thread owns the nightly iCal sync (1.24.0) even when
    # the digest itself is disabled.
    threading.Thread(target=_digest_scheduler, daemon=True, name="digest-scheduler").start()
    if BRIDGE_WATCHDOG_ENABLED and BRIDGE_WATCHDOG_SLUG:
        # Prune once at boot as well as every ~288 checks: a container that
        # restarts more often than once a day would otherwise never reach the
        # in-loop counter and the log would grow past its window unnoticed.
        kept = watchdog_mod.prune_checks(BRIDGE_CHECK_LOG)
        print(f"[watchdog] check log pruned to {watchdog_mod.CHECK_LOG_RETENTION_DAYS}d — {kept} record(s)")
        threading.Thread(target=_watchdog_scheduler, daemon=True, name="bridge-watchdog").start()
    elif BRIDGE_WATCHDOG_ENABLED:
        print("[watchdog] DISABLED — bridge_watchdog_slug is empty; set it in the add-on options")

    print("\nStarting server at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000)
