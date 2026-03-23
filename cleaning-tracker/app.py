"""
Airbnb Cleaning Schedule Tracker
A simple web app to manage cleaning schedules from Airbnb bookings.
Paste WhatsApp chat logs to verify cleaner confirmations.
"""

import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, redirect

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
            return json.load(f)
    return {"bookings": {}, "last_sync": None}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


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
            data["bookings"][uid]["start"] = start_str
            data["bookings"][uid]["end"] = end_str
            data["bookings"][uid]["status"] = "active"
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
        if uid.startswith("manual-"):
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


# ── HTML Template ────────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cleaning Schedule</title>
<style>
  :root {
    --green: #d4edda; --red: #ffcccb; --yellow: #fff3cd;
    --blue: #cce5ff; --gray: #f8f9fa; --dark: #212529;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--gray); color: var(--dark); padding: 12px; max-width: 800px; margin: 0 auto;
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
</div>

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
</body>
</html>
"""

ADD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add Manual Cleaning</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; padding: 20px; max-width: 500px; margin: 0 auto; }
  label { display: block; margin: 12px 0 4px; font-weight: 600; }
  input, select, textarea { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 1rem; }
  button { margin-top: 16px; padding: 10px 24px; background: #0d6efd; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 1rem; }
  a { color: #0d6efd; }
</style>
</head>
<body>
<h2>Add Manual Cleaning</h2>
<form action="{{ prefix }}/add" method="POST">
  <label>Cleaning Date</label>
  <input type="date" name="date" required>
  <label>Cleaner</label>
  <select name="cleaner">
    <option value="">-- Select --</option>
    {% for c in cleaners %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
  </select>
  <label>Notes</label>
  <textarea name="notes" rows="2" placeholder="e.g., Friend visit, deep clean"></textarea>
  <br>
  <button type="submit">Add</button>
  <a href="{{ prefix }}/" style="margin-left:12px;">Cancel</a>
</form>
</body>
</html>
"""


# ── View helpers ─────────────────────────────────────────────────────────────

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
    }


def build_view_data(data):
    """Build the template context from booking data."""
    bookings = data.get("bookings", {})
    today = date.today()

    upcoming = []
    past = []
    needs_cleaner = 0
    assigned_count = 0
    confirmed_count = 0

    for uid, b in bookings.items():
        fb = format_booking(uid, b)
        end = datetime.strptime(b["end"], "%Y-%m-%d").date()

        if b["status"] == "cancelled":
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
        "needs_cleaner": needs_cleaner,
        "assigned_count": assigned_count,
        "confirmed_count": confirmed_count,
        "upcoming_count": len(upcoming),
        "last_sync": last_sync,
        "cleaners": CLEANERS,
        "prefix": ingress_prefix(),
        "no_ical": not ICAL_URL,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    data = load_data()
    ctx = build_view_data(data)
    return render_template_string(
        TEMPLATE, error=request.args.get("error"),
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


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "GET":
        return render_template_string(ADD_TEMPLATE, cleaners=CLEANERS, prefix=ingress_prefix())

    cleaning_date = request.form.get("date")
    cleaner = request.form.get("cleaner", "").strip()
    notes = request.form.get("notes", "").strip()

    if not cleaning_date:
        return redirect(ingress_prefix() + "/add")

    data = load_data()
    uid = f"manual-{cleaning_date}-{len(data['bookings'])}"
    data["bookings"][uid] = {
        "start": cleaning_date,
        "end": cleaning_date,
        "cleaner": cleaner or None,
        "paid": False,
        "status": "active",
        "confirmed": bool(cleaner),
        "notes": notes or "Manual cleaning",
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
