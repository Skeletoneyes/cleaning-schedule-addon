# GCal-as-View — Shipped + Roadmap

Google Calendar is now the shared view for Joshua, Michelle, and the cleaners.
The HA add-on is the **brain**: it owns `data.json`, runs the iCal sync, hosts
the WhatsApp pipeline, and pushes a one-way projection to GCal. The add-on UI
is no longer the primary viewing surface — it exists to do the things GCal
can't: WhatsApp review and (soon) conflict resolution.

## What's shipped (as of 1.6.4)

- **`cleaning-tracker/gcal.py`** — one-way `data.json` → GCal projection.
  Diffs by `extendedProperties.private.uid`, then insert/patch/delete.
- **Service-account auth.** The add-on reads a service-account JSON from the
  `gcal_service_account_json` option (schema: password). Calendar is shared
  with the service-account email at "Make changes to events".
- **Sync triggers.** `save_data()` fires a daemon-thread push after every
  write; `POST /gcal/sync` does a manual run. Writes are coalesced under a
  module-level lock — only one sync runs at a time, the rest skip.
- **Dedupe.** `_list_existing` returns duplicates; each sync deletes them
  before diffing.
- **Local timezone.** Timed events are tagged `America/Vancouver`, matching
  how `clean_time` and Airbnb check-in/out times are stored in `data.json`.
- **Conflict signalling.** Events with `conflict=true` get `colorId=11` (red)
  and a `⚠️ ` title prefix. Resolves on the next sync.
- **Cancelled stays** are omitted from GCal (deleted).

## What the add-on still owns

- `data.json` (source of truth).
- iCal sync from Airbnb.
- WhatsApp paste flow + inbound pipeline + Review tab + JID mapping + group
  labels.
- **Per-cleaner notify queue at `/`** — drift detection between
  `cleaner_commitment` (last communicated state) and current truth.
  Replaces the FullCalendar view entirely.

## What's next

### Shipped — Per-cleaner notify queue (1.7.0)

`/` now renders a one-cleaner-at-a-time focus card listing every booking
whose `cleaner_commitment` snapshot diverges from current truth (new
assignment, date/time change, cancellation). "Mark notified" rewrites the
commitment on every listed booking for that cleaner to match truth and
advances to the next. An Unassigned bucket sits above the queue for
active bookings without a cleaner. FullCalendar view retired.

GCal red `⚠️` signal is now driven by the same drift check
(`_needs_notify`) rather than the deprecated `conflict` field.

### Other deferred work

- **Print view.** Still lives at `/print?month=YYYY-MM`. No reason to
  retire it; Michelle occasionally prints a monthly sheet.
- **Resolved-notify audit log.** Not built; revisit if Michelle asks.
- **Per-line-item notify ticking.** MVP resolves a whole cleaner at once;
  revisit if partial notifies turn out to be common.
- **Cleaner RSVP via GCal guest invites.** Skipped — WhatsApp pipeline
  already handles confirmations. Revisit only if cleaners ask for it.

## Auth — how we got here

Initial plan was OAuth with a one-time refresh-token mint
(`scripts/gcal_auth.py` + `InstalledAppFlow`). Google's consent screen
threw `unknown_error` for this project regardless of scope, test-user
casing, or fresh OAuth clients. Switched to a service account:

- No consent screen, no test-users list, no 7-day refresh-token expiry on
  apps in "Testing".
- Access is controlled by sharing the target calendar with the service
  account email, not by project IAM.
- One option in HA (`gcal_service_account_json`) instead of three
  (client id / secret / refresh token).

`scripts/gcal_auth.py` still exists, but now just validates a downloaded
service-account JSON key and prints the email to share the calendar with.

## Open design decisions (still open)

- Do cleaners want the whole year of stays, or just cleanings? Probably
  just cleanings — fewer events, less noise. Could split into two
  sub-calendars if needed.
- Cancelled stays: deleted today. If Michelle wants a record, revisit.
