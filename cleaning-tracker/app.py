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
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import requests
from flask import Flask, render_template_string, request, redirect, jsonify, abort, Response

import bridge_watchdog as watchdog_mod
import facts as facts_mod
import gcal as gcal_mod
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
DIGEST_ENABLED = bool(OPTIONS.get("digest_enabled", False))
DIGEST_TIME = OPTIONS.get("digest_time", "08:00")

# How near a dated finding must be before it repeats in the nightly digest.
# Three weeks is roughly the window in which a cleaner can still be found and
# a calendar entry still matters; beyond it, repeating is noise. See the
# comment at the `persisting` filter for why this exists.
REPEAT_HORIZON_DAYS = int(OPTIONS.get("repeat_horizon_days", 21))

# Nightly digest push to the VPS Telegram bot. The Pi initiates (the VPS is
# egress-locked and cannot pull). Payload is built by ALLOWLIST — finding ids,
# dates, severities, cleaner first names and the generated `why` line only.
# Never guest names, WhatsApp text, quotes/evidence, tokens, or secrets.
VPS_PUSH_ENABLED = bool(OPTIONS.get("vps_push_enabled", False))
VPS_PUSH_URL = OPTIONS.get("vps_push_url", "")
VPS_PUSH_SECRET = OPTIONS.get("vps_push_secret", "")

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

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            data = json.load(f)
    else:
        data = {"bookings": {}, "last_sync": None}
    for uid, b in data.get("bookings", {}).items():
        if "type" not in b:
            b["type"] = "manual_cleaning" if uid.startswith("manual-") else "airbnb"
    data.setdefault("messages", [])
    data.setdefault("cleaner_jids", {})
    data.setdefault("host_jids", [])
    data.setdefault("group_labels", {})
    data.setdefault("message_facts", {})
    return data


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
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
    """
    data = load_data()

    if not ICAL_URL:
        err = "No iCal URL configured. Set it in the add-on options."
        _write_sync_status(False, err)
        return data, err

    try:
        resp = requests.get(ICAL_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        _write_sync_status(False, e)
        return data, str(e)

    cal = __import__("icalendar").Calendar.from_ical(resp.text)
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

# Auto-apply threshold. Haiku-returned confidence ≥ this value AND a known
# cleaner AND an unambiguous booking → apply directly to the booking. Anything
# else lands in the review queue.
AUTO_APPLY_CONFIDENCE = 0.85


def upcoming_booking_list(bookings):
    """Booking list shown to the LLM — checkout within recent past + future."""
    today = date.today()
    out = []
    for uid, b in bookings.items():
        if b.get("status") != "active":
            continue
        try:
            end = datetime.strptime(b["end"], "%Y-%m-%d").date()
            start = datetime.strptime(b["start"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue
        # Include cleanings from 3 days ago up to 60 days ahead — short replies
        # like "yes" may arrive after the actual clean date for past tense.
        if end < today - timedelta(days=3) or end > today + timedelta(days=60):
            continue
        out.append({
            "uid": uid,
            "checkin": b["start"],
            "checkout": b["end"],
            "label": f"{start.strftime('%b %d')} → {end.strftime('%b %d')}",
            "current_cleaner": b.get("cleaner"),
        })
    out.sort(key=lambda x: x["checkout"])
    return out


def parse_whatsapp_message(msg, history, bookings, known_cleaners, sender_cleaner, labels):
    """Ask Haiku to interpret a single inbound WhatsApp message in context.

    `history` is a windowed slice of the cross-group archive (most recent
    PARSE_HISTORY_WINDOW messages before this one). `labels` is {group_jid: human_label}.

    Returns ({booking_uid, cleaner, action, confidence, reason}, None) or
    (None, error_str). `action` is "confirm", "decline", or "none".
    """
    if not ANTHROPIC_API_KEY:
        return None, "No Anthropic API key configured."

    booking_list = upcoming_booking_list(bookings)
    history_lines = []
    for h in history:
        grp = labels.get(h.get("group")) or h.get("group") or "unknown-group"
        sender_label = h.get("sender") or "unknown"
        history_lines.append(f"[{h.get('timestamp','')}] ({grp}) {sender_label}: {h.get('text','')}")
    history_text = "\n".join(history_lines) if history_lines else "(no prior messages)"

    sender_hint = (
        f"This sender is known to be cleaner: {sender_cleaner}."
        if sender_cleaner
        else "This sender is not yet mapped to a known cleaner."
    )
    this_group = labels.get(msg.get("group")) or msg.get("group") or "unknown-group"

    prompt = f"""You interpret a single incoming WhatsApp message from a house-cleaning group chat.

Known cleaners: {json.dumps(known_cleaners)}
{sender_hint}

Upcoming bookings (checkout date = cleaning day):
{json.dumps(booking_list)}

Message archive across all groups (most recent last). Each line is [timestamp] (group) sender: text.
---
{history_text}
---

The new message (from {msg.get('sender','unknown')} in group "{this_group}" at {msg.get('timestamp','')}):
{msg.get('text','')}

Decide whether this message is the cleaner confirming or declining a specific cleaning. Short replies like "yes"/"ok"/"can do"/"sorry full" are only meaningful relative to the prior chatter — use the archive to resolve ambiguity. Messages from other groups may still be useful context (e.g. Michelle approving a plan in the host chat). If the message isn't actionable (chit-chat, question, unrelated) return action "none".

Return ONLY valid JSON, no other text:
{{"action":"confirm|decline|none","booking_uid":"uid or null","cleaner":"cleaner name or null","confidence":0.0,"reason":"one short sentence"}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        # claude-sonnet-5 may emit a thinking block before the text block —
        # select the text block instead of assuming content[0].
        text = next(
            b["text"] for b in result["content"] if b.get("type") == "text"
        ).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        parsed = json.loads(text)
        return parsed, None
    except requests.exceptions.HTTPError as e:
        return None, f"Anthropic API error: {e.response.status_code} - {e.response.text[:200]}"
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        return None, f"Failed to parse LLM response: {e}"
    except Exception as e:
        return None, f"Error calling Anthropic API: {e}"


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

# Parse history is cross-group (host-chat context helps resolve ambiguous
# replies) so a separate, slightly larger window. Still needs a cap —
# unbounded archive hits the 50k tokens/min org rate limit.
PARSE_HISTORY_WINDOW = 50
PARSE_HISTORY_DAYS = 30
PARSE_HISTORY_MAX = 150

# Cross-chat facts digest handed to fact extraction. Bounded in both
# directions: a little history (a date agreed last week may still be being
# renegotiated) and a long forward horizon (schedules are agreed months out —
# the Aug 3 commitment was made on Mar 30).
CROSS_FACTS_BACK_DAYS = 7
CROSS_FACTS_FWD_DAYS = 150
CROSS_FACTS_MAX_LINES = 80


def _msg_day(m):
    """Day-granularity date from a message timestamp, or None.

    Deliberately string-sliced rather than parsed: stored timestamps mix
    `2026-07-28T21:08:38.000Z` (live) with `2026-07-28T14:08:00` (backfill),
    and day precision is all the history windows need.
    """
    ts = m.get("timestamp") if isinstance(m, dict) else m
    if not ts or len(ts) < 10:
        return None
    try:
        return date.fromisoformat(ts[:10])
    except ValueError:
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
    for m in data.get("messages", []) or []:
        if m.get("id"):
            group_of[m["id"]] = m.get("group")
    labels = data.get("group_labels", {}) or {}

    # Keyed on (date, cleaner, kind) keeping the most recently stated — an old
    # confirm that was later superseded must not outrank the newer one.
    best = {}
    for msg_id, rec in (data.get("message_facts", {}) or {}).items():
        grp = group_of.get(msg_id)
        if not grp or grp == tgt_group:
            continue
        stated = rec.get("extracted_at") or ""
        for f in rec.get("facts", []) or []:
            tgt_date = f.get("target_date")
            kind = f.get("kind")
            cleaner = f.get("cleaner")
            if not tgt_date or not kind or kind == "unclear":
                continue
            if not (lo <= tgt_date <= hi):
                continue
            key = (tgt_date, cleaner, kind)
            if key not in best or stated > best[key][0]:
                best[key] = (stated, {
                    "date": tgt_date,
                    "cleaner": cleaner,
                    "kind": kind,
                    "time": f.get("target_time"),
                    "chat": labels.get(grp) or grp,
                    "stated": stated[:10],
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


def _parse_history(messages, target):
    tgt_ts = target.get("timestamp") or ""
    prior = [m for m in messages if (m.get("timestamp") or "") < tgt_ts]
    return _window_by_count_or_days(
        prior, PARSE_HISTORY_WINDOW, PARSE_HISTORY_DAYS, PARSE_HISTORY_MAX, target,
    )


def process_message(msg_id):
    """Parse one inbound message with Haiku; auto-apply or flag for review."""
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
        bookings = dict(data.get("bookings", {}))
        known = cleaner_names()
        sender_cleaner = lookup_cleaner_by_jid(data, msg.get("sender"))
        labels = dict(data.get("group_labels", {}))
        cross_facts = _cross_chat_facts(data, msg)

    result, error = parse_whatsapp_message(msg, _parse_history(all_messages, msg), bookings, known, sender_cleaner, labels)
    # Facts extraction runs independently of parse routing. An empty facts list
    # is a valid result (chitchat) — only facts_err means retry via reprocess.
    facts_list, facts_err = facts_mod.extract_facts(
        ANTHROPIC_API_KEY, msg, _facts_history(all_messages, msg), known, labels,
        cross_facts=cross_facts,
    )

    # Out-of-credit (HTTP 400 "balance too low") is not a per-message failure —
    # it's an account-wide outage. Don't bury it as a pending parse_error;
    # alert + defer so it auto-reprocesses when credits return.
    if _is_low_balance_error(error) or _is_low_balance_error(facts_err):
        _flag_credit_exhausted(msg_id)
        with DATA_LOCK:
            data = load_data()
            m = _find_message(data, msg_id)
            if m:
                m["parsed"] = False  # eligible for recovery requeue
                m["parse_error"] = error or facts_err
                m["review_state"] = "pending"
                save_data(data)
        return

    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg:
            return
        msg["parsed"] = True
        msg["parse_error"] = error
        msg["haiku_result"] = result

        if facts_list is not None:
            data.setdefault("message_facts", {})[msg_id] = facts_mod.build_record(
                facts_list, msg.get("sender") or "",
            )

        if error or not result:
            msg["review_state"] = "pending"
            save_data(data)
            return

        action = (result.get("action") or "none").lower()
        confidence = float(result.get("confidence") or 0.0)
        booking_uid = result.get("booking_uid")
        cleaner = result.get("cleaner") or sender_cleaner

        sender_known = sender_cleaner is not None
        booking_known = booking_uid and booking_uid in data.get("bookings", {})
        auto = (
            action in ("confirm", "decline")
            and confidence >= AUTO_APPLY_CONFIDENCE
            and sender_known
            and booking_known
            and cleaner in known
        )

        if action == "none":
            msg["review_state"] = "ignored"
        elif auto:
            _apply_booking_change(data, booking_uid, cleaner, action, msg)
            msg["review_state"] = "auto"
            msg["applied_uid"] = booking_uid
        else:
            msg["review_state"] = "pending"

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


def _change_findings(today_str, hours=24, now=None):
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
            "cleaner": None,
            "date": today_str,
            "why": f"{when} cleaning — {'; '.join(bits)} ({src} from WhatsApp).",
            "evidence": [],
        })
    return out


def _apply_booking_change(data, booking_uid, cleaner_name, action, msg, auto=True):
    """Apply a confirm/decline to a booking. Caller holds DATA_LOCK."""
    booking = data["bookings"].get(booking_uid)
    if not booking:
        return
    before = dict(booking)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if action == "confirm":
        if cleaner_name and not booking.get("cleaner"):
            booking["cleaner"] = cleaner_name
            booking["cleaner_since"] = now
        booking["confirmed"] = True
        # Cleaner has confirmed via WhatsApp — record that as the notified state.
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
        {% if f.kind in ('unrecorded_confirmation', 'schedule_unassigned') and f.booking_uid and f.cleaner %}
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
    return render_template_string(
        FOCUS_TEMPLATE,
        error=request.args.get("error"),
        digest_enabled=DIGEST_ENABLED,
        **ctx,
        **review,
        **conflicts,
    )


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
    data = load_data()
    cleaner = request.form.get("cleaner", "").strip()
    clean_time_raw = request.form.get("clean_time", "").strip()
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
        save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/confirm/<path:uid>", methods=["POST"])
def confirm(uid):
    data = load_data()
    if uid in data["bookings"]:
        data["bookings"][uid]["confirmed"] = True
        save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/pay/<path:uid>", methods=["POST"])
def pay(uid):
    data = load_data()
    if uid in data["bookings"]:
        data["bookings"][uid]["paid"] = True
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
    data = load_data()
    booking = data["bookings"].get(uid)
    if booking and (booking.get("type") in ("custom_stay", "manual_cleaning") or booking.get("status") == "cancelled"):
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
            "timestamp": ts or datetime.now().isoformat(timespec="seconds"),
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

    Idempotent: running repeatedly is safe. The reconciler only reads
    current-version facts, so a half-complete reprocess can't corrupt results.
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
            cross_facts = _cross_chat_facts(load_data(), m)
        facts_list, err = facts_mod.extract_facts(
            ANTHROPIC_API_KEY, m, history, known, labels, cross_facts=cross_facts,
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
            ts_raw = m.get("timestamp") or ""
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                ts = None
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
    )


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
                prior_raw = list(result.get("findings_raw") or result.get("findings") or [])
                result["findings_raw"] = wd_findings + prior_raw
                result["findings"] = wd_findings + list(result.get("findings") or [])
                counts = result.setdefault("counts", {})
                counts["total"] = counts.get("total", 0) + len(wd_findings)
                for f in wd_findings:
                    counts[f["severity"]] = counts.get(f["severity"], 0) + 1
                    counts[f["kind"]] = counts.get(f["kind"], 0) + 1
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
    with DATA_LOCK:
        data = load_data()
        data.setdefault("dismissed_findings", {})[finding_id] = {
            "dismissed_at": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
        }
        save_data(data)
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


def _digest_compute_and_notify():
    """Run reconcile, diff against baseline, post HA notification, save baseline.

    Returns a dict with new/resolved/total/notified/message. Safe to call from
    both the HTTP route and the background scheduler.
    """
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
    today_iso = date.today().isoformat()
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
        new_findings = [f for f in result["findings"] if f["id"] in current_ids - baseline_ids]
        resolved_count = len(baseline_ids - current_ids)

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
        horizon = (date.today() + timedelta(days=REPEAT_HORIZON_DAYS)).isoformat()
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
                days = (date.today() - date.fromisoformat(first_seen[f["id"]])).days
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

    changes = _change_findings(date.today().isoformat())
    if changes:
        message = message + "\n" + "\n".join(f"• [changed] {c['why']}" for c in changes[:10])
        if len(changes) > 10:
            message += f"\n…and {len(changes) - 10} more changes"

    notified = _post_ha_notification(title, message)
    _push_digest_to_vps(
        new_findings, resolved_count, result["counts"],
        extra_findings=repeated + changes,
    )

    DIGEST_LAST_FILE.write_text(json.dumps({
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "finding_ids": sorted(list(current_ids)),
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
    try:
        last_sync = load_data().get("last_sync")
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
        # `evidence` (raw WhatsApp text) must not cross to the VPS.
        "findings": [
            {
                "id": f.get("id"),
                "detector": f.get("detector"),
                "kind": f.get("kind"),
                "severity": f.get("severity"),
                "date": f.get("date"),
                "cleaner": f.get("cleaner"),
                "why": f.get("why"),
            }
            for f in outgoing
        ],
    }
    try:
        resp = requests.post(
            VPS_PUSH_URL,
            json=payload,
            headers={"X-Push-Secret": VPS_PUSH_SECRET},
            timeout=20,
        )
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
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


@app.route("/admin/remap-group", methods=["POST"])
def admin_remap_group():
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
    with/without seconds, 24h, dashes/slashes)."""
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
                    "timestamp": ts.isoformat(timespec="seconds"),
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
                        "timestamp": ts.isoformat(timespec="seconds"),
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

    facts_list, facts_err = facts_mod.extract_facts(
        ANTHROPIC_API_KEY, msg, history, known, labels, cross_facts=cross_facts,
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
            _apply_booking_change(data, booking_uid, cleaner, action, msg, auto=False)
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

    ensure_workers_started()
    print("WhatsApp parse workers started (pool=2).")

    # Always start — the thread owns the nightly iCal sync (1.24.0) even when
    # the digest itself is disabled.
    threading.Thread(target=_digest_scheduler, daemon=True, name="digest-scheduler").start()
    if BRIDGE_WATCHDOG_ENABLED and BRIDGE_WATCHDOG_SLUG:
        threading.Thread(target=_watchdog_scheduler, daemon=True, name="bridge-watchdog").start()
    elif BRIDGE_WATCHDOG_ENABLED:
        print("[watchdog] DISABLED — bridge_watchdog_slug is empty; set it in the add-on options")

    print("\nStarting server at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000)
