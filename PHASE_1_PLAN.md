# Phase 1 Plan — Calendar-first UI

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
