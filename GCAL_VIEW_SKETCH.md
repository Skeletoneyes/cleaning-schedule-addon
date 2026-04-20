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
- Conflict detection (Python-side; GCal has no native overlap concept).
- The legacy FullCalendar view at `/`. Still functional; no longer primary.

## What's next

### TODO — Conflict-manager UI (future session)

The FullCalendar view has always doubled as both "browse everything" and
"resolve conflicts". Once cleaners and Michelle are on GCal, only the latter
matters inside the add-on. Design goal: a single page that lists exactly the
bookings where `conflict=true`, shows the overlapping pair(s), and offers
the minimum set of actions to resolve each (dismiss, reassign cleaner,
edit time/date, mark cancelled).

Open questions to settle when we pick this up:

- Does the current FullCalendar home page get replaced entirely, or do we
  keep a read-only month view alongside a dedicated `/conflicts` page?
- Should the unassigned-cleanings list live on the same page as the
  conflict list, or be its own view?
- Do we need a resolved-conflict audit log, or is "it's no longer on the
  list" sufficient feedback?
- How do WhatsApp-driven changes (e.g. cleaner declines, re-assignment)
  interact with conflict state — should the Review tab surface the
  conflict badge, or link over to `/conflicts`?

Until this is built, conflict resolution happens on the existing
`/edit/<uid>` page after spotting the red ⚠️ event on GCal.

### Other deferred work

- **Print view.** Still lives at `/print?month=YYYY-MM`. No reason to
  retire it; Michelle occasionally prints a monthly sheet.
- **FullCalendar view retirement.** Kept as a fallback. Can be removed
  once the conflict-manager UI lands and nothing else depends on the `/`
  route being a calendar.
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
