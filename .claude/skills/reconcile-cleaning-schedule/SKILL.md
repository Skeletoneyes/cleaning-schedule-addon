---
name: reconcile-cleaning-schedule
description: Pull the HA add-on snapshot, Airbnb iCal, GCal iCal, and WhatsApp archive, then reconcile them to surface drift and anomalies. Invoke when the user asks to reconcile cleaning data, audit bookings, or compare the four sources.
---

# Reconcile cleaning schedule

## What this does

Pulls four data sources and cross-checks them for drift:

1. **HA add-on snapshot** — `data.json` plus non-secret options, via authenticated `GET /internal/snapshot`. Includes the **WhatsApp message archive** (`data.messages`) and the **structured facts** extracted from each message (`data.message_facts` — per-message list of `{kind, target_date, target_time, cleaner, confidence, tentative, evidence}` stamped with `prompt_version`). Only facts matching the current `prompt_version` are trustworthy; see `RECONCILER_PLAN.md`.
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

3. Reconcile. At minimum check:
   - **Airbnb vs `data.bookings`**: every `VEVENT` with `SUMMARY: Reserved` in the iCal should have a matching UID in `data.bookings` with `type=airbnb`. Flag bookings present in one but not the other.
   - **`data.bookings` vs GCal**: every active booking with a cleaner assigned should project into GCal. An event titled `⚠️ <cleaner>` means drift the add-on already knows about.
   - **`data.messages` coverage**: check whether recent confirmation-style messages auto-applied (`review_state=auto`) or are stuck pending. Flag sustained pending counts.
   - **`data.message_facts` ⇄ bookings**: for each `confirm` fact with `(target_date, cleaner)` matching an active booking, check whether the booking reflects it — if the cleaner is absent or different, that's a candidate auto-apply the pipeline missed. For each `decline` fact matching a booking's current cleaner, that's a "cleaner said no but is still assigned" finding.
   - **Fact timeline**: a later `decline` on the same `(cleaner, target_date)` as an earlier `confirm` means the cleaner changed their mind; latest wins.
   - **Commitment drift**: for each booking, compare `cleaner_commitment` to current `(cleaner, date, clean_time)` — these are what the add-on's notify queue surfaces.

4. Report findings as a short punch list grouped by source, with UIDs and dates. Don't just summarize counts — name the specific bookings/messages that need attention.

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
