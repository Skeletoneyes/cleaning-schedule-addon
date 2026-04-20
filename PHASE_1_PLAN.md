# Phase 1 Plan — Calendar-first UI

> **Status (2026-04-20): superseded.** The FullCalendar home page shipped
> as planned, but the project direction has since moved to Google Calendar
> as the shared view. The FullCalendar view at `/` still works but is no
> longer the primary surface; a dedicated conflict-manager page will
> replace it. See `GCAL_VIEW_SKETCH.md` for current direction and TODOs.
> This file is kept for historical context on the month/week/agenda
> layout, custom-stay data model, and print view — all still live in the
> codebase.

## Scope

Replace the current tabs+cards UI with a FullCalendar-based view. Keep the
existing list view as a secondary tab. Add a printable month view. Extend
manual-add to support multi-day "custom stays" (e.g. friends visiting).

**Explicitly out of scope for Phase 1:**
- Push/email notifications on conflicts (Phase 2)
- WhatsApp automation / MCP / linked-device session (Phase 3)

## Data model (additive, backwards-compatible)

In `data.json`, each booking gains:

- `type`: `"airbnb"` | `"custom_stay"` | `"manual_cleaning"` — defaulted
  lazily on read (iCal UIDs default to `airbnb`, `manual-*` UIDs default to
  `manual_cleaning`).

No other fields change. Existing `data.json` files keep working without
migration.

Cleaner colors are computed server-side from a deterministic hash of the
cleaner's name (HSL). Good enough to start; `cleaners` in `config.yaml` can
later be upgraded from `list[str]` to `list[{name, color}]` without breaking
anything.

## File changes

All changes live in `cleaning-tracker/app.py`. No new files. Single-file
structure preserved.

1. `load_data()` — backfill `type` for legacy entries at read time.
2. New helper `cleaner_color(name)` — stable hash → HSL hex.
3. New route `GET /events.json?start=&end=` — returns two FullCalendar
   event streams:
   - **Stays** (`airbnb` + `custom_stay`): multi-day bars; faded for
     cancelled; orange border for conflicts.
   - **Cleanings** (checkout day of each active stay + `manual_cleaning`):
     pill events colored by cleaner; gray if unassigned.
4. `/add` — extend form: radio for *Cleaning* vs *Stay*. Stay requires
   start + end, no cleaner needed.
5. New route `GET /print?month=YYYY-MM` — minimal template, print-optimized
   CSS.
6. New main template `CALENDAR_TEMPLATE` — becomes the index view. The
   existing list UI (upcoming/past/WhatsApp panels) moves into a "List" tab
   inside the new template. No existing functionality removed.

## FullCalendar wiring

- Loaded via CDN (`cdn.jsdelivr.net/npm/fullcalendar@6...`). HA ingress has
  internet access. Vendoring can come later if air-gap support is needed.
- Views: `dayGridMonth` (default), `listWeek` (mobile fallback),
  `dayGridWeek`.
- `eventSources: ['{{ prefix }}/events.json']` — FullCalendar passes the
  visible range as `start`/`end` query params; server filters accordingly.
- Click on a cleaning event → redirect to an edit page that reuses the
  existing `/assign` + `/confirm` endpoints. (Inline popover considered but
  deferred to keep the first cut simple.)
- Click on an empty day → redirects to `/add` prefilled with that date.

## Printable view

- `/print?month=2026-04` renders a full-page month grid using hand-rolled
  HTML (an HTML table), **not** FullCalendar. FullCalendar's print output is
  fiddly and we want full control over paper layout.
- Output: black borders, color bars for stays, cleaner name + time written
  on checkout-day cells.
- `@media print` strips navigation/buttons/other panels.

## Implementation order (smallest merges first)

Each step is independently testable. Steps 1–3 are the core of the phase;
4–5 are additive.

1. Data-model `type` backfill + `cleaner_color` helper. (~30 lines)
2. `/events.json` endpoint. (~60 lines)
3. Calendar template + wire into index; existing list UI nested as a tab.
   (~150 lines HTML/JS)
4. Extend `/add` for custom stays. (~20 lines)
5. `/print` view + print CSS. (~80 lines)
6. Bump `config.yaml` version, test in HA.

## Risks and open questions

- **FullCalendar CDN dependency**: if the add-on ever needs to run
  air-gapped, FullCalendar will need to be vendored. Non-blocker for now.
- **Template size**: `app.py` will push roughly 1500 lines after this
  phase. Still tolerable as a single file, but Phase 2 or 3 may want to
  split templates out into separate files.
- **Click-to-edit UX**: starting with a redirect-to-edit-page flow rather
  than an inline popover. Simpler to build and debug; easy to upgrade to a
  popover later without changing the data model.
- **Cleaner color stability**: hashing names means renaming a cleaner
  changes their color. Acceptable for now; upgrading to explicit per-cleaner
  colors in config is a one-day change when needed.

## Definition of done for Phase 1

- Calendar view is the default landing page at `/`.
- Month view shows Airbnb stays as horizontal bars and cleanings as colored
  pills on checkout days.
- Overlapping stays render without clipping (one overlap level is enough).
- Custom multi-day stays can be added from the UI and appear as bars.
- A month can be printed from `/print?month=YYYY-MM` and is readable on
  paper.
- Existing list view, conflict-review flow, and WhatsApp paste-parse flow
  all still work (nested inside the new UI).
- `config.yaml` version bumped; tested in a running HA add-on instance.

---

## Status (as of 2026-04-18)

### Implementation: complete
All 6 steps implemented on branch `Calendar-Redo`, merged to `master` via
fast-forward.

- Commit `2520226` — Phase 1 calendar redo (FullCalendar UI, custom stays,
  print view, new routes: `/events.json`, `/edit/<uid>`, `/delete/<uid>`,
  `/print`)
- Commit `ea51ead` — version marker `1.4.0-rc1`

### Deviations from the plan (worth noting)

1. **Bug caught during review**: original `sync_ical()` gated the
   "missing-from-feed → cancelled" sweep on UIDs starting with `manual-`.
   With `custom-*` UIDs now possible, custom stays would have been wrongly
   marked cancelled on every sync. Fix: gate on `b.get("type") != "airbnb"`
   instead. (Applied in `2520226`.)

2. **Small UX polish added**: "Print Month" button in the sync bar, and
   relabelled "+ Manual Cleaning" → "+ Add Entry" since `/add` now handles
   both cleaning and stay entries.

3. **Sonnet agent UX flag (deferred)**: saving from `/edit/<uid>` redirects
   to `/`, not back to `/edit/<uid>`. Fine for the first cut; can be
   addressed with a `?next=` query param when it becomes annoying.

### Pending

- None. Smoke test passed 2026-04-19; version bumped to `1.4.0`.

### Known non-blockers

- ~~`cleaning-tracker/__pycache__/` shows up as untracked.~~ Resolved
  2026-04-19 — added to `.gitignore` and untracked via
  `git rm -r --cached`.
- FullCalendar loads from CDN; won't work air-gapped. Vendor later if
  needed.
- `app.py` is now ~1800 lines after the polish pass below. Single-file is
  still fine but Phase 2 or 3 might want to split templates out.

---

## Post-Phase-1 polish (2026-04-19)

Small, calendar-focused changes made after Phase 1 closed. Too narrow to
justify a Phase 2 doc; logged here so the motivation survives.

- **Mobile-first calendar**: verified the UI on iPhone 15 Pro Max and
  Pixel 7 via Playwright emulation. On mobile, view-switcher buttons moved
  to a footer toolbar so the top bar keeps `prev/next/today`.
- **Timed stay events**: Airbnb stays now render as timed events with a
  15:00 check-in and 11:00 checkout (user's property defaults). Custom
  stays stay all-day.
- **Cleaning time in title**: new optional `clean_time` field on bookings.
  Shown in the event title as `"Itzel · 11:00 AM"` and editable via a
  `<input type="time">` on `/edit/<uid>`. `_parse_clean_time()` backfills
  from legacy `notes: "Time: 11:00 AM | ..."` strings.
- **Plan reversal — deleted the List / Manage tab**: Phase 1 said the
  existing list UI would move into a nested "List" tab. In practice it
  duplicated FullCalendar's Agenda view, so the top-level Manage tab was
  removed entirely. Inline-assignment / stats / paste-parse features that
  lived there moved to the Review tab (which Phase 3 was already building
  out). The calendar is now the single home page.
- **Greyscaled past**: past days and past events render greyscale +
  dimmed so the eye snaps to upcoming work. Varying brightnesses per
  original colour are preserved by `filter: grayscale(100%)` rather than
  flat grey.
- **Title normalization**: Airbnb stay titles always read `"Airbnb"` —
  the earlier attempt to derive them from notes produced oddities like
  `"3p Booked"` or `"2 hours"` in month view.
- **Cancelled-stay styling**: dropped the orange conflict border on
  cancelled bookings and raised opacity from `0.45` → `0.7` (was nearly
  invisible). Cancelled bookings now show a **Dismiss** button on
  `/edit/<uid>` that routes to `/delete/<uid>`; `/delete` accepts
  cancelled bookings of any type.
- **`displayEventTime: false`**: removes FullCalendar's auto time prefix
  (which produced artifacts like `"2 Needs cleaner"`). Times we want
  visible are baked into the title string instead.
