"""
Airbnb Cleaning Schedule Tracker
A simple web app to manage cleaning schedules from Airbnb bookings.
Paste WhatsApp chat logs to verify cleaner confirmations.
"""

import calendar
import hashlib
import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect, jsonify

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
    return data


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ── Cleaner color ─────────────────────────────────────────────────────────────

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
            if b.get("cleaner") and (b["start"] != start_str or b["end"] != end_str):
                if not b.get("conflict"):
                    b["conflict"] = {
                        "type": "dates_changed",
                        "old_start": b["start"],
                        "old_end": b["end"],
                        "detected": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    }
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
            if b.get("cleaner") and not b.get("conflict"):
                b["conflict"] = {
                    "type": "cancelled",
                    "old_start": b["start"],
                    "old_end": b["end"],
                    "detected": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                }

    data["last_sync"] = datetime.now().isoformat()
    save_data(data)
    return data, None


# ── WhatsApp parsing via LLM ────────────────────────────────────────────────

def parse_whatsapp_with_llm(chat_text, bookings):
    """Use Claude Haiku to parse WhatsApp chat and match to bookings."""
    if not ANTHROPIC_API_KEY:
        return None, "No Anthropic API key configured. Set it in the add-on options."

    # Build a list of booking checkout dates for the LLM
    today = date.today()
    booking_list = []
    for uid, b in bookings.items():
        if b["status"] != "active":
            continue
        end = datetime.strptime(b["end"], "%Y-%m-%d").date()
        if end < today:
            continue
        start = datetime.strptime(b["start"], "%Y-%m-%d").date()
        booking_list.append({
            "uid": uid,
            "checkin": b["start"],
            "checkout": b["end"],
            "label": f"{start.strftime('%b %d')} → {end.strftime('%b %d')}",
        })

    booking_list.sort(key=lambda x: x["checkout"])

    prompt = f"""Parse this WhatsApp chat about cleaning schedules. Cleaning happens on booking checkout dates.

Bookings (checkout = cleaning day):
{json.dumps(booking_list)}

Chat:
---
{chat_text}
---

Match each date in the chat to the nearest booking checkout (within 1 day). Status: "confirmed" if they gave a time or said yes, "declined" if "I'm full"/can't, else "unclear".

Return ONLY valid JSON, no other text. Keep notes very short (under 10 words). Omit null fields:
{{"matches":[{{"booking_uid":"uid","booking_label":"label","cleaning_date":"YYYY-MM-DD","cleaner_name":"name","status":"confirmed|declined|unclear","time":"time or omit","note":"short note"}}],"unmatched":[{{"cleaning_date":"YYYY-MM-DD","cleaner_name":"name","status":"...","note":"short"}}],"summary":"one line"}}"""

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
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        text = result["content"][0]["text"]

        # Extract JSON from response (handle markdown code blocks)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        parsed = json.loads(text)
        return parsed, None

    except requests.exceptions.HTTPError as e:
        return None, f"Anthropic API error: {e.response.status_code} - {e.response.text[:200]}"
    except json.JSONDecodeError as e:
        return None, f"Failed to parse LLM response as JSON: {e}"
    except Exception as e:
        return None, f"Error calling Anthropic API: {e}"


# ── HTML Templates ───────────────────────────────────────────────────────────

# Shared CSS used by both the legacy TEMPLATE and the new CALENDAR_TEMPLATE.
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

# Body content shared between TEMPLATE and the List tab of CALENDAR_TEMPLATE.
# Does NOT include <html>/<head>/<body> tags — those are supplied by the
# wrapping template.
LIST_PANEL_TEMPLATE = """
<h1>Cleaning Schedule</h1>
<p class="subtitle">
  Last synced: {{ last_sync or "Never" }}
  {% if error %}<span style="color:red"> | Sync error: {{ error }}</span>{% endif %}
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
  <a href="{{ prefix }}/add" class="btn btn-secondary">+ Manual Cleaning</a>
</div>

<!-- Stats -->
<div class="stats">
  <div class="stat">
    <div class="stat-num" style="color:#dc3545">{{ needs_cleaner }}</div>
    <div class="stat-label">Need Cleaner</div>
  </div>
  <div class="stat">
    <div class="stat-num" style="color:#ffc107">{{ assigned_count }}</div>
    <div class="stat-label">Assigned</div>
  </div>
  <div class="stat">
    <div class="stat-num" style="color:#198754">{{ confirmed_count }}</div>
    <div class="stat-label">Confirmed</div>
  </div>
  <div class="stat">
    <div class="stat-num">{{ upcoming_count }}</div>
    <div class="stat-label">Upcoming</div>
  </div>
  {% if conflicts_count %}
  <div class="stat">
    <div class="stat-num" style="color:#fd7e14">{{ conflicts_count }}</div>
    <div class="stat-label">Needs Review</div>
  </div>
  {% endif %}
</div>

<!-- Conflict / Needs Review section -->
{% if conflicts %}
<div style="margin-bottom:16px;">
  <h2 style="font-size:1.05rem;font-weight:700;color:#fd7e14;margin-bottom:10px;">&#9888; Needs Review</h2>
  {% for b in conflicts %}
  <div class="card conflicted">
    <div class="card-header">
      <div>
        {% if b.conflict.type == 'dates_changed' %}
        <div class="dates">{{ b.start_fmt }} &rarr; {{ b.end_fmt }}</div>
        <div style="font-size:0.85rem;color:#fd7e14;margin-top:2px;">
          Was: {{ b.conflict.old_start }} &rarr; {{ b.conflict.old_end }}
        </div>
        {% else %}
        <div class="dates">{{ b.start_fmt }} &rarr; {{ b.end_fmt }}</div>
        {% endif %}
        <div style="font-size:0.85rem;margin-top:4px;">
          <strong>{{ b.conflict_fmt }}</strong>
        </div>
        <div style="font-size:0.85rem;color:#666;margin-top:2px;">
          Cleaner: <strong>{{ b.cleaner }}</strong>
          {% if b.cleaner_since_fmt %}<span style="color:#999;"> · assigned {{ b.cleaner_since_fmt }}</span>{% endif %}
        </div>
      </div>
      <span class="badge" style="background:#ffe8cc;color:#854d0e;">Review</span>
    </div>
    <div class="card-actions" style="margin-top:10px;">
      <form action="{{ prefix }}/resolve/{{ b.uid }}" method="POST" style="display:inline;">
        <input type="hidden" name="action" value="keep">
        <button type="submit" class="btn btn-sm btn-success">Keep Cleaning</button>
      </form>
      <form action="{{ prefix }}/resolve/{{ b.uid }}" method="POST" style="display:inline;">
        <input type="hidden" name="action" value="cancel">
        <button type="submit" class="btn btn-sm btn-danger">Cancel Cleaning</button>
      </form>
      {% if b.conflict.type == 'dates_changed' %}
      <form action="{{ prefix }}/resolve/{{ b.uid }}" method="POST" style="display:inline;">
        <input type="hidden" name="action" value="move">
        <button type="submit" class="btn btn-sm btn-warning">Move Cleaning</button>
      </form>
      {% endif %}
    </div>
  </div>
  {% endfor %}
</div>
{% endif %}

<!-- Tabs -->
<div class="tabs">
  <button class="tab active" onclick="showTab('upcoming')">Upcoming</button>
  <button class="tab" onclick="showTab('past')">Past</button>
  <button class="tab" onclick="showTab('whatsapp')">WhatsApp</button>
</div>

<!-- Upcoming Panel -->
<div id="upcoming" class="panel active">
  {% if not upcoming %}
    <div class="empty">No upcoming bookings. Hit "Sync Airbnb" to fetch.</div>
  {% endif %}
  {% for b in upcoming %}
  <div class="card {{ b.card_class }}">
    <div class="card-header">
      <div>
        <div class="dates">{{ b.start_fmt }} → {{ b.end_fmt }}</div>
        <div class="cleaning-date">Clean by: {{ b.end_fmt }} (checkout day)</div>
        {% if b.nights %}
        <div style="font-size:0.8rem;color:#666">{{ b.nights }} night{{ 's' if b.nights != 1 }} · {{ b.days_until }}</div>
        {% endif %}
      </div>
      <span class="badge badge-{{ b.status }}">{{ b.status }}</span>
    </div>
    <div class="card-actions">
      <form class="assign-form" action="{{ prefix }}/assign/{{ b.uid }}" method="POST" style="display:flex;gap:4px;align-items:center;">
        <select name="cleaner">
          <option value="">-- Assign --</option>
          {% for c in cleaners %}
          <option value="{{ c }}" {{ 'selected' if b.cleaner == c }}>{{ c }}</option>
          {% endfor %}
        </select>
        <button type="submit" class="btn btn-sm btn-outline">Save</button>
      </form>
      {% if b.cleaner and not b.confirmed %}
        <form action="{{ prefix }}/confirm/{{ b.uid }}" method="POST" style="display:inline;">
          <button type="submit" class="btn btn-sm btn-success">Mark Confirmed</button>
        </form>
      {% endif %}
      {% if b.cleaner and not b.paid %}
        <form action="{{ prefix }}/pay/{{ b.uid }}" method="POST" style="display:inline;">
          <button type="submit" class="btn btn-sm btn-warning">Mark Paid</button>
        </form>
      {% elif b.paid %}
        <span class="badge" style="background:#d4edda;color:#155724">Paid</span>
      {% endif %}
    </div>
    {% if b.notes %}
    <div style="margin-top:6px;font-size:0.85rem;color:#666">{{ b.notes }}</div>
    {% endif %}
    {% if b.cleaner_since %}
    <div style="margin-top:4px;font-size:0.75rem;color:#999">Assigned: {{ b.cleaner_since_fmt }}</div>
    {% endif %}
  </div>
  {% endfor %}
</div>

<!-- Past Panel -->
<div id="past" class="panel">
  {% if not past %}
    <div class="empty">No past bookings yet.</div>
  {% endif %}
  {% for b in past %}
  <div class="card {{ b.card_class }}">
    <div class="card-header">
      <div>
        <div class="dates">{{ b.start_fmt }} → {{ b.end_fmt }}</div>
      </div>
      <span class="badge badge-{{ b.status }}">{{ b.status }}</span>
    </div>
    <div style="font-size:0.85rem;color:#666;margin-top:4px;">
      Cleaner: {{ b.cleaner or "None" }} ·
      {{ "Paid" if b.paid else "Not Paid" }}
    </div>
  </div>
  {% endfor %}
</div>

<!-- WhatsApp Panel -->
<div id="whatsapp" class="panel">
  <p style="margin-bottom:8px;font-size:0.9rem;">
    Paste a WhatsApp chat below. An AI will parse the messages, match dates to bookings,
    and detect confirmations or declines.
  </p>
  <form action="{{ prefix }}/whatsapp" method="POST">
    <textarea name="chat" class="whatsapp-box" placeholder="Paste WhatsApp chat here — any format works.">{{ wa_text or "" }}</textarea>
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center;">
      <button type="submit" class="btn btn-primary">Parse Chat</button>
      <label style="font-size:0.85rem;">
        <input type="checkbox" name="auto_apply" value="1"> Auto-apply confirmations to bookings
      </label>
    </div>
  </form>

  {% if wa_error %}
  <div class="error-box">{{ wa_error }}</div>
  {% endif %}

  {% if wa_parsed %}
  <div class="wa-results">
    {% if wa_parsed.summary %}
    <div class="wa-summary">{{ wa_parsed.summary }}</div>
    {% endif %}

    {% if wa_parsed.matches %}
    <h3 style="margin:12px 0 8px;">Matched to Bookings</h3>
    {% for m in wa_parsed.matches %}
    <div class="wa-match {{ m.status }}">
      <div class="wa-date">
        {{ m.booking_label }} — {{ m.cleaner_name }}
        {% if m.time %} at {{ m.time }}{% endif %}
      </div>
      <span class="badge badge-{{ m.status }}" style="
        {% if m.status == 'confirmed' %}background:#d4edda;color:#155724
        {% elif m.status == 'declined' %}background:#ffcccb;color:#721c24
        {% else %}background:#fff3cd;color:#856404{% endif %}
      ">{{ m.status }}</span>
      {% if m.note %}
      <div class="wa-note">{{ m.note }}</div>
      {% endif %}
      {% if wa_booking_options %}
      <form action="{{ prefix }}/apply-match" method="POST" style="display:flex;gap:4px;align-items:center;margin-top:6px;flex-wrap:wrap;">
        <select name="booking_uid" style="padding:3px 6px;border-radius:4px;border:1px solid #ccc;font-size:0.8rem;">
          {% for opt in wa_booking_options %}
          <option value="{{ opt.uid }}" {{ 'selected' if opt.uid == m.booking_uid }}>{{ opt.label }}</option>
          {% endfor %}
        </select>
        <input type="hidden" name="cleaner_name" value="{{ m.cleaner_name }}">
        <input type="hidden" name="status" value="{{ m.status }}">
        <input type="hidden" name="time" value="{{ m.time or '' }}">
        <input type="hidden" name="note" value="{{ m.note or '' }}">
        {% if m.status == 'declined' %}
        <button type="submit" class="btn btn-sm btn-danger">Apply Decline</button>
        {% elif m.status == 'confirmed' %}
        <button type="submit" class="btn btn-sm btn-success">Apply</button>
        {% else %}
        <button type="submit" class="btn btn-sm btn-warning">Apply</button>
        {% endif %}
      </form>
      {% endif %}
    </div>
    {% endfor %}
    {% endif %}

    {% if wa_parsed.unmatched %}
    <h3 style="margin:12px 0 8px;">Unmatched Dates</h3>
    {% for m in wa_parsed.unmatched %}
    <div class="wa-match {{ m.status }}">
      <div class="wa-date">{{ m.cleaning_date }} — {{ m.cleaner_name }}</div>
      <span class="badge" style="
        {% if m.status == 'confirmed' %}background:#d4edda;color:#155724
        {% elif m.status == 'declined' %}background:#ffcccb;color:#721c24
        {% else %}background:#fff3cd;color:#856404{% endif %}
      ">{{ m.status }}</span>
      {% if m.note %}
      <div class="wa-note">{{ m.note }}</div>
      {% endif %}
      {% if wa_booking_options %}
      <form action="{{ prefix }}/apply-match" method="POST" style="display:flex;gap:4px;align-items:center;margin-top:6px;flex-wrap:wrap;">
        <select name="booking_uid" style="padding:3px 6px;border-radius:4px;border:1px solid #ccc;font-size:0.8rem;">
          <option value="">-- Select booking --</option>
          {% for opt in wa_booking_options %}
          <option value="{{ opt.uid }}">{{ opt.label }}</option>
          {% endfor %}
        </select>
        <input type="hidden" name="cleaner_name" value="{{ m.cleaner_name }}">
        <input type="hidden" name="status" value="{{ m.status }}">
        <input type="hidden" name="time" value="{{ m.time or '' }}">
        <input type="hidden" name="note" value="{{ m.note or '' }}">
        {% if m.status == 'declined' %}
        <button type="submit" class="btn btn-sm btn-danger">Apply Decline</button>
        {% elif m.status == 'confirmed' %}
        <button type="submit" class="btn btn-sm btn-success">Apply</button>
        {% else %}
        <button type="submit" class="btn btn-sm btn-warning">Apply</button>
        {% endif %}
      </form>
      {% endif %}
    </div>
    {% endfor %}
    {% endif %}

    {% if wa_applied %}
    <div style="margin-top:12px;padding:10px;background:#d4edda;border-radius:8px;font-size:0.9rem;">
      Auto-applied {{ wa_applied }} confirmation(s) to bookings.
    </div>
    {% endif %}
  </div>
  {% endif %}
</div>

<script>
function showTab(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  event.target.classList.add('active');
}
</script>
"""

# Legacy full-page template — still used by the /whatsapp POST route so the
# WhatsApp results page continues to work standalone.
TEMPLATE = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>Cleaning Schedule</title>\n"
    "<style>" + _SHARED_STYLES + "</style>\n"
    "</head>\n<body>\n"
    + LIST_PANEL_TEMPLATE +
    "</body>\n</html>\n"
)

# ── Calendar template (new index page) ───────────────────────────────────────
# Built by string concatenation so LIST_PANEL_TEMPLATE is spliced in directly,
# avoiding the need for render_template_string include support.

_CALENDAR_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cleaning Schedule</title>
<script src="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js"></script>
<style>
""" + _SHARED_STYLES + """
  /* Calendar tab extras */
  #calendar { margin-top: 8px; }
  .top-tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 2px solid #dee2e6; }
  .top-tab {
    padding: 8px 18px; cursor: pointer; border: none; background: none;
    font-size: 1rem; border-bottom: 3px solid transparent; margin-bottom: -2px;
  }
  .top-tab.active { border-bottom-color: #0d6efd; font-weight: 600; color: #0d6efd; }
  .top-panel { display: none; }
  .top-panel.active { display: block; }
  .fc-event { cursor: pointer; }
  .cancelled-stay { opacity: 0.45; }
</style>
</head>
<body>

<h1>Cleaning Schedule</h1>
<p class="subtitle">
  Last synced: {{ last_sync or "Never" }}
  {% if error %}<span style="color:red"> | Sync error: {{ error }}</span>{% endif %}
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
  <a href="{{ prefix }}/add" class="btn btn-secondary">+ Add Entry</a>
  <a href="{{ prefix }}/print" class="btn btn-outline">Print Month</a>
</div>

<!-- Top-level tabs: Calendar / List -->
<div class="top-tabs">
  <button class="top-tab active" onclick="showTopTab('calendar-tab', this)">Calendar</button>
  <button class="top-tab" onclick="showTopTab('list-tab', this)">List</button>
</div>

<!-- Calendar panel -->
<div id="calendar-tab" class="top-panel active">
  <div id="calendar"></div>
</div>

<!-- List panel — existing UI embedded inline -->
<div id="list-tab" class="top-panel">
"""

_CALENDAR_FOOT = """
</div><!-- /#list-tab -->

<script>
function showTopTab(id, btn) {
  document.querySelectorAll('.top-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.top-tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}

document.addEventListener('DOMContentLoaded', function() {
  const calendarEl = document.getElementById('calendar');
  const prefix = "{{ prefix }}";
  const isMobile = window.innerWidth < 600;
  const calendar = new FullCalendar.Calendar(calendarEl, {
    initialView: isMobile ? 'listWeek' : 'dayGridMonth',
    headerToolbar: { left: 'prev,next today', center: 'title', right: 'dayGridMonth,dayGridWeek,listWeek' },
    height: 'auto',
    eventSources: [ prefix + '/events.json' ],
    eventClick: function(info) {
      const p = info.event.extendedProps;
      if (p.type === 'cleaning') {
        window.location = prefix + '/edit/' + encodeURIComponent(p.uid);
      } else if (p.type === 'airbnb' || p.type === 'custom_stay') {
        window.location = prefix + '/edit/' + encodeURIComponent(p.uid);
      }
    },
    dateClick: function(info) {
      window.location = prefix + '/add?date=' + info.dateStr;
    }
  });
  calendar.render();
});
</script>
</body>
</html>
"""

# Concatenate at module load: head + list panel body + foot.
CALENDAR_TEMPLATE = _CALENDAR_HEAD + LIST_PANEL_TEMPLATE + _CALENDAR_FOOT

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
<a href="{{ prefix }}/">&larr; Back to calendar</a>
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

<!-- Delete (only for manual/custom types) -->
{% if deletable %}
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


# ── View helpers ─────────────────────────────────────────────────────────────

def _fmt_timestamp(ts):
    """Format an ISO timestamp to a short human-readable string."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%b %d, %I:%M %p")
    except (ValueError, TypeError):
        return ts


def format_booking(uid, b):
    start = datetime.strptime(b["start"], "%Y-%m-%d").date()
    end = datetime.strptime(b["end"], "%Y-%m-%d").date()
    today = date.today()
    nights = (end - start).days
    days_until = (end - today).days

    if days_until < 0:
        days_label = f"{abs(days_until)}d ago"
    elif days_until == 0:
        days_label = "TODAY"
    elif days_until == 1:
        days_label = "TOMORROW"
    else:
        days_label = f"in {days_until}d"

    if b["status"] == "cancelled":
        card_class = "cancelled"
    elif b["status"] == "complete":
        card_class = "complete"
    elif b.get("confirmed"):
        card_class = "confirmed"
    elif b.get("cleaner"):
        card_class = "assigned"
    else:
        card_class = "needs-cleaner"
        if 0 <= days_until <= 3:
            card_class += " urgent"

    conflict = b.get("conflict")
    conflict_fmt = None
    if conflict:
        old_end = conflict.get("old_end", "")
        if conflict["type"] == "dates_changed":
            try:
                old_end_dt = datetime.strptime(old_end, "%Y-%m-%d").date()
                conflict_fmt = f"Checkout moved from {old_end_dt.strftime('%b %d')} to {end.strftime('%b %d')}"
            except ValueError:
                conflict_fmt = "Dates changed"
        else:
            conflict_fmt = "Booking cancelled"

    return {
        "uid": uid,
        "start_fmt": start.strftime("%b %d"),
        "end_fmt": end.strftime("%b %d"),
        "start": b["start"],
        "end": b["end"],
        "nights": nights,
        "days_until": days_label,
        "cleaner": b.get("cleaner"),
        "paid": b.get("paid", False),
        "confirmed": b.get("confirmed", False),
        "status": b["status"],
        "card_class": card_class,
        "notes": b.get("notes", ""),
        "cleaner_since": b.get("cleaner_since"),
        "cleaner_since_fmt": _fmt_timestamp(b.get("cleaner_since")),
        "conflict": conflict,
        "conflict_fmt": conflict_fmt,
    }


def build_view_data(data):
    """Build the template context from booking data."""
    bookings = data.get("bookings", {})
    today = date.today()

    upcoming = []
    past = []
    conflicts = []
    needs_cleaner = 0
    assigned_count = 0
    confirmed_count = 0

    for uid, b in bookings.items():
        fb = format_booking(uid, b)
        end = datetime.strptime(b["end"], "%Y-%m-%d").date()

        if fb["conflict"]:
            conflicts.append(fb)

        if b["status"] == "cancelled":
            if not fb["conflict"]:
                past.append(fb)
            continue

        if end >= today and b["status"] == "active":
            upcoming.append(fb)
            if not b.get("cleaner"):
                needs_cleaner += 1
            elif b.get("confirmed"):
                confirmed_count += 1
            else:
                assigned_count += 1
        else:
            past.append(fb)

    upcoming.sort(key=lambda x: x["end"])
    past.sort(key=lambda x: x["end"], reverse=True)
    conflicts.sort(key=lambda x: x["end"])

    last_sync = data.get("last_sync")
    if last_sync:
        try:
            ls = datetime.fromisoformat(last_sync)
            last_sync = ls.strftime("%b %d, %I:%M %p")
        except (ValueError, TypeError):
            pass

    return {
        "upcoming": upcoming,
        "past": past[:30],
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "needs_cleaner": needs_cleaner,
        "assigned_count": assigned_count,
        "confirmed_count": confirmed_count,
        "upcoming_count": len(upcoming),
        "last_sync": last_sync,
        "cleaners": CLEANERS,
        "prefix": ingress_prefix(),
        "no_ical": not ICAL_URL,
    }


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

@app.route("/")
def index():
    data = load_data()
    ctx = build_view_data(data)
    return render_template_string(
        CALENDAR_TEMPLATE, error=request.args.get("error"),
        wa_text=None, wa_parsed=None, wa_error=None, wa_applied=0,
        wa_booking_options=[],
        **ctx,
    )


@app.route("/sync", methods=["POST"])
def sync():
    _, error = sync_ical()
    prefix = ingress_prefix()
    if error:
        return redirect(prefix + "/?error=" + error)
    return redirect(prefix + "/")


@app.route("/assign/<path:uid>", methods=["POST"])
def assign(uid):
    data = load_data()
    cleaner = request.form.get("cleaner", "").strip()
    if uid in data["bookings"]:
        data["bookings"][uid]["cleaner"] = cleaner or None
        data["bookings"][uid]["cleaner_since"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") if cleaner else None
        if not cleaner:
            data["bookings"][uid]["confirmed"] = False
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
        cleaners=CLEANERS,
        prefix=ingress_prefix(),
        deletable=booking.get("type") in ("custom_stay", "manual_cleaning"),
    )


@app.route("/delete/<path:uid>", methods=["POST"])
def delete_booking(uid):
    data = load_data()
    booking = data["bookings"].get(uid)
    if booking and booking.get("type") in ("custom_stay", "manual_cleaning"):
        del data["bookings"][uid]
        save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "GET":
        prefill_date = request.args.get("date", "")
        return render_template_string(
            ADD_TEMPLATE, cleaners=CLEANERS, prefix=ingress_prefix(),
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


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    data = load_data()
    bookings = data.get("bookings", {})
    chat_text = request.form.get("chat", "")
    auto_apply = request.form.get("auto_apply") == "1"

    parsed, error = parse_whatsapp_with_llm(chat_text, bookings)

    # Auto-apply confirmed matches to bookings
    applied_count = 0
    if parsed and auto_apply:
        for match in parsed.get("matches", []):
            uid = match.get("booking_uid")
            if uid and uid in bookings and match.get("status") == "confirmed":
                cleaner_name = match.get("cleaner_name", "")
                if cleaner_name:
                    if not bookings[uid].get("cleaner"):
                        bookings[uid]["cleaner"] = cleaner_name
                        bookings[uid]["cleaner_since"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                    bookings[uid]["confirmed"] = True
                    note_parts = []
                    if match.get("time"):
                        note_parts.append(f"Time: {match['time']}")
                    if match.get("note"):
                        note_parts.append(match["note"])
                    if note_parts and not bookings[uid].get("notes"):
                        bookings[uid]["notes"] = " | ".join(note_parts)
                    applied_count += 1
        save_data(data)

    ctx = build_view_data(data)

    today = date.today()
    wa_booking_options = []
    for uid, b in bookings.items():
        if b["status"] != "active":
            continue
        end = datetime.strptime(b["end"], "%Y-%m-%d").date()
        if end < today:
            continue
        start = datetime.strptime(b["start"], "%Y-%m-%d").date()
        wa_booking_options.append({
            "uid": uid,
            "label": f"{start.strftime('%b %d')} \u2192 {end.strftime('%b %d')}",
        })
    wa_booking_options.sort(key=lambda x: x["label"])

    return render_template_string(
        TEMPLATE, error=None,
        wa_text=chat_text,
        wa_parsed=parsed,
        wa_error=error,
        wa_applied=applied_count,
        wa_booking_options=wa_booking_options,
        **ctx,
    )


@app.route("/apply-match", methods=["POST"])
def apply_match():
    data = load_data()
    bookings = data.get("bookings", {})
    uid = request.form.get("booking_uid", "").strip()
    cleaner_name = request.form.get("cleaner_name", "").strip()
    status = request.form.get("status", "").strip()
    time_val = request.form.get("time", "").strip()
    note = request.form.get("note", "").strip()

    if uid and uid in bookings:
        if cleaner_name and not bookings[uid].get("cleaner"):
            bookings[uid]["cleaner"] = cleaner_name
            bookings[uid]["cleaner_since"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if status == "confirmed":
            bookings[uid]["confirmed"] = True
        elif status == "declined":
            bookings[uid]["confirmed"] = False
        note_parts = []
        if time_val:
            note_parts.append(f"Time: {time_val}")
        if note:
            note_parts.append(note)
        if note_parts and not bookings[uid].get("notes"):
            bookings[uid]["notes"] = " | ".join(note_parts)
        save_data(data)

    return redirect(ingress_prefix() + "/")


@app.route("/resolve/<path:uid>", methods=["POST"])
def resolve(uid):
    data = load_data()
    if uid in data["bookings"]:
        b = data["bookings"][uid]
        action = request.form.get("action", "keep")
        if action == "keep":
            b.pop("conflict", None)
        elif action == "cancel":
            b.pop("conflict", None)
            b["cleaner"] = None
            b["cleaner_since"] = None
            b["confirmed"] = False
        elif action == "move":
            b.pop("conflict", None)
            b["confirmed"] = False
        save_data(data)
    return redirect(ingress_prefix() + "/")


@app.route("/events.json")
def events_json():
    data = load_data()
    bookings = data.get("bookings", {})

    raw_start = request.args.get("start")
    raw_end = request.args.get("end")
    win_start = date.fromisoformat(raw_start[:10]) if raw_start else None
    win_end = date.fromisoformat(raw_end[:10]) if raw_end else None

    events = []

    for uid, b in bookings.items():
        btype = b.get("type", "airbnb")
        status = b.get("status", "active")
        b_start = date.fromisoformat(b["start"])
        b_end = date.fromisoformat(b["end"])

        # ── Stay events ───────────────────────────────────────────────────────
        if btype in ("airbnb", "custom_stay"):
            fc_end = b_end + timedelta(days=1)
            in_window = (win_start is None) or (b_start < win_end and fc_end > win_start)
            if in_window:
                if status == "cancelled":
                    bg = "#e9ecef"
                elif btype == "custom_stay":
                    bg = "#d1e7dd"
                else:
                    bg = "#cfe2ff"

                border = "#fd7e14" if b.get("conflict") else bg

                event = {
                    "id": f"stay-{uid}",
                    "title": (b.get("notes") or "Airbnb") if btype == "airbnb" else (b.get("notes") or "Custom stay"),
                    "start": b["start"],
                    "end": fc_end.isoformat(),
                    "allDay": True,
                    "display": "block",
                    "backgroundColor": bg,
                    "borderColor": border,
                    "extendedProps": {
                        "type": btype,
                        "uid": uid,
                        "status": status,
                        "cancelled": status == "cancelled",
                    },
                }
                if status == "cancelled":
                    event["classNames"] = ["cancelled-stay"]
                events.append(event)

        # ── Cleaning events ───────────────────────────────────────────────────
        if status == "cancelled":
            continue
        if btype == "custom_stay":
            continue

        clean_date = b_end  # checkout day for airbnb; same as start for manual_cleaning
        in_window = (win_start is None) or (win_start <= clean_date < win_end)
        if not in_window:
            continue

        cleaner = b.get("cleaner")
        confirmed = b.get("confirmed", False)
        bg_clean = cleaner_color(cleaner) if cleaner else "#dc3545"
        border_clean = "#198754" if confirmed else bg_clean

        events.append({
            "id": f"clean-{uid}",
            "title": cleaner if cleaner else "Needs cleaner",
            "start": clean_date.isoformat(),
            "allDay": True,
            "display": "block",
            "backgroundColor": bg_clean,
            "borderColor": border_clean,
            "textColor": "#fff",
            "extendedProps": {
                "type": "cleaning",
                "uid": uid,
                "cleaner": cleaner,
                "confirmed": confirmed,
                "paid": b.get("paid", False),
                "status": status,
            },
        })

    return jsonify(events)


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

    print("\nStarting server at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000)
