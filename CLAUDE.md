# Cleaning Schedule Tracker — HA Add-on

## Purpose

Home Assistant add-on that tracks Airbnb cleaning schedules. It syncs bookings
from an Airbnb iCal feed, lets you assign cleaners to checkout dates, and uses
Claude Haiku to interpret WhatsApp conversations with cleaners (live traffic
via a Baileys linked-device sidecar on the user's PC, plus a `/backfill`
paste page for catching up on historical assignments) to detect confirmations
and declines.

## Architecture

Single-file Flask app (`cleaning-tracker/app.py`) running as an HA add-on with
ingress. No database — data lives in `/data/data.json` (persists across
rebuilds). Configuration is read from `/data/options.json` (populated by HA
from `config.yaml` options).

**Current direction (1.13.x):** the add-on is the **brain**, Google Calendar
is the **shared view**. `data.json` is the source of truth; `gcal.py` pushes
a one-way projection to a GCal calendar shared with Michelle and the
cleaners. The add-on UI's job is to handle everything GCal can't — the
per-cleaner notify queue, WhatsApp review, and (next) a Conflicts tab
backed by the versioned facts layer.

The FullCalendar view is gone. `/` now renders a per-cleaner **notify
queue** driven by `cleaner_commitment` drift.

Work in progress: an LLM-driven reconciler that cross-checks `data.json`,
Airbnb iCal, GCal, and the WhatsApp archive. Step 1 (versioned facts
extraction via `facts.py`) is shipped and validated. See
`RECONCILER_PLAN.md` for step 2 (structural detectors + Conflicts tab)
and the open questions.

### Key Files

```
repository.yaml              # HA custom repo metadata
cleaning-tracker/
├── config.yaml              # Add-on config: name, version, options schema
├── Dockerfile               # python:3.12-slim, COPY app.py gcal.py facts.py ./
├── requirements.txt         # flask, requests, icalendar, anthropic, google-api-*
├── app.py                   # Flask routes, templates, logic — ~2300 lines
├── gcal.py                  # Google Calendar projection (one-way: data.json → GCal)
└── facts.py                 # Versioned structured-fact extractor (Haiku, FACTS_PROMPT_VERSION)
sidecar/whatsapp-bridge/     # Baileys sidecar — EXTERNAL Node process, not in the add-on container
├── index.js                 # Pairs as a WhatsApp linked device; POSTs group messages to the add-on
├── package.json
├── .env.example             # Config template (HA_URL, SHARED_SECRET, GROUP_ALLOWLIST, BACKFILL_*)
└── README.md                # Setup, test→prod swap, operational notes
scripts/
├── reconcile_pull.py        # Off-host puller for the reconcile-cleaning-schedule skill
└── gcal_auth.py             # Validates a GCal service-account key + prints setup steps
RECONCILER_PLAN.md           # Forward-looking plan: facts layer (shipped) + Conflicts tab (next)
.claude/skills/reconcile-cleaning-schedule/SKILL.md  # How to pull + reconcile externally
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
- `whatsapp_shared_secret` — shared token authenticating the Baileys sidecar's
  `POST /internal/whatsapp/inbound` calls. Required for any non-loopback
  caller; loopback (same-host) calls bypass auth. Must match
  `SHARED_SECRET` in `sidecar/whatsapp-bridge/.env`.

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
- `messages` — Inbound WhatsApp log. Entries:
  `{id, timestamp, sender, group, text, parsed, applied_uid,
    review_state: "auto"|"pending"|"ignored", haiku_result?, source?,
    sender_name_raw?}`. `source: "backfill"` + `sender_name_raw` are set
  by the paste-ingest path; `haiku_result.backfill_ingest=true` marks a
  facts-only ingest that should never route to the Review tab.
- `message_facts` — Parallel to `messages`, keyed by message id. Stored
  shape: `{facts: [...], reported_by_jid, model_version, prompt_version,
  extracted_at}`. Each fact: `{kind, target_date, target_time, cleaner,
  confidence, tentative, evidence}` where `kind ∈ {confirm, decline,
  time_proposal, date_proposal, schedule_assertion, unclear}`. Versioned:
  reconciler reads only records matching the current
  `facts.FACTS_PROMPT_VERSION`; bump + reprocess to migrate. See
  `RECONCILER_PLAN.md`.
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
`ack_notified(booking, via)` in `app.py` — grep rather than relying on
line numbers; the file grows.

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

### WhatsApp — backfill page (`/backfill`, added 1.9.0)
- User pastes a WhatsApp chat export (phone: group → Export chat → Without
  media). Haiku receives the transcript + the **full list of active unassigned
  bookings** (no date window) + known cleaners + an optional group-hint
  string.
- Returns per-booking proposals: `{uid, cleaner, clean_time, confidence,
  evidence}`. The review page renders each with confidence-coloured border
  (green ≥ 0.85, yellow 0.6–0.85, red < 0.6), editable cleaner dropdown,
  time input, and a checkbox pre-ticked at ≥ 0.85.
- `POST /backfill/apply` writes approved assignments and calls
  `ack_notified(booking, via="backfill")` so they skip the notify queue.
- Purpose: one-shot catch-up after install, or for reviving historical
  assignments that predate the linked-device sync window. Live traffic
  goes through the inbound pipeline instead.
- Entry point: "Backfill from chat" link in the Unassigned bookings card
  on `/`. Supersedes the older paste flow described in earlier docs — that
  older flow's routes no longer exist in the code.

### WhatsApp — inbound pipeline (live traffic)
- `POST /internal/whatsapp/inbound`: dedups on message id, enqueues to a
  2-thread worker pool. Loopback calls bypass auth; non-loopback callers
  must present `X-Shared-Secret` matching `whatsapp_shared_secret`.
- `process_message` runs TWO independent Haiku calls per message: the
  classic `parse_whatsapp_message` (routing decision for one booking) AND
  `facts_mod.extract_facts` (every scheduling assertion in the message,
  for the reconciler). Facts are stored regardless of parse outcome.
- Auto-apply gate (parse path): `confidence ≥ 0.85` AND known cleaner
  JID AND known booking → writes to booking. Everything else → Review tab.
- Review tab UI: pending-message queue with accept/override/ignore; group
  label editor; unmapped-sender flow (map to existing cleaner OR create new,
  then re-queue that sender's pending messages).

### Facts layer (`facts.py`, `data.message_facts`)
- Separate from parse. Parse answers "route this message to one booking?";
  facts answers "list every date/cleaner/time assertion this message
  makes". A 30-row schedule dump → 30 `schedule_assertion` facts; a
  cleaner's re-posted list with per-row times → 30 `confirm` facts (plus
  `time_proposal` / `decline` per row as appropriate). Re-posted-list
  recognition is load-bearing — it's the dominant real-chat pattern.
- Prompt is **role-tagged** — each history line is `<host>` or
  `<cleaner:Name>`, and `schedule_assertion` is host-only while
  `confirm`/`decline`/`time_proposal`/`date_proposal` are cleaner-only.
- `FACTS_PROMPT_VERSION` (currently `facts-v2`) stamps every stored
  record. The reconciler reads only current-version facts, so
  half-reprocessed state is safe. Bump the version after any prompt
  edit, then `POST /admin/reprocess-facts`.
- **Rate-limit handling**: `extract_facts` retries 429 / 5xx / timeouts
  with exponential backoff honouring `retry-after`. Bulk ingest paces
  at 0.8s/call.

### Admin routes (loopback / ingress / shared-secret only)
- `GET /admin/facts` — dump `message_facts` for inspection.
- `POST /admin/reprocess-facts` — re-extract every message whose stored
  `prompt_version` is stale. Idempotent.
- `POST /admin/ingest-transcript` — paste a WhatsApp chat export, parse
  each line into the messages log, run facts extraction (or full
  `process_message` if `apply=true`) in a background thread. Body:
  `{transcript, group_jid, apply}`. Parser handles both
  `[YYYY-MM-DD, HH:MM:SS AM/PM]` and `[H:MM AM/PM, M/D/YYYY]` bracket
  formats. Stable ids (`backfill-<sha1(ts|sender|text)[:16]>`) make
  re-runs idempotent and dedup against live messages.
- `GET /admin/ingest-status` — progress + `last_error`.
- `GET /admin/ingest` — HTML paste form, linked from the home page's
  Unassigned card ("Ingest transcript").

Auth for all `/admin/*` and `/internal/snapshot` routes goes through
`_require_local_or_secret()`. Accepts: loopback, HA ingress (presence
of `X-Ingress-Path` header the Supervisor proxy stamps), or matching
`X-Shared-Secret`.

### WhatsApp — Baileys sidecar (Phase 3 Step 3, test mode shipped 1.8.x)
- **Lives outside the add-on container** at `sidecar/whatsapp-bridge/`,
  runs as a Node process on the user's Windows PC. This deliberately dodges
  the `init: false` → `init: true` flip that an in-container sidecar would
  have forced. Tradeoff: the PC must stay awake (Settings → Power → "Never
  sleep while plugged in"). Promoting the sidecar to run on the HA host
  itself is possible later — same code, loopback URL, no secret needed.
- Pairs as a WhatsApp **linked device** via QR scan. **Test mode** pairs
  against the user's personal WhatsApp (ban risk eats the personal account;
  tolerated for a test). **Production path**: delete
  `sidecar/whatsapp-bridge/auth/`, re-pair against a dedicated bot number
  (SpeakOut $125/yr plan is the planned number — Step 2 still pending).
- **Read-only.** `index.js` never calls `sendMessage`. Confirmations and
  corrections still flow through the Review tab in the add-on UI, not back
  into the chat.
- Filters: `key.fromMe` dropped, non-group dropped, group not in
  `GROUP_ALLOWLIST` dropped, empty text dropped. In-process `seenIds` set
  layered on top of the add-on's id dedup.
- **Network path.** Sidecar → `http://<ha-lan-ip>:5000/internal/whatsapp/inbound`
  with `X-Shared-Secret` header. Requires port 5000 exposed via `ports:` in
  `config.yaml` AND the Network section in HA's Configuration UI must have
  the host port set to `5000` (it doesn't bind from `config.yaml` alone).
  **Windows gotcha**: `homeassistant.local` often resolves to an IPv6
  link-local (`fe80::...%22`) first, causing `Connection was reset`; use
  the direct IPv4 in `.env`.
- **Startup backfill** (`BACKFILL_PER_GROUP`, `BACKFILL_WINDOW_MS`): buffer
  messages Baileys delivers during a startup window, forward the N most
  recent per group, then switch to live mode. In practice returns zero on
  reconnects to an already-synced auth state — WhatsApp's servers don't
  replay history on linked devices that think they're caught up. For deep
  history, use `/backfill` (the paste-flow route) instead.
- `--list-groups` mode prints every group the paired account is in, for
  populating `GROUP_ALLOWLIST`.

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
  image's s6-overlay conflicts with a bare `CMD`. The Baileys sidecar was
  originally scoped as in-container (which would have forced a flip to
  `init: true` + s6-overlay). The external-sidecar decision in `sidecar/`
  means this constraint still holds; the flip is not needed.
- **Port 5000 is exposed on the LAN** via the `ports:` mapping in
  `config.yaml` so the external sidecar can reach `/internal/whatsapp/inbound`.
  Non-loopback callers must authenticate via `X-Shared-Secret`
  (`whatsapp_shared_secret`). All other routes on port 5000 are the normal
  Flask app — same routes ingress serves, minus the `X-Ingress-Path`
  prefix.
- **Admin routes are ingress-reachable.** `_require_local_or_secret`
  accepts loopback, HA ingress (via `X-Ingress-Path`), OR matching
  `X-Shared-Secret`. Ingress originates from the Supervisor's docker
  bridge (`172.30.x.x`) — without the header check, the browser can't
  reach `/admin/*` because it can't inject a shared secret. If you add a
  new gate, use this helper; don't reimplement it.
- Do NOT use Samba for iterative add-on development on HAOS from Windows —
  SMB write caching makes files stale.
- For local development, the app falls back to reading `options.json` from
  the current directory when `/data/options.json` doesn't exist.
- UI changes should be verified in a local Playwright (Chromium) run before
  being reported as done. Playwright is a dev-only dependency — do **not**
  add it to the add-on `requirements.txt`.

## Open questions / deferred

- **Reconciler step 2** — structural detectors + Conflicts tab. Plan
  lives in `RECONCILER_PLAN.md`; facts layer is shipped and validated,
  next is deterministic joins across Airbnb / GCal / bookings /
  `message_facts` and a UI that surfaces findings with one-click
  resolutions.
- **Ingest the full historical WhatsApp archive.** User has a backlog
  beyond the 33 messages already validated. `/admin/ingest` handles any
  size; budget ~45 min per 3000 messages at 0.8s/call pacing.
- **Facts dedup.** Nothing currently collapses duplicate assertions
  across messages ("Itzel May 19" asserted twice = two facts). The
  reconciler is expected to group by `(cleaner, target_date)` and pick
  the latest. Revisit with a `fact_groups` materialized view only if
  this gets painful.
- **Josh-as-host signal.** Jokey / narrative messages from Josh still
  over-extract at facts-v2. Possible fix: allow-list the actual host in
  config and treat other host-bucket senders as background chat.
- **First-run notify-queue noise.** Legacy data has no `cleaner_commitment`
  anywhere, so on install every assigned booking appears as `new`.
  Resolution is one "Mark notified" per cleaner. Revisit with a one-shot
  "trust current state" admin action if Michelle finds the initial flood
  painful.
- **Playwright coverage for the notify queue.** Mobile viewport (375×667),
  empty state, pager, Mark notified, Unassigned-card assignment, plus a
  WhatsApp auto-apply leg that writes `communicated_via="whatsapp"`. Not
  yet run end-to-end.
- **Rejected / deferred:** resolved-notify audit log, per-line-item notify
  ticking (MVP resolves a whole cleaner at once), GCal guest-invite RSVPs
  (WhatsApp pipeline covers it), split stays-vs-cleanings calendars,
  retiring `/print` (Michelle still uses it), in-container Baileys
  sidecar (external process on PC is the chosen path), bot-account swap
  to SpeakOut number (blocked on user action — see
  `sidecar/whatsapp-bridge/README.md` for the procedure).
