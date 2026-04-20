"""Google Calendar projection of data.json.

One-way sync: data.json → GCal. Events are tagged with
extendedProperties.private.source="cleaning-tracker" + .uid=<booking_uid>
so we can round-trip by uid instead of guessing by title.

Auth: service account. The user creates a service account in Google Cloud,
downloads its JSON key, shares the target calendar with the service account's
email (Make changes to events), and pastes the whole JSON blob into the add-on
options (gcal_service_account_json). No OAuth consent flow, no refresh tokens,
no expiry.
"""

import hashlib
import json
import threading
from datetime import datetime, date, timedelta

_SYNC_LOCK = threading.Lock()

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    _GCAL_AVAILABLE = True
except ImportError:
    _GCAL_AVAILABLE = False


SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SOURCE_TAG = "cleaning-tracker"
LOCAL_TZ = "America/Vancouver"

# GCal provides 11 event colours (ids "1"–"11"). We map a cleaner name onto
# one of them deterministically — lossy compared to the HSL hash in app.py
# but fine for a phone view. "11" (red) is reserved for conflicts / unassigned.
_PALETTE = ["1", "2", "3", "4", "5", "6", "7", "9", "10"]  # skip 8 (graphite) and 11 (red)
_COLOR_UNASSIGNED = "11"  # red/tomato
_COLOR_CONFLICT = "11"
_COLOR_CANCELLED = "8"   # graphite


def gcal_available():
    return _GCAL_AVAILABLE


def _cleaner_color_id(name: str) -> str:
    digest = hashlib.md5(name.encode()).hexdigest()
    idx = int(digest[:4], 16) % len(_PALETTE)
    return _PALETTE[idx]


def _build_service(service_account_json):
    if isinstance(service_account_json, str):
        info = json.loads(service_account_json)
    else:
        info = service_account_json
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _fmt_time_12h(clean_time: str) -> str:
    try:
        t = datetime.strptime(clean_time, "%H:%M:%S")
        return t.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return ""


def _desired_events(data, window_days_back=30, window_days_fwd=365):
    """Build {uid: event_body} for all bookings we want reflected in GCal.

    Cancelled bookings are omitted (we delete them from GCal on diff).
    """
    today = date.today()
    win_start = today - timedelta(days=window_days_back)
    win_end = today + timedelta(days=window_days_fwd)

    desired = {}
    bookings = data.get("bookings", {})

    for uid, b in bookings.items():
        status = b.get("status", "active")
        btype = b.get("type", "airbnb")
        if status == "cancelled":
            continue  # delete from GCal

        try:
            b_start = date.fromisoformat(b["start"])
            b_end = date.fromisoformat(b["end"])
        except (ValueError, TypeError, KeyError):
            continue

        if b_end < win_start or b_start > win_end:
            continue

        conflict = bool(b.get("_needs_notify"))

        # ── Stay event ───────────────────────────────────────────────────
        if btype in ("airbnb", "custom_stay"):
            stay_uid = f"stay:{uid}"
            if btype == "airbnb":
                ev_start = {"dateTime": f"{b['start']}T15:00:00", "timeZone": LOCAL_TZ}
                ev_end = {"dateTime": f"{b['end']}T11:00:00", "timeZone": LOCAL_TZ}
            else:
                # All-day; end is exclusive in GCal so add one day.
                ev_start = {"date": b["start"]}
                ev_end = {"date": (b_end + timedelta(days=1)).isoformat()}
            title = "Airbnb" if btype == "airbnb" else (b.get("notes") or "Custom stay")
            body = {
                "summary": title,
                "start": ev_start,
                "end": ev_end,
                "colorId": "7",  # peacock for stays
                "extendedProperties": {
                    "private": {
                        "source": SOURCE_TAG,
                        "uid": stay_uid,
                        "booking_uid": uid,
                        "kind": "stay",
                        "type": btype,
                        "status": status,
                    }
                },
            }
            desired[stay_uid] = body

        # ── Cleaning event ──────────────────────────────────────────────
        if btype == "custom_stay":
            continue
        clean_date = b_end  # checkout for airbnb; same as start for manual
        cleaner = b.get("cleaner")
        clean_time = b.get("clean_time")

        clean_uid = f"clean:{uid}"
        label = cleaner if cleaner else "Needs cleaner"
        time_suffix = _fmt_time_12h(clean_time) if clean_time else ""
        emoji = "🧹 "
        title = f"{emoji}{label}"
        if time_suffix:
            title += f" · {time_suffix}"
        if conflict:
            title = "⚠️ " + title

        if clean_time:
            ev_start = {"dateTime": f"{clean_date.isoformat()}T{clean_time}", "timeZone": LOCAL_TZ}
            # Default cleaning duration: 3 hours
            end_dt = datetime.strptime(f"{clean_date.isoformat()} {clean_time}", "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
            ev_end = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": LOCAL_TZ}
        else:
            ev_start = {"date": clean_date.isoformat()}
            ev_end = {"date": (clean_date + timedelta(days=1)).isoformat()}

        if conflict or not cleaner:
            color_id = _COLOR_CONFLICT if conflict else _COLOR_UNASSIGNED
        else:
            color_id = _cleaner_color_id(cleaner)

        body = {
            "summary": title,
            "start": ev_start,
            "end": ev_end,
            "colorId": color_id,
            "extendedProperties": {
                "private": {
                    "source": SOURCE_TAG,
                    "uid": clean_uid,
                    "booking_uid": uid,
                    "kind": "cleaning",
                    "cleaner": cleaner or "",
                    "confirmed": "1" if b.get("confirmed") else "0",
                }
            },
        }
        if conflict:
            body["description"] = "Cleaner not yet notified of current state — open the add-on to confirm."
        desired[clean_uid] = body

    return desired


def _list_existing(service, calendar_id):
    """Return ({uid: event}, [duplicate_event_ids]).

    If two events share a uid (leftover from a race), keep one and mark the
    rest for deletion.
    """
    out = {}
    dupes = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=f"source={SOURCE_TAG}",
            showDeleted=False,
            singleEvents=True,
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        for ev in resp.get("items", []):
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            uid = priv.get("uid")
            if not uid:
                continue
            if uid in out:
                dupes.append(ev["id"])
            else:
                out[uid] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out, dupes


def _events_equal(existing, desired):
    """Cheap comparison: if any meaningful field differs, patch."""
    if existing.get("summary") != desired.get("summary"):
        return False
    if existing.get("colorId") != desired.get("colorId"):
        return False
    if existing.get("description", "") != desired.get("description", ""):
        return False
    # Start / end
    for key in ("start", "end"):
        e = existing.get(key, {})
        d = desired.get(key, {})
        if e.get("date") != d.get("date") or e.get("dateTime") != d.get("dateTime"):
            return False
        if e.get("timeZone") != d.get("timeZone"):
            return False
    # ExtendedProperties.private — check subset match on desired keys
    ep = (existing.get("extendedProperties") or {}).get("private") or {}
    dp = (desired.get("extendedProperties") or {}).get("private") or {}
    for k, v in dp.items():
        if ep.get(k) != v:
            return False
    return True


def sync_to_gcal(data, service_account_json, calendar_id):
    """Run a full diff-and-patch sync. Returns (stats, error).

    stats = {"inserted": N, "patched": N, "deleted": N}
    """
    if not _GCAL_AVAILABLE:
        return None, "google-api-python-client not installed"
    if not (service_account_json and calendar_id):
        return None, "Google Calendar credentials not configured"

    if not _SYNC_LOCK.acquire(blocking=False):
        return {"skipped": 1}, None  # another sync is already running

    try:
        service = _build_service(service_account_json)
        existing, dupes = _list_existing(service, calendar_id)
        desired = _desired_events(data)

        stats = {"inserted": 0, "patched": 0, "deleted": 0, "dupes_deleted": 0}

        for dupe_id in dupes:
            try:
                service.events().delete(calendarId=calendar_id, eventId=dupe_id).execute()
                stats["dupes_deleted"] += 1
            except HttpError:
                pass

        for uid, body in desired.items():
            if uid in existing:
                if not _events_equal(existing[uid], body):
                    service.events().patch(
                        calendarId=calendar_id,
                        eventId=existing[uid]["id"],
                        body=body,
                    ).execute()
                    stats["patched"] += 1
            else:
                service.events().insert(calendarId=calendar_id, body=body).execute()
                stats["inserted"] += 1

        for uid, ev in existing.items():
            if uid not in desired:
                service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
                stats["deleted"] += 1

        return stats, None
    except HttpError as e:
        return None, f"Google API error: {e}"
    except Exception as e:
        return None, f"GCal sync failed: {e}"
    finally:
        _SYNC_LOCK.release()
