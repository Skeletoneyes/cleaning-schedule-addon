---
title: Cleaning Schedule Tracker — Project ISA
slug: cleaning-schedule-addon
type: project
effort: E3
phase: execute
updated: 2026-07-24T12:00:00-07:00
progress: 13/18
---

# Cleaning Schedule Tracker — Project ISA

Long-lived system of record for the HA add-on that tracks Airbnb cleaning
schedules, projects them to a shared Google Calendar, and uses Claude Sonnet
(`claude-sonnet-5`, upgraded from Haiku in 1.21.0) to interpret WhatsApp
coordination with cleaners. Operational detail lives in
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
- [x] ISC-12: The WhatsApp Bridge live-forwards group messages despite the benign Baileys `init queries` 408 — `fireInitQueries: false` skips the failing `fetchProps` init query the read-only bridge never needed. *(verified 2026-06-21: bridge 1.0.6 — `init queries` count 0 at 95s past connect, well beyond the 60s query-timeout window; clean `backfill complete → live mode`.)*
- [ ] ISC-13: Both cleaning groups — Itzel (`120363285451054712@g.us`) and Daria (`120363410469116316@g.us`) — are in the bridge `group_allowlist` and live-forward to the tracker. *(allowlist verified 2026-06-21; Itzel live-forward proven; Daria forward not yet live-tested — follow-up: send a Daria-group test message.)*
- [x] ISC-14: The daily digest posts an HA persistent notification — the cleaning-tracker add-on is granted `homeassistant_api: true`. *(verified 2026-06-21: 1.20.1, `/digest/run` → `"notified":true`; was `notified:false` because the Supervisor rejected Core-API calls without the grant — which also silently broke the ISC-6/7 notification leg.)*
- [ ] ISC-15: WhatsApp "going dark" is caught by **absence** detection, not just error-burst alarms. A group with history that stops forwarding for `dead_channel_days` surfaces a `channel_silent` needs-attention finding; zero messages from ANY group for `bridge_silent_days` surfaces a singleton `bridge_silent`. Both are dated today (survive the STALE_DAYS filter), stable-ided (alarm once via the digest diff, not daily), and ride the existing daily-digest HA notification. *(shipped 1.22.0; detector + parse/aggregation logic unit-verified — 8 detector cases + 7 timestamp-format cases pass; **pending live confirmation**: a real digest run showing the finding, and recovery clearing it. This is the gap the 1.1.0 error-burst alarms structurally could not cover — the exact Daria 3-month-mute failure mode.)*

- [x] ISC-16: The home-page footer shows an ambient VPS health dot fed only by container-collectable "ping" signals (TCP-connect reachability + latency + HTTP status), the target host is a config option (never hardcoded — public repo), and the widget is hidden when unconfigured. *(shipped 1.23.0 + deployed + configured; `_vps_ping` verified against real/down/edge hosts; **in-browser confirmed 2026-07-24** — Chromium on the live add-on rendered a green dot reading "VPS online · 75ms · HTTP 401", zero console errors. Scope limit recorded: pings the box's web surface only — a crashed bot on an up box won't show red.)*

- [x] ISC-17: A health alarm fires only for groups the bridge actually forwards. Decrypt failures in non-allowlisted groups are logged (Log tab) but never escalated to an HA persistent notification; failures with no parseable `remoteJid` still alarm (fail-loud on the unattributable case). Alarm text names checking before re-pairing, so a notification can never be the sole trigger for a destructive action. *(shipped 1.2.0; 5 predicate cases pass against the real log line + live allowlist; **live-proven in production 2026-07-24** — within seconds of deploy, two real `MessageCounterError` events in a non-allowlisted chat logged `decrypt failure in non-allowlisted group — not alarming` and raised nothing.)*

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
| ISC-15 | absence-detect | unit: 8 detector cases (whole-bridge dark, per-group Daria-dark, min-msgs suppress, healthy→∅, disabled→∅, never→'ever', run() survives stale-filter, dismiss by id) + 7 timestamp-format cases | all pass | python + live digest |
| ISC-16 | render | load home page in Chromium; assert `#vps-widget` visible + dot class `up` + latency text | green dot shown | playwright + curl /vps/status |
| ISC-17 | precision | 5 predicate cases (real non-allowlisted line detected-but-silent, same failure in cleaner group alarms, no-JID alarms, non-decrypt line uncounted) using the regexes read out of `index.js`; then grep journald for `not alarming` on real traffic | all pass + live hit | node + ssh ha host logs |

## Features

| name | description | satisfies | depends_on | parallelizable |
|------|-------------|-----------|------------|----------------|
| gcal-projection | one-way data.json→GCal with dedup + drift colorId | ISC-1, ISC-2, ISC-3 | — | true |
| reconciler-schedule | digest scheduler runs full reconcile daily 08:00 | ISC-4, ISC-5 | — | true |
| credit-circuit-breaker | detect 400 low-balance → notify + defer + probe-recover | ISC-6, ISC-7, ISC-7.1 | — | true |
| safe-reprocess | facts-only reprocess path for stuck/errored messages | ISC-8 | — | true |
| ingest-cost-guard | keep backfill facts-only by default; guard apply=true | ISC-9 | — | true |
| ops-access | off-host snapshot pull + Supervisor lifecycle via SSH | ISC-10 | — | true |
| channel-silence | absence-of-signal WhatsApp dark-channel detection (`_channel_silence` → bridge_silent + channel_silent) | ISC-15 | — | true |
| alarm-scoping | bridge health alarms escalate only for allowlisted groups (`noteDecryptFailure`); out-of-scope failures log, unattributable ones still alarm | ISC-17 | — | true |
| vps-status-widget | footer VPS health dot from ping-only signals (TCP-connect + HTTP HEAD, host configurable) | ISC-16 | — | true |

## Decisions

- 2026-06-11 09:00: Root-caused the "two cleaners Friday" report — it is **intentional, not a bug**: Daria cleans the downstairs bnb (tracked booking), Itzel cleans the untracked upstairs unit at noon after the guest cancelled her Sunday checkout. The system only tracks the downstairs iCal, so Itzel-upstairs reads as `contested_cleaner` / `confirm_no_booking`. Resolution path: add an upstairs `manual_cleaning` booking, or dismiss the finding.
- 2026-06-11 09:02: Enabled `digest_enabled: true` (was default-off) + restarted. This is what "schedule the reconciler" means — the digest wraps `_run_full_reconcile()`. Set via a **merged** Supervisor options POST.
- 2026-06-11 09:05: Shipped 1.19.0 credit-exhaustion circuit breaker (alert + defer + auto-recovery probe). Scoped the commit to `app.py` + `config.yaml` only; the repo had unrelated pre-existing working-tree changes that must NOT be swept into the commit.
- 2026-06-11 09:06: Chose **facts-only** reprocess (`/admin/reprocess-facts`) over `/admin/fix-parse-errors` for the 4 stuck messages, because the latter runs live `process_message` and the Itzel JID is mapped → a ≥0.85 parse could have auto-reassigned Daria's booking. Facts-only surfaces the conflict for human review without mutating bookings.
- 2026-06-11 09:06: ❌ DEAD END: Supervisor options POST is NOT safe with a partial body assumption — sending only `{digest_enabled:true}` risks wiping the api key / iCal / GCal config. Always fetch full `.data.options`, merge, and POST the complete set.
- 2026-06-11 09:06: ❌ DEAD END: Windows `python` can't see git-bash `/tmp` paths; `curl -o /tmp/x` (git-bash) then `python open('/tmp/x')` fails. Use awk/jq in the same shell, or a Windows-visible path.
- 2026-06-11 09:30: Built ISC-9 (1.20.0): chose a server-side **cost-gate interstitial** over a better default or client-side JS confirm. The route counts new messages without inserting and returns 409 `needs_confirmation` (JSON) / a red HTML confirm page (form) stating exact Haiku-call cost; processing needs `confirm_apply=1`. A default-off checkbox couldn't stop a deliberate-but-mistaken tick — the cost has to be shown at the moment of the click.
- 2026-06-21 14:00: WhatsApp Bridge looked "dead" (no inbound since 06-20 14:45, WhatsApp "last active yesterday"). It was NOT dead — live forwarding is push-based and fine; the Itzel group was just quiet. Proven by a live test message forwarding in ~1 min. Lesson: zero inbound ≠ zero health; verify liveness with a live test, not by inferring from log gaps or the lagging "last active" field.
- 2026-06-21 14:30: The Daria group was invisible to the bridge — its participating-groups enumeration is stale on cached-auth reconnects. Fix = fresh QR re-pair (forces full app-state resync), which surfaced Daria (`120363410469116316@g.us`); added it to `group_allowlist`. Re-pair needed wiping `/data/auth`, which the sandboxed SSH add-on can't reach → did it via add-on **uninstall + reinstall** (wipes `/data`), then restored options + shared_secret.
- 2026-06-21 15:00: Bumped bridge Baileys 6.7.21 → 6.7.23 (latest stable 6.x = `legacy` dist-tag). Did NOT take 7.0.0-rc13 (the `latest` tag) — a pre-release pulling native `whatsapp-rust-bridge`/`libsignal` that risks the aarch64 Docker build.
- 2026-06-21 15:10: ❌ DEAD END: regenerating the bridge `package-lock.json` re-introduces `git+ssh://` for `libsignal-node`, breaking `npm ci` in Docker (no SSH keys). Must rewrite the resolved URL to `git+https://` (keep the commit hash) after any lockfile regen.
- 2026-06-21 16:00: ❌ DEAD END / GOTCHA: don't declare the `init queries` 408 fixed from an early log read — it fires ~46s after connect (Baileys `defaultQueryTimeoutMs` = 60s). A +60s check once read 0 and was wrong; wait past the 60s window before claiming it's gone.
- 2026-06-21 16:30: Cleared the 607-message review queue: 598 were one-time transcript-**backfill** chatter (not live), 9 were stale live items from before the LID senders were mapped. Bulk-set to `ignored` via the `/internal/snapshot` → edit → `/internal/restore` round-trip (the only bulk-write path since `/data` is private; restore backs up to `data.json.bak`). LID→cleaner/host mappings were already correct (192…→Itzel, 162…/697…→host).
- 2026-07-24 12:00: GOTCHA: the documented session-corruption fix ("re-pair fresh": uninstall + reinstall to wipe `/data/auth`) is correct but had no **precondition**. Applied on the strength of an alarm alone it costs the auth state to fix nothing — today's alarm was raised by an unrelated personal group. Before re-pairing, confirm the failures are (a) still arriving and (b) in an allowlisted group; `ha addons logs` truncates to ~100 lines and the on-connect group listing floods it, so read journald (`ha host logs --identifier addon_27cbea7f_whatsapp-bridge --lines 400`). Note the observed failures are all `fromMe: true` linked-device echoes scattered across personal groups — benign for cleaning data, now correctly silent, but an ongoing pattern rather than a one-off.
- 2026-06-21 16:45: GOTCHA: `host_jids` only suppresses the "unmapped sender" prompt — it does NOT remove a host sender's messages from the pending list (`_build_review_context` includes all `pending` regardless). Host chatter keeps queuing; a real fix would skip/auto-ignore `host_jids` senders in the inbound path.

## Changelog

- 2026-07-24 | conjectured: the 1.1.0 error-burst alarms were merely *insufficient* (they miss silence — the ISC-15 finding), but what they did fire on was real.
  refuted by: a live `decrypt` alarm naming the cleaning channel and the Daria 3-month mute, raised by a decrypt failure in **"Property Bros"** — a personal group the bridge does not forward. The counter intercepted every Baileys decrypt error on the whole account (~60 groups); only 2 are allowlisted. Forwarding respected `group_allowlist` (`messages.upsert`, index.js), the alarm never did. So the 2026-07-23 entry diagnosed the alarms' recall gap and left an unexamined precision gap directly beneath it.
  learned: an alarm's scope must match the scope of the thing it protects, or it degrades the channel every *other* alarm rides — ISC-15's silence findings, ISC-6/7's credit alerts and ISC-14's digest all arrive as HA persistent notifications, and a channel that cries wolf about cleaning is one you learn to swipe away. Corollary that nearly bit: the alarm text embedded its own remediation ("stop the add-on, reinstall it to wipe /data/auth, re-scan the QR") — a destructive, auth-losing action recommended by a **false** alarm. Alert copy is not a command; a notification must never be the sole trigger for a destructive step.
  criterion now: ISC-17 (alarm precision — escalate only for allowlisted groups, stay loud on unattributable failures, verify-before-re-pair copy) added; shipped 1.2.0 and live-proven the same day.

- 2026-07-24 | conjectured: a footer "VPS status" widget can just ping the box and show up/down.
  refuted by: naively, no — (1) raw ICMP isn't available in the add-on container, so a literal ping fails; (2) hardcoding the VPS host would leak personal infra into the public GitHub repo; (3) the hub is Basic-Auth, so a 200 check would read as "down" on a healthy box.
  learned: the container-safe "ping" is a TCP connect to host:443 (+ HTTP HEAD where any response, incl. 401, means up); the host must be a config option like `ical_url`; and the honest scope is the box's *web surface* — a crashed Telegram bot on an up box can't be pinged, so the widget can't reflect it. Ungated `/vps/status` to match the already-ungated LAN-reachable home page (it returns strictly less).
  criterion now: ISC-16 (footer VPS health dot from ping-only signals, host configurable, hidden when unset) added; shipped 1.23.0.

- 2026-07-23 | conjectured: the 1.1.0 bridge health alarms (decrypt/disconnect/forward counters) closed the WhatsApp "going dark" hole.
  refuted by: all three alarms are **presence-of-error, volume-thresholded** (≥5 decrypt fails / 10 min, etc.). The failure that actually muted Daria for 3 months was **absence of signal** — one group's messages stopped arriving with either a slow trickle of decrypt errors (never 5-in-10-min) or none at all (pure absence). An error counter cannot detect silence, and the bridge cannot reliably alarm on its own death.
  learned: the missing detector is a positive-liveness check, and it belongs in the **always-running tracker** (which has the message log + runs the daily digest independent of the bridge), not in the bridge. Adding it as a reconciler *finding* means it rides the existing digest→HA-notification path for free. Two traps avoided: (1) a silence finding dated at the last-message date is auto-suppressed by the STALE_DAYS filter — it must be dated today; (2) a per-run-changing id would re-alarm every morning — the id must be stable so the digest diff fires once.
  criterion now: ISC-15 (channel-silence via `_channel_silence`, `bridge_silent` + `channel_silent`) added; shipped 1.22.0, logic unit-verified, pending live digest confirmation.


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

- 2026-06-21 | conjectured: the WhatsApp Bridge's recurring `init queries Timed Out` (408 in `fetchProps`) was Baileys version drift, fixable by updating the package.
  refuted by: bumping 6.7.21 → 6.7.23 (latest stable 6.x) — the 408 still fired ~46s after every connect with an identical stack.
  learned: it's a non-essential init query (server props/feature-flags) the read-only bridge never uses, and it's benign for live forwarding (push-based). `fireInitQueries: false` skips it entirely; on-demand API history backfill is the only thing the query path would have helped, and that's unreliable on linked devices anyway.
  criterion now: ISC-12 added (bridge live-forwards with the 408 suppressed via `fireInitQueries:false`) — verified on bridge 1.0.6.

- 2026-06-21 | conjectured: the credit-breaker (ISC-6/7) and digest HA notifications work — the code calls `_post_ha_notification` and the logic is unit-tested.
  refuted by: the digest Conflicts tab showed "✗ HA notification failed"; a live `/digest/run` returned `notified:false`; the add-on `config.yaml` never granted `homeassistant_api`, so the Supervisor rejected every `POST /core/api/...`.
  learned: ISC-6/7's notification *leg* was structurally broken all along — the unit tests verified error *detection*, never delivery. No add-on can reach the Core API without `homeassistant_api: true`.
  criterion now: ISC-14 added (digest posts an HA persistent notification; `homeassistant_api` granted) — verified on 1.20.1 (`notified:true`); retroactively un-breaks the ISC-6/7 notification path.

- 2026-06-21 | conjectured: a larger backfill window would let the bridge pull missed history (e.g. a cancellation) through the API.
  refuted by: a 7-day window hung backfill entirely (history fetch uses the same query path the 408 breaks), and Baileys docs/issues confirm WhatsApp silently drops on-demand `fetchMessageHistory` for companion/linked devices.
  learned: API history backfill is unreliable on linked devices by design; the transcript-ingest paste route is the dependable backfill path (which is why the add-on already ships it).
  criterion now: no new ISC — reaffirms the existing transcript-ingest design; bridge backfill window reverted to the known-good 15s.

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
- ISC-12: boot-log — bridge 1.0.6, 95s past connect: `grep -c "init queries"` = 0; only logged event `backfill complete — switching to live mode`. (On 1.0.5 the same window always produced the 408 at ~46s.)
- ISC-14: behavior — cleaning-tracker 1.20.1: `ha addons info` shows `homeassistant_api: true`; `POST /digest/run` → `{"notified":true,...}` (was `notified:false` pre-grant).
- ISC-15: unit (2026-07-23) — `reconcile._channel_silence` 8/8 cases pass (whole-bridge dark → single `bridge_silent` dated today; per-group Daria-dark → `channel_silent` w/ "42 days"+label; min-msgs suppression; healthy→∅; disabled→∅; never-any→`bridge_silent` "ever"; `run()` keeps finding through `filter_and_sort`; dismiss by stable id `channel_silent:gD`). `_parse_msg_ts`+aggregation 7/7 formats (UTC-Z, naive, offset, date-only, space, garbage→None). Live: 1.22.0 deployed, clean boot, `POST /reconcile/run` HTTP 200 executed the detector — 0 silence findings (healthy; absence of `bridge_silent` proves it saw recent real messages). **Pending:** a real digest firing the notification when a channel actually goes dark.
- ISC-16: in-browser (2026-07-24) — Chromium on live add-on 1.23.0: `#vps-widget` visible, `#vps-dot` class `vps-dot up`, text "VPS online · 75ms · HTTP 401", **0 console errors** (screenshot). `GET /vps/status` → `{"enabled":true,"reachable":true,"latency_ms":75,"http_status":401,"label":"VPS"}`. Options merge preserved all 16 keys (no secret wiped). `_vps_ping` verified against real/down/edge hosts.
