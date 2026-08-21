---
title: Cleaning Schedule Tracker — Project ISA
slug: cleaning-schedule-addon
type: project
effort: E5
phase: complete
updated: 2026-08-21T12:55:00-07:00
progress: 324/363
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
- [x] ISC-164: Operator actions on the live system (transcript ingest, finding dismissal) are recorded **as actions** in an append-only ops log, not left to be inferred from their side effects.
- [DEFERRED-VERIFY] ISC-165: Anti: a failure to write the ops log must never fail the operation it was logging.
- [x] ISC-166: The watchdog summary and the ops log are readable off-host via `/internal/snapshot`, so a later session can read what was done to the system instead of reconstructing it from `source:` tags on data rows.
- [x] ISC-167: Anti: the reporting helpers must not be able to raise inside `/internal/snapshot` — it is the off-host reconciliation lifeline and must never 500.
- [x] ISC-168: Anti: the watchdog's load-mutate-save is one critical section — the scheduler thread and the on-demand `/internal/watchdog/check` route must not be able to overwrite each other's events.
- [x] ISC-169: Anti: watchdog state is persisted atomically (temp + `os.replace`), so a restart mid-write cannot truncate the file and silently reset the whole incident history to defaults.
- [DEFERRED-VERIFY] ISC-170: Anti: the Telegram digest must render EVERY finding the Pi pushed — the SDK triage formatter must not be able to drop a `needs-attention` finding to satisfy its line budget, and a section reading "none" must mean zero findings, not zero findings that fit.
- [x] ISC-171: EVERY liveness check is recorded, including the passes where nothing happened — a stable stretch must be evidenced by observations, not inferred from an absence of incidents.
- [x] ISC-172: The check log is retained on a trailing 30-day window rather than a row cap — "how stable has it been lately" is a question about time, so the retention must be too.
- [x] ISC-173: Anti: appending a check must not rewrite the whole log — a 30-day window at 5-minute polling is ~8,600 records (~530 KB), and a full rewrite every pass would be ~530 KB of SSD writes every five minutes.
- [x] ISC-174: Anti: a write torn before its newline must cost at most the record it was writing — it must not concatenate the next append onto itself and destroy that one too.
- [x] ISC-175: `summary()` reports the share of checks that found the bridge healthy, so a watchdog that silently stopped running reads as missing checks rather than as apparent perfect health.
- [x] ISC-176: The full check history is served from its own endpoint, not from `/internal/snapshot` — an ~8,600-record log must not ride a payload that already carries every booking.
- [x] ISC-177: The Telegram digest must render EVERY finding pushed — the triage model returns JSON citing finding ids per bullet, and coverage is a set-equality check in code, not a hope about prose.
- [x] ISC-178: Anti: a triage result that drops, invents, or double-cites a finding id is REJECTED and the deterministic formatter takes over — it renders all findings unconditionally.
- [x] ISC-179: Anti: a fallback to deterministic formatting is stated in the message itself — a silent downgrade is how the original defect hid, since both outputs looked well-formed.
- [x] ISC-180: Bridge stability is visible **inside the cleaning app**, not only via the API — a health signal the operator will not go and fetch is the same as no health signal.
- [x] ISC-181: The 30-day strip renders a day with NO recorded checks as GREY, never green — "we never looked" and "it was fine" must never render alike.
- [x] ISC-182: Anti: today's cell must be judged against the fraction of the day elapsed, not a full day — otherwise the most-viewed cell is permanently degraded and the strip cries wolf daily.
- [x] ISC-183: Anti: the bridge panel must not be able to break the home page — its context builder returns a rendered error state rather than raising.
- [ ] ISC-184: Anti: every ISC carries a `## Test Strategy` row — a wrap-up audit joins Criteria against Test Strategy per-ISC, because a stale table scores identically to a current one under a section-presence check.
- [x] ISC-185: The candidate list handed to the parser contains CLEANINGS, one date per row — a reservation carries two dates and only one of them is a cleaning, so a reservation-shaped payload is ambiguous by construction on every turnover day.
- [x] ISC-186: Anti: a check-in date must not appear anywhere in a candidate row — not as a field, not embedded in a label. 53% of cleanings fall on a day that is also the next guest's check-in, so any surviving check-in date puts today's date on two rows.
- [x] ISC-187: The auto-apply gate requires the model's independently-stated `cleaning_date` to equal the chosen booking's cleaning day; disagreement routes to human review and says which two dates disagreed.
- [x] ISC-188: Anti: a missing or blank `cleaning_date` fails CLOSED — an older cached result or a schema-ignoring reply is exactly the case not to trust, and absence must never read as agreement.
- [x] ISC-189: Anti: `confidence` is never treated as evidence that the chosen booking is correct — it is the model's certainty about its reading of the sentence, and a self-reported number that is never contradicted cannot detect its own error.
- [x] ISC-190: The word "today" means the same day to the prompt header and to the candidate list, both anchored on the message's LOCAL send day — live timestamps are UTC, so a string slice would label the wrong row "today" for any message sent after ~17:00 Vancouver.
- [x] ISC-191: The candidate window is anchored on the message's send day, not on today, so a backfilled message is offered the cleanings that existed when it was sent rather than whatever is current.
- [x] ISC-192: The gate's decision is a pure function (`_auto_apply_decision`) taking result + booking and returning (auto, reason) — a rule embedded in a 60-line handler can only be tested through a copy of itself.

### The confirmation that ratified the wrong time (2026-08-09, diagnostic)

*Provoked by Josh: "why do I keep seeing this notification about monday's cleaning? I feel
like the whatsapp messages have the context needed for the app to figure it out." He was
right on both counts, and the second half is the finding: the facts layer had already
extracted the correct date AND the correct time from both messages. Nothing on the write
path reads either.*

- [x] ISC-193: A confirmation that states a time writes that time to the booking's `clean_time`. `facts.py` already extracts `target_time` correctly ("see you on Monday at 11:00 am" → `11:00`); `_apply_booking_change("confirm")` never touches `clean_time`, so the stated time is parsed, stored, and discarded.
- [x] ISC-193.1: A **revision** of a previously agreed time is applied, not just a first agreement. Aug 10 carried a real `time_proposal` of 17:00 from Itzel on 2026-03-30 and a revision to 11:00 on 2026-08-08; the system holds the first and cannot represent the second. Latest-wins by fact timestamp, as `_fact_timeline` already does for confirm-vs-decline.
- [x] ISC-194: Anti: `ack_notified()` must not stamp a `clean_time` the cleaner has just withdrawn. It copies *current truth* into `cleaner_commitment`, so a confirmation carrying a new time re-ratifies the superseded one — and because commitment then equals truth, the notify queue goes quiet on exactly the booking that is now wrong.
- [x] ISC-194.1: Anti: writing `clean_time` from the facts layer must not overwrite a good value with a null or a misextraction — today's failure is wrong-in-a-stale-direction, which is visible; the naive fix trades it for wrong-in-a-confidently-extracted-direction, which is not. Gate on a non-`tentative` fact whose `target_date` equals the booking's cleaning day, and hold for review on disagreement rather than overwriting.
- [x] ISC-195: A disagreement between a `confirm` fact's `target_time` and the booking's `clean_time` produces a needs-attention finding. `target_time` currently appears **nowhere** in `reconcile.py` — the facts layer extracts a field no detector reads, so a six-hour error on tomorrow's cleaning is invisible to every probe the system has.
- [x] ISC-196: Anti: an `applied_change` finding must not report `cleaner: None` for a booking that has a cleaner. `_change_findings` hardcodes the field, and the VPS triage model faithfully renders the null as "no cleaner assigned; verify intended and assign if needed" — instructing the host to repair a thing that is not broken, on a booking assigned since April.
- [x] ISC-197: The change log feeding the nightly "what I changed" report is readable off-host, as the ops log and the watchdog check log already are. `change_log.json` lives in the add-on's private `/data` with no route; the digest can therefore assert something about the system that cannot be audited without a deploy.
- [ ] ISC-198: Bookings mutated by a pre-1.35.0 auto-apply are identifiable after the fact. The gate stops new bad writes; it does not mark or repair the 16-of-48 already made, so a laundered value is indistinguishable from a sound one and keeps generating digest traffic.
- [x] ISC-199: Change and auto-ack findings enter the nightly baseline, so the once-per-episode dedup can apply to them at all. `current_ids` is built from `result["findings"]` alone and that set is what persists as `finding_ids`; `changes` + `ack_findings` ride in `extra_findings` and are never recorded. The dedup is therefore **structurally absent** for this class — an identical id would re-send every night it falls inside the 24h window, so "each night had a different id" understates it.
- [x] ISC-200: The digest has a channel for *"here is what I did"* that is not an alarm. The bot builds exactly two sections — `⚠️ Actions needed` and `❓ Unresolved conflicts` — routed on `isActionKind(f.kind)`, with `severity` used only for sorting. An `informational` work report has nowhere to land, so it lands in an alarm bucket, and it landed in a *different* one on Aug 6 (Actions) than on Aug 8 (Conflicts). Regression from the 2026-08-03 coverage fix (ISC-177..179): the old prose formatter invented a `🔧 Changes applied` section, and constraining the model to a two-array JSON contract removed it without replacing it in code.
- [x] ISC-201: Anti: a null field must not cross to the VPS carrying a meaning it does not have. `cleaning.ts` renders `${f.cleaner ?? "unassigned"}`, so `cleaner: None` — which on an `applied_change` means *not applicable to this finding type* — arrives as the assertion *this booking has no cleaner*, and the triage model expands it into remediation advice. **ISC-41's standing caveat, realised: the allowlist protects keys, not values — and the first real incident came through a meaningless field, not a leaky one.** Omit the field rather than emitting a null.
- [x] ISC-202: Every booking write is serialized under `DATA_LOCK`. Six routes do `load_data()` … `save_data()` **outside** it — `sync_ical` (line 557, holding a stale copy across a ~15s network fetch), `assign` (2909), `confirm` (2928), `pay` (2937), `delete_booking` (2962), `add` (2972) — while the two-thread WhatsApp worker pool holds the lock. A concurrent last-write-wins is the leading unproven candidate for the `confirmed → False` reset between Aug 5 and Aug 7 that left no change-log record.

### Wiring the stated time through (2026-08-09, E5 — Josh: "yes fix it")

*Josh's two rulings at OBSERVE: a revision **auto-applies and is reported** (not held for
review — the calendar being wrong for two days is the failure we are fixing); and existing
gaps should be **written, not merely surfaced**. The second ruling turned out to have zero
rows once measured against live data instead of a stale repo snapshot — see Decisions.*

**The stated time reaches the booking**
- [x] ISC-203: `_apply_booking_change("confirm")` writes `clean_time` from a time stated in **that same message's** facts, when the fact's `target_date` equals the booking's cleaning day.
- [x] ISC-204: A revision wins — a stated time replaces an existing `clean_time`, latest by message timestamp. (Josh's ruling; the March 17:00 must yield to the August 11:00.)
- [x] ISC-205: Anti: an absent or null `target_time` must never overwrite a good `clean_time` with nothing.
- [x] ISC-206: Anti: only **cleaner-authored** kinds (`confirm`, `time_proposal`) may write. `schedule_assertion` is host-only by the role-tagged prompt and is **84 of 235 timed facts** in the live archive — the largest single bucket. Writing from it would let Josh's own plan masquerade as the cleaner's agreement. `unclear` carries a time 5× and must never write.
- [x] ISC-207: Anti: a `tentative` fact never writes. (Only 2 of 235 are flagged, so this is necessary and nowhere near sufficient — it must not be mistaken for the safety gate.)
- [x] ISC-208: Anti: a message asserting **two or more different times for the same date** writes nothing and routes to review. Real case in the archive: *"anytime after 11am and before 3pm"* produced facts of `11:00` **and** `15:00`. A range is not a time, and silently picking one end fabricates an agreement.
- [x] ISC-209: Anti: `target_time` is validated as `HH:MM` before use. 235/235 archive samples parse clean, which is evidence about the model's habit and not about the next sample.
- [x] ISC-210: The `clean_time` write happens **before** `ack_notified()`, so the commitment records the time she just stated rather than the one she just replaced.
- [x] ISC-211: Anti: when a stated time is present but deliberately not applied (range, bad format, host-authored), `ack_notified()` must not stamp the contradicted time — the drift stays visible instead of being ratified.
- [x] ISC-212: A `clean_time` change is recorded in the change log and reported in the digest as work done.

**Detection — the two ways a time can be wrong**
- [x] ISC-213: `time_mismatch` (needs-attention) when a cleaner-authored non-tentative fact's `target_time` disagrees with the booking's `clean_time`. This is the probe that would have caught Aug 10 on the morning of Aug 8.
- [x] ISC-214: `time_unagreed` (suggest) when an active cleaning has a cleaner, no `clean_time`, and falls inside the repeat horizon. **`gcal.py` substitutes `11:00:00` at three sites (110, 147, 188), so a missing time renders on the shared calendar as a specific, plausible, agreed-looking hour.** Absence must not fail open on the one surface the cleaners read. Live today: Darya, Aug 14 / Aug 21 / Aug 24.
- [x] ISC-215: Both findings are dated on the **cleaning date**, so the 21-day repeat horizon governs them rather than the STALE_DAYS filter dropping them.
- [x] ISC-216: Both ids are stable across nights, so they alarm once and can be dismissed.
- [x] ISC-217: Anti: the detector is pure — inputs passed in, no data access and no clock, consistent with every other detector in `reconcile.py`.
- [x] ISC-218: Anti: no `why` on the new kinds carries raw WhatsApp text — f-string templates over structured fields only (ISC-41 discipline).
- [x] ISC-219: Anti: `counts` agree with `findings` when the new kinds are present (the ISC-40 lesson, applied a third time).

**Digest honesty — stop reporting work as an alarm**
- [x] ISC-220: An `applied_change` finding carries the booking's actual cleaner instead of a hardcoded `None`.
- [x] ISC-221: Anti: the bot must never render a missing cleaner as the word "unassigned" — omit the clause instead. A null meaning *not applicable to this finding type* must not arrive as the assertion *this booking has no cleaner*.
- [x] ISC-222: The bot renders a third section, `🔧 Changes applied`, for `informational` findings. Restores the channel the 2026-08-03 two-array contract removed, so a work report has somewhere to land that is not an alarm bucket.
- [x] ISC-223: Anti: the ISC-177 set-equality coverage check still enforces that every pushed finding id is rendered exactly once, now across three sections rather than two.
- [x] ISC-224: Change and auto-ack finding ids enter the persisted nightly baseline, so the once-per-episode dedup can apply to them at all.

**Auditability and concurrency**
- [x] ISC-225: The change log is readable off-host via `/internal/snapshot`.
- [x] ISC-226: All six unlocked writers — `sync_ical`, `assign`, `confirm`, `pay`, `delete_booking`, `add` — perform their read-modify-write under `DATA_LOCK` (an `RLock`, so nesting is safe).
- [x] ISC-227: Anti: `sync_ical` must not hold `DATA_LOCK` across its network fetch — the feed is fetched first, the lock is taken only for the merge, or a ~15s stall blocks every WhatsApp worker.

**Ship and prove**
- [x] ISC-228: `config.yaml` version bumped.
- [x] ISC-229: `py_compile` passes on every changed Python file.
- [x] ISC-230: New unit tests exist and the full existing suite still passes (no regression).
- [x] ISC-231: Antecedent: the **real** 2026-08-08 message replayed through the confirm path yields `clean_time == "11:00:00"` — the defect that started this, reproduced as a passing test.
- [x] ISC-232: Bot suite passes and `tsc --noEmit` is clean.
- [x] ISC-233: Work committed and pushed to both repos.
- [x] ISC-234: Supervisor reports the new add-on version running on the Pi.
- [x] ISC-235: Live: a post-deploy `POST /reconcile/run` surfaces the three `time_unagreed` findings and introduces no regression in the pre-existing count.

**Found 2026-08-18 during a live reconcile — open**
- [x] ISC-236: A dismissed watchdog finding stays dismissed across a full reconcile run. *(CLOSED 1.37.5. Root cause was never the dismiss endpoint: `_run_full_reconcile` prepended `watchdog_mod.findings()` onto `result["findings"]` — the list `filter_and_sort` had already produced — so the dismissed filter had run before the finding arrived. Fixed by merging into `findings_raw` only and re-deriving. Live 2026-08-20T19:09: `dismissed` count 0 → 1, `bridge_blind_window` present in `findings_raw` and absent from `findings`, a dismissal Josh made two days earlier applying for the first time. This and the ranking defect (ISC-321) were tracked as two problems and were one line.)*
- [ ] ISC-237: The facts extractor resolves relative dates against **America/Vancouver**, not UTC. An evening message (`2026-08-18T02:44Z` = Aug 17 19:44 local) saying "see u tomorrow" was extracted as `target_date: 2026-08-19`; the clean actually happened Aug 18. Same conversation produced both Aug 18 and Aug 19 for the same event, so the bug is silent and self-contradicting rather than uniformly offset.
- [ ] ISC-238: Josh confirms a digest/alert notification **actually arrived on his phone** — not that the send returned 200. Carried here from `PROJECTS.md` 2026-08-18, where it had sat as index status with no home in the record. Open because the ISA's own principle says the alert channel is part of the alert: an unverified delivery path fails in the direction that feels fine.

### 1.37.0–1.37.4 — data durability, routing in code, subject resolution (2026-08-20)

- [x] ISC-239: `save_data` writes to a temp file in the same directory, fsyncs, then `os.replace`s — no truncating write of the live store.
- [x] ISC-240: A failed serialize leaves the previous `data.json` byte-identical and no `.tmp` file behind.
- [x] ISC-241: `load_data` raises `DataVanished` when `data.json` is absent but `.data-initialized` exists, instead of returning an empty store the next save commits.
- [x] ISC-242: Deleting `.data-initialized` still permits a deliberate fresh start.
- [x] ISC-243: `/internal/restore` does not call `load_data`, so the guard cannot block its own recovery path.
- [x] ISC-244: `_merge_ical_events` refuses a feed that would cancel every future active booking, at any count.
- [x] ISC-245: It also refuses one cancelling >= `MASS_CANCEL_MIN` and > `MASS_CANCEL_RATIO` of future active bookings.
- [x] ISC-246: A refused merge calls `save_data` zero times and mutates no booking status.
- [x] ISC-247: One genuine cancellation still applies — the guard does not break what it guards.
- [x] ISC-248: Every sync poll logs the absent-future-booking count, so a guard that never evaluated is distinguishable from one that passed.
- [x] ISC-249: `process_message` makes exactly one model call per message.
- [x] ISC-250: `parse_whatsapp_message`, `_auto_apply_decision`, `upcoming_booking_list` and `_relative_day` are absent from `app.py`.
- [x] ISC-251: `_route_from_facts` resolves a fact's `target_date` to exactly one active non-`custom_stay` booking, or writes nothing.
- [x] ISC-252: Anti: no booking identifier is ever read from model output — a fabricated `booking_uid` on a fact changes nothing.
- [x] ISC-253: Anti: the model is not shown the booking list, so a turnover day cannot present two rows bearing one date.
- [x] ISC-254: A fact naming a cleaner other than the sender never writes.
- [x] ISC-255: `tentative` facts and facts below `ROUTE_CONFIDENCE` are held with a stated reason.
- [x] ISC-256: A date carrying two bookings is held for a human, not resolved by guess.
- [x] ISC-257: A message that both confirms and declines one cleaning writes nothing.
- [x] ISC-258: One message decides many bookings; `applied_uids` carries the full set.
- [x] ISC-259: `haiku_result` is synthesized from routed decisions, so its `booking_uid` always resolves.
- [x] ISC-260: `_unread_messages` emits a finding for a message with `review_state: pending` or an unresolved `parse_error`.
- [x] ISC-261: That finding is dated to the cleaning it concerns, not to the message.
- [x] ISC-262: Anti: its `why` never carries message text (ISC-41's caveat, at a new site).
- [x] ISC-263: It is wired into `reconcile.run()` — a shipped detector nothing calls is the 2026-06-11 failure.
- [x] ISC-264: Over the full 724-message live corpus it emits 4 findings, not a sweep of history.
- [x] ISC-265: `resolve_subjects` collapses findings about one booking into one statement before any ranking.
- [x] ISC-266: A finding carrying no `booking_uid` joins by date when exactly one booking sits on it.
- [x] ISC-267: The merged finding takes the MAX severity of its group, so it still repeats nightly.
- [x] ISC-268: The merged finding carries the resolved `booking_uid`, so the one-tap action survives.
- [x] ISC-269: Anti: calendar-projection findings are never merged into a cleaning.
- [x] ISC-270: Anti: health findings never join by date, though they are stamped with today.
- [x] ISC-271: A finding is dismissed when its own id is, or when every absorbed id was.
- [x] ISC-272: Findings rank by decision state; an evidence-free finding no longer outranks one holding the answer.
- [x] ISC-273: The Conflicts-tab Assign button keys on `decision == "approve"`, not a kind whitelist.
- [x] ISC-274: `_schedule_vs_bookings` gates on confidence and `tentative`, and `schedule_mismatch` is `suggest`.
- [x] ISC-275: `_facts_vs_bookings` honours `tentative`.
- [x] ISC-276: `/admin/reprocess-facts` returns `409 needs_confirmation` without `confirm=1` and writes nothing.
- [x] ISC-277: The VPS payload carries `decision`; the bot validates it against a closed enum and drops anything else.
- [x] ISC-278: The bot accepts a payload from an add-on that omits `decision`, so the two deploy independently.
- [x] ISC-279: The triage prompt groups by subject, not by kind.
- [x] ISC-280: Live: the Sept 10 booking reads Itzel / 11:00:00; Nov 24 reads Itzel / 13:00:00.
- [x] ISC-281: Live: add-on 1.37.0 is installed and `started` on the Pi. [BLOCKED — deploy commands refused by the session's command classifier; Josh runs them]
- [x] ISC-282: Live: post-deploy snapshot returns 200 with booking count 67, unchanged from the pre-deploy baseline.
- [x] ISC-283: Live: `/data` holds zero `data.json.tmp*` files after a save.
- [x] ISC-284: Live: the first real inbound message makes one model call and routes correctly.
- [x] ISC-285: Live: the next nightly digest arrives and leads Sept 10 with the confirmation rather than the gap.
- [x] ISC-286: The bot is redeployed to the VPS with the subject-grouping triage prompt.
- [x] ISC-287: `notify_ack` compares acknowledgement evidence as aware instants, never as raw strings.
- [x] ISC-288: Both stored timestamp shapes parse — UTC-`Z` (241 of 724) and naive local (483).
- [x] ISC-289: A naive stamp resolves through `zoneinfo`, so it is correct in PDT and PST alike.
- [x] ISC-290: Anti: an unparseable stamp returns `None` rather than sorting as long-ago.
- [x] ISC-291: Anti: identical instants do not resolve to "strictly after" via the `.000Z` suffix.
- [x] ISC-292: The candidate's `timestamp` stays a string for display; the instant rides in a separate key.
- [x] ISC-293: An untimed cleaning renders on GCal as an all-day event, not a fabricated hour.
- [x] ISC-294: Anti: `gcal.py`'s stay-block `T11:00:00` — the real Airbnb checkout — is untouched.
- [x] ISC-295: `_desired_events` contains no hardcoded cleaning time; both sites go through `_event_window`.
- [x] ISC-296: The `time_unagreed` finding text no longer asserts a default the code no longer applies.
- [x] ISC-297: `_parse_clean_time` is called from `load_data`'s lazy backfill — it had zero callers.
- [x] ISC-298: Anti: a real `clean_time` is never overwritten by a time parsed from prose.
- [x] ISC-299: Live: recoverable-but-unfilled cleaning times went 17 → 0 on the first load after deploy.
- [x] ISC-300: `_facts_vs_bookings` collapses to the cleaner's LATEST statement per (cleaner, date).
- [x] ISC-301: Anti: a decline superseded by a later confirm does not emit `decline_still_assigned`.
- [x] ISC-302: A standing decline — where the decline IS the latest word — still fires.
- [x] ISC-303: Anti: a `tentative` confirm never supersedes a standing decline.
- [x] ISC-304: Findings merged after `reconcile.run()` carry a `decision`; `app.py` stamps them.
- [x] ISC-305: Watchdog and health kinds are mapped explicitly, so `investigate` is a stated choice.
- [x] ISC-306: `time_mismatch` ranks `adjudicate` — two times for one cleaning is a contradiction.
- [x] ISC-307: Health findings are excluded from subject resolution by DETECTOR, not by a null cleaner.
- [x] ISC-308: An `unread_messages` finding joins its cleaning, though it names no cleaner.
- [x] ISC-309: `schedule_mismatch` is no longer emitted by `_schedule_vs_bookings`.
- [x] ISC-310: `schedule_unassigned` still fires — the half that surfaces a real gap.
- [x] ISC-311: Anti: the `schedule_mismatch` kind stays mapped, so 13 live dismissal ids still resolve.
- [x] ISC-312: Live: a fresh reconcile returns 17 findings, 0 without a decision, 0 `schedule_mismatch`.
- [x] ISC-313: Live: 2026-09-10 resolves to ONE finding, `approve`, leading with "latest is confirm".
- [ ] ISC-314: Live: a nightly digest is observed arriving on Josh's phone with the new ranking.

### 1.37.5 — distance decides urgency, one list decides order (2026-08-20, third pass)

- [x] ISC-315: `_drift` derives severity from the cleaning's distance instead of stamping a literal at the emit site.
- [x] ISC-316: An unassigned cleaning ≤ `UNASSIGNED_URGENT_DAYS` (30) out is `needs-attention`; beyond it is `suggest`. Josh's rule, stated 2026-08-20: *"for cleanings where nobody is assigned it's only a problem if that's less than one month from today's date."*
- [x] ISC-317: Anti: the proximity rule applies to `drift_unassigned` ONLY. `drift_new`, `drift_changed` and `drift_cancelled` describe a cleaner who IS assigned and may not have been told — a question Josh has not ruled on, and not one to widen into quietly.
- [x] ISC-318: Anti: a past-due unassigned cleaning is never demoted. `days` goes negative there, which is the most urgent case, and an off-by-one would silence the one finding that cannot wait.
- [x] ISC-319: Anti: `_drift` called without a reference date demotes nothing — a caller that forgets to pass today must fail toward shouting.
- [x] ISC-320: `run()` passes `today_str` into `_drift`. The wiring, not the rule: nothing else in the suite would notice if it stopped.
- [x] ISC-321: Watchdog findings enter `findings_raw` only, and the whole result is re-derived by `filter_and_sort` — so they pass through the decision ranking 1.37.2 stamped them for.
- [x] ISC-322: A dismissed watchdog finding stays dismissed through a full reconcile. Same change as ISC-321; see ISC-236.
- [x] ISC-323: `counts` is derived from the rendered list, not incremented by hand at the merge site — a third copy of a rule that already existed once.
- [x] ISC-325: Anti: a new finding suppressed by the horizon still enters `finding_ids`, so it never re-announces as new later; it resurfaces by `_drift` promoting it back inside the window, with no state kept.
- [x] ISC-326: Every booking mutation logs actor, operation and the booking uid it resolved to. Precondition for a CLI: once something other than a human at the HA UI can assign and delete, "who did this and to which booking" must be answerable after the fact.
- [x] ISC-327: A write against a uid that does not resolve is logged rather than silently dropped — the signature of a stale automation, and invisible everywhere else because `_apply_booking_change` returns before `_record_change`.
- [x] ISC-328: `_actor()` derives from the same three signals `_require_local_or_secret` gates on, so the write log cannot disagree with the door.
- [x] ISC-329: Live: line 1 of a fresh reconcile is a one-tap `approve`, not the blind window.
- [x] ISC-330: Live: needs-attention drops 14 → 3 over the same corpus.
- [DEFERRED-VERIFY] ISC-324: The digest's new-finding path applies the same horizon as the repeat filter, so a reservation arriving eleven months out does not push to the phone the night it lands. *(Code + comment shipped; the probe is the 08:00 digest on 2026-08-21 — a synthetic trigger would put an out-of-band message on Josh's phone. Follow-up: ISC-314's digest observation covers both.)*
- [DEFERRED-VERIFY] ISC-331: The first real booking write appears in the ops log carrying an actor. *(Three unit tests green and deployed; live evidence arrives on the next genuine confirm/decline. Follow-up: read `/admin` ops log after the next cleaner message lands.)*

### Open — found by review of 1.37.0–1.37.4, not yet fixed (2026-08-20)

- [ ] ISC-332: `auto_block` is written from the PRE-hold decision list. `process_message` synthesizes `haiku_result` and sets/pops `auto_block` before `_hold_destructive_on_blocks` runs, so a message mixing a routable decline with an unroutable confirm takes the `else` branch, pops `auto_block`, and then holds — leaving `review_state: pending` with **no stated reason**, the exact ISC-8 failure 1.37.0 was written to end, at a new address. `haiku_result` also still reports `action: decline` for a decline that was never applied, and that is what the Review tab and `/review/accept` read. Found independently by `/code-review` and by inspection; the routing tests exercise both functions purely and never `process_message`, which is why it survived 24 new tests.
- [ ] ISC-333: The contradiction sentinel in `_route_from_facts` is unreachable. `if prior and prior != kind` is evaluated before `if seen.get(uid) == "contradiction"`, and `"contradiction" != "confirm"` is true, so a third fact for the same uid re-enters the first branch and appends a duplicate `"message both confirms and declines"` string. Visible to a human as a repeated clause in the held-for-review text.
- [ ] ISC-334: A decline from a cleaner who is not the assigned one still sets `confirmed = False`. `_apply_booking_change` correctly refuses to clear a cleaner it does not own, then un-confirms the booking two lines later — so Itzel declining a date Darya has confirmed silently drops Darya's confirmation and manufactures a `time_unagreed`/notify gap. *(Checked against the Sept 10 history: Itzel was both decliner and assignee there, so this is not the origin of that incident.)*
- [x] ISC-335: A dismissed finding that becomes a merge primary suppresses the live findings absorbed under it. `_is_dismissed` handles "primary dismissed" and "every absorbed member dismissed", but nothing consults dismissal *during* `resolve_subjects` — so a dismissal made last month can swallow a finding that did not exist when it was made. The mirror of the case its own docstring defends.
- [ ] ISC-336: The mass-cancellation guard has no override and can wedge iCal sync indefinitely. `len(to_cancel) == len(future_active)` fires when exactly one future booking exists and a guest legitimately cancels it; every subsequent poll raises `SuspiciousFeed` identically, and behind fail-closed `load_data` that is a silent stop-the-world. Note the history: the advisor found this guard **inert** at low booking counts on 2026-08-20 and it now **over-fires** at low booking counts — a ratio on a denominator too small to have a ratio. Wants absolute floor + ratio + a logged, documented override.

### Backlog — the chat/CLI interface (catalogued 2026-08-20, not started)

Context: Josh, 2026-08-20 — *"I find the interface of the cleaning app confusing and I never use it to manage cleaning. I always just tell you to surface issues and resolve them in a chat like this."* The finding that reframes it: **the chat interface already exists and is undocumented.** This whole session managed the add-on with zero UI use — SSH, derive the shared secret from the Supervisor API, call endpoints that already ship. What was slow was re-deriving all of that. Of 37 routes only ~14 are Josh-facing UI; deleting every one leaves `app.py` over 4,000 lines, because the size is detectors and extraction, not buttons. So this is an interface change, not a reduction of the app — the daemon polls iCal, receives WhatsApp and pushes Google Calendar 24/7 and Claude Code does not.

- [ ] ISC-337: A `cleaning` CLI wraps the existing HTTP surface — status, assign, accept, dismiss.
- [ ] ISC-338: Anti: the CLI never accepts a booking uid. It takes a DATE and resolves it in code by the same rule `_route_from_facts` uses — 0 refuses, 1 applies, 2+ asks — ideally by calling that resolver rather than copying it. Without this the CLI is `parse_whatsapp_message` wearing a shell: a model transcribing a 56-character opaque key into a write, which is the pure-downside contract 1.37.0 deleted.
- [ ] ISC-339: Anti: the CLI holds zero business logic. Anything needing logic goes behind a route.
- [ ] ISC-340: The CLI derives the shared secret from the Supervisor API at call time. The repo is public; the skill documents the derivation and never the value.
- [ ] ISC-341: The nine `/admin/*` routes are reachable through the CLI, so raw `curl` is not institutionalised as a second idiom.
- [ ] ISC-342: A skill (or `CLAUDE.md` section) tells a cold session the CLI exists — the missing piece, not the capability.
- [ ] ISC-343: Route-hit logging records route + method + actor + timestamp, excludes the engine routes (`/internal/whatsapp/inbound` fires on every WhatsApp message), never logs payloads, and rotates.
- [ ] ISC-344: Anti: a UI route is deleted only when it has zero hits AND the CLI covers the operation. Rare is not unused — `/delete` may fire quarterly and read zero over a three-week window.
- [ ] ISC-345: A read-only home page survives as break-glass. Google Calendar is the human view of the *schedule*, but nothing outside the app renders pending reviews or engine state, and the only inspector of that state must not be the model path that might be the thing failing.
- [ ] ISC-346: Digest-item → action latency is instrumented before any Telegram inline-keyboard work. "Only if the away-from-desk gap actually bites" is the same unmeasurable judgement this project rejects everywhere else.
- [ ] ISC-347: Anti: no new inbound listener on the Pi for Telegram write-back. The Pi already holds a VPS credential (it pushes the digest nightly), so it polls for pending taps — the existing `telegram-write-queue-generalization` envelope is the right shape but drains to the desktop, not here.
- [ ] ISC-348: Anti: Telegram callback data carries uid + action only, never message text — the payload allowlist boundary holds in the reverse direction too.

### Council plan 2026-08-21 — digest fidelity before architecture (ISC-349..360)

Context: Josh's hypothesis that VPS triage working on projected findings is the structural
weakness went to a 4-agent Opus council. Unanimous verdict: projection accounts for at most
1 of the 4 failures in the 2026-08-21 digest; the pain is reconciler defects + the missing
desktop→Pi write-back. Key reframe (Quinn): the desktop/VPS asymmetry is turns-and-tools,
not location — a no-tools single-turn formatter cannot "resolve" anything wherever it runs.
Adjacent open item: ISC-335 (dismissal consulted during merge) belongs in the same W1 pass.

**W1 — Reconciler defect fixes**

- [x] ISC-349: Dismissals are subject-scoped with an evidence cutoff. Dismissing a finding records `booking_uid` + `dismissed_at`; a later finding about the same booking whose evidence is *entirely older* than `dismissed_at` is filtered, while any post-dismissal message re-opens the subject. Probe: unit replay of the Aug-16-dismissal / Aug-21-refinding sequence — suppressed on old evidence, surfaced when the Aug-18 messages are included.
- [x] ISC-350: Acceptance replay on the live corpus: with the 2026-08-21 data and the Aug 16 dismissal records, the rendered digest carries no bullet re-reporting the adjudicated Darya/Itzel contest, and the two Aug 18 unread messages still surface (they are genuinely new). Probe: reconcile → render → grep.
- [x] ISC-351: A failed re-extraction never falsifies a successful one. When a message already holds facts at the current prompt version, a later extraction error leaves it classified as *waiting for a decision*; `unread_messages` says "extraction failed" only when no facts exist. Probe: unit test forcing an API error on a message with stored facts. (Fixes the Sep 8 "extraction failed" lie — 3 of 4 queued messages were in this state on 2026-08-21.)
- [x] ISC-352: A coherent schedule story is not a conflict. When every cleaner's *latest* statement about a date is consistent with current booking state (assignee's latest = confirm, non-assignees' latest = decline, booking active), the merged finding takes `decision: observe` and severity informational. Probe: fixture mirroring Aug 21 → observe; fixture where the *assignee's* latest is decline → still adjudicate.
- [x] ISC-353: Anti: the ISC-352 downgrade never suppresses an unread/undecided message member — an unread message keeps its own severity even when the schedule story around it is coherent.

**W2 — Desktop→Pi write-back**

- [ ] ISC-354: A conflict resolved in a desktop session ends with `POST /reconcile/dismiss` carrying a dated reason. Enforced procedurally: the `reconcile-cleaning-schedule` skill and this repo's CLAUDE.md state it as a mandatory closing step. Probe: one live round trip — dismissal in `ops_log`, finding absent from the next digest payload.
- [ ] ISC-355: Anti: write-back is desktop/LAN-only. Nothing on the VPS or Telegram path can dismiss a finding — the wall holds in the reverse direction (consistent with ISC-347/348).

**W3 — Projection enrichment**

- [ ] ISC-356: `booking_status` and `absorbed` (finding ids) ride the projection: added to the Pi allowlist, accepted by the bot's parser, present in the triage prompt. Probe: capture one live payload, assert both keys.
- [ ] ISC-357: Anti: `quote` and `evidence` still never cross — the regression probe re-runs *after* the allowlist widening and passes.

**W4 — Measurement + relocation gate**

- [ ] ISC-358: Every nightly push appends its outgoing payload to `/data/digest_archive.jsonl` (30-day trailing retention, same pattern as `bridge_checks.jsonl`). Probe: two consecutive nights → two lines. Without this no replay of causality claims is possible — nothing currently retains past payloads.
- [ ] ISC-359: A 14-night ledger classifies every digest item Josh flags as wrong or already-resolved by cause: `pi-defect | projection-poverty | write-back-gap | tooling-asymmetry`. Lives beside this ISA; ≥14 nights covered before any verdict.
- [ ] ISC-360: Anti: no triage-relocation implementation begins before ISC-359's verdict is recorded as a Decision in this file. If relocation happens: prepaid API credits (never subscription OAuth in the add-on), stage flags retained in the payload so the VPS attestation counter survives.

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
| ISC-156 | unit | `check()` on a stopped bridge → the pass is recorded with its action | `restarted` present | python3 scripts/test_bridge_watchdog.py |
| ISC-157 | unit | 10 healthy passes → every record's action | all `none`, none omitted | test_bridge_watchdog.py |
| ISC-158 | unit | append > cap, then read back | length == cap, newest kept | test_bridge_watchdog.py |
| ISC-159 | unit | `summary()` restart counts vs seeded windows | 24h/7d/30d match | test_bridge_watchdog.py |
| ISC-160 | fault-injection | `summary()` over a record with a garbage `at` | returns, does not raise | test_bridge_watchdog.py |
| ISC-161 | unit | 5 consecutive probe failures under the sparse design | exactly 1 event | test_bridge_watchdog.py (superseded by ISC-171) |
| ISC-162 | render | `summary()["caveat"]` text | contains `>=` | test_bridge_watchdog.py |
| ISC-163 | boot-log | add-on log after deploy | `checking … every 5 min` | ssh ha addons logs |
| ISC-164 | live-probe | dismiss a finding → `/internal/snapshot` → `ops_log` | entry with id + reason | curl + jq |
| ISC-165 | fault-injection | make `ops_log.json` unwritable, dismiss a finding | dismissal still lands | **NOT RUN — deferred** |
| ISC-166 | live-probe | `GET /internal/snapshot` top-level keys | `bridge_watchdog` + `ops_log` | curl + jq |
| ISC-167 | live-probe | `/internal/snapshot` status with watchdog state present | 200, never 500 | curl -i |
| ISC-168 | concurrency | 8 threads through a `Barrier` into `check()` | `checks == 8`, no lost write | test_bridge_watchdog.py |
| ISC-169 | invariant | `save_state()` then glob the directory | no `*.tmp` residue, file parses | test_bridge_watchdog.py |
| ISC-170 | render | read the Telegram message the human receives vs `[vps-push]` counts | every pushed finding cited | ssh journal + phone — **due 2026-08-04 08:00** |
| ISC-171 | unit | 10 healthy passes → lines in the check log | 10, all `action: none` | test_bridge_watchdog.py |
| ISC-172 | unit | seed a 45-day-old row, prune at 30d | stale row dropped, recent kept | test_bridge_watchdog.py |
| ISC-173 | perf | 8,640 appends, measure file + per-record cost | ~532 KB, ~63 B/record, O(1) append | offline simulation |
| ISC-174 | fault-injection | append a line with no newline, then append again | both records readable | test_bridge_watchdog.py |
| ISC-175 | unit | 3 healthy + 1 down → `healthy_pct` | 75.0 | test_bridge_watchdog.py |
| ISC-176 | live-probe | `GET /internal/watchdog/history?days=1` | 200 + record count | curl + jq |
| ISC-177 | unit | triage JSON citing every id → rendered message | model's wording survives | bun test test/cleaning.test.ts |
| ISC-178 | unit | triage JSON dropping / inventing / double-citing an id | throws each time | bun test |
| ISC-179 | render | service-level: model drops a finding | message says fallback + names the finding | bun test |
| ISC-180 | live-probe | `GET /` on the add-on | contains `id="bridge-tab"` | curl + grep |
| ISC-181 | render | day with zero records in the 30-day strip | cell class `nodata`, never `ok` | offline render of FOCUS_TEMPLATE |
| ISC-182 | render | today's cell with a part-day of checks | class `ok`, not `partial` | offline render + live `GET /` |
| ISC-183 | fault-injection | force `_build_bridge_context()` to raise | home page still 200, error state shown | code inspection + live `GET /` |
| ISC-184 | invariant | set-difference of ISC ids in Criteria vs Test Strategy | empty | python3 audit in wrap-up |
| ISC-185 | unit | turnover pair (Aug 03→05, Aug 05→10); count rows whose `cleaning_date` is Aug 5 | exactly 1, and it is the Aug-5 cleaning | test_cleaning_match.py |
| ISC-186 | unit | assert no `checkin`/`checkout`/`label` key, and the check-in date absent from `str(row)` | absent in all rows | test_cleaning_match.py |
| ISC-187 | replay | 7 real archived messages (4 previously mis-attributed, 2 previously correct, 1 original) through the live prompt + `claude-sonnet-5` | 7/7 name the correct cleaning day | replay harness + Inference.ts |
| ISC-188 | unit | `cleaning_date` of None / "" / "   " with everything else valid | auto=False, reason names "(unstated)" | test_cleaning_match.py |
| ISC-189 | unit | the original failure replayed: conf 0.90, known cleaner, real booking, wrong date | auto=False | test_cleaning_match.py |
| ISC-190 | unit | `2026-08-06T01:00:00.000Z` (18:00 PDT Aug 5) through `_msg_local_day`; header vs list agreement | Aug 5 both | test_cleaning_match.py |
| ISC-191 | unit | February booking with an August anchor, then a February anchor | [] then 1 row, "tomorrow" | test_cleaning_match.py |
| ISC-192 | unit | gate called directly across 8 cases incl. every pre-existing clause | each clause still bites | test_cleaning_match.py |
| ISC-193 | unit | replay the 2026-08-08 message through the confirm path; read back `booking.clean_time` | `11:00:00` | test_cleaning_match.py |
| ISC-194 | unit | confirm a booking whose `clean_time` differs from the fact's `target_time` | commitment carries the STATED time, or no commitment is stamped | test_cleaning_match.py |
| ISC-195 | unit + live | detector over a booking at 17:00 with a `confirm` fact at 11:00; then `POST /reconcile/run` | ≥1 needs-attention finding naming both times | pytest + curl |
| ISC-196 | unit | `_change_findings` over a change on a booking with `cleaner: "Itzel"` | finding `cleaner == "Itzel"` | pytest |
| ISC-197 | live | `GET /internal/snapshot` → top-level key for the change log | present, ≥1 record | curl + jq |
| ISC-198 | live | scan bookings for a marker distinguishing pre-1.35.0 auto-applied writes | every such booking flagged | curl + python |
| ISC-199 | replay | feed the Aug 6/7/8 finding sets to the digest differ; assert change ids appear in the persisted `finding_ids` | ≤1 message per booking per episode | pytest |
| ISC-200 | replay | Aug 6 + Aug 8 payloads through `renderTriageResult` | change report lands in a third, non-alarm section | bun test |
| ISC-201 | unit | a finding with no cleaner field vs one with `cleaner: null` | neither renders the word "unassigned" | bun test |
| ISC-202 | static | AST/grep for `load_data()`…`save_data()` pairs whose enclosing function lacks `DATA_LOCK` | 0 | python script |

| ISC-203 | unit | real Aug-8 fact record through `_stated_clean_time` | ("11:00:00", None) | test_clean_time.py |
| ISC-204 | unit | confirm on a booking already at 17:00 with a stated 11:00 | clean_time == 11:00:00 | test_clean_time.py |
| ISC-205 | unit | confirm with no timed fact | clean_time unchanged | test_clean_time.py |
| ISC-206 | unit | schedule_assertion / unclear vs confirm / time_proposal | first two refuse, last two write | test_clean_time.py |
| ISC-207 | unit | tentative fact | (None, None) | test_clean_time.py |
| ISC-208 | unit | two distinct times for one date | refuses, reason names both | test_clean_time.py |
| ISC-209 | unit | six malformed time strings | all refuse | test_clean_time.py |
| ISC-210 | unit | commitment after a confirm carrying a new time | commitment holds the NEW time | test_clean_time.py |
| ISC-211 | unit | confirm whose time is unusable | no cleaner_commitment written | test_clean_time.py |
| ISC-212 | unit | clean_time in `watched` tuple of `_record_change` | present | grep + test_clean_time.py |
| ISC-213 | replay | live archive, Aug 10 booking rewound to 17:00 | exactly 1 time_mismatch | python + live snapshot |
| ISC-214 | live | `POST /reconcile/run` after deploy | 3 time_unagreed (Darya Aug 14/21/24) | curl |
| ISC-215 | unit | unagreed booking dated 2027-05-16 | no finding | test_clean_time.py |
| ISC-216 | unit | detector run twice | identical id lists | test_clean_time.py |
| ISC-217 | unit | detector run twice, same inputs | identical output, no clock read | test_clean_time.py |
| ISC-218 | unit | scan every `why` for the evidence quote | absent | test_clean_time.py |
| ISC-219 | live | counts.total vs len(findings) with new kinds present | equal (18) | curl + python |
| ISC-220 | unit | `_change_findings` over a booking with cleaner Itzel | finding.cleaner == "Itzel" | test_clean_time.py |
| ISC-221 | unit | finding with null cleaner through the bot renderer | string "unassigned" absent | bun test |
| ISC-222 | unit | informational finding through renderTriageResult | lands in third section | bun test |
| ISC-223 | unit | id dropped from all three arrays; id cited twice | both throw | bun test |
| ISC-224 | unit | resolved diff taken over finding_ids only | 0 phantom resolutions | test_clean_time.py |
| ISC-225 | live | `/internal/snapshot` → change_log key | present, >=1 record | curl + jq |
| ISC-226 | static | AST scan for load_data/save_data pairs lacking DATA_LOCK | 0 (was 6) | python script |
| ISC-227 | code | `sync_ical` body: fetch before `with DATA_LOCK` | load_data inside lock | read |
| ISC-228 | file | config.yaml version | 1.36.2 | grep |
| ISC-229 | build | `python3 -m py_compile app.py reconcile.py` | exit 0 | bash |
| ISC-230 | unit | all nine suites | 244 pass, 0 fail | bash loop |
| ISC-231 | unit | verbatim archive fact record replayed | clean_time == 11:00:00 | test_clean_time.py |
| ISC-232 | build | `bun test` + `bunx tsc --noEmit` in pai-telegram-bot | pass, clean | bun |
| ISC-233 | git | both repos pushed | remote sha matches | git |
| ISC-234 | live | Supervisor addon info | version 1.36.2, started | ssh ha |
| ISC-235 | live | post-deploy reconcile vs pre-deploy baseline | 15 pre-existing unchanged | curl |
| ISC-236 | behavior | dismiss a `bridge_blind_window` id, then `POST /reconcile/run` | id absent from `findings` | curl + jq |
| ISC-237 | unit | replay an evening-local message with a relative date through `facts.py` | target_date == local tomorrow | test file |
| ISC-238 | live | Josh reports receipt on-device after a digest run | confirmed by Josh | ask |
| ISC-349 | unit | replay Aug-16 dismissals against Aug-21 findings, with and without the Aug-18 messages | suppressed / re-opened respectively | test file |
| ISC-350 | behavior | full reconcile + render on 2026-08-21 corpus snapshot | no Darya/Itzel contest bullet; 2 unread bullets present | script + grep |
| ISC-351 | unit | force API error on a message with stored current-version facts | classified undecided, wording ≠ "extraction failed" | test file |
| ISC-352 | unit | Aug-21-shaped fixture vs assignee-declines fixture | observe / adjudicate respectively | test file |
| ISC-353 | unit | coherent-schedule fixture containing an unread message | unread member keeps needs-attention | test file |
| ISC-354 | live | resolve one real conflict in a session, POST dismiss, check next payload | ops_log entry + finding absent | curl + jq |
| ISC-355 | invariant | grep VPS bot + Telegram callback paths for any dismiss call | zero matches | rg |
| ISC-356 | live | capture one nightly payload | booking_status + absorbed keys present | jq |
| ISC-357 | invariant | re-run quote/evidence exclusion probe after allowlist change | zero raw-text fields in payload | test file + jq |
| ISC-358 | live | read /data/digest_archive.jsonl after two nights | ≥2 lines, valid JSON each | ssh + wc |
| ISC-359 | artifact | ledger file beside this ISA | ≥14 dated nights, every flagged item cause-tagged | read |
| ISC-360 | invariant | no relocation branch/commit before the ISC-359 Decision exists | grep Decisions for verdict first | git log + read |

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
| digest-dismissal-semantics | subject-scoped dismissal with evidence cutoff (+ close ISC-335 in the same pass) | ISC-349, ISC-350 | — | true |
| parse-error-truthfulness | failed re-extraction cannot falsify a successful one | ISC-351 | — | true |
| schedule-consistency-downgrade | coherent latest-statements story renders as observe, not conflict | ISC-352, ISC-353 | digest-dismissal-semantics | false |
| desktop-write-back | session-resolved conflicts POST /reconcile/dismiss as a mandatory closing step | ISC-354, ISC-355 | — | true |
| projection-enrichment | booking_status + absorbed ids cross the wire; quote/evidence still never | ISC-356, ISC-357 | — | true |
| digest-measure-gate | nightly payload archive + 14-night cause ledger gating any triage relocation | ISC-358, ISC-359, ISC-360 | — | true |

## Decisions

- 2026-08-21 12:00: **Council verdict on triage placement (4 Opus agents, unanimous): keep VPS triage; projection is not the primary cause.** Josh's hypothesis — that the triager working on projected rather than full facts is the structural weakness — attributed at most 1 of the 4 failures in that morning's digest, and that one was a detector defect first. The plan instead: reconciler defect fixes + desktop write-back + projection enrichment (ISC-349..357), with relocation gated on a 14-night measured ledger (ISC-358..360). Key reframe (Quinn): the desktop/VPS capability asymmetry is *turns-and-tools, not location* — a no-tools single-turn formatter cannot resolve anything wherever it runs. Withdrawn objection worth keeping (Odum): Pi-side triage on prepaid API credits with stage flags retained would survive both the billing audit and the attestation counter, so relocation stays a live option, not a dead one.
- 2026-08-20 (wrap): **Structural — 110 criteria (ISC-239..348) were living inside `## Decisions`, not `## Criteria`.** The drift began with the 1.37.0 pass appending after the last decision bullet rather than into the criteria list, and four passes compounded it silently. Moved wholesale, with the ISC id set asserted identical before and after (351 both sides) and the bare ISC-239..314 run given a dated header — unheaded, it would have read as belonging to the 2026-08-09 block it now sits under. Worth recording because it is this project's own recurring shape: **a section-presence check scores a drifted file identically to a correct one**, which is exactly what ISC-184 says about the Test Strategy table. Nothing in the repo would have caught this; it surfaced only because a reconcile printed the section boundaries.
- 2026-08-20 (wrap): **Index trap-ledger — one line left `PROJECTS.md`, none vanished.** *"⚠️ `bridge_blind_window` is CLOSED and **unclearable** — dismissals re-inject (ISC-236). It leads every digest until fixed."* → **IN** ISC-236, which now carries the closure, the root cause and the live evidence. The trap is retired rather than relocated: 1.37.5 fixed it. Replaced by a pointer line naming ISC-332..348 so a cold session finds the open findings and the CLI direction. `PROJECTS.md` 14,946 → 14,701 chars — well under the 30K rail, no trimming attempted beyond what this session finished.
- 2026-08-20: **Scoped the proximity rule to `drift_unassigned` and left `drift_new` shouting.** Josh's rule named cleanings "where nobody is assigned"; `drift_new` describes a cleaner who IS assigned and has not been told. Live after the change, `drift_new:2026-11-24` still reads needs-attention at position 7. Flagged to him rather than silently widened — the requested scope is the deliverable, and quietly extending a rule to an adjacent case is how a stated preference becomes an inferred one.
- 2026-08-20: **Deployed rather than staged.** 1.37.5 restarts a live household scheduler mid-evening. Shipped because the whole value of items 1–3 is what the 08:00 digest looks like, the change is rollback-able to 1.37.4 by version bump, 15 suites were green, and the effect had already been simulated over the live corpus with the same input on both sides before the deploy. This project's established rhythm is deploy-then-probe — 1.37.2 and 1.37.3 were both found *"by probing the live system after deploy, not by testing."*
- 2026-08-20: ❌ **Rule 2a did not fire and is not back-filled.** `codex` is not installed on joshua-Ubuntu-PC, so `CrossVendorAudit.ts` had nothing to invoke. Every adversarial pass on 1.37.5 — the `/code-review` fan-out, the advisor, and the executor — ran on Anthropic-family models and shares their blind spots. Recorded as a miss (ISC-42 remains unsatisfied) rather than papered over with a same-family audit wearing a cross-vendor label, per the standing rule that a doctrine gate can pass while not having run.
- 2026-08-20: Delegation floor (E3 soft ≥2) met at 1 — the `/code-review` skill, which fanned out and independently found ISC-332 and ISC-333. Show-your-math on the second: the remaining work was surgical edits to two files I had already read closely in the same session, and a second write-agent on those files would have tripped the isolation gate for no coverage gain. Thinking floor met at 6 (ContextSearch, ISA, RootCauseAnalysis, SystemsThinking, Advisor, ReReadCheck) against an E3 hard floor of 4.

- 2026-08-18 (wrap): **Index trap-ledger — three lines left `PROJECTS.md`, none vanished.** (1) *"Deploy: bump `config.yaml` → push → `ha store reload && ha addons update 27cbea7f_cleaning-tracker` over SSH"* → **IN** `cleaning-schedule-addon/CLAUDE.md:731` (+ host/key in `HomeAssistant/CLAUDE.md`); pure procedure the record states in full. (2) *"Live P0: bridge session-corruption can drop messages; re-pair is manual"* → **IN** `ISA.md:618` (the 2026-07-24 GOTCHA) and `CLAUDE.md:464`; both state it more accurately than the index did — the index line invited re-pairing as a first response, and the record's precondition is that re-pair is the LAST step. Replaced in the index by a pointer, not deleted blind. (3) *"Needs Josh: confirm the test phone notification actually arrived"* → **MOVED** here as ISC-238; it was live status with no home in any record, which is the one thing the index is not for. `PROJECTS.md` 11,955 → 11,881 chars.
- 2026-08-18: **The Jul 28 – Aug 2 bridge blind window is CLOSED — Josh backfilled it, and the evidence was already in the archive. Do not raise it again.** The `bridge_blind_window` finding's own `why` text says the undelivered messages "cannot be recovered"; that wording is stale and wrong. A message-archive check shows **27 `source: backfill` messages dated inside the window** (Jul 28 ×2, Jul 29 ×2, Jul 30 ×25), and they were present in the 12:32 snapshot — i.e. the backfill predates this session. Jul 31 – Aug 2 hold zero messages, which is consistent with quiet days and was not investigated further on Josh's instruction. ⚠️ **The lesson is mine: I relayed the finding's 'unrecoverable' claim three times in one session without once querying `data.messages` for that date range, which would have refuted it in a single pass.** A detector's prose is a conjecture about the world, not a reading of it — check the archive before repeating the alarm. ⚠️ Second defect, independent of the above: the finding cannot be cleared from the UI. `_run_full_reconcile()` merges `watchdog_mod.findings()` into both `findings` and `findings_raw` **after** `reconcile_mod.run()` has applied the dismissed-id filter, so the id is recorded in `data.dismissed_findings` (it is, with a corrected reason) and still re-injected by every subsequent full run — an unclearable Conflicts badge. Fix is a filter pass over the merged list; not shipped as of 1.36.2.
- 2026-08-09 23:40: **The authorised backfill was not built, because it had nothing to operate on.** Josh chose "backfill them all now" over "surface only", explicitly accepting a ~21-item notify-queue flood. Measured against a live pull, the candidate set is **zero** — the 21 came from an eleven-week-old `_live.json` (see Changelog). Both options he was choosing between therefore produce identical output today, so no scope was narrowed by not building a migration for an empty set; the detector (ISC-214) covers the class going forward and surfaces the three genuinely time-less bookings now. Reported to him with the numbers rather than silently dropped. If he wants the one-shot route anyway for future use, it is ~20 lines.
- 2026-08-09 23:55: **Capability honesty — two named at OBSERVE were not invoked as tools.** Council and Science appeared in `🏹 CAPABILITIES SELECTED`; Council was never run, and Science's *method* was performed (hypothesis-plural falsifiable replay: H1 the detector fires on the real Aug 10 case, H2 it goes quiet once repaired, H2b it does not flood at a wide horizon — and H2 failed on the first attempt, correctly, because the input snapshot was stale) but the skill was not invoked. Tool-invoked thinking capabilities: IterativeDepth, SystemsThinking, RootCauseAnalysis, FirstPrinciples, ISA, Advisor, plus ReReadCheck inline — **7 against an E5 hard floor of 8**. Recorded as a miss rather than back-filled with a ceremonial invocation, because the point of the floor is depth actually applied and a retroactive call would be the phantom the rule exists to prevent. The one question Council would genuinely have earned — whether skipping `ack_notified` on the unusable-time path overloads one flag with two jobs, which the advisor argued and I did not accept — is left open and named here so it can be taken up deliberately.
- 2026-08-09 23:40: ISC floor relaxation (E5 soft ≥256): 33 new criteria, each naming a single-tool probe in the Test Strategy table. Same show-your-math as the 2026-08-01 relaxation — inflating to 256 would fabricate granularity on a ~250-line change to a one-household add-on.
- 2026-08-09 23:40: Delegation — Forge given the VPS bot repo as a **disjoint** target while the Pi add-on was edited here, satisfying the isolation gate without a worktree (the two repos share no files). Cato run via `codex exec` directly rather than `Agent(subagent_type="Cato")`, and `model_used` checked in the returned JSON: `"GPT-5 Codex"`. Rule 2a genuinely fired rather than a same-family audit wearing a cross-vendor label.
- 2026-08-09 22:30: **Josh confirmed 11:00 and the live booking was corrected by hand** — `POST /assign` (`clean_time 17:00 → 11:00`), `POST /gcal/sync`, then `POST /review/notify/itzel` to clear the drift flag the correction itself raised. Blast radius of the notify clear was measured first and was exactly one booking (the same one), so it could not silently ack an unrelated pending item. Verified end-to-end on the shared calendar feed. ⚠️ **This repairs one row; it satisfies no ISC.** ISC-193..202 are all still open — the code is unchanged, and the next revised time will be dropped the same way.
- 2026-08-09 22:15: **Diagnosis only — no write to the Aug 10 booking, deliberately.** The correct `clean_time` is almost certainly 11:00, but changing it projects to the Google Calendar Michelle and the cleaners read, for a cleaning less than 24 hours away, on the strength of an LLM-extracted fact. That is precisely the class of write this ISA's third Principle reserves for a human. Surfaced to Josh with the evidence instead. Also **not** dismissed the finding: dismissing would silence the only signal pointing at a live error.
- 2026-08-09 22:15: `refined:` **the recurrence is three different defects wearing one costume, and only the third matters.** (1) The Aug 6 bullet is residue of the pre-fix wrong-booking write of Aug 5 — already understood, already fixed forward by ISC-185..192, but never *repaired backward* (ISC-198). (2) The Aug 8 bullet is real and correct — a genuine confirmation — rendered as a false alarm because `_change_findings` hardcodes `cleaner: None` and the VPS model turns that null into "no cleaner assigned; verify and assign" (ISC-196). (3) Underneath both, the booking's time is wrong by six hours and no probe in the system can see it (ISC-193..195). The nag Josh noticed was pointing at a real problem it was incapable of describing.
- 2026-08-09 22:15: Delegation floor (E3 soft ≥2) relaxed to 0 sub-agents. Show-your-math: the session operates under a standing instruction not to invoke the Agent tool. The work a delegate would have done — parallel archaeology across `app.py`, `reconcile.py`, the VPS journal and the GCal feed — was done directly with five targeted probes, and each probe's output is quoted in `## Verification` rather than summarised by an intermediary. Thinking floor met at 5 (RootCauseAnalysis, SystemsThinking, ISA, Advisor, ReReadCheck).
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

- 2026-08-03 11:10: 🔴 **OPEN — the last hop drops findings silently, and it is the one hop with an LLM in it.** Verified from two sides: the Pi logged `[vps-push] ok — 1 new, 2 carried`, and the message that arrived contained one bullet plus `Unresolved conflicts — none`. The SDK triage prompt (`~/dev/pai-telegram-bot/src/cleaning.ts`) instructs "keep the ENTIRE message to about 15 lines maximum" and to group findings, with no requirement that every finding id appear in the output — so a formatter under a line budget can drop a `needs-attention` finding and still look well-formed. This defeats the Principle "fail loudly, never silently" at the very last step, after five separate mechanisms upstream have worked correctly to get the finding that far. Note the deterministic `formatFallbackDigest` does NOT have this flaw — it renders every finding — so the failure is exclusive to the healthy path. Suggested fix: assert set-equality between pushed finding ids and ids cited in the rendered message; on mismatch, fall back to the deterministic formatter and say so. Not fixed today — it lives in the VPS bot, not this add-on.
- 2026-08-03 10:35: `refined:` this morning's claim that the digest "didn't tell you about the 14 unassigned bookings" was **wrong, and the ISA already said so** — the 21-day relevance horizon shipped 2026-08-02 (ISC-128/129, 1.27.3) suppresses dated findings beyond the horizon by design, and they resume on their own as the date approaches. All 14 are Sep 2026 – Jul 2027. Reading the Changelog before diagnosing would have caught this; the genuine omission was never the drift findings, it was the two that WERE pushed and never rendered.

- 2026-08-03 12:26: Shipped 1.32.1 + 1.33.0 as a **single** add-on update on Josh's explicit instruction, mid-turnover rather than after it. One restart, not two — 1.33.0's tree already contains the 1.32.1 lock fix, so updating once carried both and halved the exposure. The stated risk was accepted knowingly: the bridge's `forward()` has no retry, so an inbound WhatsApp message landing during the restart is dropped permanently, and a cleaner was on site. Recorded because the reasoning matters more than the outcome — the message watermark was unchanged across the deploy (636 messages, newest 2026-07-30T12:35), which shows nothing WAS lost but could never have shown that nothing COULD be.

- 2026-08-03 13:10: Left ISC-184 **failing on purpose** rather than backfilling 110 Test Strategy rows at wrap-up. Writing a `check | threshold | tool` line for ISC-86..155 today would mean inventing probes for work verified in earlier sessions whose actual evidence I did not observe — a table full of plausible retroactive entries is strictly worse than a gap that is visible and counted, because it converts a known unknown into a confident wrong. The honest remediation is per-increment: when a run touches an old ISC, give it a row then, from real evidence. The counter is the backlog. Rejected alternative: mark ISC-184 `[x]` on the grounds that the *gate* now exists and today's rows are complete — that is completion-by-format bias, the failure GPT-5.5 named on the financial ISA, and the criterion says every ISC has a row, not that a checker exists.

## Changelog

### 2026-08-20 (third pass) — two refutations from reviewing what shipped

- **conjectured:** 1.37.2 closed the `decision: null` gap on watchdog findings, and 1.37.4's
  decision-ranking put the reader's next action at the top of the digest. Both were verified: 19 of
  20 findings carried a decision, then 20 of 20 did, and Sept 10 collapsed to one `approve` line.
  **refuted by:** the live list at 15:00 the same day. `bridge_blind_window` ranks `investigate` and
  was sitting **above three `approve` items**. The field was stamped correctly and then never read —
  `_run_full_reconcile` prepends the watchdog findings onto `result["findings"]`, the list
  `filter_and_sort` has already produced, so they arrive downstream of the sort that would have used
  the stamp. The same line makes every dismissal inert, which had been filed separately as ISC-236
  for two days.
  **learned:** **stamping a field is not the same as routing through the thing that consumes it**,
  and a fix verified by reading the field it wrote cannot detect that. The probe that found it was
  looking at the rendered *order*, which is the artifact the human receives — the rule that keeps
  earning its place. The sharper half is the merge: two open items with one cause is exactly the
  "mechanism to catch a mechanism" debt the Principles target, and neither would have been found by
  reading the other's ticket. Corollary for the next hand-merge: three lists were being maintained
  in parallel at that site — `findings`, `findings_raw` and `counts` — and only the second was
  authoritative. Write to the authority and re-derive.
  **criterion now:** ISC-321, ISC-322, ISC-323 shipped in 1.37.5 and live-verified; ISC-236 closed.

- **conjectured:** the severity defect diagnosed that morning — *"a property of the function that
  spoke"* — was fixed by ranking on `decision` instead, so severity no longer decided what Josh read
  first.
  **refuted by:** the counts badge reading **14 needs-attention** on a day five things needed a
  human. Ranking stopped *reading* severity for order; nothing stopped severity being **wrong**.
  `_drift` still typed the literal at its emit site, and two other consumers still keyed on it — the
  nightly repeat filter (`severity == "needs-attention"`) and the counts the digest prints.
  **learned:** **removing a bad input from one consumer is not fixing the input.** The morning's fix
  was aimed at the symptom it could see, and the two consumers it did not touch kept the defect
  alive in a form that was quieter and therefore lasted. The general shape: when a value is wrong at
  its source, enumerate the readers before deciding the blast radius — grep for the field, not for
  the bug. And the correct fix was smaller than the workaround: deriving severity from the date at
  the emit site also stops far-future findings repeating and lets them promote themselves back on
  the right day, with no state to keep and nothing to remember to undo.
  **criterion now:** ISC-315..ISC-320 shipped in 1.37.5; needs-attention 14 → 3 on the same corpus.

### 2026-08-20 (second pass) — three refutations from deploying, not testing

- **conjectured:** a required `polarity` field on every fact was the right fix for
  `schedule_assertion` having no negative and no interrogative — the council reached it
  from first principles and from the iCalendar precedent.
  **refuted by:** the dismissal record, which nobody had counted before proposing the fix.
  `schedule_mismatch` is the most-dismissed kind in the system — **13 of 30** — and every
  dismissal carrying a written reason condemns it: four *"redundant: contested_cleaner for
  same booking+cleaner already dismissed"*, one *"mis-extraction: Josh's 'do we have you
  booked for July 24?' QUESTION was read as a host schedule assertion"*. Josh: *"the
  polarity field feels over complex."*
  **learned:** structurally correct and proportionate are different tests, and the second
  one is answered by what the defect has already cost, not by what the fix would buy. 624
  records, a prompt bump and a full reprocess to patch an asymmetry in one kind — when
  deleting the one finding that consumed it costs ten lines and the cleaner-side detector
  already catches the real cases.
  **criterion now:** ISC-309, ISC-310 — delete the finding, keep the useful half.

- **conjectured:** the 2026-08-18 message *"I can come tomorrow 7:00am"* proved the model
  resolves "tomorrow" to the send date, and a facts-prompt fix was a prerequisite for any
  `date_proposal` work.
  **refuted by:** `_msg_local_day`. The message was sent `2026-08-18T02:38:50Z` — 19:38 on
  **August 17** in Vancouver — so "tomorrow" IS August 18 and the extraction was right. The
  comparison had been made against the raw UTC date slice instead of the local send day.
  **learned:** this is the trap `_msg_local_day` exists to prevent, walked into while
  auditing for exactly this class of thing. Compare against the local day the codebase
  itself computes, never against `ts[:10]`.
  **criterion now:** no change — the behaviour was already correct, and the audit item was
  withdrawn rather than fixed.

- **conjectured:** requiring a non-null `cleaner` in `_subject_of` was what kept health
  findings out of a cleaning's group.
  **refuted by:** the live 1.37.2 deploy, where Sept 10 rendered as two lines — the schedule
  finding and *"a message about the 2026-09-10 cleaning is waiting for a decision"* — because
  `unread_messages` is in `MERGEABLE_DETECTORS` and names no cleaner. Two guards for one job,
  disagreeing.
  **learned:** exclusion belongs to the detector allowlist, which is durable — a health
  finding is health regardless of which fields happen to be null. A second guard on a
  correlated-but-different property will eventually disagree with the first.
  **criterion now:** ISC-307, ISC-308.

### 2026-08-20 — the model was reading correctly and the pipeline discarded it

- **conjectured:** the false "Sept 10 unbooked" digest meant the LLM had misread
  Itzel's confirmation — the interpretation layer was the weak part.
  **refuted by:** the stored `haiku_result`, correct on every semantic field
  (`confirm` / `2026-09-10` / `Itzel` / 0.90) and naming the right booking, with
  the facts call independently agreeing. Set membership showed the emitted uid
  resolved only after appending `@airbnb.com`.
  **learned:** the model failed at *transcription*, not comprehension, and the
  pipeline had handed it a machine's job — exact-matching a 56-character opaque
  key — while ignoring the machine-checkable field it got right. A field a model
  is asked to emit is a field it can get wrong; ask only for what the model alone
  can supply.
  **criterion now:** ISC-251, ISC-252 — route on the stated date resolved in
  code; never on a model-transcribed key.

- **conjectured:** the date-agreement gate (`cleaning_date == booking["end"]`)
  was a genuine cross-check, being "two fields that must agree".
  **refuted by:** reading the prompt that produced them. It instructed the model
  to answer `cleaning_date` first and *then pick the uid of the row with that
  date* — so the two were one estimate and a transcription of it. Agreement
  between a value and a copy of that value is the health-probe-reports-its-own-
  expectations shape, and it could only ever catch transcription failure, which
  is the sole error class the uid itself introduced.
  **learned:** two fields corroborate only when produced by independent routes.
  **criterion now:** ISC-250 — the gate and the field it guarded are both gone.

- **conjectured:** the five findings for one date were redundant noise from
  over-detection, and the fix was fewer detectors.
  **refuted by:** the repeat filter. Severity is a literal fixed at each emit
  site, and `persisting` requires `needs-attention`, so the only finding that
  survived past night one was `drift_unassigned` — whose entire input is one
  booking dict and whose `evidence` is a hardcoded `[]`. The digest did not
  mis-order once; it decayed toward noise by construction.
  **learned:** ranking by detector provenance inverts the information gradient,
  and any filter keyed on that ranking inherits the inversion. Resolution to one
  statement per subject must happen before ranking.
  **criterion now:** ISC-265 … ISC-272.

- **conjectured:** merging findings was a presentation change, safe to apply to
  every finding about a booking.
  **refuted by:** `test_gcal_repair.py`, which failed the moment
  `gcal_stale_event` was absorbed into a schedule finding — and by the merged
  Sept 10 finding inheriting `informational` from its primary, which would have
  silently stopped it repeating.
  **learned:** a merge is a claim that two findings have the same repair and the
  same audience. Severity must be the group's max, and the mergeable set is an
  allowlist so a detector added later surfaces on its own.
  **criterion now:** ISC-267, ISC-269, ISC-270.

- **conjectured:** the mass-cancellation guard needed a floor and a ratio.
  **refuted by:** the advisor pass, which pointed out that at three future
  bookings a feed cancelling all three is under the `>= 4` floor — inert exactly
  where this household lives.
  **learned:** a proportional guard needs an absolute total-wipe clause; the
  low-count regime is the one a ratio cannot see.
  **criterion now:** ISC-244.
- 2026-08-09 (late) | conjectured: the archive numbers quoted earlier in this session — 21 of 22 bookings carrying an unrecorded stated time, 11 turnovers since June where GCal rendered its 11:00 fallback against a contrary assertion — described the system as it is now, and justified Josh's ruling to backfill every one of them.
  refuted by: measuring against a live pull instead of the file the analysis had read. `_live.json`, sitting in the repo root, is a snapshot from **2026-05-23** — eleven weeks stale. Live: 22 active bookings, not 43; 8 with a cleaner, not 35; **3** with no time, not 22; and **0** of those three has any fact asserting a time. The authorised backfill had zero rows.
  learned: **a file in the working tree is not a probe, and an eleven-week-old artifact answers a question about May in the present tense.** This project already carries the rule in a narrower form — *never compare a cached reading against a live one and call the difference an effect* — and this is the same error one step earlier: not comparing a cache to live, but never checking whether the thing being read was live at all. The number was quoted with a caveat ("not re-verified"), which is what made it cheap to falsify; had it gone into the ISA unqualified it would have justified writing 21 bookings that do not exist. **Two survivors of the refutation:** the *principle* was right even though the count was fiction — `gcal.py` really does substitute `11:00:00`, so a missing time really does render as an agreed-looking hour, and that is now ISC-214 with three live instances. And the reasoning shape generalises: an agent handed a repo will read what is in the repo, so the freshness of its inputs is the caller's job, not the agent's.
  criterion now: ISC-214 (kept, re-grounded on 3 live rows); the one-shot backfill route was not built — see Decisions.
- 2026-08-09 | conjectured: the pipeline's job on a WhatsApp confirmation is to decide **which booking** it is about. Get the row right and the message has been understood; ISC-185..192 closed that question on 2026-08-06, so the wrong-booking era was over.
  refuted by: Josh, three mornings in a row — *"why do I keep seeing this notification about monday's cleaning? I feel like the whatsapp messages have the context needed for the app to figure it out."* He was right, and the evidence is embarrassing: on 2026-08-08 Itzel wrote *"see you on Monday at 11:00 am"*, `facts.py` extracted `{kind: confirm, target_date: 2026-08-10, target_time: "11:00"}` — **completely correct, date and time** — the parse layer independently picked the right booking, the gate passed, and the write path set `confirmed = True` and **nothing else**. `_apply_booking_change("confirm")` never touches `clean_time`. `ack_notified()` then copied the booking's existing `17:00` into `cleaner_commitment`, so the system recorded *"Itzel agreed to 17:00, via WhatsApp, 2026-08-07 21:35"* — an agreement she had **withdrawn in the very message being processed**. Commitment now equalled truth, so the notify queue fell silent. The shared Google Calendar reads **`🧹 Itzel · 5:00 PM`** for a cleaning she is arriving at 11:00 am. A fresh `POST /reconcile/run` returns 15 findings and **not one of them is about Aug 10**: `target_time` appears nowhere in `reconcile.py`.
  learned: **a field that is extracted but never read is worse than one that was never extracted — it makes the system look like it understood.** Every artifact in the chain says the message was comprehended: the facts record is right, the parse is right, the gate passed, the digest reported an applied change, the reconciler is clean. The single thing that did not happen is the one that mattered. The deeper shape is that this project has **two model calls per message and only one of them can write** — the parse call answers "which row?" and owns the pen; the facts call answers "what was actually said?" and is read only by detectors. So the layer with the better answer is structurally forbidden from acting on it, and the layer with the pen was never asked about time at all. Note the second-order failure: because `ack_notified` stamps *current truth* rather than *stated intent*, a correct confirmation is the mechanism that ratifies a superseded value and switches off the alarm that would have caught it. And the nag Josh complained about was the system's only remaining signal — pointed at the right booking, unable to say why.
  correction within this entry (probed after the advisor refused the first draft): the 17:00 is **not** a stale default and never was. `message_facts` holds a real `time_proposal` from **Itzel herself on 2026-03-30** — *"August 10 - 5:00pm"* — plus a host `schedule_assertion` restating it on 2026-04-13. So 17:00 was a genuine, negotiated, correctly-recorded agreement for four months. The defect is narrower and sharper than "a default got laundered": **this system can record a first agreement and cannot record a revision of it.** The first draft of this entry asserted staleness from the current value alone, which is a provenance claim made without a provenance probe — the same error shape the 2026-08-01 entry above is about.
  criterion now: ISC-193, ISC-194, ISC-195, ISC-196, ISC-197, ISC-198, ISC-199.
- 2026-08-06 | conjectured: `confidence ≥ 0.85` plus a known cleaner plus a known booking was a sufficient gate for writing to a booking unattended, because a high score means the model understood the message.
  refuted by: "Hello guys I'm here", sent 2026-08-05 at 12:12 local as Itzel walked into the cleaning for the stay checking out that day. Scored **0.90** and auto-applied to the stay checking *in* that day — marking the **Aug 10** cleaning confirmed on the strength of a message about Aug 5. The score was not wrong: she really was a known cleaner really confirming a real cleaning. It simply never spoke to the question that mattered. An audit of the whole archive found **16 of 48** auto-applied confirmations had done the same thing, 8 of them unambiguously ("I'm done", "the doors are closed", "see u today").
  learned: **a self-reported confidence is a claim about the model's reading, not about the row it picked, and nothing that is never contradicted can detect its own error.** This is the health-probe placebo in a new costume — the same shape as a sync heartbeat writing back the sha the builder *believed* it deployed. The fix is not a higher threshold and not a better-worded prompt: it is a second field that can disagree. The model now names the cleaning day in its own words and the gate requires it to match the booking it chose. Note the base rate this hid behind: the pipeline looked healthy for months because every individual decision looked confident and most of them were right.
  criterion now: ISC-187, ISC-188, ISC-189, ISC-192.
- 2026-08-06 | conjectured: the parser prompt was correct, since it stated plainly that "checkout date = cleaning day" — so the mis-matching had to be a model-reasoning problem to be fixed by better instructions.
  refuted by: reading the payload the prompt actually shipped. The header said it once; the candidate list then contradicted it sixty times, emitting **reservations** — `{checkin, checkout, label: "Aug 05 → Aug 10"}` — with the check-in leading the label. **53% of cleanings here fall on a day that is also the next guest's check-in**, so on most cleaning days two different rows displayed that day's date, one as a checkout and one as a check-in. Rewriting the instruction would have argued louder against the same data.
  learned: **when instructions and data disagree, the data wins, and the fix belongs in the data.** The unit of work shown to a model must be the unit of work being decided about: a reservation carries two dates and only one is a cleaning, so *any* reservation-shaped list re-creates this ambiguity forever. Emitting one date per row removes the mechanism rather than arguing with it — the model cannot match on a date it never sees. Corollary found in passing: the anchor for "today" was `ts[:10]` on a **UTC** timestamp, so every message sent after ~17:00 Vancouver was silently anchored to tomorrow.
  criterion now: ISC-185, ISC-186, ISC-190, ISC-191.
- 2026-08-03 | conjectured: a session that adds criteria, verifies them, and records Decisions and Changelog entries has kept the ISA whole.
  refuted by: the wrap-up audit. First pass: 0 of 28 new criteria (ISC-156..183) had a `## Test Strategy` row. Writing the gate to prove that fixed then revealed the real number — **110 of 185 criteria have no row**, in runs at ISC-11..14, 25..31, 46..53, 62..71, 74..77 and above all **ISC-86..155**, an unbroken 70-criterion gap covering the entire GCal-repair, attestation, liveness and facts-layer era. The table has been decorative since roughly ISC-85. Structure passed the E5 gate throughout, because `## Test Strategy` was `present` on the strength of 41 rows written months earlier.
  learned: **section-level completeness cannot see per-row rot, and the first measurement of a gap usually understates it.** A stale table scores identically to a current one under a presence check, so "all twelve sections present" was true and uninformative for months. Two compounding causes: Test Strategy is written at OBSERVE/PLAN, and *reactive* work — a user correction, a review finding, a defect surfaced mid-deploy — jumps straight to EXECUTE and never comes back for it; and nothing ever joined the two sections, so the omission was unobservable by construction. Note also the second-order error: the entry originally written here claimed the gap was 28, because it measured only the ISCs in front of it. Widen the probe before writing the lesson.
  criterion now: ISC-184 (open — the invariant is asserted, not yet met).
- 2026-08-03 | conjectured: shipping the check log behind `GET /internal/watchdog/history` delivered the stability record that was asked for.
  refuted by: Josh — *"I'm never going to do curl commands to read something like this."* True and predictable: the operator's interface to this system is the add-on's web UI, and everything else in it lives there. An API-only health signal is one nobody looks at.
  learned: **a monitoring feature is not delivered until it is delivered to where the human already is.** This project already had the lesson in a different costume — a finding that landed in HA persistent notifications rather than Telegram was "indistinguishable from no detection" — and I re-made it one layer down, in the instrument built to fix the first version. The reach question ("who sees this, where, without being told to look") belongs in the design, not in a follow-up.
  criterion now: ISC-180, ISC-181.
- 2026-08-03 | conjectured: logging only transitions was right, because a 5-minute poll writing 288 rows a day would bury the handful of rows that matter.
  refuted by: Josh, asking for the opposite — every check, with its action, on a trailing 30 days. The sparse design has a defect the volume argument hid: **an empty log is ambiguous.** Zero incidents and a watchdog that never ran render identically, which is the same "silence is indistinguishable from death" failure this project already has a Principle about, reappearing one level up in the instrument built to detect it.
  learned: **for an availability record, the uneventful observations ARE the signal.** 8,640 consecutive `started / none` rows is a claim with evidence behind it; an empty file is only a claim. Optimising a log for readability at the cost of its evidentiary value is the wrong trade — read the incidents through a filter (`?actions_only=1`), don't refuse to write the evidence. Corollary on volume: the objection was really about write cost, and that is solved by an append-only JSONL (O(1) per pass) rather than by recording less.
  criterion now: ISC-171, ISC-172, ISC-173, ISC-175.
- 2026-08-03 | conjectured: appending one line per check is trivially safe, so the only failure worth handling was an unparseable line on read.
  refuted by: `test_a_torn_line_does_not_break_the_history` — a write killed before its newline leaves an unterminated line, and the *next* append concatenates onto it, so one interrupted write destroys two records and the casualty is the newer one. The test was written expecting to pass and failed.
  learned: **append-only is not the same as append-safe.** The record you are about to write depends on the integrity of the one before it whenever the delimiter is part of the payload. Repair the terminator before appending. Also worth keeping: this was found by a test written to document intent, not by review of the code — the third time in this project that deploying-and-probing beat reasoning.
  criterion now: ISC-174.
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

### The confirmation that ratified the wrong time (2026-08-09) — diagnostic evidence

- Symptom, live — `journalctl -u pai-telegram-bot` on the VPS, four consecutive nights, all naming Aug 10 2026: `Aug 06 findings=1` "Auto-applied confirmation change (False → True via WhatsApp) for Aug 10 (2026) cleaning — cleaner is unassigned; verify assignment." · `Aug 07 findings=3` "notify-pending cleared after WhatsApp review… Itzel already notified" + "GCal cleaning event for Aug 10 (2026) is stale (Itzel)" · `Aug 08 findings=1` "confirmed status flipped False → True — no cleaner assigned; verify intended and assign if needed." · `Aug 09 findings=0` quiet.
- Contradiction, live — `GET /internal/snapshot` (fresh, HTTP 200, 549,570 bytes, `generated_at 2026-08-09T22:02:54`): the sole booking ending 2026-08-10 reads `cleaner: "Itzel"`, `confirmed: true`, `cleaner_since: "2026-04-20T17:47:27"`. "No cleaner assigned" was false on both nights it was sent.
- ISC-196 source — `app.py:1423` sets `"cleaner": None` unconditionally inside `_change_findings`; `cleaning.ts` `formatFindingLine` renders a null cleaner as `unassigned`, and the SDK triage path expands it into remediation advice.
- ISC-193 source — `_apply_booking_change` (`app.py:1438-1444`) writes `cleaner`, `cleaner_since`, `confirmed`, `last_wa_msg_id`. `clean_time` is absent from the confirm branch.
- ISC-193 evidence that the data was there — `data.message_facts` for the 2026-08-08T04:35:34Z message: `[{"kind":"confirm","cleaner":"Itzel","target_date":"2026-08-10","target_time":"11:00","confidence":0.95,"evidence":"see you on Monday at 11:00 am"}]`. Booking `clean_time` is `"17:00:00"`.
- ISC-194 source — `ack_notified()` writes `"clean_time": booking.get("clean_time")`, i.e. current truth, not the stated time. Resulting record: `{"cleaner":"Itzel","date":"2026-08-10","clean_time":"17:00:00","communicated_at":"2026-08-07T21:35:42","communicated_via":"whatsapp"}`.
- ISC-195 source — `grep -n "target_time" cleaning-tracker/reconcile.py` → **0 matches**. Live confirmation: `POST /reconcile/run` (HTTP 200) → `counts {"needs-attention": 15, "drift_unassigned": 14, "bridge_blind_window": 1, "total": 15}`, **zero findings referencing 2026-08-10**.
- Consequence, live — shared Google Calendar iCal feed, HTTP 200: `'🧹 Itzel · 5:00 PM' | 20260811T000000Z -> 20260811T020000Z` = 2026-08-10 17:00 America/Vancouver. Neighbouring real cleanings in the same feed run 11:00 AM / 12:00 PM / 12:30 PM / 1:00 PM / 2:00 PM, so 17:00 is the stale April default rather than a negotiated time.
- Aug 6 bullet provenance — the 2026-08-05T15:33:04Z message "Hello guys yes see u at noon" carries `haiku_result {action: confirm, confidence: 0.97, cleaning_date: null, booking_uid: <the Aug 10 booking>}` and `review_state: auto`, while its facts record correctly reads `target_date: 2026-08-05, target_time: "12:00"`. It was processed **one day before** the ISC-187/188 gate shipped, so `cleaning_date` was absent and could not fail closed. Deployed version confirmed current: Supervisor reports `1.35.1`, matching `config.yaml`.
- ISC-193.1 provenance, live — every fact ever recorded for `target_date 2026-08-10`, in order: `2026-03-23 schedule_assertion "August 10 - anytime after 11am"` · `2026-03-30 confirm + time_proposal Itzel "August 10 - 5:00pm" → 17:00` · `2026-04-13 schedule_assertion Itzel 17:00` · `2026-08-06 schedule_assertion Itzel (host: "next Monday the 10th right?")` · `2026-08-08 confirm Itzel 11:00`. The 17:00 was correctly agreed and correctly stored in March; the 11:00 revision is what the system cannot represent.
- Blast radius, measured — across all `active` bookings, joining each on the latest non-tentative fact carrying a `target_time` for its cleaning day **and the same cleaner**: **1 disagreement** (Aug 10, booking 17:00 vs stated 11:00), and 0 cases where the booking had no `clean_time` under that join. ⚠️ A parallel analysis using a looser join (any assigned cleaner, `clean_time` absent) reported 21-of-22 bookings with an asserted-but-unwritten time and 11 turnovers since June where GCal rendered its `"11:00:00"` fallback against a contrary assertion — **not re-verified here**, and it measures a different population (missing time) than this probe (contradicted time). Both are real classes; only the first is confirmed by this session's own probe.
- ISC-193 root cause, verified at the schema — the parse prompt's declared output is `{"action","cleaning_date","booking_uid","cleaner","confidence","reason"}` (`app.py:812`). **There is no time field.** The lane that owns the write was never asked what time she said, and `_apply_booking_change` would not have written it if it had been. Two independent places where 11:00 had nowhere to go, which is why no prompt change alone can fix this.
- Live repair, 2026-08-09 22:30 (operational, not an ISC pass) — booking reads `Itzel / 11:00:00 / confirmed true`; commitment rewritten to `clean_time 11:00:00, via manual, 2026-08-09T22:30:14`; `gcal_push_status.ok true`. Shared calendar feed re-fetched twice: first `⚠️ 🧹 Itzel · 11:00 AM` (drift flag raised by the correction), then after the notify clear `🧹 Itzel · 11:00 AM | 20260810T180000Z` — right time, no warning prefix.
- ISC-199 source, verified — `app.py:4050` `current_ids = {f["id"] for f in result["findings"]}`; `app.py:4197` persists exactly that set as `finding_ids`. `changes` and `ack_findings` are constructed at 4166/4181 and passed only as `extra_findings` to `_push_digest_to_vps`. They never touch the baseline.
- ISC-200 source, verified — `cleaning.ts` defines two headers (391/392) and routes on `isActionKind(f.kind)` (343/344); `severityRank` (313) is used only in `.sort()`. No `informational` section exists in either the SDK or fallback renderer.
- ISC-201 source, verified — `cleaning.ts:333` `${f.cleaner ?? "unassigned"}`.
- ISC-202, verified by static scan of `app.py` — functions containing both `load_data()` and `save_data(` with no `DATA_LOCK` in the body: **`sync_ical` (557), `assign` (2909), `confirm` (2928), `pay` (2937), `delete_booking` (2962), `add` (2972)**. 21 other such functions are correctly locked, so the pattern is understood in the codebase and these six are omissions rather than a design choice. Six, not the four originally reported — the scan found `delete_booking` and `add` as well.
- ISC-197 — probed and **absent**: `change_log.json` is not in `/internal/snapshot` (top-level keys are `bridge_watchdog, data, gcal_push_status, generated_at, ops_log, options, sync_status`), no route serves it, and `find / -name change_log.json` over SSH returns nothing because the Terminal & SSH add-on cannot see another add-on's `/data`. The intermediate write that reset `confirmed` to `False` between Aug 5 and Aug 7 therefore **could not be identified** — recorded as an open gap rather than guessed at.

### Wiring the stated time through (2026-08-09, 1.36.0 → 1.36.2)

- ISC-203/204/210, ISC-231 antecedent: unit — `python3 scripts/test_clean_time.py` → **39 tests, OK**. `test_the_real_august_message` feeds the verbatim fact record from the live archive (`{confirm, Itzel, 2026-08-10, "11:00"}`) and asserts `("11:00:00", None)`; `test_commitment_records_the_new_time_not_the_old` asserts `cleaner_commitment.clean_time == "11:00:00"` — the exact defect, now a passing test.
- ISC-206: unit — `schedule_assertion` and `unclear` both return `(None, None)`; `time_proposal` writes. Grounded in the live archive split (confirm 80, schedule_assertion 84, time_proposal 63, unclear 5, date_proposal 3 of 235 timed facts).
- ISC-208/209: unit — the real range case (`11:00` + `15:00` for one date) returns `(None, "names 2 different times (11:00, 15:00)")`; six malformed strings (`11am`, `25:00`, `11:60`, `""`, `1100`, `11:0`) all refuse.
- ISC-211: unit — after an unusable time the booking keeps its old `clean_time` **and has no `cleaner_commitment` at all**, so the drift stays visible instead of being ratified.
- ISC-213: **replayed against the real archive**, not a fixture. Rewinding the Aug 10 booking to its pre-repair `17:00` and running the detector over live `message_facts` emits exactly one `time_mismatch`: *"Itzel said 11:00 for the 2026-08-10 cleaning but the booking says 17:00; the shared calendar shows 17:00."* Against current state: zero. The probe fires on the real defect and goes quiet when it is fixed.
- ISC-214: live — `POST /reconcile/run` on 1.36.2 returns 3 `time_unagreed` findings (Darya, Aug 14 / 21 / 24). Confirmed against `gcal.py:110,147,188`, which substitute `11:00:00`; the shared-calendar feed shows those three as bare `🧹 Darya` at `T18:00:00Z` = 11:00 local, an hour nobody agreed to.
- ISC-219: live — `counts.total == len(findings)` (18 == 18) with the new kinds present.
- ISC-226: **verified by the same probe that found the defect.** The AST scan for functions containing `load_data()` + `save_data(` with no `DATA_LOCK` in the body returned six before (`sync_ical`, `assign`, `confirm`, `pay`, `delete_booking`, `add`) and returns **NONE** after.
- ISC-227: code — `sync_ical` fetches the feed, then `with DATA_LOCK: return _merge_ical_events(cal)`; `load_data()` runs inside the lock immediately before the merge, so no stale copy is held across the 15s request.
- ISC-225: live — `/internal/snapshot` now carries `change_log` (5 records). It immediately paid for itself: both Aug 10 confirms record `confirmed: false → true`, with **no intervening write** that set it false — the lost-update signature ISC-226 now prevents.
- ISC-230: **244 tests green across all nine suites**, 39 new, 205 pre-existing unchanged.
- ISC-234: Supervisor — `{"version": "1.36.2", "state": "started"}`.
- ISC-235: live — 18 findings, of which the 15 pre-existing (14 `drift_unassigned` + 1 `bridge_blind_window`) are unchanged from the pre-deploy baseline. No regression.
- End-to-end, tomorrow's cleaning: shared calendar feed reads `🧹 Itzel · 11:00 AM | 20260810T180000Z` — right hour, and the `⚠️` drift prefix cleared.

**Bot side (ISC-221..223, ISC-232) — delegated to Forge on the VPS repo, verified independently here:**
- ISC-232: re-ran rather than trusted the report — `bun test` **170 pass / 0 fail**, 355 expect() calls across 4 files; `bunx tsc --noEmit` exit 0.
- ISC-221: `formatFindingLine` now omits the clause instead of substituting a word. The `undefined` / empty-string edge is closed one layer up: `cleaner: isNonEmptyString(r.cleaner) ? r.cleaner : null` normalises both to null before rendering, so the `!== null` test is total.
- ISC-222/223: `changes` is parsed as a third `TriageBullet[]` and folded into the SAME set-equality check — `for (const b of [...actions, ...conflicts, ...changes])` — so a dropped id and an id cited in two sections both still throw and fall back to the deterministic renderer.
- **End-to-end replay of the two messages Josh actually received.** Feeding the real Aug 8 finding (`cleaner: null`) through the new renderer: the word "unassigned" is gone and it lands under `🔧 Changes applied` instead of `❓ Unresolved conflicts`. Feeding the post-1.36.2 shape: *"2026-08-09 — Itzel: 2026-08-10 cleaning — time 17:00:00 → 11:00:00; confirmed False → True (auto-applied from WhatsApp)."*
- Deployed: `scp` to `/opt/pai-telegram-bot/src/cleaning.ts`, sha1 **matches local** (`ad699d69…`), service restarted, journal shows `cleaning digest listener started {port:8899}` + `Bot started: @josh_vela_claude_bot`, and `GET /cleaning/health` through Caddy returns **200**.
- ⚠️ Forge flagged a genuine read-trap: the pre-existing `sampleFinding` fixture uses the literal string `"unassigned"` as a real cleaner value, so `- unassigned:` lines in the test log are legitimate data, not a regression of the bug.

**Advisor findings (Rule 2), both fixed in 1.36.1:**
- Ordering by `extracted_at` let a *reprocess* — run after every prompt-version bump — restamp an ancient message as the newest opinion and resurrect a superseded time. Now ordered by the message's **send** time. Test: `test_reprocessing_cannot_resurrect_a_superseded_time`.
- A stated **window** is a valid human answer, not a parse failure. It refused to write, correctly, then left nothing behind and fell into the drift queue whose prescribed action is "tell the cleaner" — the opposite of the truth. New `time_ambiguous` finding says to ask which hour, and suppresses the mismatch for the same booking. `/assign` clears `time_note` so the finding cannot outlive its cause.
- ⚠️ One advisor claim **refuted**: it said `time_mismatch` "has never fired in the wild" and only the empty case was proven. The archive replay above had already fired it on real data before the advisor was called.

**Cross-vendor audit (Rule 2a, `model_used: "GPT-5 Codex"` — confirmed non-Anthropic), all three findings real, fixed in 1.36.2:**
- **A regression this release introduced.** Merging change/ack ids into `finding_ids` made each one count as *resolved* the next night, since they never appear in a subsequent reconciler run and `resolved_count = len(baseline − current)`. Split into `finding_ids` (drives the new/resolved diff) and `reported_ids` (drives suppression), and the send path now actually consults it. **Persisting ids without reading them was bookkeeping that resembled deduplication** — the audit's own phrasing, and the sharpest line in it.
- The detector recognised ambiguity only from `time_note`, which only the *new* writer sets — so the historical archive still collapsed a two-time window to whichever fact was visited last and reported a confident mismatch against half a range. Ambiguity is now detected from raw facts as well.
- The `HH:MM` guard existed on the write path only, so a malformed model output could ride into a finding id and into prose the host reads. The same guard now applies in `reconcile.py`.

### Operational history (2026-08-03, 1.32.0)

- ISC-156..162: unit — `python3 scripts/test_bridge_watchdog.py` → **26 tests, OK**. New `EventLogTests` cover sparse steady state, restart counting, the cap dropping oldest-first, malformed-timestamp tolerance, and probe-error de-duplication.
- ISC-163: live — `ha addons logs` tail reads `[watchdog] started — checking '27cbea7f_whatsapp-bridge' every 5 min`, and Supervisor `info` reports `{"version":"1.32.0","state":"started","interval":5}`.
- ISC-166: live — `GET /internal/snapshot` **HTTP 200** with `bridge_watchdog` and `ops_log` present at top level alongside the existing `gcal_push_status` / `sync_status`. Reports `restarts_lifetime: 1` (the Aug 2 heal, preserved from the pre-existing counter), `restarts_logged: 0` (event log starts empty by construction), `unacknowledged_blind_windows: 1`.
- ISC-167: happy-path live probe (snapshot returned 200 with both new keys) plus structural — both `_watchdog_summary()` and `_read_ops_log()` swallow their own exceptions and degrade to `{"enabled": True, "error": ...}` / `[]`.
- ISC-168, ISC-169: unit — `ConcurrencyTests` (8 threads through a `Barrier` into `check()`): `heals` equals the count of `restart` events and `checks == 8`, i.e. no thread's write was lost. Suite now **38 tests, OK**.
- ISC-171..175: unit — `CheckLogTests` (10 tests) covers uneventful passes still being logged, action + `from_state` capture, `healthy_pct` from observations, per-pass probe-failure logging, trailing-window prune, and torn-line recovery. Suite **41 tests, OK**. Volume simulated at full scale: 8,640 records = 532 KB, 63 bytes/record, prune drops a 45-day-old row and keeps 8,640.
- ISC-184: `[ ]` — **FAILS, deliberately left failing.** The join now runs (expanding the table's `ISC-81/82` and `ISC-156..162` shorthand) and reports **110 of 185 criteria with no Test Strategy row**. This session's 28 were backfilled; the pre-existing 110 were not — see Decisions 2026-08-03 13:10 for why fabricating them would be worse than the visible gap.
- ISC-180..183: live — cleaning-tracker **1.34.0** deployed 2026-08-03 13:0x; `GET /` returns **HTTP 200**, 52,163 bytes, containing `id="bridge-tab"`, `bridge-tab-btn`, the 30-day strip and the plain-language caveat. Day cells render `29 nodata + 1 partial` — correct and honest on day one, because the check log only began at 12:26 today, so 29 days genuinely have no observations and today has far fewer than a full day's. Self-corrects as the window fills.
- ISC-177..179: deployed — `src/` synced to `/opt/pai-telegram-bot/`, `systemctl is-active` → `active`, `cleaning digest listener started {port: 8899}`, `Bot started: @josh_vela_claude_bot`.
- ISC-177..179: unit — `renderTriageResult()` coverage suite in `~/dev/pai-telegram-bot/test/cleaning.test.ts` (8 cases: full coverage renders the model's wording; a dropped finding throws `dropped N finding(s)`; invented id throws; double-cite throws; prose throws; fenced JSON tolerated; empty section renders `none`; malformed bullet rejected) plus 3 service-level cases proving the dropped finding still reaches Telegram via the fallback and the message says so. **Bot suite 122 pass / 0 fail, `tsc --noEmit` clean.** The pre-existing "SDK succeeds -> sends verbatim" test was rewritten: it fed prose, which now falls back, so it had started passing through the fallback path while claiming to test the happy path.
- ISC-180..183: rendered offline against a seeded 30-day log (8,352 records incl. a restart day and a day with no checks at all) through the real `FOCUS_TEMPLATE`: 21,768 bytes, tab + panel + strip + stats + caveat all present; day cells `{ok: 29, nodata: 1}`; the watchdog-gap day reads `2026-07-14: no checks recorded` in grey; today reads `151 checks, all healthy` in green after pro-rating.
- ISC-176: live — 1.33.0 deployed 2026-08-03 12:26 (Supervisor: `{"version":"1.33.0","state":"started","interval":5}`). `GET /internal/watchdog/history?days=1` → **HTTP 200**, `count: 1`, first record `{"at":"2026-08-03T12:26:26","state":"started","action":"none"}` — the startup pass, logged although nothing happened, which is the criterion. `/internal/snapshot` carries `summary` + `recent_checks` (1) and no longer carries `events`. Boot prune fired clean: `[watchdog] check log pruned to 30d — 0 record(s)`. Ops log survived the restart with both dismissals intact.
- ISC-164: live — two real dismissals (the stale blind-window and the Itzel false alarm) at 2026-08-03 10:35, authorised by Josh. `/internal/snapshot` → `ops_log` holds both entries with `action: finding_dismissed`, the finding id, and the reason; `/reconcile/last` drops 16 → 14 findings with only genuine `drift` remaining. No synthetic write was ever made — a fake entry in an audit log is worse than an empty one.
- ISC-165: `[DEFERRED-VERIFY]` — structural only (`_log_op` wraps its whole body in `try/except` and is called after `save_data()` has committed). Proving it needs fault injection: make `OPS_LOG_FILE` unwritable, dismiss a finding, confirm the dismissal still lands. Follow-up: bundle with the 1.32.1 deploy tonight.
- ISC-170: `[DEFERRED-VERIFY]` — **FIXED in code and deployed** (see ISC-177..179); live proof is the next real digest at 08:00 on 2026-08-04, since a synthetic trigger would put an out-of-band message on Josh's phone. Originally found 2026-08-03: Add-on log reads `[vps-push] ok — 1 new, 2 carried, heartbeat sent`, so three findings crossed to the bot; the Telegram message Josh received rendered exactly one and printed "❓ Unresolved conflicts — none". Two `needs-attention` findings were silently dropped between the push and the message.

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

### 1.37.5 (2026-08-20, third pass)

- ISC-315..320: `bun`-free unit suite — `scripts/test_drift_severity.py`, 9 cases, including the nine real far-future dates live that day, the 30-day boundary on both sides, a past-due date, and `run()` passing the date through. `Ran 13 tests ... OK`.
- ISC-321..323: same suite, 4 cases against `filter_and_sort` with a watchdog-shaped finding present — ordering, dismissal, derived counts, and stale-filter survival.
- ISC-326..328: `scripts/test_clean_time.py::WriteAuditLogTests`, 3 cases. The recording stub caught a real collision in the first cut — `_log_write(op, ...)` was being called with `op=action` as a detail kwarg, `TypeError: got multiple values`. Fixed in production, not in the test.
- ISC-329/330: live — `POST /reconcile/run` on the deployed 1.37.5 at 2026-08-20T19:09:48. Line 1 `changed_mind / approve / 2026-08-21`; counts `total=16 needs-attention=3 suggest=12 informational=1`, against `total=17 needs-attention=14 suggest=2` from the 15:00 run on 1.37.4. Both readings taken the same way (`/reconcile/run` vs the cached `/reconcile/last` was deliberately NOT the comparison — the pre-change number was re-derived from the same raw corpus in simulation before deploying, and the deployed run then reproduced it).
- ISC-236/322: live — `dismissed` count 0 → 1; `bridge_blind_window` present in `findings_raw`, absent from `findings`. A dismissal Josh made on 2026-08-18 applying for the first time.
- Unpredicted and correct: `schedule_unassigned:2026-10-01` demoted itself to `suggest`. It absorbs a `drift_unassigned` member, and merged severity takes the most severe of the group — demote the member and the group follows. One detector changed; the effect propagated through `resolve_subjects`.
- Deploy: `ha store reload && ha addons update 27cbea7f_cleaning-tracker`; `version 1.37.5, state started` confirmed over SSH before probing.
- ISC-324, ISC-331: `[DEFERRED-VERIFY]`, probes named in the criteria.
- **Not covered by any of the above:** every adversarial pass on 1.37.x — the code review, the advisor and the executor — ran on Anthropic-family models. `codex` is not installed on joshua-Ubuntu-PC, so Rule 2a did not run and ISC-42 remains unsatisfied for this release.

### W1 digest-fidelity fixes — 1.38.0, verified live (2026-08-21)

- ISC-349: unit + live — `scripts/test_dismissal_subject_scope.py` (11 tests, OK): subject-scoped filtering with evidence cutoff, legacy uid-parsed-from-id records, re-open on post-dismissal evidence, instant-correct comparison across naive-local/UTC-Z stamps. Live corpus: the Aug-21 merged finding correctly SURVIVED the Aug-16 cutoff (`evidence_latest: 2026-08-18T16:22:54.000Z` > cutoff) — new signal re-opens, as specified.
- ISC-350: replay + live — `scripts/test_council_w1_replay.py` (7 tests, OK): old-evidence-only case fully suppressed by the legacy Aug-16-shaped dismissals; with the Aug-18 messages added, exactly one merged finding survives, primary an undecided message. Live `/reconcile/run` on 1.38.0 (`generated 2026-08-21T12:52:33`): the Aug-21 finding is `[needs-attention] [approve] undecided_message` — "a message about the 2026-08-21 cleaning is waiting for a decision · Darya said confirm then decline…" — the contest is corroboration, not the claim. No adjudicate finding on that date.
- ISC-351: unit + live — `scripts/test_unread_messages.py` (13 tests, OK; two rewritten to the new contract). Live: the Sep-8 finding reads "a message about the 2026-09-08 cleaning is waiting for a decision" — the "extraction failed" wording is gone from all three messages holding stale error flags over good facts.
- ISC-352: unit + live — `scripts/test_schedule_coherence.py` (11 tests, OK): coherent date detected via latest-wins per cleaner; assignee-declines and contested fixtures stay adjudicate; tentative newest ignored. Live: both `changed_mind` findings on Aug 21 and the one on Sep 10 carry `decision_override: observe` and rode into their merges as corroboration.
- ISC-353: unit + live — coherence downgrade touches decision only; the Aug-21 merged finding kept `needs-attention` from its undecided-message members (severity max over the group is unchanged).
- ISC-335: unit — `test_dismissal_subject_scope.py`: a dismissed member can no longer become primary while a live member exists; all-dismissed groups still filter. `resolve_subjects` now takes `dismissed` and deprioritizes dismissed ids in primary selection.
- Full suite: 18 files, all OK (two print trailing log noise after their OK line — not failures). Deploy: push 70b0b2a → `ha store reload && ha addons update`; `version 1.38.0, state started` confirmed over SSH before probing.
- Forge review observations, recorded not fixed: (a) `findings_raw` is post-merge/pre-dismiss, not pre-merge — the name invites misreading and absorbed members' own fields are uninspectable from the public return; (b) `_facts_vs_bookings`/`_schedule_vs_bookings` exclude by `status == "cancelled"` while `resolve_subjects`/`_coherent_dates` require `status == "active"` — the families disagree if a `complete` status ever appears inside a detector window; (c) cosmetic: two undecided messages merging on one date repeat the identical "waiting for a decision" clause in `why`.
