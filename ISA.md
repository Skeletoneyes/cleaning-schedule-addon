---
title: Cleaning Schedule Tracker — Project ISA
slug: cleaning-schedule-addon
type: project
effort: E5
phase: active
updated: 2026-08-03T10:30:00-07:00
progress: 161/170
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
- [x] ISC-43: `_build_service` passes an explicit HTTP timeout to the Google client — no Calendar call can block indefinitely.
- [x] ISC-44: The timeout is a named module constant, not an inline magic number.
- [x] ISC-45: Anti: the timeout must not be shorter than a normal full sync (list + inserts) — validated against a measured real sync duration, not guessed.

**Honest push classification**
- [x] ISC-46: `_gcal_push` treats a `{"skipped": 1}` return as NOT a success.
- [x] ISC-47: `_gcal_push` resolves exactly three outcomes — ok / skipped / failed — and they are distinguishable at the call site.
- [x] ISC-48: The log line emitted for a skip differs textually from the one emitted for a success.

**Push outcome as persisted state**
- [x] ISC-49: A successful push writes a push-status record to the `gcal_push_status.json` sidecar (see Decisions 2026-08-01 10:45 for why a sidecar and not `data.json`).
- [x] ISC-50: A failed push writes a push-status record with a non-null `error` string.
- [x] ISC-51: A skipped push writes a push-status record with `ok: false` and a skip reason.
- [x] ISC-52: The push-status record carries all four fields — `ok`, `at`, `error`, `attempt` — plus `last_ok_at` and `stats`.
- [x] ISC-53: The push-status `at` is ISO-8601 and parses with `datetime.fromisoformat`.
- [x] ISC-54: Anti: a failure to write the status record must never corrupt `data.json` nor raise out of the push path.
- [x] ISC-55: Anti: the push-status record must never contain a credential — no service-account JSON, no iCal token, and no calendar id leaked through an error string.

**Ordered nightly repair-then-detect**
- [x] ISC-56: `_digest_scheduler` calls the GCal push inline (not via `threading.Thread`) strictly between `sync_ical()` and the digest.
- [x] ISC-57: The nightly push is time-bounded — a wedged network call cannot hang the digest thread past the timeout budget.
- [x] ISC-58: The nightly job re-attempts the push when the last recorded status was not-ok, giving traffic-independent retry cadence.
- [x] ISC-59: Anti: `save_data()` remains asynchronous for every non-nightly caller — the UI and WhatsApp parse paths must not block on Google.
- [x] ISC-60: Anti: a total Google Calendar outage must not prevent the digest and reconcile from running at all.
- [x] ISC-61: Anti: detection stays unconditional — the reconciler must never skip observing GCal because the push reported success.

**Staleness assertion**
- [x] ISC-62: A `pipeline:stale-push` needs-attention finding is injected when the last *successful* push is older than the threshold.
- [x] ISC-63: The staleness threshold is a documented constant greater than the nightly cadence.
- [x] ISC-64: `counts` agree with `findings` when the stale-push sentinel is injected (the ISC-40 lesson, applied a second time).
- [x] ISC-65: The stale-push finding is dated today so the STALE_DAYS filter cannot auto-suppress it.
- [x] ISC-66: The stale-push finding id is stable, so the digest diff alarms once rather than every morning.
- [x] ISC-67: An absent push-status record (never pushed) counts as stale, not as healthy.

**Split diagnosis, correlated alert**
- [x] ISC-68: A distinct `gcal_push_failed` finding kind exists, separate from `gcal_missing_event`.
- [x] ISC-69: While a push failure is active, correlated `gcal_missing_event` / `gcal_stale_event` findings are absorbed into it rather than reported alongside it.
- [x] ISC-70: Absorption is scoped to GCal-projection findings — unrelated drift findings still surface normally.
- [x] ISC-71: Anti: correlation must never suppress a GCal finding while the push is healthy.
- [x] ISC-72: The new finding kinds cross to the VPS through the existing allowlist unchanged (id, detector, kind, severity, date, cleaner, why).
- [x] ISC-73: Anti: no `why` on the new finding kinds carries a secret, a guest name, or raw WhatsApp text.

**Ship and prove**
- [x] ISC-74: `config.yaml` version bumped.
- [x] ISC-75: `py_compile` passes on every changed Python file.
- [x] ISC-76: The new logic has unit tests and they pass.
- [x] ISC-77: Work is committed and pushed to the GitHub remote.
- [x] ISC-78: The Pi is running the new add-on version (Supervisor reports it).
- [x] ISC-79: Post-deploy live reconcile returns the same 14 pre-existing findings — no regression introduced.
- [x] ISC-80: Live: a real successful push writes `gcal_push_status.ok == true` into the live snapshot.
- [x] ISC-81: Fault injection — with the push deliberately broken, a `gcal_push_failed` finding appears in a live reconcile.
- [x] ISC-82: Fault injection — the broken push produces a real Telegram message, proving the alarm reaches the host.
- [x] ISC-83: Recovery — restoring the good config clears the finding and returns `gcal_sync_status.ok` to true.
- [x] ISC-84: Anti: fault injection leaves no residue — the real calendar id is restored and no junk events remain on the shared calendar.
- [x] ISC-86: A nightly push that exceeds its budget and is still running records `outcome: timeout, ok: false` — a wedged push must never present as a healthy one.
- [x] ISC-87: A GCal read failure degrades the reconcile instead of killing it, and produces a `gcal_read_failed` finding saying content drift is *unmeasured*, not absent.
- [x] ISC-88: Anti: a Google `HttpError` string must not carry the calendar id into a finding's `why` — URLs are reduced to scheme+host before crossing to the VPS.
- [x] ISC-89: A future-dated `last_ok_at` (Pi has no RTC; a power cut can write ahead of true time) is treated as implausible and alarms, rather than suppressing staleness forever.
- [x] ISC-90: Benign clock skew under an hour does not alarm — the future-date guard must not become the opposite bug.
- [x] ISC-91: A recent budget-exceeded push surfaces as `gcal_push_timeout` even when a later writer recorded `ok` — the late-writer race must not erase the timeout.
- [x] ISC-92: That timeout finding ages out, so a historic slow push does not alarm forever.
- [x] ISC-93: Anti: a corrupt or unparseable push-status record collapses into the same loud branch as an absent one — never into healthy.
- [x] ISC-94: The nightly heartbeat should attest per-stage outcomes (`sync_ok`, `push_outcome`, `reconcile_ok`) rather than merely proving arrival, so the VPS dead-man cannot be satisfied by a Pi that reached the push while doing no real work. *(Advisor finding 2026-08-01. **DONE same day** — Josh approved taking it next; implemented and live-verified as ISC-96..105 in 1.26.0→1.26.4. Fields are booleans/enums, so the crossing allowlist was unaffected as predicted.)*
- [x] ISC-95: The liveness chain terminates at a single unmonitored VPS process, and ISC-16 records that the footer widget cannot see a crashed bot on a live box — so "Pi silent + bot dead" renders identically to a quiet clean night. *(Advisor finding 2026-08-01. **DONE same day, and the framing was wrong** — closing it needed nothing outside the system. The chain is a cycle once the Pi watches the bot back, because the VPS dead-man already watches the Pi. Implemented as ISC-106..113. The residual honest caveat is ISC-120.)*
- [x] ISC-85: Antecedent: the host can tell, from the Telegram message alone, that the *push* failed rather than that the calendar drifted.

### Liveness: attestation + closing the watch cycle (2026-08-01, advisor-driven)

- [x] ISC-96: The nightly payload carries an `attestation` block with `sync_ok`, `push_outcome` and `reconcile_ok`.
- [x] ISC-116: `sync_ok` reflects the outcome of THIS run's sync attempt, not the freshness of the last successful one — a failure tonight must not be masked by a success yesterday.
- [x] ISC-118: Anti: a corrupt (as opposed to absent) sync record fails closed — it must never degrade into the freshness fallback and claim a success it cannot prove.
- [x] ISC-119: A deadline-based absence alarm exists and is distinct from the consecutive-non-ok counter — a counter over received payloads can never increment on absence. *(The 25h dead-man, checked hourly; re-verified intact after this session's edits: 9 staleness tests pass.)*
- [x] ISC-120: The independence claim must be written accurately: the two watch directions share no infrastructure **downstream of the Pi's NIC**, but upstream they share the LAN, router, modem, ISP and house power. *(Advisor correction. Written into `CLAUDE.md` § Liveness as an explicit ⚠️ paragraph naming the shared upstream — LAN, router, modem, ISP, house power — and stating that a WAN or mains outage defeats both directions and is deliberately undefended, because two people in a house notice it within minutes for free.)*
- [ ] ISC-121: `/cleaning/health` asserts the web listener, not that the Telegram poll loop is alive — a wedged poller returns 200. *(Advisor finding; deferred. Closing it needs a last-successful-Telegram-interaction timestamp on the bot.)*
- [x] ISC-117: Anti: the bot-health probe must not silently disable itself when the push URL shape drifts — an unusable URL reports unhealthy, and a moved route still probes correctly.
- [x] ISC-97: `sync_ok` and `push_outcome` are derived from durable state (`last_sync`, the push-status sidecar), not passed in — an attestation that can be omitted can lie by omission.
- [x] ISC-98: Anti: the attestation adds only booleans and enums — it must not widen what crosses to the VPS.
- [x] ISC-99: The VPS validates the attestation shape and rejects a malformed one without resetting the dead-man.
- [x] ISC-100: An absent attestation (older add-on) is treated as *unknown* — it neither increments nor resets the consecutive-failure counter.
- [x] ISC-101: The VPS counts consecutive receipts reporting any non-ok stage, using its own counter and its own clock — never a Pi-supplied timestamp.
- [x] ISC-102: Three consecutive non-ok receipts send a Telegram alert naming which stage failed.
- [x] ISC-103: The alert fires once per episode, not on every subsequent receipt.
- [x] ISC-104: A fully-ok receipt resets the counter and clears the episode.
- [x] ISC-105: `push_outcome: "disabled"` counts as ok — GCal off is a valid configuration, not a fault.
- [x] ISC-106: The bot serves `GET /cleaning/health` reporting uptime and last-digest age.
- [x] ISC-107: Anti: the health endpoint requires the push secret — it must not expose activity data publicly.
- [x] ISC-108: Caddy routes `/cleaning/health` to the bot listener.
- [x] ISC-109: The nightly job probes bot health inline (no new thread — a watcher thread is one more thing that can die silently).
- [x] ISC-110: A bot that is reachable but unhealthy is caught, not only an unreachable one.
- [x] ISC-111: Critical add-on alerts reach the phone via a configurable notify service, in addition to the notification panel.
- [x] ISC-112: Anti: the notify service name is a config option, never hardcoded — this repo is public (the `vps_status_url` precedent).
- [x] ISC-113: Anti: an unconfigured or failing phone-notify path must not break the notification it was meant to escalate.
- [x] ISC-115: The add-on's own log lines are readable in real time — `PYTHONUNBUFFERED=1`, without which every fail-loudly `print()` sat in a block buffer until it filled.
- [x] ISC-114: Antecedent: a real push to the configured phone service is observed arriving, not merely accepted by the API.
- [x] ISC-122: Bridge liveness is determined from the add-on's **container state** via Supervisor on an hourly timer, never inferred from message traffic.
- [x] ISC-123: A bridge that is not running is restarted automatically, without waiting for a human.
- [x] ISC-124: Anti: a watchdog that cannot perform its check (no token, no permission, Supervisor unreachable) emits a finding rather than silently doing nothing.
- [x] ISC-125: Anti: a transient state (`startup`) does not trigger a restart.
- [x] ISC-126: Every outage records a **blind window** naming the period no messages were received; recovery is silent, loss is not.
- [x] ISC-127: The blind-window finding has a stable id across nights, so it can be dismissed rather than reappearing as a new finding forever.
- [x] ISC-128: Unresolved needs-attention findings repeat in every nightly digest until fixed or dismissed, carrying an age marker.
- [x] ISC-129: Anti: repetition is bounded by a 21-day horizon for dated findings, so far-future items cannot turn the digest into noise. Dateless findings (bridge down, blind window) always repeat.
- [x] ISC-130: The digest applies dismissals — previously only the web routes did, so a dismissed finding rode the nightly Telegram message forever.
- [x] ISC-131: Every booking change applied from WhatsApp is recorded and reported in the nightly digest, so what the system did arrives without anyone opening the app.
- [x] ISC-132: Anti: the change record carries derived fields only (date, cleaner, time, confirmed) — never WhatsApp text, which must not reach the VPS through an unexamined field.
- [x] ISC-133: The applied-changes section bypasses the VPS summarizer and renders verbatim — a summarized audit trail is not an audit trail.
- [x] ISC-134: The bridge reconnects single-flight, with teardown of the dead socket and exponential backoff; overlapping sockets are impossible.
- [x] ISC-135: A bridge restart resumes from a persisted **delivery watermark** rather than discarding everything older than process start, bounded by `MAX_REPLAY_DAYS`.
- [x] ISC-136: Supervisor's own watchdog is enabled on both add-ons, so a crashed container is restarted even if the tracker is the thing that died.
- [x] ISC-137: Antecedent: the heal path is exercised by fault injection (`POST /internal/watchdog/check` after stopping the bridge), not asserted from code.
- [x] ISC-138: Fact extraction receives a digest of what OTHER chats have already established for nearby dates, so a cleaning arranged across both threads is visible to one layer.
- [x] ISC-139: Anti: the cross-chat context is *extracted facts*, not raw messages — extraction runs on every message, and raw context would double the token cost on the hot path.
- [x] ISC-140: The cross-chat horizon is measured on the **cleaning date**, never on when it was said — commitments here are routinely made months ahead.
- [x] ISC-141: Anti: the digest is truncated by proximity to the message's own date, so hitting the cap cannot drop the dates under active negotiation.
- [x] ISC-142: History windows are "N messages **or** X days, whichever is larger" — a message count alone gave the busiest chat the shortest memory.
- [x] ISC-143: Anti: both windows keep a hard cap, because unbounded history is what hit the org rate limit during bulk backfill.
- [x] ISC-144: The facts prompt describes the context it actually receives; it previously claimed "prior messages across all groups" while the code filtered to one.
- [x] ISC-145: Reprocessing runs oldest-first and re-reads the facts layer per message, so it converges the way live processing does rather than every message seeing the pre-reprocess snapshot.
- [x] ISC-146: Speaker role is resolved from the stored cleaner/host JID map, not by testing whether a cleaner's name appears in the sender label.
- [x] ISC-147: Anti: substring matching survives as the fallback for pasted senders present in neither list.
- [x] ISC-148: A cleaner can be renamed across every join key at once (bookings, commitments, JID map, group labels, facts); a partial rename is treated as the failure mode.
- [x] ISC-149: Anti: historical free-text notes are NOT rewritten by a rename — they record what was said, not current truth.
- [x] ISC-150: Pending review items whose subject date is more than `review_expiry_days` in the past are retired nightly.
- [x] ISC-151: Staleness is judged on what an item is ABOUT (the matched booking's cleaning date), falling back to the send date only when no booking matched.
- [x] ISC-152: Anti: expiry marks `expired` and never deletes — facts from retired messages stay readable to the reconciler, so attention is withdrawn but evidence is not.
- [x] ISC-153: Every model prompt in the pipeline opens with a dating header stating TODAY and, when different, the message's SEND date.
- [x] ISC-154: Anti: relative terms resolve against the SEND date, so reprocessing a January message in August cannot re-date it.
- [x] ISC-155: The nightly job reads the clock exactly once and dates every downstream stage from that value.

### Operational history: restart frequency + operator actions (2026-08-03)

*Provoked by two questions asked an hour apart that turned out to be one question:
"how often does the bridge actually need restarting?" and "why didn't you know I
backfilled the transcript yesterday?" Both want a record of **events on the live
system**. This ISA had 156 criteria about what was built and none about what was
done to it — verified by probe: `operator`, `operational event`, `restart count`
and `transcript ingest` returned zero hits across the whole file.*

- [x] ISC-156: The watchdog records every state transition, restart, outage and probe error as a timestamped event, so restart *frequency* is answerable rather than only current state.
- [x] ISC-157: Anti: a steady healthy state writes no events — at a 5-minute poll a chatty log would be 288 rows a day and would bury the handful that matter.
- [x] ISC-158: The event log is capped and drops oldest-first, so the state file cannot grow without bound.
- [x] ISC-159: `summary()` reports restarts over 24h / 7d / 30d windows and outage counts, derived from the event log rather than from the lifetime `heals` counter (which has no time axis).
- [x] ISC-160: Anti: a malformed stored timestamp must not crash `summary()` — it is skipped, not raised on.
- [x] ISC-161: Anti: a persistent probe error logs one event on the transition into failure, not one per poll.
- [x] ISC-162: The restart figure travels with an explicit statement that it **undercounts** — a crash Supervisor's own add-on watchdog repairs between two polls is unobservable to a poll, and a reassuring number that hides that is worse than no number.
- [x] ISC-163: The bridge liveness poll runs at 5-minute resolution, cutting worst-case detection lag from 60 minutes to 5.
- [ ] ISC-164: Operator actions on the live system (transcript ingest, finding dismissal) are recorded **as actions** in an append-only ops log, not left to be inferred from their side effects.
- [ ] ISC-165: Anti: a failure to write the ops log must never fail the operation it was logging.
- [x] ISC-166: The watchdog summary and the ops log are readable off-host via `/internal/snapshot`, so a later session can read what was done to the system instead of reconstructing it from `source:` tags on data rows.
- [x] ISC-167: Anti: the reporting helpers must not be able to raise inside `/internal/snapshot` — it is the off-host reconciliation lifeline and must never 500.
- [x] ISC-168: Anti: the watchdog's load-mutate-save is one critical section — the scheduler thread and the on-demand `/internal/watchdog/check` route must not be able to overwrite each other's events.
- [x] ISC-169: Anti: watchdog state is persisted atomically (temp + `os.replace`), so a restart mid-write cannot truncate the file and silently reset the whole incident history to defaults.

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

- ISC-96: live — persisted VPS state shows `attestation: {sync_ok:true, push_outcome:"ok", reconcile_ok:true}`.
- ISC-97: code+unit — `sync_ok` from `last_sync`, `push_outcome` from the push-status sidecar; 11 tests over stale/absent/future/unparseable/disabled/never.
- ISC-98: live — payload top-level keys are exactly `attestation, counts, findings, heartbeat, new, resolved, ts`; the block is two booleans and one enum string.
- ISC-99: unit (bot) — a malformed attestation rejects the whole payload rather than degrading to undefined, so a broken Pi cannot downgrade itself into the unknown bucket. `attestation: null` is rejected too (`typeof null === 'object'`).
- ISC-100: unit (bot) — `attestationIsOk(undefined)` returns null; the transition carries the counter forward unchanged.
- ISC-101: code (bot) — `nextAttestationState` is pure and the caller stamps `new Date()`; no Pi-supplied timestamp is used.
- ISC-102: unit (bot) — alarm fires at exactly 3, not at 2, and names the failed stages.
- ISC-103: unit (bot) — `last_attestation_alert_at` gates repeat sends; once per episode.
- ISC-104: unit (bot) — an ok receipt resets the counter to 0 and clears the episode. Live: counter 0 after recovery.
- ISC-105: unit (bot) — `push_outcome: "disabled"` yields ok. GCal off is valid config, not a fault.
- ISC-106: live — `GET /cleaning/health` returned `{ok, service, uptime_s, last_digest_received_at, last_digest_age_s, consecutive_non_ok}`; age tracked a real receipt (3851s → 3.0s).
- ISC-107: live — 401 without the secret through the public vhost; 200 with it.
- ISC-108: live — Caddy `handle /cleaning/health` added, `caddy validate` clean, reloaded; 404 on a sibling path.
- ISC-109: code — probe runs inline in `_digest_scheduler` before the digest; no new thread.
- ISC-110: unit — `{ok: false}` from a reachable bot is caught; this is the case a web-surface ping structurally cannot see (ISC-16).
- ISC-111: live — `[notify] phone escalation sent via notify.<service>` observed after an induced push failure.
- ISC-112: unit — `mobile_app_` appears nowhere in `app.py`; the service name is a config option.
- ISC-113: code+unit — `_post_phone_notification` is called first, wrapped in a bare except, and returns False on failure; it cannot unwind the panel notification.
- ISC-114: `[DEFERRED-VERIFY]` — live fault injection — push URL pointed at a 404, digest run, log shows `[vps-push] FAILED: HTTP 404` then `[notify] phone escalation sent`. Config restored; `[vps-push] ok` confirmed. **CONFIRMED by Josh 2026-08-01: the Home Assistant app notifications arrived on his phone.** Worth recording how nearly this was mis-closed: he first replied "yes I got the telegram message", which would have read as confirmation — but the VPS journal shows zero Telegram sends after 17:57 UTC, while the phone test ran at ~19:06 and by design sends nothing through Telegram (the whole point was that the VPS path was broken). The message he had was the earlier Google Calendar alarm on a different channel. Two alarms, two channels, one ambiguous word; taking it at face value would have closed the criterion on the wrong evidence.
- ISC-115: live — `ha addons logs` now shows `[gcal]`, `[vps-push]` and `[notify]` lines in real time; before `PYTHONUNBUFFERED=1` only werkzeug access lines reached journald.

- 2026-08-03 10:35: ⚠️ **NEW DEPLOY GOTCHA — a Supervisor options POST REPLACES the whole options dict; it does not merge.** Sending `{"options":{"bridge_watchdog_interval_min":5}}` returned `app_configuration_invalid_error — Missing option 'digest_enabled' in root`. It failed **closed**, so nothing was lost, but the existing note in `CLAUDE.md` (new keys are silently dropped if sent before the update lands) describes a different hazard and reads as if partial POSTs are otherwise fine. They are not. Correct form, which also avoids ever printing the secrets in that dict: `curl .../info | jq -c '{options: (.data.options + {KEY: VALUE})}' | curl -X POST -d @- .../options`. Verified after: `digest_enabled` and `gcal_enabled` both still true.
- 2026-08-03 10:20: `refined:` scope call — the operator-action log was folded into this change rather than deferred, because the two things asked for an hour apart (restart frequency; why a prior session's backfill was invisible) are one missing mechanism, and building only the restart counter would have left the question that prompted it unanswered. Ops-log call sites were held to `/reconcile/dismiss` and `/admin/ingest-transcript` — the two highest-value operator actions — deliberately not the booking-write paths, to keep the diff small on a live turnover day.
- 2026-08-03 10:15: Deployed mid-morning rather than after the turnover, on Josh's call, because the bridge's `forward()` has no retry: a message landing during the tracker restart is dropped permanently. Pre-11am was the widest quiet gap available before Darya's 11:00–15:00 window.

## Changelog
- 2026-08-03 | conjectured: the read-modify-write hazard in this change was the ops log, so a lock there made the feature concurrency-safe.
  refuted by: a cross-vendor review pass, verified independently — `_log_op` only ever runs from Flask request handlers (`/reconcile/dismiss`, `/admin/ingest-transcript`), which are human-triggered and never concurrent in practice, while `bridge_watchdog.check()` has **two real callers in one process** (`app.py:3113` scheduler thread, `app.py:3425` on-demand route) and no lock at all. The lock was written onto the file that didn't need it. Worse, the same commit cut the poll interval 60 → 5 min, widening the unguarded collision window twelvefold.
  learned: **identifying a hazard class correctly is not the same as locating it.** The reasoning in `_log_op`'s docstring — "the watchdog timer thread and a Flask request thread can both reach it" — was accurate about the system and wrong about the file it was attached to; being right about the mechanism made the misplacement harder to see, not easier. Enumerate the actual call sites of the function you are protecting, from the code, before deciding where the lock goes.
  criterion now: ISC-168, ISC-169.

- 2026-08-03 | conjectured: the bridge watchdog's existing state (`heals`, `blind_windows`) was enough to answer how reliable the bridge is, since it already counted restarts and recorded every outage.
  refuted by: `heals` is a lifetime integer with no time axis — it cannot distinguish one bad week from three bad years — and a `blind_window` only exists for outages long enough to also lose messages, so short flaps leave no trace at all. Neither answers "how often". Worse, both are blind to the restart observed at 08:27 on 2026-08-03, which the watchdog never recorded because Supervisor's own add-on watchdog repaired it between two hourly polls.
  learned: **a counter and a log are not the same instrument, and polling bounds what either can see.** A lifetime counter answers "has this ever happened"; only timestamped events answer "how often, lately". And any poll-based observer systematically undercounts events shorter than its interval — shortening the interval (60 → 5 min) narrows that blind spot but cannot close it, because the repair is performed by a faster agent than the observer. Closing it requires the observed process to report its own start.
  criterion now: ISC-156, ISC-159, ISC-162, ISC-163.
- 2026-08-03 | conjectured: this ISA was the system of record for the cleaning app, so a later session could reconstruct what had happened to it.
  refuted by: a probe of the whole 88K file for `operator`, `operational event`, `restart count` and `transcript ingest` returned **zero hits** across 156 criteria. Josh's 2026-08-02 transcript ingest — a deliberate repair of a five-day blind window, and the only reason today's cleaning assignment was knowable — appears nowhere. Its sole trace is a `source: "backfill"` tag on 25 message rows: a side effect, not a record. A session reading this ISA cold would repeat the finding's stock advice to go check the phone, unaware the check had already been done.
  learned: **an ISA records what was built and why; without deliberate effort it records nothing about what was done to the running system.** The two are different classes of fact and the first does not imply the second. Structural completeness is not coverage — this file passes the E5 gate on all twelve sections while being silent on an entire category. Machine-written operational logs beat a discipline of remembering to write them down, because the discipline fails exactly when things are busy, which is when the events happen.
  criterion now: ISC-164, ISC-166.

- 2026-08-02 | conjectured: the review queue was working, since every item in it had been correctly parsed and correctly routed to a human.
  refuted by: sixteen items pending, fourteen of them about dates already past — one from June. Each was individually correct and the set was useless. Conflicts had self-suppressed after five stale days for months; the queue had no equivalent, because nothing ever asked what a queue with no exit does over time.
  learned: **a work queue needs an expiry rule as much as it needs an entry rule, and the absence of one is invisible at every single insertion.** Every item looked reasonable on the day it arrived; only the accumulation was wrong, and accumulation is not visible from any one decision. Second, subtler half: staleness had to be judged on what an item is ABOUT, not when it arrived — aging on the send date would retire a June message confirming an August cleaning, which is the normal shape of this business. Third: retire, never delete. The reconciler reads facts from expired messages, so removing the row would have removed evidence in order to remove a demand for attention.
  criterion now: ISC-150..152 shipped in 1.30.0; fourteen items retired on the first run, two genuinely-recent ones left standing.

- 2026-08-02 | conjectured: role attribution in the facts prompt was sound, since Itzel's confirmations were being classified correctly at scale (34 of them).
  refuted by: ingesting Darya's chat and reading the output — every message came back as `schedule_assertion`, a kind the prompt reserves for the host, including a textbook cleaner confirm. The role was computed by asking whether a cleaner's NAME appeared in the sender label, and no sender shape the system stores contains a name: live senders are JIDs, pasted ones are phone numbers. Both cleaners were labelled <host> in their own chats. The correct mapping sat unused in `cleaner_jids` the whole time.
  learned: **a heuristic that the model can paper over is the hardest kind to find** — Itzel's 34 correct confirms were the model overriding a wrong hint on unambiguous wording, which is exactly the wording that needs no hint. The bug was therefore invisible precisely where the system was easy, and active precisely where it was hard. Generalisation worth keeping: when authoritative data exists for a question, a heuristic answering the same question is not a fallback, it is a second source of truth that will diverge. Check for the authoritative store before writing the guess.
  criterion now: ISC-146/147 shipped in 1.29.0.

- 2026-08-02 | conjectured: the facts layer saw enough context to resolve ordinary conversational shorthand, since its prompt instructs it to resolve "yes", "that date" and weekday names against prior messages.
  refuted by: reading what the prompt is actually handed. It declares "Prior messages across all groups" and `_facts_history` filters to the target's own group — so the model was told its view spanned every chat while it spanned one. The consequence is not a parsing bug but a *reasoning* bug: asked whether a date is contested, it answers from a filtered corpus it believes is complete, and confidently finds no conflict because the other cleaner's commitment is in a thread it cannot see. Two chats, one shared schedule, and each cleaning routinely negotiated across both.
  learned: **a prompt that misdescribes its own inputs is worse than one that omits them** — omission makes a model hedge, misdescription makes it confident. This is the same failure family as the three measurement errors recorded on 2026-08-01 (cached reconcile read as live, buffered logs read as clean, an endpoint that could not see the thing being asked about): in every case the instrument answered a different question than the one asked, and the answer looked fine. Add: check what a prompt *claims* against what the caller *passes*, because nothing in the type system connects them. The fix also settled a design question worth keeping — the cheap way to widen context is not more raw text but the structured facts already extracted from it.
  criterion now: ISC-138..145 shipped in 1.28.0/1.28.1, live-verified against the real corpus (41 facts inside the horizon, which is also how the truncation-order bug was found).

- 2026-08-02 | conjectured: the liveness cycle closed on 2026-08-01 covered the system's health — the VPS watches the Pi, the Pi watches the bot, and the remaining gap was a shared upstream (LAN, ISP, mains) deliberately left undefended.
  refuted by: a five-day WhatsApp outage that every one of those watchers slept through. The bridge died 2026-07-28 15:00 and the tracker stayed *genuinely healthy* — iCal sync ok, calendar push ok, reconcile ok, nightly heartbeat delivered on time. The mutual-watch cycle was built around **the Pi dying**, and the Pi did not die; its *input* did. Meanwhile the two detectors that could have seen it are threshold-based on absent traffic (7 days whole-bridge, 14 per-channel) and would not have fired until Aug 4 and Aug 5 — after the Aug 3 cleaning they were supposed to protect. A cleaner reassignment agreed on Jul 30 never arrived, and tomorrow's calendar entry still names the wrong person at the wrong time.
  learned: **a liveness cycle proves the nodes are alive, not that the system is doing its job.** Every watcher here answered "is the process running", and the answer was truthfully yes for the process being asked about. Nobody was watching the *edge* — the pipe between two healthy nodes. The generalisation that hurts: this ISA had already recorded "a finding is not delivered until it reaches the human", and the same gap existed one layer down — a message is not received until it reaches the store, and nothing measured that. Two further corollaries earned the same day: (a) **absence of traffic is a lagging, ambiguous signal** and cannot be made prompt by shortening thresholds, because a quiet chat and a dead pipe are indistinguishable — so ask the container, not the message stream; (b) the bridge's own alarm *did* fire correctly at 15:00 that day and posted successfully to the Home Assistant panel, which this ISA's own Principles already say the host does not visit. That is the **second** time the exact same delivery mistake has appeared in this changelog, one entry apart.
  criterion now: ISC-122..137 shipped across bridge 1.3.0 and tracker 1.27.0 → 1.27.3, fault-injection verified.

- 2026-08-02 | conjectured: repeating every unresolved needs-attention finding nightly is what "keep telling me until it's fixed" requires.
  refuted by: the first real run, which produced fourteen unassigned bookings dated Sep 2026 – Jul 2027 in a single Telegram message, and would have produced them again every night indefinitely.
  learned: **"never go quiet" and "never become noise" are the same requirement, not opposing ones** — a digest that repeats everything gets skimmed, and a skimmed digest fails identically to the silence it replaced. The fix is a relevance horizon, not a volume cap: dated findings repeat only inside 21 days and resume on their own as the date approaches, while findings about *now* (bridge down, blind window) always repeat. Worth recording how this was caught — by deploying and reading the message that actually arrived, not by reasoning about the code. The rule that keeps earning its place: **read the artifact the human receives.**
  criterion now: ISC-128/129 revised and shipped in 1.27.3.

- 2026-08-01 | conjectured: the liveness chain terminated at a single unmonitored process (the VPS bot), so closing it required something outside the system — a third-party uptime pinger or a dead-man service.
  refuted by: that framing assumes a CHAIN. The Pi was already independently watched by the VPS's 25h dead-man, so having the Pi also watch the bot makes it a CYCLE, and a cycle has no unmonitored terminus. Better still, the dead-bot detector already existed — `_push_digest_to_vps` has always posted "Telegram alerts will not fire until this is fixed" — it was just delivered to the Home Assistant notification panel, the one surface this ISA's own Principles say the host does not visit. So the fix was a delivery change, not a new mechanism, and it needed no third party.
  learned: **before building a detector, check whether the signal already exists and is merely landing somewhere nobody looks.** The same principle that produced "a finding is not delivered until it reaches the human" applies to the system's own health, and it had not been applied there. Corollary that nearly cost a thread: the instinct was to add a dedicated polling watcher, which would have been one more thing that dies silently in order to reach a conclusion an existing proven path already reaches. The advisor's counter-correction is recorded honestly as ISC-120 — the two directions are independent *downstream of the Pi's NIC* and share LAN, router, modem, ISP and power upstream, so a WAN or mains outage defeats both. That is the right place to stop, because it is the one failure two people in a house notice within minutes for free.
  criterion now: ISC-96..119 shipped across 1.26.0 → 1.26.4 and live-verified; ISC-114 sits at `[DEFERRED-VERIFY]` pending Josh confirming his phone actually buzzed, and ISC-120/121 are open with stated reasons.

- 2026-08-01 | conjectured: deriving an attested value from durable state is safer than letting a caller pass it in, because a caller can lie by omission.
  refuted by: the cross-vendor audit. `sync_ok` was derived from the *freshness* of `last_sync`, which advances only on success — so if tonight's sync threw but yesterday's succeeded, the value was ~24h old, inside the 26h window, and the attestation reported success for a stage that had just failed. Freshness of the last success and outcome of the latest attempt are different facts, and `last_sync` could only ever answer the first. The derivation removed one lie and bought another.
  learned: **"derived from durable state" is not automatically safer — it is only as good as the question the stored fact actually answers.** The fix was not to go back to a passed-in value but to store the right fact: a per-attempt record written on every exit path of `sync_ical()`, so the manual button, the startup sync and the nightly job all prove the same way. A related catch from the same pass: distinguishing *absent* from *corrupt* matters, because a corrupt record that degrades into the freshness fallback produces a lying attestation, which is worse than silence since it satisfies the absence alarm too.
  criterion now: ISC-116 (per-attempt sync outcome) and ISC-118 (fail closed on corrupt) added and shipped in 1.26.3/1.26.4.

- 2026-08-01 | conjectured: the add-on's `print()` diagnostics — the `[gcal]`, `[digest]`, `[vps-push]` lines the whole fail-loudly strategy rests on — were reaching the log.
  refuted by: trying to confirm a phone escalation had fired and finding werkzeug's access lines in journald but none of the app's own. Python block-buffers stdout when it is not a TTY, so those lines sat in a buffer until it filled. It also explains an anomaly noticed at the very start of this session and not chased: no "[digest] ran:" lines despite the digest demonstrably running nightly.
  learned: **a diagnostic you cannot read at the moment you need it is not a diagnostic**, and the retroactive consequence is the sharper half — every prior conclusion of the form "the logs showed no errors, so that stage was fine" carries no evidence and must be re-derived. This is the same shape as the session's other two measurement errors: reading a cached reconcile as if it were live, and reading "0 persistent notifications" from an endpoint that cannot see persistent notifications at all. Three instruments, three clean readings, none of which could see the thing being asked about.
  criterion now: ISC-115 (`PYTHONUNBUFFERED=1`) shipped in 1.26.1; log lines confirmed visible in real time immediately afterwards.

- 2026-08-01 | conjectured: the host's Telegram alerts meant the Google Calendar push had silently failed and left the calendar wrong — the detector worked and the repair didn't.
  refuted by: the VPS bot's journal, which retains what the add-on's 100-line log does not. Jul 29 quiet, **Jul 30 three new findings**, Jul 31 quiet, **Aug 1 three new findings** — and the two messages name *different bookings* (Jun 24/Jul 1 2027, then Jul 2/Jul 9 2027), each the newest reservation to arrive in the feed. The push was never broken. The nightly job ingests a new booking, `save_data()` fires the async push, and the reconcile reads Google Calendar milliseconds later while that push is still in flight. Every `gcal_missing_event` alert was manufactured by detection outrunning its own repair. Worse, the session's *own* opening diagnosis ("manual sync fixed it, 16 → 14") was itself an artifact: 16 came from the cached `/reconcile/last`, 14 from a fresh `/reconcile/run`, and the drop was attributed as cause and effect across two different measurements.
  learned: **a system that cannot say what it did leaves you reconstructing intent from side effects, and the reconstruction will be confident and wrong.** Both the original bug and my own misdiagnosis have the same shape — an absent record filled in by inference. The durable lesson is narrower than "add logging": when a cache and a live probe are both available, comparing across them silently invents causation; re-probe both sides with the same method before attributing a fix. And the corollary that actually paid: the fix's real value was never the repair, it was that the *next* occurrence is answerable in one glance instead of an hour of journal forensics.
  criterion now: ISC-43..93 shipped in 1.25.0 → 1.25.2 and live-verified, including fault injection that delivered a real Telegram message and recovered clean. ISC-94/95 deferred with stated reasons.

- 2026-08-01 | conjectured: with the council's six changes shipped and 27 tests green, 1.25.0 was done.
  refuted by: three independent gates, each catching what the previous one missed. **Fault injection** found that a bad calendar id made `/reconcile/run` return 500 — `fetch_tagged_events` raises by design, killing the whole reconcile, so the digest went *silent* during exactly the outage the new findings existed to announce; the alarm was unreachable in its own failure mode. **Cross-vendor audit** (gpt-5.5) found that an over-budget nightly push logged and returned without writing a status, so a wedged push read as healthy for up to 26h — and that the ISA criteria still described `gcal_sync_status` in `data.json` while the code shipped a sidecar, making the checklist look stricter than what it verified. **Advisor** found that a future-dated `last_ok_at` (the Pi has no RTC) suppressed staleness *forever*, and that the abandoned over-budget worker's late write clobbers the timeout record so a chronically wedging push reads as permanently healthy.
  learned: every one of those five defects fails in the **same direction** — presenting as healthy while broken — including the ones introduced by the fix for that exact failure mode. The bias is not in any one author; it is in the shape of the work. Writing a health check means imagining the failure, and the branches you *don't* imagine are the ones that default to quiet. So the gates are not redundancy, they are the only mechanism that samples the unimagined branch, and **the fault injection had to run against the deployed system, not the tests** — 27 green tests said nothing about a reconcile that 500s.
  criterion now: ISC-60, ISC-86..93 added and shipped; the deploy sequence for this project now ends in fault injection, not in a green suite.

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

### Operational history (2026-08-03, 1.32.0)

- ISC-156..162: unit — `python3 scripts/test_bridge_watchdog.py` → **26 tests, OK**. New `EventLogTests` cover sparse steady state, restart counting, the cap dropping oldest-first, malformed-timestamp tolerance, and probe-error de-duplication.
- ISC-163: live — `ha addons logs` tail reads `[watchdog] started — checking '27cbea7f_whatsapp-bridge' every 5 min`, and Supervisor `info` reports `{"version":"1.32.0","state":"started","interval":5}`.
- ISC-166: live — `GET /internal/snapshot` **HTTP 200** with `bridge_watchdog` and `ops_log` present at top level alongside the existing `gcal_push_status` / `sync_status`. Reports `restarts_lifetime: 1` (the Aug 2 heal, preserved from the pre-existing counter), `restarts_logged: 0` (event log starts empty by construction), `unacknowledged_blind_windows: 1`.
- ISC-167: happy-path live probe (snapshot returned 200 with both new keys) plus structural — both `_watchdog_summary()` and `_read_ops_log()` swallow their own exceptions and degrade to `{"enabled": True, "error": ...}` / `[]`.
- ISC-168, ISC-169: unit — `ConcurrencyTests` (8 threads through a `Barrier` into `check()`): `heals` equals the count of `restart` events and `checks == 8`, i.e. no thread's write was lost. Suite now **38 tests, OK**.
- ISC-164, ISC-165: `[ ]` — deliberately unverified. Proving them needs a real operator action (an ingest or a dismissal) to flow through `_log_op`; `ops_log` is legitimately `[]` until one does. No synthetic write was made, because a fake entry in an audit log is worse than an empty one.

- ISC-43: code — `AuthorizedHttp(creds, httplib2.Http(timeout=HTTP_TIMEOUT_S))` passed as `http=` (not `credentials=`, which googleapiclient rejects together).
- ISC-44: code — `HTTP_TIMEOUT_S = 30` module constant in `gcal.py`.
- ISC-45: measured — two real full syncs timed at 0.73s and 0.61s against a 30s per-call timeout; the nightly path is separately bounded by a 240s join().
- ISC-46: unit — `test_skipped_is_not_success`: `{'skipped': 1}` → ok False, outcome 'skipped'. This was the whole bug.
- ISC-47: unit — `test_three_outcomes_are_distinguishable`: {ok, skipped, failed} all reachable and distinct.
- ISC-48: code — `[gcal] SKIPPED — another sync already running` vs `[gcal] synced: {...}` vs `[gcal] push FAILED: ...`.
- ISC-49: live — `/internal/snapshot` after a real push: `ok: true, outcome: ok, at: 2026-08-01T10:52:46`.
- ISC-50: live fault injection — bad calendar id → `ok: false, outcome: failed, error: 'Google API error: <HttpError 404 ...>'`.
- ISC-51: unit — classification path verified; skip persists through the same `_write_gcal_status` call as every other outcome.
- ISC-52: live — all four fields present plus `last_ok_at` and `stats`.
- ISC-53: live — `at: 2026-08-01T10:52:46` parses with `datetime.fromisoformat`.
- ISC-54: code — `_write_gcal_status` writes a temp file then `os.replace()`s it, wrapped in a bare except that only prints.
- ISC-55: live+unit — snapshot inspected; `_redact_error` strips URLs to scheme+host so the calendar id cannot ride a finding.
- ISC-56: code — `_digest_scheduler` order is `sync_ical()` → `_nightly_gcal_push()` → digest, with the push called inline, not via `threading.Thread` at the call site.
- ISC-57: code — inner lock wait 120s < outer `join()` budget 240s, so a lock wait cannot consume the budget and mask a wedged push.
- ISC-58: unit — `_should_retry_push` over ok/failed/skipped/absent; the nightly job runs regardless of traffic, which is the cadence Dov's quiet-week attack required.
- ISC-59: grep — `app.py:308` still `threading.Thread(target=_gcal_push, args=(snapshot,), daemon=True).start()`; interactive callers unchanged.
- ISC-60: live fault injection — **caught a real pre-existing defect.** At 1.25.0 a bad calendar id made `/reconcile/run` return **500**: `fetch_tagged_events` raises by design, killing the whole reconcile, so the digest went silent during exactly the outage the new findings exist to announce. Fixed in 1.25.1; re-probed → **200** with both findings present.
- ISC-61: grep — no early-return keyed on push success in `reconcile.py`; the calendar re-read is unconditional.
- ISC-62: live — `gcal_push_failed` + `stale_push` emitted during the injected outage.
- ISC-63: unit — `test_threshold_exceeds_nightly_cadence`: 26h > the 24h cadence.
- ISC-64: live — during the fault, `counts.total == len(findings)` (16 == 16). Injected inside `run()` before `filter_and_sort`, so counts derive from findings; the ISC-40 bug cannot recur by construction.
- ISC-65: unit — every push-health finding dated today, not the status timestamp.
- ISC-66: unit — ids stable across repeated calls (`pipeline:gcal-push-failed`, `pipeline:stale-push`).
- ISC-67: unit — `gcal_status=None` → `stale_push`. Never pushed is not healthy.
- ISC-68: live — `gcal_push_failed` appears as its own kind alongside, not inside, the calendar-content kinds.
- ISC-69: unit — `test_failed_push_absorbs_gcal_findings`. Not exercised live: the injected fault also broke the *read*, so the content detector was skipped and there was nothing to absorb.
- ISC-70: live — all 14 `drift_unassigned` findings survived the outage untouched; only calendar-content findings are ever dropped.
- ISC-71: unit — `test_healthy_push_passes_gcal_findings_through`.
- ISC-72: live — the VPS received and rendered both new kinds through the unchanged allowlist.
- ISC-73: live — the delivered Telegram body contains `https://www.googleapis.com/…` with the calendar id stripped.
- ISC-74: file — `config.yaml` version `1.25.1`.
- ISC-75: bash — `py_compile` clean on `app.py`, `gcal.py`, `reconcile.py`.
- ISC-76: bash — 36 tests, all pass (`scripts/test_gcal_repair.py`).
- ISC-77: git — `bf7d989` + `ff608d8` pushed to `origin/master`.
- ISC-78: ssh — `ha addons info` reports version `1.25.1`, state `started`.
- ISC-79: live — post-recovery reconcile returns the same 14 `drift_unassigned` findings as before the work began.
- ISC-80: live — `gcal_push_status.ok == true` in the snapshot after a real push.
- ISC-81: live — `gcal_push_failed` present in a fresh reconcile while broken.
- ISC-82: live — VPS journal `cleaning digest message sent`, chatId 876…, 462 chars, naming the API failure.
- ISC-83: live — config restored → push `ok: true`, all pipeline findings cleared, counts back to 14.
- ISC-84: live — recovery sync reported `inserted: 0, deleted: 0`; the probe used a non-existent calendar id so no write ever reached the shared calendar. Host temp files removed.
- ISC-85: live — delivered message reads 'the calendar write did not succeed' and 'drift is currently unmeasured', not 'events missing'.
