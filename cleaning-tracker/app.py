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
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect, jsonify, abort

import facts as facts_mod
import gcal as gcal_mod

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

GCAL_ENABLED = bool(OPTIONS.get("gcal_enabled", False))
GCAL_CALENDAR_ID = OPTIONS.get("gcal_calendar_id", "")
GCAL_SERVICE_ACCOUNT_JSON = OPTIONS.get("gcal_service_account_json", "")

WHATSAPP_SHARED_SECRET = OPTIONS.get(
    "whatsapp_shared_secret", os.environ.get("WHATSAPP_SHARED_SECRET", "")
)


def _gcal_push(data):
    """Fire-and-forget GCal projection after a write. Errors are swallowed so
    a GCal outage never blocks the local app."""
    if not GCAL_ENABLED:
        return
    try:
        stats, err = gcal_mod.sync_to_gcal(
            data, GCAL_SERVICE_ACCOUNT_JSON, GCAL_CALENDAR_ID,
        )
        if err:
            print(f"[gcal] sync error: {err}")
        elif stats:
            print(f"[gcal] synced: {stats}")
    except Exception as e:
        print(f"[gcal] unexpected: {e}")


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
    """Fetch Airbnb iCal and merge into local data."""
    data = load_data()

    if not ICAL_URL:
        return data, "No iCal URL configured. Set it in the add-on options."

    try:
        resp = requests.get(ICAL_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
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

    `history` is the full cross-group message archive (volume is low enough to
    just pass everything). `labels` is {group_jid: human_label}.

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
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        text = result["content"][0]["text"].strip()
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


def process_message(msg_id):
    """Parse one inbound message with Haiku; auto-apply or flag for review."""
    with DATA_LOCK:
        data = load_data()
        msg = _find_message(data, msg_id)
        if not msg:
            return
        if msg.get("parsed"):
            return
        # Snapshot everything we need, then release the lock while we call the
        # API. Re-acquire on write. Volume is low, so pass the full archive
        # across all groups rather than a per-group window.
        history = [m for m in data["messages"] if m.get("id") != msg_id]
        bookings = dict(data.get("bookings", {}))
        known = cleaner_names()
        sender_cleaner = lookup_cleaner_by_jid(data, msg.get("sender"))
        labels = dict(data.get("group_labels", {}))

    result, error = parse_whatsapp_message(msg, history, bookings, known, sender_cleaner, labels)
    # Facts extraction runs independently of parse routing. An empty facts list
    # is a valid result (chitchat) — only facts_err means retry via reprocess.
    facts_list, facts_err = facts_mod.extract_facts(
        ANTHROPIC_API_KEY, msg, history, known, labels,
    )

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


def _apply_booking_change(data, booking_uid, cleaner_name, action, msg):
    """Apply a confirm/decline to a booking. Caller holds DATA_LOCK."""
    booking = data["bookings"].get(booking_uid)
    if not booking:
        return
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
</div>

<div id="notify-tab" class="panel active">
  {% if unassigned %}
  <div class="unassigned-card">
    <div class="unassigned-header">Unassigned bookings ({{ unassigned|length }}) · <a href="{{ prefix }}/backfill" style="font-weight:500;font-size:0.85rem;">Backfill from chat</a></div>
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
    var btn = document.getElementById('review-tab-btn');
    showTab('review-tab', btn);
  });
}
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
    return render_template_string(
        FOCUS_TEMPLATE,
        error=request.args.get("error"),
        **ctx,
        **review,
    )


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
    pull the upstream feed itself.
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
    """Gate: loopback open, otherwise X-Shared-Secret must match. Aborts 403."""
    remote = request.remote_addr or ""
    if remote in ("127.0.0.1", "::1"):
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

    extracted = 0
    errors = 0
    for m in stale:
        history = [h for h in all_messages if h.get("id") != m.get("id")]
        facts_list, err = facts_mod.extract_facts(
            ANTHROPIC_API_KEY, m, history, known, labels,
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


# ── Transcript ingest (historical backfill into facts layer) ────────────────
#
# /admin/ingest-transcript takes a pasted WhatsApp export and threads each
# line through the same pipeline the live sidecar uses — but with an `apply`
# switch. apply=false (historical catch-up): facts only, no parse, no
# auto-apply. apply=true (future bulk adds): full process_message path.

_TRANSCRIPT_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}),\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\]\s+([^:]+?):\s?(.*)$"
)

INGEST_LOCK = threading.Lock()
INGEST_STATUS = {"running": False, "total": 0, "done": 0, "errors": 0, "apply": False}


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
            date_s, time_s, sender, body = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
            try:
                ts = datetime.strptime(f"{date_s} {time_s.upper().replace('  ', ' ')}", "%Y-%m-%d %I:%M:%S %p")
            except ValueError:
                try:
                    ts = datetime.strptime(f"{date_s} {time_s.upper().replace('  ', ' ')}", "%Y-%m-%d %I:%M %p")
                except ValueError:
                    try:
                        ts = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        try:
                            ts = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
                        except ValueError:
                            continue
            out.append({
                "timestamp": ts.isoformat(timespec="seconds"),
                "sender": sender,
                "text": body,
            })
        else:
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
        history = [m for m in data["messages"] if m.get("id") != msg_id]
        known = cleaner_names()
        labels = dict(data.get("group_labels", {}))

    facts_list, facts_err = facts_mod.extract_facts(
        ANTHROPIC_API_KEY, msg, history, known, labels,
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
            except Exception as e:
                print(f"[ingest] error on {mid}: {e}")
                INGEST_STATUS["errors"] += 1
            INGEST_STATUS["done"] += 1
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
    apply_flag = str(payload.get("apply") or "").lower() in ("1", "true", "yes", "on")

    if not transcript:
        return jsonify({"error": "missing transcript"}), 400
    if INGEST_LOCK.locked() or INGEST_STATUS.get("running"):
        return jsonify({"error": "ingest already running"}), 409

    entries = _parse_whatsapp_transcript(transcript)
    if not entries:
        return jsonify({"error": "no messages parsed from transcript"}), 400

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

    pending = []
    unmapped_senders = {}  # sender_jid -> first msg preview
    for m in data.get("messages", []):
        if m.get("review_state") != "pending":
            continue
        sender = m.get("sender") or ""
        if sender and sender not in known_jids and sender not in unmapped_senders:
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
            _apply_booking_change(data, booking_uid, cleaner, action, msg)
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


def _unassigned_active_bookings(bookings):
    """All active bookings with no cleaner. No date window — backfill may span
    arbitrary history."""
    out = []
    for uid, b in bookings.items():
        if b.get("status") != "active":
            continue
        if b.get("cleaner"):
            continue
        try:
            start = datetime.strptime(b["start"], "%Y-%m-%d").date()
            end = datetime.strptime(b["end"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue
        out.append({
            "uid": uid,
            "checkin": b["start"],
            "checkout": b["end"],
            "label": f"{start.strftime('%b %d, %Y')} → {end.strftime('%b %d, %Y')}",
        })
    out.sort(key=lambda x: x["checkout"])
    return out


def _backfill_haiku(transcript, unassigned, known_cleaners, group_hint):
    """Ask Haiku to match unassigned bookings against a pasted chat transcript.

    Returns (proposals, None) or (None, error). proposals is a list of
    {uid, cleaner, clean_time, confidence, evidence} — one entry per booking
    it could match. Bookings with no match are omitted.
    """
    if not ANTHROPIC_API_KEY:
        return None, "No Anthropic API key configured."

    prompt = f"""You are backfilling cleaner assignments from a WhatsApp chat export.

Known cleaners: {json.dumps(known_cleaners)}
Group hint: {group_hint or "(not provided)"}

Unassigned bookings (checkout date = cleaning day):
{json.dumps(unassigned)}

Chat transcript:
---
{transcript}
---

For each booking, scan the transcript for a message where a cleaner clearly
commits to that specific cleaning date (e.g. "yes I can do Aug 17",
"confirmed for the 17th", or the host proposing a date that the cleaner
then accepts). Only propose an assignment if a single cleaner's confirmation
is unambiguous — skip bookings with no clear match, conflicting confirmations,
or only a host-side proposal that was never accepted. If a specific cleaning
time is mentioned (e.g. "11am"), include it as "HH:MM:SS"; otherwise null.

Return ONLY valid JSON, no other text. Shape:
{{"proposals":[{{"uid":"...","cleaner":"...","clean_time":"HH:MM:SS or null","confidence":0.0,"evidence":"one-sentence quote or paraphrase from the transcript"}}]}}"""

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
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        parsed = json.loads(text)
        return parsed.get("proposals", []), None
    except requests.exceptions.HTTPError as e:
        return None, f"Anthropic API error: {e.response.status_code} - {e.response.text[:200]}"
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        return None, f"Failed to parse LLM response: {e}"
    except Exception as e:
        return None, f"Error calling Anthropic API: {e}"


BACKFILL_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Backfill Assignments</title>
<style>{{ shared_styles|safe }}
.proposal { padding: 10px; border: 1px solid #dee2e6; border-radius: 6px; margin-bottom: 8px; background: #fff; }
.proposal .meta { font-size: 0.85rem; color: #666; }
.proposal .evidence { font-style: italic; color: #555; margin-top: 4px; font-size: 0.88rem; }
.conf-high { border-left: 4px solid #198754; }
.conf-mid { border-left: 4px solid #ffc107; }
.conf-low { border-left: 4px solid #dc3545; }
textarea { width: 100%; min-height: 280px; padding: 10px; border-radius: 6px; border: 1px solid #ccc; font-family: monospace; font-size: 0.85rem; }
label.inline { display: inline-flex; align-items: center; gap: 6px; margin-right: 12px; }
</style></head><body>
<h1>Backfill cleaner assignments</h1>
<p class="subtitle"><a href="{{ prefix }}/">← Back to queue</a></p>

{% if error %}<div class="card" style="border-left-color:#dc3545;"><strong>Error:</strong> {{ error }}</div>{% endif %}

{% if not proposals %}
<div class="card">
  <p>Paste a WhatsApp chat export (Settings → Export chat → Without media). Haiku will scan it against your <strong>{{ unassigned_count }} unassigned bookings</strong> and propose matches for your review. Nothing is written until you confirm.</p>
</div>
<form method="POST" action="{{ prefix }}/backfill">
  <label>Group hint (optional): <input type="text" name="group_hint" placeholder="e.g. Maria group" style="padding:6px;border-radius:4px;border:1px solid #ccc;width:260px;"></label>
  <div style="margin-top:10px;"><textarea name="transcript" placeholder="Paste full transcript here..."></textarea></div>
  <button type="submit" class="btn btn-primary" style="margin-top:10px;">Analyse</button>
</form>
{% else %}
<div class="card"><strong>{{ proposals|length }} proposal{{ 's' if proposals|length != 1 else '' }}</strong> from Haiku. Tick the ones to apply; override cleaner name if needed. Applying will also stamp the commitment so these won't appear in the notify queue.</div>
<form method="POST" action="{{ prefix }}/backfill/apply">
  {% for p in proposals %}
  <div class="proposal {% if p.confidence >= 0.85 %}conf-high{% elif p.confidence >= 0.6 %}conf-mid{% else %}conf-low{% endif %}">
    <label class="inline"><input type="checkbox" name="apply_{{ p.uid }}" value="1" {% if p.confidence >= 0.85 %}checked{% endif %}> Apply</label>
    <strong>{{ p.label }}</strong>
    <span class="meta">— confidence {{ '%.2f'|format(p.confidence) }}</span>
    <div style="margin-top:6px;">
      <label class="inline">Cleaner:
        <select name="cleaner_{{ p.uid }}">
          {% for c in cleaners %}<option value="{{ c }}" {% if c == p.cleaner %}selected{% endif %}>{{ c }}</option>{% endfor %}
        </select>
      </label>
      <label class="inline">Time:
        <input type="time" name="clean_time_{{ p.uid }}" value="{{ p.clean_time_hhmm or '' }}">
      </label>
    </div>
    <div class="evidence">“{{ p.evidence }}”</div>
  </div>
  {% endfor %}
  <div style="margin-top:14px;">
    <button type="submit" class="btn btn-success">Apply selected</button>
    <a href="{{ prefix }}/backfill" class="btn btn-outline">Discard &amp; restart</a>
  </div>
</form>
{% endif %}
</body></html>
"""


@app.route("/backfill", methods=["GET", "POST"])
def backfill():
    data = load_data()
    bookings = data.get("bookings", {})
    unassigned = _unassigned_active_bookings(bookings)

    if request.method == "GET":
        return render_template_string(
            BACKFILL_TEMPLATE, prefix=ingress_prefix(), shared_styles=_SHARED_STYLES,
            proposals=None, unassigned_count=len(unassigned), cleaners=CLEANERS, error=None,
        )

    transcript = (request.form.get("transcript") or "").strip()
    group_hint = (request.form.get("group_hint") or "").strip()
    if not transcript:
        return render_template_string(
            BACKFILL_TEMPLATE, prefix=ingress_prefix(), shared_styles=_SHARED_STYLES,
            proposals=None, unassigned_count=len(unassigned), cleaners=CLEANERS,
            error="Paste a transcript first.",
        )

    proposals, err = _backfill_haiku(transcript, unassigned, CLEANERS, group_hint)
    if err:
        return render_template_string(
            BACKFILL_TEMPLATE, prefix=ingress_prefix(), shared_styles=_SHARED_STYLES,
            proposals=None, unassigned_count=len(unassigned), cleaners=CLEANERS, error=err,
        )

    uid_to_label = {u["uid"]: u["label"] for u in unassigned}
    enriched = []
    for p in proposals:
        uid = p.get("uid")
        if uid not in uid_to_label:
            continue
        ct = p.get("clean_time")
        hhmm = ct[:5] if ct and isinstance(ct, str) and len(ct) >= 5 else ""
        enriched.append({
            "uid": uid,
            "label": uid_to_label[uid],
            "cleaner": p.get("cleaner") or "",
            "clean_time": ct,
            "clean_time_hhmm": hhmm,
            "confidence": float(p.get("confidence") or 0),
            "evidence": p.get("evidence") or "",
        })
    enriched.sort(key=lambda x: (-x["confidence"], x["label"]))

    return render_template_string(
        BACKFILL_TEMPLATE, prefix=ingress_prefix(), shared_styles=_SHARED_STYLES,
        proposals=enriched, unassigned_count=len(unassigned), cleaners=CLEANERS, error=None,
    )


@app.route("/backfill/apply", methods=["POST"])
def backfill_apply():
    applied = 0
    with DATA_LOCK:
        data = load_data()
        bookings = data.get("bookings", {})
        for key, val in request.form.items():
            if not key.startswith("apply_") or val != "1":
                continue
            uid = key[len("apply_"):]
            booking = bookings.get(uid)
            if not booking or booking.get("status") != "active":
                continue
            cleaner = (request.form.get(f"cleaner_{uid}") or "").strip()
            clean_time = (request.form.get(f"clean_time_{uid}") or "").strip()
            if not cleaner:
                continue
            booking["cleaner"] = cleaner
            booking["cleaner_since"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            booking["clean_time"] = (clean_time + ":00") if clean_time else None
            ack_notified(booking, via="backfill")
            applied += 1
        if applied:
            save_data(data)
    return redirect(ingress_prefix() + "/")


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

    print("\nStarting server at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000)
