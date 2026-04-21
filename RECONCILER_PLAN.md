# Reconciler — LLM-driven conflict detection across sources

> **Status (2026-04-21, add-on 1.13.0):** Step 1 (versioned facts extraction)
> is shipped and validated against real traffic. Step 2 (structural
> detectors + Conflicts tab) is the next concrete work.

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

## Step 2 — Structural detectors + Conflicts tab (next)

Goal: turn the four sources + facts into a ranked list of specific
contradictions a human can resolve in one click each.

**Data plane**:

- A pull step that snapshots Airbnb iCal, GCal iCal, `data.json`, and
  `data.message_facts` into a single in-memory bundle. Reuse
  `scripts/reconcile_pull.py` for the external off-host variant; add a
  loopback-only `/reconcile/pull` that produces the same structure
  in-process so the detector can run inside the add-on without a second
  round-trip.
- Persist the latest detector run to `/data/reconciler_last.json` so the
  Conflicts tab can render without recomputing. Stamp with
  `{generated_at, prompt_version, inputs_hash}`. Re-run on demand
  (`POST /reconcile/run`) and on a daily schedule.

**Detectors** (deterministic — no LLM; facts are the LLM output, detectors
are plain joins):

1. **Airbnb ⇄ bookings** — `VEVENT Reserved` present in one but not the
   other. Already partially covered by iCal sync's sweep; lift the logic
   into a reusable detector and emit one finding per UID.
2. **Bookings ⇄ GCal** — every active booking with a cleaner should
   project into GCal. Missing projection or stale title is a finding.
   `gcal.py::sync_to_gcal` already converges; the detector should flag
   when the last sync reported errors or was skipped.
3. **Commitment drift** — `needs_notify(b)` per booking. Already shipped
   in the notify queue; the reconciler exposes the same signal as a
   finding so it shows up in the one unified list.
4. **Facts ⇄ bookings** — for each (target_date, cleaner) in `confirm`
   facts, find the booking. If confidence ≥ 0.85, known cleaner, known
   booking, and no existing commitment → this is an auto-apply candidate
   the live pipeline missed (or historical paste-ingest found). Surface
   as a "suggest applying" finding. For `decline` facts that match a
   booking's current cleaner → "cleaner said no but is still assigned".
5. **Fact ⇄ fact** — a `confirm` followed by a later `decline` on the
   same (cleaner, date) means the cleaner changed their mind. The
   latest fact wins; surface if the booking still reflects the earlier
   state.
6. **Schedule ⇄ facts** — host `schedule_assertion` with a cleaner
   named, but the booking's `cleaner` is unset or different → "host
   said X, booking says Y".

**Conflicts tab UI**:

- Third panel on `/` alongside Notify queue and Review.
- Findings grouped by severity: `needs-attention` (cleaner said no,
  missing projection, Airbnb drift), `suggest` (auto-apply candidate
  from historical facts), `informational` (fact-fact timeline notes).
- Each finding has a primary action ("Apply Itzel to 2026-05-19 11:00",
  "Dismiss — I handled this in WhatsApp", "Re-sync GCal"). Actions
  mutate `data.json` / GCal directly and mark the finding resolved in
  the next `/reconcile/run`.

## Step 3 — Daily digest (deferred; discuss before building)

Concept from the original Full-LLM-Control sketch: once the facts layer
and detectors are solid, wire a cron to post a "here's what changed
since yesterday" digest somewhere (HA notification? email? WhatsApp
back-channel to the host only?). Out of scope until Step 2 is trusted.

## Open questions

- **First-pass ingestion of the full historical WhatsApp archive.** User
  has a backlog beyond the 33 messages already tested. Paste-ingest via
  `/admin/ingest` handles any size; pace at 0.8s/call means ~45 min per
  3000 messages. Acceptable. Reprocess cost is similar.
- **Facts dedup.** Nothing currently dedupes across messages — if
  Michelle asserts "Itzel, May 19" twice, that's two facts. The
  reconciler is expected to group facts by `(cleaner, target_date)`
  and pick the latest. If this turns out to be painful, consider a
  `fact_groups` materialized view.
- **Cleaner name vs JID canonicalisation.** `facts.cleaner` stores a
  canonical cleaner name, not a JID. That's right for cross-source
  joining (JIDs don't appear in iCal or GCal), but means a rename of a
  cleaner in `config.yaml` strands old facts. Acceptable until it
  matters.
- **Josh-as-host signal.** Josh posts jokey / narrative messages ("Oh I
  booked this one 😂") that the v2 prompt still over-extracts. Possible
  fix: pass a `hosts: ["Michelle Groves"]` allow-list, let the prompt
  treat other host-bucket senders as "background chat". Not critical.

## Files touched by the facts layer

```
cleaning-tracker/
├── facts.py                    # Extractor + retry/backoff + prompt (facts-v2)
├── app.py
│   ├── process_message         # Live: parse + facts in parallel
│   ├── _ingest_facts_only      # Paste-ingest facts-only path
│   ├── _ingest_worker          # Background queue for paste-ingest
│   ├── _parse_whatsapp_transcript  # Handles [YYYY-MM-DD,...] and [H:MM AM/PM, M/D/YYYY]
│   ├── /admin/facts            # GET dump
│   ├── /admin/reprocess-facts  # POST re-extract stale records
│   ├── /admin/ingest-transcript  # POST paste ingest
│   ├── /admin/ingest-status    # GET progress
│   └── /admin/ingest           # GET HTML paste form
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
