---
name: reconcile-cleaning-schedule
description: Pull the HA add-on snapshot, Airbnb iCal, GCal iCal, and WhatsApp archive, then reconcile them to surface drift and anomalies. Invoke when the user asks to reconcile cleaning data, audit bookings, or compare the four sources.
---

# Reconcile cleaning schedule

## What this does

Pulls four data sources and cross-checks them for drift. Use this skill
when you need cross-source findings that the in-add-on reconciler
doesn't yet cover . ⚠️ **All ten detectors ship in the add-on now**, including
Airbnb iCal ⇄ bookings and bookings ⇄ GCal, which this file used to say it
could not do. Prefer `POST /reconcile/run` and read `/reconcile/last` — it is
faster, it feeds the Conflicts tab, and re-implementing a shipped detector by
hand is wasted work.

1. **HA add-on snapshot** — `data.json` plus non-secret options, via authenticated `GET /internal/snapshot`. Includes the **WhatsApp message archive** (`data.messages`), the **structured facts** (`data.message_facts` — per-message list of `{kind, target_date, target_time, cleaner, confidence, tentative, evidence}` stamped with `prompt_version`; the reconciler reads ALL versions, so a fragmented corpus mixes prompt generations), and `data.dismissed_findings` (the human-dismissed finding ids — filter these out before reporting). See `RECONCILER_PLAN.md`.
2. **Airbnb iCal** — upstream feed fetched directly from the URL stored in the add-on options.
3. **GCal iCal** — the shared calendar's secret iCal URL (configured in `.secrets/urls.json`).

## How to run

1. Run the pull script from the repo root:

   ```bash
   python scripts/reconcile_pull.py
   ```

   The last line of stdout is the pull directory (e.g. `.secrets/pulls/2026-04-20T09-13-02/`). Any per-source failures print to stderr as `warn:` lines but don't abort the run.

2. Read the files in that directory:
   - `ha_snapshot.json` — `{generated_at, options, data}` where `data` is the full `data.json` (bookings, messages, cleaner_jids, group_labels).
   - `airbnb.ics` — raw iCal.
   - `gcal.ics` — raw iCal.
   - `manifest.json` — summary + errors.

3. Reconcile. The add-on already runs detectors 3–6; call
   `POST /reconcile/run` (with `X-Shared-Secret`) or read
   `GET /reconcile/last` to retrieve the current findings and
   start from there. Then extend with what the in-process reconciler
   can't see:
   - **Airbnb vs `data.bookings`** *(shipped as `_ical_vs_bookings`)*: every `VEVENT` with `SUMMARY: Reserved` in the iCal should have a matching UID in `data.bookings` with `type=airbnb`. Flag bookings present in one but not the other.
   - **`data.bookings` vs GCal** *(shipped as `_bookings_vs_gcal`)*: every active booking with a cleaner assigned should project into GCal. An event titled `⚠️ <cleaner>` means drift the add-on already knows about.
   - **`data.messages` coverage**: check whether recent confirmation-style messages auto-applied (`review_state=auto`) or are stuck pending. Flag sustained pending counts.
   - **Cross-check the cached findings**: fetch `GET /reconcile/last` to see what the add-on's in-process reconciler currently reports. If your pull surfaces something the cache doesn't (e.g. Airbnb dropped a booking), flag it.

4. Report findings as a short punch list grouped by source, with UIDs
   and dates. Don't just summarize counts — name the specific
   bookings/messages that need attention. Exclude anything in
   `data.dismissed_findings`: the user already handled those
   out-of-band.

## ⚠️ Write-back — the MANDATORY closing step (ISC-354)

**A conflict resolved in this session that leaves no record on the Pi will be
re-reported by tonight's digest — correctly, because nothing told the Pi.**
The chat is not the system of record; `data.json` is. Before ending any
session that resolved, adjudicated, or explained away a finding, write the
resolution back through the route that matches what actually happened:

| What the session concluded | Write-back |
|---|---|
| A pending message's content should apply | `POST /review/accept/<msg_id>` (or re-queue via `POST /review/map` when routing was starved by an unmapped sender/stale error) |
| A pending message is chitchat / needs no action | `POST /review/ignore/<msg_id>` |
| A finding is resolved out-of-band, no data change needed | `POST /reconcile/dismiss` with `{"finding_id": ..., "reason": "<dated reason naming this session>"}` |
| A booking's cleaner/time is wrong | `POST /assign` |

Dismissal reasons must carry the date and enough context that a cold reader
understands the adjudication — they land in `ops_log` and are the audit trail.
Dismissals are subject-scoped with an evidence cutoff (1.38.0): new messages
about the same booking re-open it, so dismiss without fear of muting the future.

**Anti (ISC-355): write-back is desktop/LAN-only.** These routes are called
with the shared secret from inside the LAN. Nothing on the VPS or the
Telegram path may ever call them — the projection boundary holds in both
directions.

## Setup (one-time)

- Copy `.secrets/urls.json.example` to `.secrets/urls.json` and fill in:
  - `ha_snapshot_url` — e.g. `http://192.168.x.x:5000/internal/snapshot`
  - `ha_shared_secret` — same value as the `whatsapp_shared_secret` add-on option
  - `gcal_ical_url` — from GCal settings → "Integrate calendar" → "Secret address in iCal format"
- `.secrets/` is gitignored.

## Notes

- Safe to run any time; it only reads. No writes to HA, GCal, or WhatsApp.
- Pull directories accumulate under `.secrets/pulls/`. Prune manually if they get large.
- If `ha_snapshot` errors with 403, the shared secret is wrong. If it errors with connection reset, HA's IPv6 link-local issue is biting — use the direct IPv4.
