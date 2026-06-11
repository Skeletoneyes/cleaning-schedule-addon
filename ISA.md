---
title: Cleaning Schedule Tracker — Project ISA
slug: cleaning-schedule-addon
type: project
effort: E3
phase: execute
updated: 2026-06-11T09:30:00-07:00
progress: 9/12
---

# Cleaning Schedule Tracker — Project ISA

Long-lived system of record for the HA add-on that tracks Airbnb cleaning
schedules, projects them to a shared Google Calendar, and uses Claude Haiku to
interpret WhatsApp coordination with cleaners. Operational detail lives in
`CLAUDE.md`; this file holds the ideal state, the criteria for "working," and
the error-correction trail.

## Problem

Airbnb turnovers need a cleaner assigned to every checkout, the cleaner has to
be told, and changes (cancellations, reassignments, ad-hoc swaps) happen over
WhatsApp in two languages and three group chats. Before this system, the
schedule lived in someone's head and a paper print; drift between "who's
actually booked," "who was told," and "who agreed" was invisible until a
cleaner showed up to a wrong door — or didn't show at all. The LLM and calendar
layers add a second failure surface: when they break, they break *silently*.

## Vision

The host opens one page and sees exactly which cleaners are out of sync with
reality, and a shared calendar everyone trusts. WhatsApp chatter is read by the
machine, not just humans — every confirm, decline, and schedule change becomes a
fact the reconciler can cross-check, so conflicts surface the same day they're
created, not the morning a clean is missed. When a dependency fails (no credits,
calendar outage), the system says so out loud and self-heals when the dependency
returns.

## Out of Scope

Two-way calendar editing (cleaners confirm via WhatsApp, never by editing GCal).
Auto-sending WhatsApp messages (the bridge is read-only; the host still presses
send). Multi-property generalization beyond the current upstairs/downstairs
units. Replacing Michelle's `/print` view. A real database — `data.json` is the
source of truth by design.

## Principles

- **Fail loudly, never silently.** A broken dependency must produce a signal a
  human sees, not a row stuck in `pending`. Silent degradation is the worst
  outcome because it hides the schedule drift the whole system exists to catch.
- **The add-on is the brain; GCal is the shared view.** `data.json` is truth;
  the calendar is a one-way projection. Never let the view become a second
  source of truth.
- **Reads can be cheap and wrong-tolerant; writes to bookings must be
  deliberate.** Auto-applying an LLM judgment to a booking can move a real
  person to the wrong house — that bar is higher than surfacing a finding.

## Constraints

- HAOS host has **no `python3`, no `docker`** in PATH; parse `--raw-json`
  locally. The Terminal & SSH add-on is sandboxed (can't see other add-ons'
  `/data`).
- The add-on `/data` volume is private — no `map:` — so it can only be written
  from inside the add-on (`POST /internal/restore`), never over SSH/Samba.
- Add-on store/install endpoints **401 the external long-lived token**; lifecycle
  ops must go through the SSH `ha` CLI or the Supervisor API from inside the host.
- Anthropic is a hard external dependency for parse + facts; the system must
  degrade gracefully (alert + defer + recover) when it's unavailable.
- Supervisor `addons` CLI namespace is deprecated → use `apps` (cosmetic warning
  for now; commands still work).

## Goal

A self-checking cleaning scheduler whose calendar projection is dedup-clean, whose
reconciler runs on a schedule and surfaces every cleaner conflict the day it
appears, and whose LLM dependency fails loudly and recovers automatically — so the
host never silently loses a same-day schedule change again.

## Criteria

- [x] ISC-1: GCal projection contains zero duplicate events (same uid tag → one event); `gcal.py` dedup converges. *(verified 2026-06-11: 92 events, 92 distinct.)*
- [x] ISC-2: No two Airbnb stay intervals overlap in time (contiguous turnovers, not double-bookings). *(verified: 46 stays, 0 real overlaps.)*
- [ ] ISC-3: Zero `gcal_stale_event` findings from the reconciler (sync fully converged). *(2 stale as of 2026-06-11 — Dec/Jan unassigned booking.)*
- [x] ISC-4: Reconciler runs automatically daily; `digest_enabled: true` and scheduler thread starts on boot. *(verified: `[digest] scheduler started — daily at 08:00`.)*
- [x] ISC-5: A cleaner assigned to a date that another cleaner confirmed surfaces as a `contested_cleaner` finding within one reconcile cycle. *(verified: Itzel-vs-Daria 2026-06-12 flagged.)*
- [x] ISC-6: An Anthropic out-of-credit 400 posts an HA notification and does NOT bury the message as a silent `pending` parse_error. *(shipped 1.19.0; detector unit-tested.)*
- [x] ISC-7: Deferred messages auto-reprocess once credits return, with a "restored" notification — no manual reprocess needed. *(shipped 1.19.0 recovery probe.)*
- [ ] ISC-7.1: Anti: the credit-recovery probe must not hot-loop or bill tokens while exhausted (max_tokens=1 probe, ≥10-min interval, 400s are pre-billing). *(code review only; not yet observed firing live.)*
- [x] ISC-8: Reprocessing stuck messages uses the **facts-only** path (`/admin/reprocess-facts`), never live `process_message`, so it cannot auto-reassign a booking. *(verified this session: 4 reprocessed, Daria booking untouched.)*
- [x] ISC-9: Anti: a transcript backfill must not silently run at double Haiku cost — `apply=true` requires explicit confirmation stating the cost; unconfirmed apply is a no-op. *(verified 2026-06-11: 1.20.0 cost gate — apply without confirm → 409, inserts nothing, no tokens.)*
- [x] ISC-10: Live snapshot is pullable off-host via `/internal/snapshot` + `X-Shared-Secret` for diagnosis without disturbing the running add-on. *(verified: 913KB HTTP 200.)*
- [ ] ISC-11: A credit/health outage is visible on the home page (banner), not only as a transient notification. *(deferred — notification-only in 1.19.0.)*

## Test Strategy

| isc | type | check | threshold | tool |
|-----|------|-------|-----------|------|
| ISC-1 | invariant | count VEVENTs vs distinct (dtstart+summary) in live ical feed | equal | curl + awk |
| ISC-2 | invariant | sort stay intervals; assert no end > next start | 0 overlaps | python/awk |
| ISC-3 | finding-count | `/reconcile/last` → count `gcal_stale_event` | 0 | curl + jq |
| ISC-4 | boot-log | grep startup log for `scheduler started` + option `digest_enabled` | present/true | ssh ha logs + supervisor info |
| ISC-5 | behavior | inject confirm fact vs differing booking cleaner; reconcile | finding emitted | curl POST /reconcile/run |
| ISC-6/7 | behavior | simulate 400 low-balance; assert notification + deferral + requeue | all three | unit + live (next outage) |
| ISC-8 | safety | reprocess 4 stuck msgs; assert booking cleaner unchanged | unchanged | snapshot diff |
| ISC-9 | default | GET /admin/ingest; assert apply checkbox unchecked | unchecked | curl + grep |
| ISC-10 | reachability | GET /internal/snapshot with secret | HTTP 200 | curl |

## Features

| name | description | satisfies | depends_on | parallelizable |
|------|-------------|-----------|------------|----------------|
| gcal-projection | one-way data.json→GCal with dedup + drift colorId | ISC-1, ISC-2, ISC-3 | — | true |
| reconciler-schedule | digest scheduler runs full reconcile daily 08:00 | ISC-4, ISC-5 | — | true |
| credit-circuit-breaker | detect 400 low-balance → notify + defer + probe-recover | ISC-6, ISC-7, ISC-7.1 | — | true |
| safe-reprocess | facts-only reprocess path for stuck/errored messages | ISC-8 | — | true |
| ingest-cost-guard | keep backfill facts-only by default; guard apply=true | ISC-9 | — | true |
| ops-access | off-host snapshot pull + Supervisor lifecycle via SSH | ISC-10 | — | true |

## Decisions

- 2026-06-11 09:00: Root-caused the "two cleaners Friday" report — it is **intentional, not a bug**: Daria cleans the downstairs bnb (tracked booking), Itzel cleans the untracked upstairs unit at noon after the guest cancelled her Sunday checkout. The system only tracks the downstairs iCal, so Itzel-upstairs reads as `contested_cleaner` / `confirm_no_booking`. Resolution path: add an upstairs `manual_cleaning` booking, or dismiss the finding.
- 2026-06-11 09:02: Enabled `digest_enabled: true` (was default-off) + restarted. This is what "schedule the reconciler" means — the digest wraps `_run_full_reconcile()`. Set via a **merged** Supervisor options POST.
- 2026-06-11 09:05: Shipped 1.19.0 credit-exhaustion circuit breaker (alert + defer + auto-recovery probe). Scoped the commit to `app.py` + `config.yaml` only; the repo had unrelated pre-existing working-tree changes that must NOT be swept into the commit.
- 2026-06-11 09:06: Chose **facts-only** reprocess (`/admin/reprocess-facts`) over `/admin/fix-parse-errors` for the 4 stuck messages, because the latter runs live `process_message` and the Itzel JID is mapped → a ≥0.85 parse could have auto-reassigned Daria's booking. Facts-only surfaces the conflict for human review without mutating bookings.
- 2026-06-11 09:06: ❌ DEAD END: Supervisor options POST is NOT safe with a partial body assumption — sending only `{digest_enabled:true}` risks wiping the api key / iCal / GCal config. Always fetch full `.data.options`, merge, and POST the complete set.
- 2026-06-11 09:06: ❌ DEAD END: Windows `python` can't see git-bash `/tmp` paths; `curl -o /tmp/x` (git-bash) then `python open('/tmp/x')` fails. Use awk/jq in the same shell, or a Windows-visible path.
- 2026-06-11 09:30: Built ISC-9 (1.20.0): chose a server-side **cost-gate interstitial** over a better default or client-side JS confirm. The route counts new messages without inserting and returns 409 `needs_confirmation` (JSON) / a red HTML confirm page (form) stating exact Haiku-call cost; processing needs `confirm_apply=1`. A default-off checkbox couldn't stop a deliberate-but-mistaken tick — the cost has to be shown at the moment of the click.

## Changelog

- 2026-06-11 | conjectured: the cleaning app burned "a ton of Haiku tokens today."
  refuted by: live data shows only ~18 messages processed today (7 ingest + 11 live); the genuinely heavy days were Apr 21 (737 facts) and May 4 (285); today's 4 evening messages 400'd with "credit balance is too low."
  learned: the symptom was credit *exhaustion*, not a same-day spike — the balance was drained over time and today's normal traffic crossed zero. 400s are rejected pre-billing, so they don't even add token cost.
  criterion now: ISC-6 (out-of-credit must alert, not fail silently) added and shipped.

- 2026-06-11 | conjectured: the reconciler runs automatically, so contested-cleaner conflicts get caught.
  refuted by: `/reconcile/last` was stamped 2026-05-23, three weeks stale; `digest_enabled` was `false` (its default) so the scheduler thread never started.
  learned: the daily digest IS the reconciler schedule, and it is opt-in. A shipped detector is worthless if nothing runs it.
  criterion now: ISC-4 (reconciler runs daily, digest_enabled true, scheduler boots) added and verified.

- 2026-06-11 | conjectured: GCal has overlapping/duplicate bookings (a sync bug).
  refuted by: 92 events all distinct (dedup works); 46 stay intervals with zero real time-overlap — turnovers are contiguous (stay→clean→next stay on the shared checkout day).
  learned: the "overlap" is visual stacking of two-events-per-booking on a near-fully-booked month grid, not a data defect. Only real issue: 2 stale future events.
  criterion now: ISC-1/ISC-2 reframed as invariants (dedup + no-overlap) that already hold; ISC-3 tracks the residual stale events.

- 2026-06-11 | conjectured: a transcript backfill is a cheap facts-only catch-up.
  refuted by: today's 10:07 ingest ran with the Apply box ticked → full `process_message` (parse + facts = 2 calls/msg) and routed lines into the live review queue.
  learned: `apply=true` is for future bulk adds, not historical backfill; it doubles cost and pollutes review. The form already defaults OFF — the failure mode is a manual tick, so a default fix alone is insufficient.
  criterion now: ISC-9 (anti: backfill must not silently double-spend) added; needs a confirm/guard, not just a default.

## Verification

- ISC-1: invariant — `curl <gcal ical>` → `BEGIN:VEVENT` count 92; distinct (dtstart+summary) 92. No duplicates.
- ISC-2: invariant — 46 Airbnb stay intervals sorted; assertion `end > next_start` matched 0 times. "checked 46 stays" / "real overlaps: 0".
- ISC-4: boot-log — `[digest] scheduler started — daily at 08:00`; Supervisor info `digest_enabled: true`, `version: 1.19.0`, `state: started`.
- ISC-5: behavior — fresh `/reconcile/run` after facts reprocess emitted `[needs-attention] contested_cleaner date=2026-06-12 cleaner=Itzel :: Itzel confirmed for 2026-06-12 but booking is assigned to Daria`.
- ISC-6: unit — `_is_low_balance_error` matched the real 400 string and rejected 429 / parse-fail / network / none (ALL PASS); module imported on server with no traceback.
- ISC-7: code — `_credit_recovery_loop` requeues deferred ids and posts "credits restored" (reuses proven `_post_ha_notification` / `enqueue_message`). Not yet observed firing live.
- ISC-8: safety — `/admin/reprocess-facts` → `{"stale":4,"extracted":4,"errors":0}`; June 12 booking cleaner still `Daria` post-reprocess.
- ISC-9: behavior — live POST `apply=1` without `confirm_apply` → `HTTP 409` `{"needs_confirmation":true,"new_messages":1,"haiku_calls":2,...}`; message count `1053→1053` (inserted nothing, no tokens). Confirmed path gated behind `confirm_apply=1`.
- ISC-10: reachability — `GET /internal/snapshot` + `X-Shared-Secret` → HTTP 200, 913KB.
