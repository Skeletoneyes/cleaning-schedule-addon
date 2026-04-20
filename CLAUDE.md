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

**Current direction (1.7.x):** the add-on is the **brain**, Google Calendar
is the **shared view**. `data.json` is the source of truth; `gcal.py` pushes
a one-way projection to a GCal calendar shared with Michelle and the
cleaners. The add-on UI's job is to handle everything GCal can't — the
per-cleaner notify queue (see below) and WhatsApp review.

The FullCalendar view is gone. `/` now renders a per-cleaner **notify
queue** driven by `cleaner_commitment` drift — see
`GCAL_VIEW_SKETCH.md` for rationale and shipped state.

### Key Files

```
repository.yaml              # HA custom repo metadata
cleaning-tracker/
├── config.yaml              # Add-on config: name, version, options schema
├── Dockerfile               # python:3.12-slim, pip install, runs app.py
├── requirements.txt         # flask, requests, icalendar, anthropic, google-api-*
├── app.py                   # Entire application (Flask routes, templates, logic) — ~1900 lines
└── gcal.py                  # Google Calendar projection (one-way: data.json → GCal)
scripts/
├── whatsapp_fixture.py      # Synthetic inbound-message harness for Phase 3
└── gcal_auth.py             # Validates a GCal service-account key + prints setup steps
GCAL_VIEW_SKETCH.md          # Current direction: GCal-as-view. Shipped state + TODOs
PHASE_1_PLAN.md              # Historical: FullCalendar-first UI. Superseded; kept only as archaeology
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
  - `cleaner_commitment` — snapshot of the last state the cleaner was told:
    `{cleaner, date, clean_time, communicated_at, communicated_via}`. Written
    by `ack_notified()` on manual "Mark notified" and by the WhatsApp auto-
    apply path (`communicated_via="whatsapp"`). Absent on legacy bookings
    and on freshly-assigned ones — in both cases they show up in the notify
    queue as "new" until cleared. Drift between this snapshot and current
    truth is what drives the `/` view and the GCal `⚠️` signal.
  - `notes` — free-text.
  - **Deprecated:** `conflict` — old two-stays-overlap flag. No longer
    written; `needs_notify()` / `review_item()` supersede it. Safe to
    ignore on read.
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

### Notify queue (`/`)
The home page renders a focused, one-cleaner-at-a-time card listing every
booking whose `cleaner_commitment` diverges from current truth. A booking
enters the queue when:
- `cleaner` is assigned but no `cleaner_commitment` exists → kind `new`
- the commitment exists but `(cleaner, date, clean_time)` has drifted →
  kind `changed`
- the booking was cancelled after a commitment was written → kind
  `cancelled`
- an active Airbnb booking has no `cleaner` at all → goes to the separate
  **Unassigned** bucket at the top of the page

Buckets are grouped by cleaner and sorted by name. `?i=<n>` paginates
through buckets. "Mark notified" (`POST /review/notify/<slug>`) rewrites
the commitment on every listed booking for that cleaner to match current
truth and advances to the next bucket. There's no per-line ticking —
unit of work is one cleaner = one WhatsApp message.

Empty state: "All cleaners up to date ✓". The WhatsApp Review tab lives
as a second panel on the same page (tab-switched, not a separate route).

Helpers: `review_item(uid, b)`, `review_queue(data)`, `needs_notify(b)`,
`ack_notified(booking, via)` in `app.py` around line 500.

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
- **Drift signal (cleanings only):** cleaning events whose booking has
  unresolved drift (`needs_notify(b)` → `_needs_notify` annotation on the
  snapshot passed to `sync_to_gcal`) get `colorId=11` (red) and a `⚠️ `
  title prefix. Stay events never get this treatment — red means "a
  cleaner needs to be told", which is always a property of the cleaning,
  not the stay. Resolves on the next sync after "Mark notified".
- Cleaner colour: md5-hashed onto 9 GCal palette slots (slot 8 reserved for
  cancelled if ever shown, 11 reserved for drift/unassigned).
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
