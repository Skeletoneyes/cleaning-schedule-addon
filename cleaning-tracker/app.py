"""
Airbnb Cleaning Schedule Tracker
A simple web app to manage cleaning schedules from Airbnb bookings.
Paste WhatsApp chat logs to verify cleaner confirmations.
"""

import json
import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, jsonify, redirect

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
CLEANERS = OPTIONS.get("cleaners", [])
DATA_FILE = DATA_DIR / "data.json"


def ingress_prefix():
    """Get the HA ingress path prefix from the request header."""
    return request.headers.get("X-Ingress-Path", "")


def url_for_ingress(path):
    """Build a URL that works behind HA ingress."""
    return ingress_prefix() + path


# ── Data persistence ─────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"bookings": {}, "whatsapp_logs": [], "last_sync": None}


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


# ── WhatsApp parsing ─────────────────────────────────────────────────────────

WHATSAPP_LINE_RE = re.compile(
    r"[\[>]?(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*[APMapm]{0,2})"
    r"[\]>]?\s*[-–]?\s*([^:]+):\s*(.*)"
)

DATE_IN_MSG_RE = re.compile(
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?",
    re.IGNORECASE,
)

CONFIRM_WORDS = re.compile(
    r"\b(yes|ok|okay|sure|confirmed?|i can|sí|si|claro|listo|está bien|dale|perfecto)\b",
    re.IGNORECASE,
)

MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_whatsapp(text):
    messages = []
    for match in WHATSAPP_LINE_RE.finditer(text):
        date_str, time_str, sender, body = match.groups()
        sender = sender.strip()
        body = body.strip()

        is_cleaner = any(c.lower() in sender.lower() for c in CLEANERS)
        has_confirm = bool(CONFIRM_WORDS.search(body))

        mentioned_dates = []
        for dm in DATE_IN_MSG_RE.finditer(body):
            month_name, day = dm.groups()
            month_num = MONTH_MAP.get(month_name.lower())
            if month_num:
                now = date.today()
                guess = date(now.year, month_num, int(day))
                if guess < now - timedelta(days=180):
                    guess = date(now.year + 1, month_num, int(day))
                mentioned_dates.append(guess.isoformat())

        messages.append({
            "date": date_str,
            "time": time_str.strip(),
            "sender": sender,
            "body": body,
            "is_cleaner": is_cleaner,
            "has_confirm": has_confirm,
            "mentioned_dates": mentioned_dates,
        })

    return messages


def match_messages_to_bookings(messages, bookings):
    matches = {}

    for uid, b in bookings.items():
        b_start = datetime.strptime(b["start"], "%Y-%m-%d").date()
        b_end = datetime.strptime(b["end"], "%Y-%m-%d").date()
        cleaning_date = b_end

        relevant = []
        for msg in messages:
            for md in msg["mentioned_dates"]:
                md_date = datetime.strptime(md, "%Y-%m-%d").date()
                if abs((md_date - cleaning_date).days) <= 1:
                    relevant.append(msg)
                    break
            else:
                try:
                    parts = msg["date"].replace("/", "-").split("-")
                    if len(parts[2]) == 2:
                        parts[2] = "20" + parts[2]
                    msg_date = date(int(parts[2]), int(parts[0]), int(parts[1]))
                    if timedelta(0) <= (cleaning_date - msg_date) <= timedelta(days=7):
                        if msg["is_cleaner"] or any(c.lower() in msg["body"].lower() for c in CLEANERS):
                            relevant.append(msg)
                except (ValueError, IndexError):
                    pass

        if relevant:
            matches[uid] = relevant

    return matches


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
  .wa-msg {
    background: #fff; border-radius: 8px; padding: 10px; margin-bottom: 6px;
    border-left: 3px solid #25d366;
  }
  .wa-msg.confirm { background: #d4edda; }
  .wa-msg .sender { font-weight: 600; color: #25d366; }
  .wa-msg .meta { font-size: 0.75rem; color: #999; }

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

  .filter-bar { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }

  .empty { text-align: center; color: #999; padding: 40px; }

  .config-warning {
    background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px;
    padding: 12px; margin-bottom: 16px; font-size: 0.9rem;
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
      {% if b.wa_matches %}
        <span class="badge" style="background:#25d366;color:#fff">{{ b.wa_matches }} WhatsApp msg{{ 's' if b.wa_matches != 1 }}</span>
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
    Paste a WhatsApp chat export below. The app will match messages to bookings
    and highlight cleaner confirmations.
  </p>
  <form action="{{ prefix }}/whatsapp" method="POST">
    <textarea name="chat" class="whatsapp-box" placeholder="Paste WhatsApp chat here...&#10;&#10;Example:&#10;[3/20/26, 2:15:30 PM] Jane: Yes I can clean on March 25th">{{ wa_text or "" }}</textarea>
    <div style="margin-top:8px;">
      <button type="submit" class="btn btn-primary">Parse Chat</button>
    </div>
  </form>

  {% if wa_results %}
  <div class="wa-results">
    <h3 style="margin:12px 0 8px;">Matched Messages</h3>
    {% for uid, msgs in wa_results.items() %}
      <div style="font-weight:600;margin:8px 0 4px;">
        Booking: {{ wa_booking_labels[uid] }}
      </div>
      {% for m in msgs %}
      <div class="wa-msg {{ 'confirm' if m.has_confirm }}">
        <span class="sender">{{ m.sender }}</span>
        <span class="meta">{{ m.date }} {{ m.time }}</span>
        <div>{{ m.body }}</div>
        {% if m.has_confirm %}<span class="badge badge-complete">Confirmation detected</span>{% endif %}
      </div>
      {% endfor %}
    {% endfor %}

    {% if wa_unmatched %}
    <h3 style="margin:12px 0 8px;">Unmatched Cleaner Messages</h3>
    {% for m in wa_unmatched %}
      <div class="wa-msg">
        <span class="sender">{{ m.sender }}</span>
        <span class="meta">{{ m.date }} {{ m.time }}</span>
        <div>{{ m.body }}</div>
      </div>
    {% endfor %}
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

def format_booking(uid, b, wa_match_counts=None):
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
        "wa_matches": (wa_match_counts or {}).get(uid, 0),
    }


def build_view_data(data, wa_match_counts=None):
    """Build the template context from booking data."""
    bookings = data.get("bookings", {})
    today = date.today()

    upcoming = []
    past = []
    needs_cleaner = 0
    assigned_count = 0
    confirmed_count = 0

    for uid, b in bookings.items():
        fb = format_booking(uid, b, wa_match_counts)
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
        wa_text=None, wa_results=None, wa_booking_labels=None, wa_unmatched=None,
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

    messages = parse_whatsapp(chat_text)
    matches = match_messages_to_bookings(messages, bookings)

    labels = {}
    for uid in matches:
        b = bookings[uid]
        start = datetime.strptime(b["start"], "%Y-%m-%d").date()
        end = datetime.strptime(b["end"], "%Y-%m-%d").date()
        labels[uid] = f"{start.strftime('%b %d')} → {end.strftime('%b %d')}"

    matched_msgs = set()
    for msgs in matches.values():
        for m in msgs:
            matched_msgs.add(id(m))
    unmatched = [m for m in messages if m["is_cleaner"] and id(m) not in matched_msgs]

    for uid, msgs in matches.items():
        for m in msgs:
            if m["is_cleaner"] and m["has_confirm"]:
                if uid in bookings:
                    for c in CLEANERS:
                        if c.lower() in m["sender"].lower():
                            if not bookings[uid].get("cleaner"):
                                bookings[uid]["cleaner"] = c
                            bookings[uid]["confirmed"] = True
                            break

    save_data(data)

    wa_match_counts = {uid: len(msgs) for uid, msgs in matches.items()}
    ctx = build_view_data(data, wa_match_counts)

    return render_template_string(
        TEMPLATE, error=None,
        wa_text=chat_text,
        wa_results=matches if matches else None,
        wa_booking_labels=labels,
        wa_unmatched=unmatched if unmatched else None,
        **ctx,
    )


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
