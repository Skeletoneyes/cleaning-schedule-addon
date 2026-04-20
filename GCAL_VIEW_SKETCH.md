# GCal-as-View — Shipped + Roadmap

Google Calendar is the shared view for Joshua, Michelle, and the cleaners.
The HA add-on is the **brain**: it owns `data.json`, runs the iCal sync,
hosts the WhatsApp pipeline, and pushes a one-way projection to GCal. The
add-on UI's remaining job is the per-cleaner **notify queue** — deciding
who needs a WhatsApp message and what to say.

## Shipped

### GCal projection (1.6.x → 1.7.x)

- **`cleaning-tracker/gcal.py`** — one-way `data.json` → GCal. Diffs by
  `extendedProperties.private.uid`, then insert/patch/delete.
- **Service-account auth.** JSON key in option `gcal_service_account_json`;
  calendar shared with the service-account email at "Make changes to events".
- **Sync triggers.** `save_data()` fires a daemon-thread push after every
  write; `POST /gcal/sync` does a manual run. Module-level lock serializes
  runs — concurrent ones skip.
- **Dedupe.** `_list_existing` flags any duplicate-uid events; each sync
  deletes them before diffing.
- **Local timezone.** Timed events are tagged `America/Vancouver`.
- **Cancelled stays** are omitted from GCal (deleted).
- **Drift signal on cleanings only.** Cleaning events whose booking has
  unresolved `cleaner_commitment` drift get `colorId=11` (red) + `⚠️`
  prefix. Stay events never get the warning. Resolves on the next sync
  after "Mark notified".

### Per-cleaner notify queue (1.7.x)

`/` renders a one-cleaner-at-a-time focus card listing every booking whose
`cleaner_commitment` snapshot diverges from current truth. Kinds:
`new` (first assignment), `changed` (drift), `cancelled` (after commit),
plus a separate **Unassigned** bucket for active Airbnb bookings with no
cleaner. Pager via `?i=<n>`. "Mark notified" rewrites the commitment on
every listed booking for that cleaner and advances.

FullCalendar view is retired. `/events.json` is gone. The deprecated
`conflict` field is no longer read or written. `/add`, `/edit/<uid>`,
`/print` remain as escape hatches.

## What the add-on still owns

- `data.json` (source of truth).
- iCal sync from Airbnb.
- WhatsApp paste flow + inbound pipeline + Review tab + JID mapping +
  group labels.
- Per-cleaner notify queue — drift detection driving both `/` and the
  GCal `⚠️` signal.

## Roadmap / open questions

### Near-term

- **First-run noise.** Legacy data has no `cleaner_commitment` anywhere,
  so on install every assigned booking appears as `new`. Resolution is
  one "Mark notified" per cleaner. Haven't decided if that's acceptable
  onboarding or if we want a one-shot "trust current state" admin action
  that bulk-writes commitments to match truth. Revisit if Michelle finds
  the initial flood painful.
- **Playwright verification.** Task #8 — not yet run. The fix for
  `current_bucket.items` (Jinja dot-access returning the `.items`
  method) landed untested in the browser; first manual verify was in HA.
  Should cover: mobile viewport (375×667), empty state, pager, Mark
  notified, Unassigned-card assignment, plus a WhatsApp auto-apply
  leg that writes `communicated_via="whatsapp"`.

### Deferred / rejected

- **Resolved-notify audit log.** Not built. Revisit if Michelle asks.
- **Per-line-item notify ticking.** MVP resolves a whole cleaner at
  once. Revisit if partial notifies turn out to be common.
- **Cleaner RSVP via GCal guest invites.** Skipped — WhatsApp pipeline
  already handles confirmations.
- **Stays vs cleanings split calendars.** Do cleaners want all stays or
  just cleanings? Open; defer until someone complains.
- **Print view retirement.** No — Michelle still prints `/print`.

## Auth — how we got here

Initial plan was OAuth with a one-time refresh-token mint. Google's
consent screen threw `unknown_error` regardless of scope or test-user
casing. Switched to a service account:

- No consent screen, no test-users list, no 7-day refresh-token expiry.
- Access controlled by sharing the target calendar with the service
  account email, not by project IAM.
- One option in HA instead of three (client id / secret / refresh token).

`scripts/gcal_auth.py` now just validates a downloaded JSON key and
prints the email to share with.
