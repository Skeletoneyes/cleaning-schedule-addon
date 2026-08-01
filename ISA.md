---
title: Cleaning Schedule Tracker — Project ISA
slug: cleaning-schedule-addon
type: project
effort: E5
phase: execute
updated: 2026-08-01T10:30:00-07:00
progress: 37/85
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

Two further failures, both found 2026-07-27, are about **liveness and reach**
rather than detection. (1) The world-model only refreshed on process start:
the reconciler ran daily but `sync_ical()` did not, so a deploy-free stretch
had it confidently reconciling against days-old bookings — detection without
ingestion verifies the staleness it created. (2) Findings landed in Home
Assistant persistent notifications, a surface the host visits rather than one
that reaches him. A correct finding nobody reads is indistinguishable from no
finding at all.

## Vision

The host doesn't open anything. Each morning the schedule has already
re-checked itself against the world, and if something needs him he has a
message — in plain language, naming what to do and what the system couldn't
work out on its own. If nothing needs him, there is no message, and that
silence is trustworthy because the system tells him when it has gone dark.
Behind that: a shared calendar everyone trusts, and WhatsApp chatter read by
the machine, not just humans — every confirm, decline, and schedule change
becomes a fact the reconciler can cross-check, so conflicts surface the same
day they're created, not the morning a clean is missed. When a dependency
fails (no credits, calendar outage), the system says so out loud and self-heals
when the dependency returns.

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
- **Ingest before you judge.** Any check that runs on a schedule must refresh
  its inputs on that same schedule, in that order. A detector reading a stale
  world produces confident, well-formatted wrongness — and looks healthy while
  doing it.
- **A finding is not delivered until it reaches the human.** The alert channel
  is part of the alert. Detection that lands somewhere the host doesn't visit
  is indistinguishable from no detection, and it fails in the direction that
  feels fine.
- **Silence must be earned, and must be distinguishable from death.** Quiet
  nights are the goal, so anything that stays quiet needs an independent
  liveness signal — otherwise "nothing to report" and "the pipeline stopped"
  render identically.

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
- **The app stays on the Pi; only a derived read-model may cross to the VPS**
  (Council ruling 2026-07-23, reaffirmed 2026-07-27). The VPS is deliberately
  credential-free and egress-locked with no route back to the Pi, so **the Pi
  initiates all cross-host traffic** — outbound HTTPS only, never a VPS pull.
- **What may cross:** finding id, detector, kind, severity, date, cleaner first
  name, and the generated `why` summary. Cleaner names are permitted (Josh's
  ruling 2026-07-27 — they already appear in the shared Google Calendar).
  **What may never cross:** guest names, raw WhatsApp text (`quote`/`evidence`),
  the iCal token, and any secret. Enforced by building the payload from an
  allowlist rather than deleting fields from a finding.
- Supervisor validates an options POST against the **installed** add-on
  version's schema — new option keys sent before the update lands are silently
  dropped while still returning `{"result":"ok"}`. Update first, then set
  options, then restart.

## Goal

A self-checking cleaning scheduler whose calendar projection is dedup-clean, that
nightly re-reads the world before judging it, surfaces every cleaner conflict the
day it appears, and **delivers what needs a human to where the human actually is**
— with silence reserved for genuinely clean nights and a dark pipeline announcing
itself. Its LLM dependency fails loudly and recovers automatically, so the host
never silently loses a same-day schedule change again.

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

### Nightly pipeline (2026-07-27, Council-specced: sync → sanitized push → subscription triage → Telegram)

- [x] ISC-18: The nightly scheduler calls `sync_ical()` strictly before the digest/reconcile each night (code order + live: `last_sync` advances daily without deploys). *(code: sync-then-digest in `_digest_scheduler`; boot log "daily at 08:00 (sync then digest)"; liveness self-verifying — a dead loop stops the heartbeat → dead-man alarms in ≤26h. Plus ISC-39 freshness sentinel.)*
- [x] ISC-19: The nightly sync runs even when `digest_enabled` is false (sync gated only on `ICAL_URL`; thread always started at boot).
- [x] ISC-20: A sync failure posts an HA persistent notification (code probe; reuses `_post_ha_notification`, delivery leg live-proven under ISC-14).
- [x] ISC-21: The Oct 16–18 booking is `status: cancelled` in data.json. *(live snapshot 2026-07-27 10:07.)*
- [x] ISC-22: The May 14–16 2027 iCal reservation exists as a booking. *(live snapshot: 2027-05-14→16 active.)*
- [x] ISC-23: The push payload contains ONLY allowlisted fields — built by allowlist, not deletion. *(code + live: persisted payload on VPS shows exactly the enumerated fields.)*
- [x] ISC-24: Anti: the serialized payload never contains the iCal token, secrets, WhatsApp text, or `quote`/`evidence`. *(live: VPS-persisted payload scanned — quote/evidence absent; fields enumerated at construction.)*
- [x] ISC-25: Push POSTs with `X-Push-Secret`; failure posts an HA notification. *(live: 1.24.0's first real push was 400-rejected (validator bug) and logged `[vps-push] FAILED` + notified; subsequent pushes 200.)*
- [x] ISC-26: Heartbeat fires on clean nights too. *(live: three `new:0` heartbeats received and persisted on the VPS.)*
- [x] ISC-27: Options exist in schema; disabled = clean no-op. *(1.24.0 first boot ran with push disabled, zero errors; schema gotcha: options POSTed against the old installed schema are silently dropped — re-POST after update.)*
- [x] ISC-28: Add-on 1.24.1 running on the Pi. *(supervisor info: version 1.24.1, state started, push enabled.)*
- [x] ISC-29: Localhost-only listener; valid POST → 200 + state persisted. *(live curl + state file read-back; `ss -ltnp` shows 127.0.0.1:8899 only.)*
- [x] ISC-30: Wrong/missing secret → 401, nothing persisted. *(live: 401 + rejection log; state file unchanged.)*
- [x] ISC-31: Public route via Caddy works end-to-end. *(live https curl through play.joshuamohan.com; hub vhost unaffected — 401 Basic-Auth intact.)*
- [x] ISC-32: New findings → subscription triage → Telegram message sent. *(live 17:09:54: "cleaning digest message sent" chatId 87…, no fallback warning → SDK path; service env has no API key, so billing is OAuth subscription by construction.)*
- [x] ISC-33: Clean digest sends no message. *(live: three "quiet — no new findings" log lines.)*
- [x] ISC-34: Dead-man >25h → Telegram alert; fresh digest resets. *(live: staled state 26h → restart → "dead-man alarm firing" + alert sent 17:10:49; heartbeat POST reset `received_at`, alert flag cleared.)*
- [x] ISC-35: Anti: no new write path — policy/gate/toolset/sanitize untouched; only `export` keyword added in bot.ts. *(git diff verified.)*
- [x] ISC-36: Anti: triage failure → deterministic fallback message, never silence. *(unit: SDK-fail/SDK-empty/SDK-timeout paths all send fallback — 80-test suite; fallback consumes the same allowlisted payload, so it cannot widen what crosses.)*
- [x] ISC-37: Bot restarts clean post-deploy. *(systemd active; "listener started" + "Bot started" log lines; 3 restarts, no crash-loop.)*
- [x] ISC-38: End-to-end live proof on real data. *(Pi digest run → VPS receipt logged → Telegram message delivered to Josh's phone.)*
- [x] ISC-40: Anti: an injected synthetic finding must never leave `counts` disagreeing with `findings` — a consumer keying off counts would read a stale-sync night as healthy. *(Cato cross-vendor finding, gpt-5.5; fixed 1.24.2 — counts incremented with the sentinel; live-verified push post-deploy.)*
- [x] ISC-42: Every date in the Telegram message carries its year — the schedule spans >1 year ahead, so a bare "Jul 28" cannot be read. *(Josh caught this on the first real message, 2026-07-27. Root cause: the triage prompt's own worked example omitted years ("Jul 28, Jul 30, Aug 2") and the model reproduced the example, not the data — the findings JSON always carried full ISO dates. Fixed + live-verified: "4 unassigned bookings: Sep 8, Sep 10, Sep 18, Sep 20 (2026)". The sent message body is now logged so rendered output is inspectable — the bug was invisible in the data and only existed in the rendering.)*
- [ ] ISC-41: Anti: no finding's `why` value may carry raw WhatsApp text, guest names, or evidence content across to the VPS. *(Cato blind-spot: field allowlists create false confidence when sensitive content can move INTO an allowed value. Verified safe by inspection 2026-07-27 — all 18 `why` sites in `reconcile.py` are f-string templates over structured fields (cleaner, date, uid); raw text lives only in `quote`, which is excluded. NOT yet enforced by a test — a future detector could regress this silently. Follow-up: assert in a unit test that every `why` template is constructed, never copied from `messages[].text`.)*
- [x] ISC-39: A digest computed against a stale world cannot read as a healthy night — if `last_sync` is >26h old (or absent) at push time, a synthetic `pipeline:stale-sync` needs-attention finding is injected so staleness rides the normal triage → Telegram path and breaks quiet-when-clean. *(1.24.1; advisor-identified gap; code probe + py_compile; window chosen 26h > nightly cadence, aligned with the VPS 25h+1h dead-man.)*

### GCal repair reliability (2026-08-01, Council-specced: timeout → honest classification → persisted status → ordered nightly → staleness → correlated findings)

**Bounded network calls**
- [ ] ISC-43: `_build_service` passes an explicit HTTP timeout to the Google client — no Calendar call can block indefinitely.
- [ ] ISC-44: The timeout is a named module constant, not an inline magic number.
- [ ] ISC-45: Anti: the timeout must not be shorter than a normal full sync (list + inserts) — validated against a measured real sync duration, not guessed.

**Honest push classification**
- [ ] ISC-46: `_gcal_push` treats a `{"skipped": 1}` return as NOT a success.
- [ ] ISC-47: `_gcal_push` resolves exactly three outcomes — ok / skipped / failed — and they are distinguishable at the call site.
- [ ] ISC-48: The log line emitted for a skip differs textually from the one emitted for a success.

**Push outcome as persisted state**
- [ ] ISC-49: A successful push writes `gcal_sync_status` into `data.json`.
- [ ] ISC-50: A failed push writes `gcal_sync_status` with a non-null `error` string.
- [ ] ISC-51: A skipped push writes `gcal_sync_status` with `ok: false` and a skip reason.
- [ ] ISC-52: `gcal_sync_status` carries all four fields — `ok`, `at`, `error`, `attempt`.
- [ ] ISC-53: `gcal_sync_status.at` is ISO-8601 and parses with `datetime.fromisoformat`.
- [ ] ISC-54: Anti: a failure to write the status record must never corrupt `data.json` nor raise out of the push path.
- [ ] ISC-55: Anti: `gcal_sync_status` must never contain a credential — no service-account JSON, no iCal token.

**Ordered nightly repair-then-detect**
- [ ] ISC-56: `_digest_scheduler` calls the GCal push inline (not via `threading.Thread`) strictly between `sync_ical()` and the digest.
- [ ] ISC-57: The nightly push is time-bounded — a wedged network call cannot hang the digest thread past the timeout budget.
- [ ] ISC-58: The nightly job re-attempts the push when the last recorded status was not-ok, giving traffic-independent retry cadence.
- [ ] ISC-59: Anti: `save_data()` remains asynchronous for every non-nightly caller — the UI and WhatsApp parse paths must not block on Google.
- [ ] ISC-60: Anti: a total Google Calendar outage must not prevent the digest and reconcile from running at all.
- [ ] ISC-61: Anti: detection stays unconditional — the reconciler must never skip observing GCal because the push reported success.

**Staleness assertion**
- [ ] ISC-62: A `pipeline:stale-push` needs-attention finding is injected when the last *successful* push is older than the threshold.
- [ ] ISC-63: The staleness threshold is a documented constant greater than the nightly cadence.
- [ ] ISC-64: `counts` agree with `findings` when the stale-push sentinel is injected (the ISC-40 lesson, applied a second time).
- [ ] ISC-65: The stale-push finding is dated today so the STALE_DAYS filter cannot auto-suppress it.
- [ ] ISC-66: The stale-push finding id is stable, so the digest diff alarms once rather than every morning.
- [ ] ISC-67: An absent `gcal_sync_status` (never pushed) counts as stale, not as healthy.

**Split diagnosis, correlated alert**
- [ ] ISC-68: A distinct `gcal_push_failed` finding kind exists, separate from `gcal_missing_event`.
- [ ] ISC-69: While a push failure is active, correlated `gcal_missing_event` / `gcal_stale_event` findings are absorbed into it rather than reported alongside it.
- [ ] ISC-70: Absorption is scoped to GCal-projection findings — unrelated drift findings still surface normally.
- [ ] ISC-71: Anti: correlation must never suppress a GCal finding while the push is healthy.
- [ ] ISC-72: The new finding kinds cross to the VPS through the existing allowlist unchanged (id, detector, kind, severity, date, cleaner, why).
- [ ] ISC-73: Anti: no `why` on the new finding kinds carries a secret, a guest name, or raw WhatsApp text.

**Ship and prove**
- [ ] ISC-74: `config.yaml` version bumped.
- [ ] ISC-75: `py_compile` passes on every changed Python file.
- [ ] ISC-76: The new logic has unit tests and they pass.
- [ ] ISC-77: Work is committed and pushed to the GitHub remote.
- [ ] ISC-78: The Pi is running the new add-on version (Supervisor reports it).
- [ ] ISC-79: Post-deploy live reconcile returns the same 14 pre-existing findings — no regression introduced.
- [ ] ISC-80: Live: a real successful push writes `gcal_sync_status.ok == true` into the live snapshot.
- [ ] ISC-81: Fault injection — with the push deliberately broken, a `gcal_push_failed` finding appears in a live reconcile.
- [ ] ISC-82: Fault injection — the broken push produces a real Telegram message, proving the alarm reaches the host.
- [ ] ISC-83: Recovery — restoring the good config clears the finding and returns `gcal_sync_status.ok` to true.
- [ ] ISC-84: Anti: fault injection leaves no residue — the real calendar id is restored and no junk events remain on the shared calendar.
- [ ] ISC-85: Antecedent: the host can tell, from the Telegram message alone, that the *push* failed rather than that the calendar drifted.

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
| ISC-18/19 | code+log | scheduler source order + boot log "(sync then digest)"; liveness via dead-man | present | rg + ha logs |
| ISC-21/22 | snapshot | `/internal/snapshot` → booking statuses post-sync | cancelled / active | curl + python |
| ISC-23/24 | wire-capture | VPS-persisted payload field set + quote/evidence scan | exact allowlist, absent | ssh + python |
| ISC-29–31 | live-probe | curl 401 (bad secret), 200+persist (good), via public Caddy route; `ss -ltnp` loopback bind | all as specced | curl + ssh |
| ISC-32/33/38 | live-e2e | Pi `/digest/run` → VPS receipt log → "message sent" w/ chatId; quiet runs log "no message sent" | delivered / quiet | curl + journalctl |
| ISC-34 | fault-injection | stale state file to 26h + restart → alarm fires + alert sent; heartbeat resets | fired once, reset | ssh + journalctl |
| ISC-36 | unit | SDK-fail/empty/timeout → fallback send (mocked), 80-test suite | all pass | bun test |
| ISC-39 | code | freshness guard injects `pipeline:stale-sync` finding when last_sync >26h | present | rg + py_compile |
| ISC-43/44 | code | `rg "timeout"` in gcal.py — constant defined, passed into `_build_service` | present | rg |
| ISC-45 | timing | time a real full `/gcal/sync` against the chosen timeout | sync ≪ timeout | bash time + curl |
| ISC-46–48 | unit | `_classify_push` over ok / skipped / error inputs | 3 distinct outcomes | python unittest |
| ISC-49–53 | live-probe | `/internal/snapshot` → `data.gcal_sync_status` after a real push | 4 fields, ISO `at` | curl + python |
| ISC-54/55 | code+scan | status write wrapped; scan snapshot value for credential substrings | no raise, no secret | rg + python |
| ISC-56 | code-order | source order in `_digest_scheduler`: sync → push → digest, no `Thread(` on that call | ordered inline | rg |
| ISC-57 | code | nightly push path bounded by the ISC-43 timeout | bounded | rg |
| ISC-58 | unit | scheduler retry predicate over ok/not-ok/absent status | retries on not-ok+absent | python unittest |
| ISC-59 | code | `save_data()` still spawns `threading.Thread(target=_gcal_push...)` | unchanged | rg |
| ISC-60 | fault-injection | break calendar id → run digest → digest still completes and reconciles | completes | curl |
| ISC-61 | code | no early-return in reconcile keyed on push success | absent | rg |
| ISC-62–67 | unit | staleness predicate over fresh / stale / absent status; id stability; counts parity | all pass | python unittest |
| ISC-68–71 | unit | correlation over push-failed+drift, push-healthy+drift, unrelated findings | absorb/pass-through correct | python unittest |
| ISC-72/73 | wire-capture | VPS-persisted payload for the new kinds; grep `why` templates | allowlist only, no raw text | ssh + rg |
| ISC-74–77 | build | version bump, `py_compile`, unit suite, `git log` | clean | bash |
| ISC-78 | deploy | `ha addons info` version string | matches bump | ssh |
| ISC-79 | regression | `/reconcile/run` → counts | 14, same kinds | curl |
| ISC-80 | live-probe | snapshot after real push | `ok: true` | curl |
| ISC-81/82 | fault-injection | bad calendar id → reconcile → digest → Telegram | finding + message | curl + ssh |
| ISC-83/84 | recovery | restore config → push → reconcile → GCal event count | clean, no residue | curl |
| ISC-85 | render | read the actual Telegram message text produced by the fault | names push failure | ssh journal |

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
| nightly-sync | iCal sync inside the nightly scheduler, strictly before digest; runs even with digest off | ISC-18, ISC-19, ISC-20, ISC-21, ISC-22 | — | true |
| vps-digest-push | allowlisted findings push Pi→VPS (heartbeat nightly, fail-loud, freshness sentinel) | ISC-23..ISC-28, ISC-39 | nightly-sync | true |
| bot-cleaning-service | VPS bot: loopback listener behind Caddy, subscription triage, Telegram delivery, 25h dead-man (repo: pai-telegram-bot `src/cleaning.ts`) | ISC-29..ISC-38 | vps-digest-push | true |
| gcal-bounded-io | explicit HTTP timeout on the Google client so no Calendar call blocks forever | ISC-43, ISC-44, ISC-45 | — | false |
| gcal-push-classification | `_gcal_push` resolves ok/skipped/failed as distinct outcomes instead of one print | ISC-46, ISC-47, ISC-48 | gcal-bounded-io | false |
| gcal-push-status | push outcome persisted to `data.json` on every attempt as a first-class fact | ISC-49..ISC-55 | gcal-push-classification | false |
| nightly-ordered-repair | nightly job runs sync → inline push (with retry-on-not-ok) → reconcile; other callers stay async | ISC-56..ISC-61 | gcal-push-status | false |
| gcal-staleness-sentinel | `pipeline:stale-push` finding when the last successful push ages out, mirroring `pipeline:stale-sync` | ISC-62..ISC-67 | gcal-push-status | false |
| gcal-finding-correlation | `gcal_push_failed` as its own kind, absorbing the drift findings it caused | ISC-68..ISC-73 | gcal-staleness-sentinel | false |
| gcal-repair-proof | ship, deploy, then deliberately break the push and prove the alarm reaches Telegram | ISC-74..ISC-85 | all of the above | false |

## Decisions

- 2026-08-01 11:05: **`refined:` ROOT CAUSE FOUND — and it is the race, not a silent push failure. My own earlier diagnosis was a measurement artifact.** Evidence from the VPS bot journal (`journalctl -u pai-telegram-bot`), which retains what the add-on's truncated log does not: Jul 29 quiet · **Jul 30 new=3** · Jul 31 quiet · **Aug 1 new=3**. The Jul 30 message names *stay Jun 24 2027 / cleaning Jul 1 2027*; the Aug 1 message names *stay Jul 2 2027 / cleaning Jul 9 2027* — **different bookings each time, each one the newest reservation to arrive in the Airbnb feed.** So the sequence is: a new booking lands in the iCal → the nightly `sync_ical()` ingests it → `save_data()` spawns the async GCal push → `_digest_compute_and_notify()` runs immediately and `fetch_tagged_events` reads Google Calendar *before the push has inserted the new events* → two `gcal_missing_event` findings → Telegram. By the following night the push has long since landed them, so the findings vanish (the quiet nights), and the cycle repeats on the next new reservation. **Every `gcal_missing_event` alert the host has received on this is a false positive manufactured by detection outrunning its own repair.** The paired `drift_unassigned` finding in the same messages is genuine — a new booking really does need a cleaner.
- 2026-08-01 11:05: ❌ **DEAD END / self-inflicted, worth recording because it is the exact error class this session is about:** the session's opening diagnosis ("the events were genuinely missing; a manual `POST /gcal/sync` fixed them, findings 16 → 14") **does not survive scrutiny.** The 16 was read from `GET /reconcile/last` — the *cached* 08:00 result — and the 14 from a *fresh* `POST /reconcile/run` after the manual sync. Two different measurements, one attributed as the effect of the other. A fresh reconcile run later the same day, with no manual sync preceding it, also returns 14. The correct statement is that the calendar was probably already correct by mid-morning and the cached finding was stale. Lesson, and it generalises: **when a cache and a live probe are both available, comparing across them silently invents a causal story.** Re-probe both sides with the same method before attributing a fix.
- 2026-08-01 11:05: Consequence for the shipped work — **D4 (ordering) is the change that stops the alerts**; D1/D2/D3/D5/D6 (timeout, honest classification, persisted status, staleness, correlated findings) fix a *real but so-far-unobserved* silent-failure surface and are what would have made this diagnosis a five-second glance at `gcal_push_status.json` instead of an hour of journal forensics. Both ship. Neither is relabelled as the other.
- 2026-08-01 10:40: Council of 4 (reliability builder / silent-failure skeptic / pragmatic implementer / convergence analyst), 2 rounds, full quorum. Converged 4-0 on: persist push outcome on every attempt; a skip must stop reading as a success; assert staleness or the record is just a fancier log line; order repair before detection **in the nightly path only**; no queue/daemon/backoff library; keep detection unconditional. Two unresolved splits recorded rather than forced: (a) the skeptic's sentinel canary event — rejected, because the reconciler's independent re-read of the real calendar already provides the verification a canary would, and a synthetic event would litter a calendar Michelle and the cleaners actually read; (b) lock-wait vs plain inline call — resolved empirically against the code, not by argument: `_SYNC_LOCK` *is* released in a `finally` (`gcal.py:361`) so the skeptic's exception-hang worry was unfounded, but `_build_service` (`gcal.py:59`) passes no HTTP timeout at all, so an unbounded network wedge was real for **either** design. That makes the timeout a prerequisite rather than a refinement, and settles the choice for the simpler inline call plus a bounded `join()`.
- 2026-08-01 10:45: `refined:` deviation from the council's literal recommendation, recorded before building it: they specified writing push status **into `data.json`**. Shipping it as a sidecar `gcal_push_status.json` instead, because `save_data()` is what triggers the push — writing status back into `data.json` from inside the push path would recurse. Same fact, same off-host visibility (exposed via `/internal/snapshot`), no loop.
- 2026-08-01 10:45: `refined:` improvement on the ISC-39 precedent. The `pipeline:stale-sync` sentinel is injected in `_push_digest_to_vps()`, *downstream* of where counts are computed, which is why 1.24.2 needed a manual counts fix-up (Cato's catch, ISC-40). The new push-health findings are injected inside `reconcile.run()` **before** `filter_and_sort()`, so counts derive from findings automatically and the ISC-40 failure mode cannot recur by construction rather than by remembering to increment.
- 2026-08-01 10:45: ISC floor relaxation (E5 soft ≥256): the natural atomic decomposition of this feature is 43 new ISCs, each already naming a single-tool probe. Inflating to 256 would fabricate granularity on a 60–80 line change to a one-household add-on. Show-your-math: every ISC-43..85 names its probe in the Test Strategy table.
- 2026-07-27 14:10: `refined:` Problem / Vision / Goal / Principles rewritten to match the ideal state the system now pursues. The old Vision opened "The host opens one page and sees…" — that was the correct ideal *before* delivery existed, and leaving it would have made the ISA describe a system one generation behind its own criteria. Three principles added (ingest-before-judge, a finding is not delivered until it reaches the human, silence must be earned and distinguishable from death), each earned by a specific failure this session rather than asserted.
- 2026-07-27 14:05: Open backlog after this run (6 ISCs, none blocking): ISC-3 (2 stale GCal events, far-future, low priority), ISC-7.1 (credit-probe anti-loop, never observed live), ISC-11 (health banner on the home page, deferred), ISC-13 (Daria-group live forward test), ISC-15 (channel-silence live confirmation — needs a real dark channel), ISC-41 (test that no `why` value can carry raw text). ISC-41 is the only one this session created.
- 2026-07-27 10:00: Council of 4 (Builder/Skeptic/Pragmatist/Analyst, 2 rounds) specced the nightly pipeline. Converged 4-0: sync inline in the add-on scheduler (no external orchestrator), detection-only preserved, diff-based quiet-when-clean + dead-man mandatory. 3-1: Telegram via the existing VPS bot (one sender; a dead Pi can't self-report, killing HA-native as primary). Josh overruled the names split: cleaner names MAY cross to the VPS/Telegram. Analyst correction on record: the Pi→VPS push is new egress (outbound HTTPS), not reused plumbing.
- 2026-07-27 10:05: ISC floor relaxation (E4 soft ≥128): this is a feature addition to a project ISA whose natural atomic decomposition is 22 new ISCs; inflating to 128 would fabricate granularity. Show-your-math: every criterion already names a single-tool probe.
- 2026-07-27 10:07: GOTCHA (cost a deploy cycle): Supervisor options POSTed while the OLD add-on version is installed are validated against the OLD schema — unknown keys are silently dropped, `{"result":"ok"}` anyway. Order must be: update add-on → THEN POST new options → restart.
- 2026-07-27 10:10: Forge's payload validator required non-empty `cleaner`/`date` — but the most common real finding (`drift_unassigned`) has `cleaner: null` by definition; first live push 400'd. Fixed as nullable-by-design + regression tests. Lesson: validators for cross-system payloads must be tested against REAL producer output, not the spec's happy path.
- 2026-07-27 10:15: Advisor (Rule 2) raised: (a) stale-sync nights read as healthy → shipped ISC-39 sentinel finding in 1.24.1 (rides normal triage path, breaks quiet-when-clean); (b) dead-man independence — accepted as cross-host by design (VPS watches Pi; bot death is observable via chat + systemd restart), per Council; (c) double-send — prevented by the digest diff baseline (re-run → new=0 → quiet, live-proven); (d) fallback allowlist bypass — impossible by construction: the allowlist is applied at payload build on the Pi, both triage and fallback consume the same sanitized object.
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

- 2026-07-27 | conjectured: the reconciler running daily meant schedule drift was caught daily.
  refuted by: the reconciler *detects* daily but `sync_ical()` ran only at process startup and on a manual button — so detection ran against a world that only updated on deploys. A 3-day deploy-free stretch left an upstream Airbnb cancellation (Oct 16–18) and a new booking (May 2027) unapplied; the digest dutifully reported "active booking not in iCal — cancelled upstream?" every morning to an HA notification panel nobody opens, while data.json and GCal stayed wrong. Detection without ingestion is a tautology: it verifies the staleness it created.
  learned: every deploy masked the gap (each version bump restarts the process → startup sync), so the failure only surfaced when the system ran *quietly well* for days. Cadence must be designed before sophistication (the same lesson as ISC-4's digest_enabled default-off, now earned twice) — and the alert channel matters as much as the alert: HA persistent notifications are a place Josh visits, Telegram is a place messages find him.
  criterion now: ISC-18/19 (sync inside the nightly loop, ungated), ISC-23..28 (sanitized push), ISC-29..38 (VPS triage + Telegram + dead-man), ISC-39 (stale-sync sentinel). Shipped 1.24.1 + bot cleaning service, all live-proven 2026-07-27.

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
- ISC-18/19: code+log (2026-07-27) — `_digest_scheduler` calls `sync_ical()` then `_digest_compute_and_notify()`; `if not DIGEST_ENABLED: continue` sits BETWEEN them so sync is ungated. Boot log: `[digest] scheduler started — daily at 08:00 (sync then digest)`. Thread now started unconditionally at `__main__`.
- ISC-21/22: snapshot (2026-07-27 10:07) — post-deploy `/internal/snapshot`: Oct 16–18 `status: cancelled` (was `active`); `2027-05-14 → 2027-05-16 active` now present. `last_sync` advanced to 10:07:43 from 3-day-old 07-24T08:26.
- ISC-23/24: wire-capture — VPS-persisted `cleaning-digest.json` payload contains exactly `ts, heartbeat, counts{4}, new, resolved, findings[{id,detector,kind,severity,date,cleaner,why}]`; substring scan for `quote`/`evidence` on the serialized payload → False.
- ISC-25: live — the first 1.24.0 push was rejected `400` by the bot validator; add-on logged `[vps-push] FAILED: HTTP 400` and posted the HA notification (fail-loud proven by a real failure, not a simulated one). Post-fix pushes return 200.
- ISC-29/30/31: live-probe — `POST https://play.joshuamohan.com/cleaning/digest` with bad secret → `401` + log `push rejected: missing or invalid X-Push-Secret`, state file untouched; with correct secret → `{"ok":true}` 200 + state persisted. `ss -ltnp` on the VPS: `LISTEN 127.0.0.1:8899 users:(("bun"...))` — loopback only, never 0.0.0.0. Caddy `handle /cleaning/digest` added; hub vhost still returns its Basic-Auth 401 (unaffected).
- ISC-32/38: live-e2e (2026-07-27 17:09) — dismiss/undismiss cycle on a real finding produced `new:1`; VPS logged `cleaning digest received {new:1, findingsCount:1}` then `cleaning digest message sent {chatId:87…, length:78}` with no fallback warning → the subscription SDK triage path ran. Service env carries no `ANTHROPIC_API_KEY` (systemd `ExecStartPre` refuses to boot with one), so billing is OAuth-subscription by construction.
- ISC-33: live — three separate clean digests each logged `cleaning digest quiet — no new findings, no message sent`; zero Telegram sends.
- ISC-34: fault-injection (17:10) — state `received_at` back-dated 26h + service restart → `cleaning pipeline dead-man alarm firing {reason:"stale"}` then `message sent {length:120}`. A fresh heartbeat POST reset `received_at` to now and cleared `last_alert_at`. (Start-time immediate check added by hand — Forge's version only checked hourly, so a restart could grant a dark pipeline an extra hour of silence.)
- ISC-36: unit — `bun test` 80 pass / 0 fail / 146 expects across 3 files, incl. SDK-fail, SDK-empty and SDK-timeout (hung generator raced to a 50ms deadline) all producing a fallback send.
- ISC-37: systemd — `systemctl is-active pai-telegram-bot` → active across 3 restarts; logs show `cleaning digest listener started {port:8899}` + `Bot started: @josh_vela_claude_bot`. `git diff src/bot.ts` = one `export` keyword; policy/gate/toolset/sanitize untouched.
- ISC-39: code — `_push_digest_to_vps` computes `last_sync` age and appends `pipeline:stale-sync` when >26h or absent; `python3 -m py_compile app.py` clean; deployed 1.24.1.
- ISC-40: code+live — `counts = dict(counts)` then `total`/`needs-attention` incremented alongside the sentinel; 1.24.2 deployed (`version: 1.24.2, state: started`), post-deploy digest push accepted 200.
- ISC-16: in-browser (2026-07-24) — Chromium on live add-on 1.23.0: `#vps-widget` visible, `#vps-dot` class `vps-dot up`, text "VPS online · 75ms · HTTP 401", **0 console errors** (screenshot). `GET /vps/status` → `{"enabled":true,"reachable":true,"latency_ms":75,"http_status":401,"label":"VPS"}`. Options merge preserved all 16 keys (no secret wiped). `_vps_ping` verified against real/down/edge hosts.
