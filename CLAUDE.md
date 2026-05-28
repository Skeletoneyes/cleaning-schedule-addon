# Cleaning Schedule Tracker — HA Add-on

## Purpose

Home Assistant add-on that tracks Airbnb cleaning schedules. It syncs bookings
from an Airbnb iCal feed, lets you assign cleaners to checkout dates, and uses
Claude Haiku to interpret WhatsApp conversations with cleaners (live traffic
via a dedicated WhatsApp Bridge HA add-on, plus a `/backfill` paste page for
catching up on historical assignments) to detect confirmations and declines.

## Architecture

Single-file Flask app (`cleaning-tracker/app.py`) running as an HA add-on with
ingress. No database — data lives in `/data/data.json` (persists across
rebuilds). Configuration is read from `/data/options.json` (populated by HA
from `config.yaml` options).

**Current direction (1.16.x):** the add-on is the **brain**, Google Calendar
is the **shared view**. `data.json` is the source of truth; `gcal.py` pushes
a one-way projection to a GCal calendar shared with Michelle and the
cleaners. The add-on UI's job is to handle everything GCal can't — the
per-cleaner notify queue, WhatsApp review, and the Conflicts tab backed by
the versioned facts layer + structural detectors.

The FullCalendar view is gone. `/` now renders three tabs: Notify queue
(per-cleaner `cleaner_commitment` drift), WhatsApp review, and Conflicts
(reconciler findings).

The reconciler cross-checks `data.json`, Airbnb iCal, GCal, and the
WhatsApp archive. Step 1 (versioned facts extraction via `facts.py`)
and all six Step 2 detectors are shipped. `/reconcile/run` fetches
the Airbnb iCal and tagged GCal events inline (fail-loudly — no
fallbacks) and passes them to the detectors. See `RECONCILER_PLAN.md`.

### Key Files

```
repository.yaml              # HA custom repo metadata
cleaning-tracker/
├── config.yaml              # Add-on config: name, version, options schema
├── Dockerfile               # python:3.12-slim, COPY app.py gcal.py facts.py reconcile.py ./
├── requirements.txt         # flask, requests, icalendar, anthropic, google-api-*
├── app.py                   # Flask routes, templates, logic — ~2800 lines
├── gcal.py                  # Google Calendar projection (one-way: data.json → GCal)
├── facts.py                 # Versioned structured-fact extractor (Haiku, FACTS_PROMPT_VERSION)
└── reconcile.py             # Pure-function reconciler: detectors → ranked findings
whatsapp-bridge/             # WhatsApp Bridge HA add-on (replaces old PC sidecar)
├── config.yaml              # Add-on config: slug=whatsapp-bridge, host_network: true
├── Dockerfile               # node:20-slim + git; uses npm ci + committed package-lock.json
├── index.js                 # Baileys linked device; forwards all group messages (incl. fromMe) to cleaning-tracker
├── package.json
└── package-lock.json        # git+ssh URLs rewritten to git+https so Docker build works without SSH keys
sidecar/whatsapp-bridge/     # DEPRECATED — superseded by whatsapp-bridge/ HA add-on. Kept for reference.
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
- `whatsapp_shared_secret` — shared token authenticating
  `POST /internal/whatsapp/inbound` calls from non-loopback callers. The
  WhatsApp Bridge add-on uses `host_network: true` and dials
  `127.0.0.1:5000`, but the tracker is **not** on host network — Docker
  NATs the source IP to the bridge gateway (`172.30.32.1`), so the
  tracker does **not** see the bridge as loopback. The bridge therefore
  needs this secret too: set the same string in both add-ons'
  `shared_secret` / `whatsapp_shared_secret` options (1.0.4+).

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
- `dismissed_findings` — `{finding_id: {dismissed_at, reason}}`. Set via
  `POST /reconcile/dismiss` when a human decides a reconciler finding is
  resolved out-of-band. `reconcile.run()` filters these before sorting.
  Undo via `POST /reconcile/undismiss`.

The latest reconciler output is cached to `/data/reconciler_last.json`
(not in `data.json`). Written by `POST /reconcile/run`; read by the
Conflicts tab and `GET /reconcile/last`.

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

Empty state: "All cleaners up to date ✓". The WhatsApp Review and
Conflicts panels live as sibling tabs on the same page (tab-switched,
not separate routes). Tab hashes persist via `location.hash`
(`#review`, `#conflicts`).

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
- **History window**: `_facts_history(messages, target)` passes only the
  most recent `FACTS_HISTORY_WINDOW = 30` messages from the **same group**,
  sorted by timestamp. Without this cap, bulk reprocess blows through the
  Anthropic TPM budget (one stalled ingest sat at 284/952 for 5 hours
  before the cap was added). Only the facts path uses the window; the
  parse path keeps full history.
- `FACTS_PROMPT_VERSION` (currently `facts-v2`) stamps every stored
  record. The reconciler reads only current-version facts, so
  half-reprocessed state is safe. Bump the version after any prompt
  edit, then `POST /admin/reprocess-facts`.
- **Rate-limit handling**: `extract_facts` retries 429 / 5xx / timeouts
  with exponential backoff honouring `retry-after`. Bulk ingest paces
  at 0.8s/call.

### Reconciler (`reconcile.py`, `/reconcile/*`)
Pure-function detectors that join `data.json` + `message_facts` into a
ranked list of findings with `{id, detector, kind, severity, booking_uid,
cleaner, date, why, evidence, quote}`. Severity tiers: `needs-attention`
(drift, decline-still-assigned, contested cleaner, host schedule vs
booking mismatch), `suggest` (unrecorded confirmation, schedule vs
unassigned booking), `informational` (confirm with no booking,
changed-mind timeline). Findings dedup on stable id so re-runs are
idempotent.

**Shipped detectors**:
- `_ical_vs_bookings` — Airbnb iCal ⇄ bookings. `/reconcile/run`
  fetches the feed inline. Emits `ical_missing_booking`,
  `booking_not_in_ical`, `ical_date_mismatch`, `ical_resurrected`.
- `_bookings_vs_gcal` — bookings ⇄ GCal. `/reconcile/run` calls
  `gcal.fetch_tagged_events` on an annotated snapshot. Emits
  `gcal_missing_event`, `gcal_stale_event`, `gcal_orphan`. Only
  runs when `gcal_enabled`.
- `_drift` — reshapes the notify-queue into findings (new / changed /
  cancelled / unassigned).
- `_facts_vs_bookings` — confirm/decline facts ⇄ booking state; emits
  `unrecorded_confirmation`, `contested_cleaner`,
  `decline_still_assigned`, `confirm_no_booking`.
- `_fact_timeline` — `changed_mind` when a cleaner said both confirm and
  decline on the same date (latest wins).
- `_schedule_vs_bookings` — host `schedule_assertion` ⇄ booking cleaner
  (emits `schedule_mismatch` / `schedule_unassigned`).

The cached result stores `findings_raw` (pre-dismiss) alongside
`findings` (post-filter). `reconcile.filter_and_sort()` is the pure
re-filter used by `_rerun_reconcile_cached` after dismiss/undismiss
— those paths never re-fetch iCal/GCal.

**Known issues (see `RECONCILER_PLAN.md` Next steps):**
- ~~Detector 2 `_events_equal` dateTime offset mismatch~~ — **Fixed 1.16.1.**
  `_parse_gcal_dt` now normalises both sides to timezone-aware datetimes
  before comparing.
- Detector 6 (`_schedule_vs_bookings`) doesn't collapse stale host
  assertions. A latest-wins pass over `(cleaner, target_date)` keyed
  by message timestamp would drop Mar/Apr backfilled-host-message
  mismatches. Until fixed, expect `schedule_mismatch` noise from old
  host messages.
- Detector 1 has been healthy — zero findings on first live run.

**Routes** (all `_require_local_or_secret`-gated):
- `POST /reconcile/run` — recompute + persist to `reconciler_last.json`.
  Accepts form posts (redirects to `/#conflicts`) or JSON (returns body).
- `GET /reconcile/last` — serve cached JSON.
- `POST /reconcile/dismiss` — body `{finding_id, reason?}`. Appends to
  `data.dismissed_findings` and re-runs the cache.
- `POST /reconcile/undismiss` — inverse.

**Conflicts tab** on `/`: renders the cached findings grouped by
severity with one-click actions — `Assign <cleaner>` for
`unrecorded_confirmation` / `schedule_unassigned`, `Edit booking`,
`Dismiss`. Badge count on the tab = `needs-attention` count.

### Admin routes (loopback / ingress / shared-secret only)
- `GET /admin/facts` — dump `message_facts` for inspection.
- `POST /admin/reprocess-facts` — re-extract every message whose stored
  `prompt_version` is stale. Idempotent.
- `POST /admin/ingest-transcript` — paste a WhatsApp chat export, parse
  each line into the messages log, run facts extraction (or full
  `process_message` if `apply=true`) in a background thread. Body:
  `{transcript, group_jid, apply}`. Parser handles three formats:
  `[YYYY-MM-DD, HH:MM:SS AM/PM]`, `[H:MM AM/PM, M/D/YYYY]`, and Android
  `YYYY-MM-DD, H:MM a.m./p.m. - Sender: text`. Stable ids
  (`backfill-<sha1(ts|sender|text)[:16]>`) make re-runs idempotent and
  dedup against live messages.
- `GET /admin/ingest-status` — progress + `last_error`.
- `GET /admin/ingest` — HTML paste form, linked from the home page's
  Unassigned card ("Ingest transcript").
- `POST /admin/remap-group` — bulk-rewrite `group` on messages and
  update `group_labels`. Body: `{mapping: {old_jid: new_jid}, labels:
  {jid: label}}`. Useful when a paste-ingested transcript used a
  placeholder group JID that you later want to consolidate with the
  live group's real JID.

Auth for all `/admin/*` and `/internal/snapshot` routes goes through
`_require_local_or_secret()`. Accepts: loopback, HA ingress (presence
of `X-Ingress-Path` header the Supervisor proxy stamps), or matching
`X-Shared-Secret`.

### Disaster recovery — `POST /internal/restore` (1.18.0)
Inverse of `/internal/snapshot`: repopulates `/data/data.json` after a host
wipe or fresh install. The add-on's `/data` is a private volume with no `map:`,
so it can't be written from outside (e.g. an SSH session) — this endpoint is
the only way to inject a full snapshot back in. Same auth as `/internal/snapshot`
(loopback open; remote callers present `X-Shared-Secret`). Body is the bare
`data` object **or** a `{"data": {...}}` wrapper, so a `/internal/snapshot`
response replays verbatim. It moves the existing file to `data.json.bak`, writes
via `save_data()` (so GCal re-syncs), and returns post-restore counts. After
restoring, hit `POST /sync` to merge any feed reservations newer than the
snapshot (assignments are preserved by UID). Snapshot backups live in the repo
at `.secrets/pulls/<ts>/ha_snapshot.json`.

### WhatsApp — Bridge HA add-on (`whatsapp-bridge/`, shipped 1.0.x)
- **Runs as a standalone HA add-on** alongside the cleaning tracker. Uses
  `host_network: true` so it reaches the cleaning tracker via
  `http://127.0.0.1:5000`. Despite host_network, the tracker is on the
  docker bridge with port 5000 mapped — Docker NATs the source to
  `172.30.32.1`, so the tracker doesn't see this as loopback. The bridge
  must therefore present `X-Shared-Secret` matching the tracker's
  `whatsapp_shared_secret` (the bridge's own `shared_secret` option,
  added in 1.0.4). Auth state persists in `/data/auth/` across restarts.
- Pairs as a WhatsApp **linked device** via QR scan. QR appears in the
  add-on's Log tab on first start. Subsequent starts reconnect
  automatically using the saved auth state.
- **Forwards all group messages including the host's own outbound messages**
  (`key.fromMe` is NOT filtered). Both cleaner replies and Josh's messages
  reach the facts layer. The payload includes `from_me: true/false` so the
  cleaning tracker can role-tag correctly.
- **Read-only.** `index.js` never calls `sendMessage`.
- Filters: non-group dropped, group not in `group_allowlist` option dropped,
  empty text dropped. In-process `seenIds` set layered on top of the
  add-on's id dedup.
- **On connect**, logs all visible groups with JIDs — check the Log tab
  after first pairing to populate the `group_allowlist` option.
- **Startup backfill** (`backfill_per_group`, `backfill_window_ms` options):
  buffers messages Baileys delivers during a startup window, forwards the
  N most recent per group, then switches to live mode. In practice returns
  zero on reconnects to an already-synced auth state — WhatsApp's servers
  don't replay history on caught-up linked devices. For deep history, use
  the transcript ingest route instead.
- **Production path**: currently paired against personal WhatsApp (tolerated
  for testing). To move to a dedicated bot number: stop the add-on, delete
  `/data/auth/`, register WhatsApp Business on the bot phone (SpeakOut
  $125/yr — pending), restart and scan the new QR.
- **Docker build note**: `package-lock.json` is committed with `libsignal-node`
  resolved to `git+https://` (not `git+ssh://`) so `npm ci` works in the
  build container without SSH keys.

### Google Calendar projection (primary view, `gcal_enabled`)
- One-way sync: `data.json` → GCal. Cleaners don't edit the calendar; they
  confirm via WhatsApp.
- `gcal.py::sync_to_gcal()` diffs desired events against existing ones
  tagged with `extendedProperties.private.source="cleaning-tracker"`, then
  inserts / patches / deletes to converge. Cancelled bookings are omitted
  from desired events (deleted from GCal) **unless** the cleaner has an
  unacknowledged `cleaner_commitment` — in that case a red
  `❌ Cleaning Cancelled · Notify <cleaner>` timed event is kept until
  "Mark notified" clears the commitment.
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
- **Cleaning events are always timed** (never all-day). When `clean_time`
  is set, the event runs from that time for 2 hours. When unset, defaults
  to 11:00 AM (checkout) → 1:00 PM. Property checkout is 11:00 AM,
  check-in is 3:00 PM.
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

Installed via HA custom repository. **Preferred: SSH CLI** (scriptable; the
external long-lived token gets 401 on store/install endpoints, so the UI or the
CLI are the only options):
```bash
SSH="ssh -p 22 -i ~/.ssh/id_ed25519_ha root@homeassistant.local"
$SSH "ha store add-repository https://github.com/Skeletoneyes/cleaning-schedule-addon"
$SSH "ha addons install 27cbea7f_cleaning-tracker && ha addons install 27cbea7f_whatsapp-bridge"
# set options via the Supervisor API (full access from inside the session):
$SSH 'curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" \
  --data @/tmp/opts.json http://supervisor/addons/27cbea7f_cleaning-tracker/options'
$SSH "ha addons start 27cbea7f_cleaning-tracker"
```
Repo slug is `27cbea7f`; add-on slugs are `27cbea7f_cleaning-tracker` and
`27cbea7f_whatsapp-bridge`. Or via the UI: Add-ons > Add-on Store >
Repositories > paste the GitHub URL > install > configure options > start.

Restore data after a wipe with `POST /internal/restore` (see Disaster recovery
above) — the data volume is private, so you cannot just drop a file in `/data`
over SSH.

Updates: bump `version` in `config.yaml`, push to GitHub, then
`ha store reload && ha addons update 27cbea7f_cleaning-tracker`.

## Important notes

- **Always bump `config.yaml` version when pushing changes.** The Supervisor
  caches add-on configs, and updates for an existing slug only take effect
  on a version bump.
- `init: false` in `config.yaml` is required — without it, the HA base
  image's s6-overlay conflicts with a bare `CMD`. The WhatsApp Bridge runs
  as a separate add-on so this constraint is unchanged.
- **Port 5000 is exposed on the LAN** via the `ports:` mapping in
  `config.yaml`. The WhatsApp Bridge add-on uses loopback and doesn't need
  this, but the port is kept for the `scripts/reconcile_pull.py` off-host
  puller and any other tooling that hits `/internal/snapshot` or
  `/internal/whatsapp/inbound` from outside the host. Non-loopback callers
  must authenticate via `X-Shared-Secret` (`whatsapp_shared_secret`).
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

- **Reconciler step 3 (daily digest)** — **Shipped in 1.17.1.** Background
  scheduler runs at `digest_time` (default 08:00), diffs against the
  previous run's baseline, and posts a HA persistent notification with
  new/resolved finding counts. Enable via `digest_enabled: true` in add-on
  options. "Run digest" button in the Conflicts tab triggers on demand.
  Detector 6 latest-wins collapse also shipped in 1.17.1.
- **Facts dedup.** Nothing currently collapses duplicate assertions
  across messages ("Itzel May 19" asserted twice = two facts). The
  reconciler groups by `(cleaner, target_date)` in `_fact_timeline`
  and `_schedule_vs_bookings` but not across detectors. Revisit with a
  `fact_groups` materialized view only if this gets painful.
- **Josh-as-host signal.** Jokey / narrative messages from Josh still
  over-extract at facts-v2. Possible fix: allow-list the actual host in
  config and treat other host-bucket senders as background chat.
- **First-run notify-queue noise.** Legacy data has no `cleaner_commitment`
  anywhere, so on install every assigned booking appears as `new`.
  Resolution is one "Mark notified" per cleaner. Revisit with a one-shot
  "trust current state" admin action if Michelle finds the initial flood
  painful.
- **Playwright coverage for the notify queue + Conflicts tab.** Mobile
  viewport (375×667), empty state, pager, Mark notified, Unassigned-card
  assignment, Conflicts-tab dismiss + Assign actions, plus a WhatsApp
  auto-apply leg that writes `communicated_via="whatsapp"`. Not yet run
  end-to-end.
- **Rejected / deferred:** resolved-notify audit log, per-line-item notify
  ticking (MVP resolves a whole cleaner at once), GCal guest-invite RSVPs
  (WhatsApp pipeline covers it), split stays-vs-cleanings calendars,
  retiring `/print` (Michelle still uses it), bot-account swap to SpeakOut
  number (pending — see `whatsapp-bridge/` for the re-pair procedure).
