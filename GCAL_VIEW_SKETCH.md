# GCal-as-View Sketch

An alternative architecture where Google Calendar replaces the FullCalendar UI
as the shared view for Joshua, Michelle, and the cleaners. The add-on keeps
the parts that are actually hard (iCal sync, WhatsApp review, conflict logic)
and stops reinventing the parts Google already does well (calendar rendering,
mobile apps, notifications, sharing).

## What stays in the add-on

- **Source of truth** — `data.json` continues to hold bookings and cleanings
  as first-class objects with `type`, `status`, `cleaner`, `clean_time`,
  `conflict`, `notes`. GCal is a projection, not the canonical store.
- **iCal sync** — unchanged. Pulls from Airbnb, merges into `data.json`.
- **WhatsApp pipeline** — unchanged. Paste flow, inbound worker, Review tab,
  JID mapping, group labels. This is the actual value and doesn't map onto
  anything Google offers.
- **Conflict detection** — computed in Python when bookings are written. GCal
  has no native "conflict" concept; we reflect it via event colour + title
  prefix (see below).
- **Assignment UI** — a minimal "unassigned cleanings" list stays in the
  add-on. Everything else (viewing, reminders, month/week navigation) moves
  to GCal.

## What GCal replaces

- FullCalendar month/week/agenda views and the month-print view.
- Per-device installs — cleaners already have Google accounts or can
  subscribe via iCal URL.
- Push notifications / reminders for upcoming cleanings.
- "Did you see this?" — cleaners and Michelle see the same calendar on
  their phones natively.

## Answering the conflict-state question

Yes, you can keep bookings and cleanings as your own objects and still use
GCal as the view. Two mechanisms make it work:

1. **`extendedProperties.private`** on each GCal event — arbitrary key/value
   metadata (up to 1024 chars per value, 300 properties per event). Store
   `{uid, type, status, cleaner, conflict, clean_time}`. This lets the
   add-on round-trip events: when you re-sync, you find "your" event by
   `privateExtendedProperty=uid={uid}` rather than guessing by title.
2. **Visual conflict signalling** — GCal won't flag overlaps for you, but
   you compute `conflict` server-side and reflect it by:
   - Setting `colorId` to red (`"11"`) on conflicted events.
   - Prefixing the title with `⚠️ ` so it's obvious on a phone.
   - Optionally adding a description line: "Conflicts with: <other uid>".

   When the conflict resolves (one side cancels), the next sync rewrites
   colour and title.

So the state lives in your JSON; GCal just renders whatever state you push.

## Proposed GCal layout

One dedicated calendar (e.g. "Airbnb Cleaning") shared with Michelle +
cleaners. Single calendar keeps permissions simple. Event types:

- **Stay** — all-day or timed 15:00→11:00, title `"Airbnb"`, neutral
  colour. Cancelled stays: colour `"8"` (graphite) + title `"Airbnb
  (cancelled)"`, or deleted outright.
- **Cleaning** — on checkout day, title `"🧹 Itzel · 11:00 AM"`, per-cleaner
  colour (map the existing HSL hash to the 11 GCal colour slots — lossy but
  fine). Unassigned: title `"🧹 Needs cleaner"`, colour red.
- **Conflict** — any event with `conflict=true` gets red + ⚠️ prefix,
  regardless of type.

All events carry `extendedProperties.private.uid` so the sync is
idempotent.

## Sync model

One direction: `data.json` → GCal. Cleaners don't edit the calendar; they
confirm via WhatsApp, which flows through the existing Review tab. This
avoids the thorniest problem (two-way sync, conflict resolution between
GCal edits and iCal re-pulls).

A single `sync_to_gcal()` function, called after any write to `data.json`:

1. List GCal events in the relevant window with
   `privateExtendedProperty=source=cleaning-tracker`.
2. Build the desired event set from `data.json`.
3. Diff by uid: `insert` new, `patch` changed, `delete` removed.
4. For each event, set `colorId`, title, description, and
   `extendedProperties.private` from the booking object.

Runs on the same cadence as the iCal sync, plus on any `/assign`, `/add`,
`/edit`, `/delete`, and on successful WhatsApp auto-apply.

## Auth

Service account with domain-wide delegation is overkill for a household
tool. Simpler: OAuth once as Joshua, store refresh token in `/data/`,
use that to write to a calendar Joshua owns and has shared with the
others. Add-on option: `gcal_calendar_id` + a one-time "connect" button
that runs the OAuth flow through ingress.

## What the add-on UI becomes

Stripped down to the things GCal can't do:

- **Home page** — "Unassigned cleanings" list (the only workflow actually
  driven inside the add-on) + a "Open calendar in Google" button.
- **Review tab** — unchanged. WhatsApp triage, JID mapping, group labels.
- **Settings** — iCal URL, API key, cleaners, GCal calendar id, OAuth
  connect button.

The `/print`, `/edit`, `/add` routes can stay as escape hatches but are
no longer the primary interaction surface. The FullCalendar view at `/`
can be removed or kept as a fallback.

## Migration path (rough)

1. Add `google-api-python-client` + `google-auth-oauthlib` to
   `requirements.txt`.
2. Add OAuth connect flow + token storage in `/data/gcal_token.json`.
3. Implement `sync_to_gcal()` and wire it into all write paths.
4. Ship behind an option flag (`gcal_enabled: bool`) so the existing UI
   keeps working during rollout.
5. Once the shared calendar is adopted in practice, decide whether to
   retire the FullCalendar view or keep it as a secondary view.

## What you lose vs. today

- Print view (keep it — it's already hand-rolled and doesn't depend on
  GCal).
- Greyscale-past-days visual polish (GCal doesn't offer this).
- Precise per-cleaner colour (11-slot palette vs. infinite HSL). Minor.
- Everything else survives, because `data.json` is still the source of
  truth.

## Open questions

- Do cleaners want the whole year of stays, or just cleanings? (Probably
  just cleanings — fewer events, less noise. Could split into two
  sub-calendars if needed.)
- Should cancelled stays/cleanings be deleted from GCal or kept with a
  "(cancelled)" marker? Leaning delete — less clutter on cleaners'
  phones.
- RSVPs: worth inviting cleaners as guests so they can accept/decline per
  event, or is WhatsApp-only cleaner? WhatsApp is already working; skip
  RSVPs for v1.
