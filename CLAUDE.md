# Cleaning Schedule Tracker — HA Add-on

## Purpose

Home Assistant add-on that tracks Airbnb cleaning schedules. It syncs bookings
from an Airbnb iCal feed, lets you assign cleaners to checkout dates, and uses
Claude Haiku to interpret WhatsApp conversations with cleaners (both pasted
text and, via Phase 3, a read-only bot account) to detect confirmations and
declines.

## Architecture

Single-file Flask app (`cleaning-tracker/app.py`) running as an HA add-on with
ingress. No database — data lives in `/data/data.json` (persists across
rebuilds). Configuration is read from `/data/options.json` (populated by HA
from `config.yaml` options).

**Current direction (1.6.x):** the add-on is the **brain**, Google Calendar
is the **shared view**. `data.json` is the source of truth; `gcal.py` pushes
a one-way projection to a GCal calendar shared with Michelle and the
cleaners. The add-on UI exists to do the things GCal can't — WhatsApp review
and (soon) conflict resolution. See `GCAL_VIEW_SKETCH.md` for the full
picture and outstanding work.

The legacy FullCalendar view at `/` still works but is no longer the primary
viewing surface. A dedicated conflict-manager page will replace it in a
future session (tracked in `GCAL_VIEW_SKETCH.md`). The Review tab already
handles WhatsApp triage, unmapped-sender mapping, and group labelling.

### Key Files

```
repository.yaml              # HA custom repo metadata
cleaning-tracker/
├── config.yaml              # Add-on config: name, version, options schema
├── Dockerfile               # python:3.12-slim, pip install, runs app.py
├── requirements.txt         # flask, requests, icalendar, anthropic, google-api-*
├── app.py                   # Entire application (Flask routes, templates, logic) — ~1800 lines
└── gcal.py                  # Google Calendar projection (one-way: data.json → GCal)
scripts/
├── whatsapp_fixture.py      # Synthetic inbound-message harness for Phase 3
└── gcal_auth.py             # Validates a GCal service-account key + prints setup steps
GCAL_VIEW_SKETCH.md          # Current direction: GCal-as-view. Shipped state + TODOs
PHASE_1_PLAN.md              # Historical: FullCalendar-first UI. Superseded by GCAL_VIEW_SKETCH.md
PHASE_3_SKETCH.md            # WhatsApp automation — Step 1 shipped, Steps 2–3 pending
```

Tests / doc-builder helpers live under `scripts/` and are not shipped in the
add-on image.

### No build.yaml

The Dockerfile hardcodes `python:3.12-slim` directly. The `BUILD_FROM` arg
pattern from HA docs did not work — the Supervisor wasn't passing it through,
resulting in an empty base image.

## Add-on Options (set in HA UI)

- `ical_url` — Airbnb iCal calendar URL (contains private token, never commit
  it)
- `anthropic_api_key` — API key for Claude Haiku (WhatsApp parsing, both
  paste-flow and Phase 3 inbound). Stored as password type.
- `cleaners` — List of cleaner names (used for assignment dropdowns and as
  the canonical name set for JID mapping)
- `gcal_enabled` — toggle Google Calendar projection (default: off)
- `gcal_calendar_id` — target GCal calendar id (e.g.
  `abc@group.calendar.google.com`)
- `gcal_service_account_json` — full JSON blob for a Google Cloud service
  account key. The service account's email must be added to the target
  calendar's "Share with specific people" list with "Make changes to events"
  permission. Use `scripts/gcal_auth.py` to validate a downloaded key and
  print the exact sharing email.

## Data model (`/data/data.json`)

Lazily backfilled on read; all fields are additive and backwards-compatible.

- `bookings` — keyed by UID.
  - `type`: `"airbnb"` | `"custom_stay"` | `"manual_cleaning"`. iCal UIDs
    default to `airbnb`, `manual-*` UIDs to `manual_cleaning`, `custom-*` to
    `custom_stay`.
  - `start`, `end` — ISO dates (end is exclusive, Airbnb-style).
  - `status` — `"active"` | `"cancelled"` | `"complete"`.
  - `cleaner` — assigned cleaner name or null.
  - `clean_time` — `"HH:MM:SS"` or null. Shown in the calendar title
    (`"Itzel · 11:00 AM"`) and editable on the edit page. `_parse_clean_time()`
    backfills this from legacy `notes: "Time: 11:00 AM | ..."` strings.
  - `conflict` — set when two stays overlap; renders an orange border (unless
    cancelled, which suppresses the border).
  - `notes` — free-text.
- `messages` — Phase 3 inbound WhatsApp log. Entries:
  `{id, timestamp, sender_jid, group_jid, text, parsed, applied_uid,
    review_state: "auto"|"pending"|"ignored"}`.
- `cleaner_jids` — `{jid: cleaner_name}` map built from Review-tab mappings.
- `group_labels` — `{group_jid: label}` so the UI shows "Maria group" instead
  of a raw JID.

## How it works

### iCal sync
- Fetches Airbnb iCal, extracts `VEVENT` entries with `SUMMARY: Reserved`.
- Merges into `data.json`, preserving cleaner assignments and `clean_time`
  across syncs.
- A booking that disappears from the feed is marked `cancelled` only when
  `type == "airbnb"` — custom stays and manual cleanings are never
  auto-cancelled by the sync sweep.

### Calendar UI (`/`)
- FullCalendar views: `dayGridMonth` (default), `dayGridWeek`, `listWeek`
  (labelled "Agenda"). On mobile the view switcher moves to a footer toolbar.
- Event feed: `GET /events.json?start=&end=` returns two streams:
  - **Stays** — Airbnb stays render as timed events with 15:00 check-in and
    11:00 checkout; custom stays render as all-day bars. Cancelled stays are
    muted (`opacity: 0.7`) and drop the conflict border.
  - **Cleanings** — pill events on checkout day, coloured from the cleaner
    name via a deterministic HSL hash (`cleaner_color()`). Title includes
    the cleaning time when set. Unassigned events show "Needs cleaner".
- Past days and past events are greyscaled
  (`filter: grayscale(100%); opacity: 0.6`) with a lighter day-cell
  background, so the eye snaps to upcoming work.
- Clicking an event goes to `/edit/<uid>`. Clicking an empty day goes to
  `/add?date=YYYY-MM-DD`.

### Edit / add / delete
- `/add` — one form, radio for Cleaning vs Stay. Stays require start+end;
  cleanings take a single date + optional cleaner + optional `clean_time`.
- `/edit/<uid>` — edit cleaner, notes, and `clean_time`. Cancelled bookings
  get a **Dismiss** button (hits `/delete/<uid>`); custom stays and manual
  cleanings get a normal Delete. Airbnb stays can only be dismissed once
  cancelled.
- `/assign` — writes cleaner and clean_time; clears clean_time if blank.

### Print view
- `/print?month=YYYY-MM` — hand-rolled HTML table (not FullCalendar) with
  print-optimized CSS. Black borders, colour bars for stays, cleaner + time
  on checkout cells.

### WhatsApp — paste flow
- User pastes a chat transcript; Haiku is given the transcript plus the list
  of upcoming checkout dates and known cleaners.
- Output: structured JSON of confirm/decline per date with inferred cleaner
  and time. Optional auto-apply checkbox writes directly to bookings.

### WhatsApp — inbound pipeline (Phase 3 Step 1, shipped; Steps 2–3 pending a bot account)
- `POST /internal/whatsapp/inbound` (loopback-only): dedups on message id,
  enqueues to a 2-thread worker pool.
- Parse worker hands Haiku the full cross-group archive + booking list +
  known cleaners + sender hint. Returns
  `{action, booking_uid, cleaner, confidence, reason}`.
- Auto-apply gate: `confidence ≥ 0.85` AND known cleaner JID AND known
  booking → writes to booking. Everything else → Review tab.
- Review tab UI: pending-message queue with accept/override/ignore; group
  label editor; unmapped-sender flow (map to existing cleaner OR create new,
  then re-queue that sender's pending messages).
- Scripted harness: `scripts/whatsapp_fixture.py` POSTs synthetic messages
  (confirm / decline / ambiguous / unmapped / chitchat).
- The Baileys sidecar that would feed real WhatsApp traffic is not built yet
  (blocked on user procuring a bot account; see PHASE_3_SKETCH.md).

### Google Calendar projection (primary view, `gcal_enabled`)
- One-way sync: `data.json` → GCal. Cleaners don't edit the calendar; they
  confirm via WhatsApp.
- `gcal.py::sync_to_gcal()` diffs desired events (cancelled stays omitted,
  deleted from GCal) against existing ones tagged with
  `extendedProperties.private.source="cleaning-tracker"`, then inserts /
  patches / deletes to converge.
- Triggered via `save_data()` in a daemon thread (fire-and-forget, errors
  logged and swallowed so GCal outages don't block local writes).
- **Serialized** with a module-level lock — concurrent calls skip and return
  `{"skipped": 1}`. iCal sync hits `save_data()` many times in a row, and
  without the lock, racing threads inserted duplicate events (each thread
  listed GCal before the others' inserts landed + indexed).
- **Dedupes on the fly.** `_list_existing` returns any duplicate events
  (same `uid` tag, multiple events); they're deleted at the start of each
  sync.
- Manual trigger: `POST /gcal/sync` (button on the home page when enabled).
- Conflicts: events with `conflict` truthy get `colorId=11` (red) and a
  `⚠️ ` title prefix; resolves on the next sync.
- Cleaner colour: md5-hashed onto 9 GCal palette slots (slot 8 reserved for
  cancelled if ever shown, 11 reserved for conflict/unassigned).
- Timed events are tagged `America/Vancouver` (constant `LOCAL_TZ` in
  `gcal.py`), matching how `clean_time` and Airbnb check-in/out are stored
  in `data.json` as naive local clock times.
- **Auth: service account.** Create one in Google Cloud, download its JSON
  key, share the target calendar with the service account's email at
  "Make changes to events", and paste the JSON into the
  `gcal_service_account_json` option. No OAuth flow, no consent screen,
  no refresh-token expiry. `scripts/gcal_auth.py` validates a downloaded
  key and prints the email to share with.

### Ingress
All URLs are prefixed with the `X-Ingress-Path` request header so forms and
redirects work behind HA's ingress proxy. The `ingress_prefix()` helper is
passed to every template as `{{ prefix }}`.

## Deployment

Installed via HA custom repository:
1. Add-ons > Add-on Store > Repositories > paste GitHub URL.
2. Install "Cleaning Schedule Tracker".
3. Configure options (iCal URL, API key, cleaners).
4. Start — appears in sidebar as "Cleaning Schedule".

Updates: bump `version` in `config.yaml`, push to GitHub, refresh in HA.

## Important notes

- **Always bump `config.yaml` version when pushing changes.** The Supervisor
  caches add-on configs, and updates for an existing slug only take effect
  on a version bump.
- `init: false` in `config.yaml` is required — without it, the HA base
  image's s6-overlay conflicts with a bare `CMD`. Phase 3 Step 3 may need
  to revisit this when adding the Node.js sidecar; document the flip there.
- Do NOT use Samba for iterative add-on development on HAOS from Windows —
  SMB write caching makes files stale.
- For local development, the app falls back to reading `options.json` from
  the current directory when `/data/options.json` doesn't exist.
- UI changes should be verified in a local Playwright (Chromium) run before
  being reported as done. Playwright is a dev-only dependency — do **not**
  add it to the add-on `requirements.txt`.
- FullCalendar loads from CDN; the add-on won't work air-gapped. Vendor
  later if that ever matters.
