# Reconciler — LLM-driven conflict detection across sources

> **Status (2026-04-22, add-on 1.16.0):** Step 1 (versioned facts
> extraction) and Step 2 are fully shipped. All six detectors run in
> `reconcile.py`; Conflicts tab with dismiss mechanism is live.
> `/reconcile/run` now fetches the Airbnb iCal and tagged GCal events
> inline — fetch failures propagate as 500s rather than being papered
> over. Dismiss / undismiss re-uses the cached `findings_raw` list so
> those actions never re-fetch.

## Goal

Merge four sources of scheduling truth into a single reconciled view and
surface drift before it becomes a missed cleaning:

1. **Add-on `data.json`** — bookings (iCal sync + manual) and their
   `cleaner_commitment` state.
2. **Airbnb iCal feed** — upstream reservations.
3. **Google Calendar** — the shared view projected from `data.json`.
4. **WhatsApp archive** (`data.messages` + `data.message_facts`) — what
   cleaners and the host actually said.

The reconciler does not own truth; it points at contradictions so a human
can reconcile. `data.json` remains the single source of truth.

## Architecture decision: versioned facts layer

Why a facts layer separate from the existing parse step:

- The parse step (`parse_whatsapp_message` in `app.py`) decides routing
  for **one** booking — `{action, booking_uid, cleaner, confidence}`.
  It's optimised for the live-apply decision.
- The reconciler needs **every scheduling assertion** a message carries,
  from **both** sides of the chat — a host's 30-row schedule dump is 30
  facts, a cleaner's re-posted list with per-row times is 30 confirms.
- Versioning (`FACTS_PROMPT_VERSION`) lets prompts evolve. The reconciler
  reads only current-version facts, so half-reprocessed state is safe.
  Bump the version + `POST /admin/reprocess-facts` to bring everything
  forward.

## Step 1 — Facts extraction (shipped, facts-v2)

Location: `cleaning-tracker/facts.py`.

**Schema** (stored at `data.message_facts[msg_id]`):

```json
{
  "facts": [
    {
      "kind": "confirm|decline|time_proposal|date_proposal|schedule_assertion|unclear",
      "target_date": "YYYY-MM-DD or null",
      "target_time": "HH:MM or null",
      "cleaner": "canonical name or null",
      "confidence": 0.95,
      "tentative": false,
      "evidence": "short quote from the source message"
    }
  ],
  "reported_by_jid": "<sender jid>",
  "model_version": "claude-haiku-4-5-20251001",
  "prompt_version": "facts-v2",
  "extracted_at": "2026-04-21T09:14:00"
}
```

**Key prompt behaviours** (learned from real-traffic validation):

- **Role-tagged history** — every prior message is labelled `<host>` or
  `<cleaner:Name>` so the model picks the right fact kind by sender role.
  `schedule_assertion` is host-only; `confirm`/`decline`/`time_proposal`/
  `date_proposal` are cleaner-only.
- **Re-posted lists are bulk confirms.** When a cleaner quotes the host's
  list and adds per-row times or "I'm full" markers, emit one `confirm`
  or `decline` per row — not a `schedule_assertion`. The evidence field
  carries the original row text. This is *the* dominant real-chat pattern
  and v1 of the prompt missed it entirely.
- **Narration ≠ fact.** "Daria couldn't make it today" is past-tense
  narration. "I'm full that day" is a fact. The prompt calls this out
  explicitly because Haiku would otherwise over-extract.
- **`tentative: true` only when the speaker flags it** ("let me check my
  agenda and reconfirm"), not when the model is uncertain.

**Rate-limit handling** — `extract_facts` retries 429 / 5xx / timeout
with exponential backoff, honouring `retry-after`. Bulk paths
(`_ingest_worker`, `admin_reprocess_facts`) pace at 0.8s/call to stay
under the Anthropic TPM budget.

**Ingest paths**:

- **Live** (`POST /internal/whatsapp/inbound`) — `process_message` runs
  both the parse step and `facts_mod.extract_facts` independently. Facts
  are stored regardless of parse outcome.
- **Paste-ingest** (`POST /admin/ingest-transcript`, UI at `/admin/ingest`)
  — parses a WhatsApp export into individual message records, then either
  facts-only (`apply=false`, default — marks each `review_state="ignored"`
  with a `haiku_result.backfill_ingest` sentinel so the Review tab
  doesn't surface them) or full `process_message` (`apply=true`, for
  future bulk adds you want auto-applied).
- **Reprocess** (`POST /admin/reprocess-facts`) — re-extracts any message
  whose stored `prompt_version` is stale. Idempotent and safe to
  interrupt.
- **Inspect** (`GET /admin/facts`, `GET /admin/ingest-status`).

All admin routes gate on `_require_local_or_secret()` — loopback, HA
ingress (via `X-Ingress-Path`), or matching `X-Shared-Secret`.

**Validated real-traffic quality** (33-message Itzel transcript,
2026-04-21):

- 38/38 processed, 0 errors.
- 70 total facts, 16 messages carried facts.
- The critical re-posted-list message produced 28 correctly-dated facts
  (22 confirm with times, 5 decline, 1 bare confirm).
- Residual noise: jokey messages and "see you soon" still emit
  low-confidence confirms. Reconciler is expected to dedup/filter; don't
  keep tightening the extractor.

## Step 2 — Structural detectors + Conflicts tab

Goal: turn the four sources + facts into a ranked list of specific
contradictions a human can resolve in one click each.

### Slice 1 — core + detectors 3/4/5 (shipped, 1.14.0)

`reconcile.py` is pure-function: `run(data, drift_items, today=None)`
returns `{generated_at, version, findings, counts}`. Caller (app.py's
`/reconcile/run`) supplies `drift_items` from `review_queue` to avoid a
circular import.

**Finding schema** — `{id, detector, kind, severity, booking_uid,
cleaner, date, why, evidence: [msg_ids], quote?}`. Severity tiers:
`needs-attention`, `suggest`, `informational`. Stable ids (e.g.
`contested_cleaner:<uid>:<cleaner>`) mean re-runs dedup cleanly.

Cached to `/data/reconciler_last.json` after every `/reconcile/run`.
Conflicts tab reads the cache — no recompute on page load.

### Slice 2 — Conflicts tab + detector 6 (shipped, 1.15.0)

- Third panel on `/` alongside Notify queue and Review. Renders the
  cached findings grouped by severity with one-click actions:
  `Assign <cleaner>` (for `unrecorded_confirmation` and
  `schedule_unassigned`), `Edit booking`, `Dismiss`. Re-run button
  recomputes the cache. Badge count = `needs-attention` findings.
- **Dismiss mechanism**: `POST /reconcile/dismiss` writes `{finding_id,
  reason}` into `data.dismissed_findings`. `reconcile.run()` filters
  these out before sorting and reports the count. `POST
  /reconcile/undismiss` is the inverse. Both endpoints accept JSON or
  form-encoded and trigger a re-run of the cache.
- Detector 6 (`_schedule_vs_bookings`): host `schedule_assertion` names
  a cleaner for a date where the booking is unassigned (`suggest`,
  `schedule_unassigned`) or assigned to someone else (`needs-attention`,
  `schedule_mismatch`).

### Detectors

1. **Airbnb ⇄ bookings** *(`_ical_vs_bookings`, shipped 1.16.0)* —
   `/reconcile/run` fetches the iCal feed and passes parsed
   `{uid, start, end}` rows. Emits `ical_missing_booking` (iCal UID
   local hasn't seen — sync stale), `ical_resurrected` (local cancelled
   but feed still has it), `ical_date_mismatch` (same UID, dates differ),
   `booking_not_in_ical` (active airbnb booking dropped upstream).
   Filtered to `end >= today`.
2. **Bookings ⇄ GCal** *(`_bookings_vs_gcal`, shipped 1.16.0)* —
   `/reconcile/run` calls `gcal.fetch_tagged_events` to pull every
   event tagged `source=cleaning-tracker` keyed by its private `uid`
   tag. Detector rebuilds the desired projection via
   `gcal._desired_events` (on a `_needs_notify`-annotated snapshot, to
   match what sync would produce) and diffs. Emits
   `gcal_missing_event` (needs-attention), `gcal_stale_event` (suggest
   — content diverges), `gcal_orphan` (suggest — tagged event points
   at a booking that's gone or cancelled).
3. **Commitment drift** *(`_drift`)* — `needs_notify(b)` per booking.
   Reshapes `review_queue` output into findings so the notify queue
   and Conflicts tab agree.
4. **Facts ⇄ bookings** *(`_facts_vs_bookings`)* — emits
   `confirm_no_booking`, `unrecorded_confirmation`,
   `contested_cleaner`, `decline_still_assigned`. Only considers future
   dates and `confidence >= CONFIRM_THRESHOLD` (0.85) for confirms.
5. **Fact ⇄ fact** *(`_fact_timeline`)* — a `confirm` followed by a
   later `decline` on the same (cleaner, date) emits `changed_mind`
   (informational). Latest-wins; detector 4 separately handles the
   action-worthy case via `decline_still_assigned`.
6. **Schedule ⇄ facts** *(`_schedule_vs_bookings`)* — host
   `schedule_assertion` with a cleaner named, but the booking's
   `cleaner` is unset (`schedule_unassigned`, suggest) or different
   (`schedule_mismatch`, needs-attention).

## Step 3 — Daily digest (deferred; discuss before building)

Concept from the original Full-LLM-Control sketch: once the facts layer
and detectors are solid, wire a cron to post a "here's what changed
since yesterday" digest somewhere (HA notification? email? WhatsApp
back-channel to the host only?). Unblocked now that detectors 1/2 are
live — revisit after a week or two of real traffic to see whether the
findings list is trustworthy enough to push.

## Open questions

- **Historical archive ingestion — done.** 1022 messages across Itzel,
  Daria, and two other groups are ingested (1134 facts extracted). The
  `_facts_history` window (30 messages, same-group, timestamp-sorted)
  was added after an unbounded history stalled a 952-message ingest at
  284/5h under TPM pressure. Current observed rate: ~3s/call with
  history window; ~0.8s nominal without facts.
- **Facts dedup.** Nothing currently dedupes across messages — if
  Michelle asserts "Itzel, May 19" twice, that's two facts. Detectors
  4 and 6 group by `(uid, cleaner)` / `(cleaner, target_date)` at
  emit-time via stable ids, so the same underlying issue doesn't double
  up in the findings list. A `fact_groups` view is still deferred
  until this actually hurts.
- **Cleaner name vs JID canonicalisation.** `facts.cleaner` stores a
  canonical cleaner name, not a JID. That's right for cross-source
  joining (JIDs don't appear in iCal or GCal), but means a rename of a
  cleaner in `config.yaml` strands old facts. Acceptable until it
  matters.
- **Josh-as-host signal.** Josh posts jokey / narrative messages ("Oh I
  booked this one 😂") that the v2 prompt still over-extracts. Possible
  fix: pass a `hosts: ["Michelle Groves"]` allow-list, let the prompt
  treat other host-bucket senders as "background chat". Not critical.
- **Confirm_no_booking noise.** Paste-ingested historical facts often
  reference dates where the booking has since rolled off the Airbnb
  feed. These become `informational` `confirm_no_booking` findings
  forever. Dismissible, but bulk-dismiss-by-kind would save clicks if
  the count stays high.

## Files touched

```
cleaning-tracker/
├── facts.py                    # Extractor + retry/backoff + prompt (facts-v2)
├── reconcile.py                # Pure-function detectors → findings
├── app.py
│   ├── process_message         # Live: parse + facts in parallel
│   ├── _facts_history          # 30-message same-group window for facts
│   ├── _ingest_facts_only      # Paste-ingest facts-only path
│   ├── _ingest_worker          # Background queue for paste-ingest
│   ├── _parse_whatsapp_transcript  # 3 formats: iOS, legacy, Android
│   ├── _build_conflicts_context    # Reads reconciler_last.json for the tab
│   ├── _rerun_reconcile_cached     # Helper called after dismiss/undismiss
│   ├── /admin/facts            # GET dump
│   ├── /admin/reprocess-facts  # POST re-extract stale records
│   ├── /admin/ingest-transcript  # POST paste ingest
│   ├── /admin/ingest-status    # GET progress
│   ├── /admin/ingest           # GET HTML paste form
│   ├── /admin/remap-group      # POST bulk group JID + label rewrite
│   ├── /reconcile/run          # POST recompute + persist cache
│   ├── /reconcile/last         # GET cached JSON
│   ├── /reconcile/dismiss      # POST add to dismissed_findings
│   └── /reconcile/undismiss    # POST remove from dismissed_findings
└── config.yaml                 # current version lives here
```

## Operational playbook

- **After a prompt change**: bump `FACTS_PROMPT_VERSION` in `facts.py`,
  deploy, then `curl -X POST -H "X-Shared-Secret: …" http://<ha-ip>:5000/admin/reprocess-facts`.
- **After a bulk paste**: check `GET /admin/ingest-status` for progress
  and `last_error`. Then `GET /admin/facts` (or `GET /internal/snapshot`
  for the full data bundle) to inspect.
- **Shared secret lives in** `sidecar/whatsapp-bridge/.env` as
  `SHARED_SECRET`, mirrored to the add-on option `whatsapp_shared_secret`.
