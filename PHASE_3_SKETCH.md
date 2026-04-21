# Phase 3 Plan — WhatsApp automation

> **Status (2026-04-20): Steps 1 and 3 shipped in test mode (add-on 1.8.x);
> Step 2 still pending.** The inbound pipeline + Review tab are live. The
> Baileys sidecar is built and running at `sidecar/whatsapp-bridge/`,
> paired against the user's personal WhatsApp as a linked device (test
> mode). Real messages from the two allowlisted cleaner groups are flowing
> into the Review tab. **Pivot from the original plan:** the sidecar runs
> **outside** the add-on container, as a Node process on the user's PC —
> avoids the `init: true` / s6-overlay flip that an in-container sidecar
> would have forced. Step 2 (procure dedicated SpeakOut bot number) still
> blocks the promotion to unattended production operation; swapping to
> that account is a `rm -rf sidecar/whatsapp-bridge/auth/` + re-pair.
>
> **New in 1.9.0:** `/backfill` page — paste a WhatsApp chat export to
> match unassigned bookings against historical messages. Covers the
> history that linked-device sync can't reach.

## Current status (2026-04-19)

**Step 1 is complete and merged to `Calendar-Redo`** (commits `8969f9b`,
`66a85e4`; add-on `1.5.1`). Built against synthetic messages; no real
WhatsApp traffic yet.

Shipped:

- `data.json` schema extended with `messages`, `cleaner_jids`, and
  `group_labels`. All backwards-compatible defaults.
- `POST /internal/whatsapp/inbound` — loopback-only, dedups on message
  id, enqueues to a 2-thread worker pool.
- Haiku parse worker: passes the **full cross-group archive** + booking
  list + known cleaners + sender hint. Returns
  `{action, booking_uid, cleaner, confidence, reason}`.
- Auto-apply gate: confidence ≥ 0.85 + known cleaner JID + known
  booking → writes to booking. Everything else → Review tab.
- Review tab in the index page with:
  - Groups section (label editor — "Maria group" instead of a JID).
  - Unmapped-sender flow (map to existing cleaner OR create new one;
    re-queues that sender's pending messages on save).
  - Pending messages with accept/override/ignore controls.
  - Pending-count badge on the tab.
- `scripts/whatsapp_fixture.py` — synthetic-message harness covering
  confirm / decline / ambiguous / unmapped / chitchat.

Deviations from the original Step 1 sketch (recorded here so the
motivation isn't lost):

- **History window**: the plan said "last 10 messages in the same
  group." At observed volume (<1 message/day across all chats) it's
  cheaper and smarter to hand Haiku the entire archive across all
  groups. Revisit if volume ever climbs above ~10/day.
- **No tool-use**: the earlier design considered a
  `search_messages` / `list_groups` tool the model could call. Dropped
  — at this volume the archive fits in one prompt, so there's nothing
  for the tool to do. Preserve the sketch below under "Deferred" in
  case volume changes.
- **No retention / compaction**: a monthly Sonnet pruning pass was
  considered and dropped for the same reason. `data.json` grows
  linearly at <400 messages/year; revisit only if the file itself
  becomes slow.

## Next steps

1. **Step 2 — user procures bot account** (blocking; see below). Nothing
   for the assistant to do until this is done.
2. **Step 3 — wire Baileys sidecar** once the account exists. Details
   unchanged from the original sketch below.
3. After Step 3 is paired to a real group: week-long observation with
   auto-apply disabled, then tune the 0.85 confidence threshold + the
   parse prompt against real messages.
4. Add the host/Michelle chat (third group) once the bot has been
   trusted in one cleaner group.

## Deferred (revisit if volume grows past ~10 msg/day)

- Tool-use for the LLM: `search_messages(group?, since?, limit?, text?)`
  and `list_groups()` as loopback-only Flask routes, fed into the
  Anthropic request's `tools` field. Would let the model pull its own
  context window instead of receiving the full archive every time.
- Monthly Sonnet compaction: wake on a day-cadence daemon, pass
  batches of messages older than 60 days + still-relevant bookings,
  soft-delete anything that only references past dates. Archive to
  `/data/messages-archive.jsonl`. Preserve standing arrangements
  ("Maria can't do Sundays") by prompt.

---

## Route

**Baileys sidecar** (unofficial linked-device). Route A (WhatsApp Cloud
API) is rejected: it can't participate in the existing group chats
(Me + Michelle + cleaner), and the user's explicit requirement is that
the bot only operates in visible, human-auditable chats.

## Three-step rollout

### Step 1 — Build against fake data  ✅ DONE

Everything except the Baileys link is built and tested first, using
synthetic inbound messages. Zero WhatsApp risk.

> Status: shipped in `8969f9b` + `66a85e4`. See "Current status" above
> for what was built and what was simplified from this plan.

**Add-on changes** (`cleaning-tracker/app.py`):

1. **Message log** in `data.json`: `messages: [{id, timestamp, sender,
   group, text, parsed: bool, applied_uid: str|null, review_state:
   "auto"|"pending"|"ignored"}]`. Backwards-compatible default (empty
   list).
2. **Inbound endpoint** `POST /internal/whatsapp/inbound` — accepts
   `{id, timestamp, sender_jid, group_jid, text}` from `127.0.0.1`
   only. **Dedups on `id`** before appending (Baileys replays on
   reconnect; duplicate IDs must not trigger a second auto-apply).
   Enqueues the message for the parse worker (see below) rather than
   spawning a thread per request.
3. **Haiku parse with chat context** — reuse the existing prompt,
   adapted to: the incoming message + **the last 10 messages in the
   same group** + current booking list. Reply context matters: in v1
   the bot is read-only, so short replies like "yes" / "ok" /
   "confirmed" only make sense against the prior human chatter.
   Output: `{booking_uid, cleaner, action: "confirm"|"decline",
   confidence}` or `null` if not actionable. A single parse worker
   (or small pool, size 2) drains the queue — prevents burst traffic
   in one group from fanning out unbounded Anthropic API calls.
4. **Auto-apply rule** — if confidence is high AND message matches a
   known cleaner AND references an unambiguous date → apply to
   booking. Otherwise → enqueue in review state `pending`.
5. **Review queue UI** — new tab in the calendar view listing pending
   messages with "Accept Haiku's suggestion" / "Ignore" buttons. Each
   entry shows the raw message, the sender, the inferred booking, and
   what would change.
6. **Cleaner ↔ sender mapping** — extend `cleaners` config entry
   structure (backwards-compatible) to optionally carry a `whatsapp`
   field (list of sender JIDs, not just one — a cleaner may text from
   a second number). First message from an unmapped sender surfaces
   in the review queue with two options: "map to existing cleaner
   [dropdown]" or "new cleaner." Answer is saved to config/data.

**Testing harness**:

- A small `POST /internal/whatsapp/inbound` fixture script + a set of
  sample messages (user will supply real WhatsApp text). Exercises:
  plain confirmation, decline, ambiguous date, conflicting names,
  unmapped sender.
- Verify the review queue, auto-apply thresholds, and the mapping
  flow before any real WhatsApp account is involved.

### Step 2 — User procures the number and account  ⏳ BLOCKED (user action)

User, not assistant:

1. Buy SpeakOut Wireless SIM at 7-Eleven ($25 for 365 days).
2. Put it in a spare/used Android phone.
3. Install WhatsApp Business; register with the new number; receive
   SMS OTP on the phone.
4. Set display name / photo for the bot account (e.g. "Cleaning Bot").

Nothing technical on the add-on side during this step.

### Step 3 — Wire Baileys  ✅ DONE in test mode (external sidecar); bot-account swap blocked on Step 2

> **Reality check (2026-04-20):** shipped at `sidecar/whatsapp-bridge/`
> (repo-root, not under `cleaning-tracker/`) as an **external** Node
> process on the user's PC, not an in-container service. The Dockerfile
> changes + `init: true` / s6-overlay flip described below were NOT done —
> the external-sidecar decision made them unnecessary. What was built:
>
> - Pairs as linked device via QR scan (`npm run list-groups` on first run
>   prints groups; subsequent `npm start` uses persisted auth in `./auth/`).
> - Read-only: never calls `sendMessage`.
> - POSTs to `<HA_URL>/internal/whatsapp/inbound` with `X-Shared-Secret`
>   matching the `whatsapp_shared_secret` option (exposed via `ports:` in
>   `config.yaml`; HA Network section must set host port = 5000).
> - Filters: fromMe dropped, non-group dropped, `GROUP_ALLOWLIST` enforced.
> - `BACKFILL_PER_GROUP` / `BACKFILL_WINDOW_MS` for a one-shot startup
>   backfill window — forwards the N most recent per group from whatever
>   history Baileys delivers during the window. Often returns zero on
>   reconnects (linked devices already synced don't get replays).
> - `/backfill` page in the add-on handles deeper historical matching
>   via paste-a-chat-export + Haiku.
>
> The original in-container plan below is preserved as archaeology.

User supplies the new number/account; assistant does the integration.

**New: `cleaning-tracker/sidecar/`** (Node.js project)

- `package.json` — deps: `baileys` (pinned), `express`, `pino`.
- `index.js` (~150 lines):
  - Boot Baileys with auth state persisted to `/data/whatsapp-auth/`.
  - On first boot, emit QR to add-on logs (and surface in UI) for
    pairing. User scans from the bot phone's WhatsApp Business →
    Linked Devices.
  - Read-only posture: subscribe to message events, POST each to
    `http://127.0.0.1:5000/internal/whatsapp/inbound` with `{id,
    timestamp, sender_jid, group_jid, text}`.
  - **No outbound.** The sidecar does not call `sendMessage` in v1.
    If Haiku needs clarification, the review queue surfaces it in the
    add-on UI, not in the chat.
  - `GET /health` for the Python side to poll.
  - Reconnect on socket drop; persist auth-state changes.

**Dockerfile**:

- Add Node.js 20 layer.
- `npm ci` the sidecar.
- **Process supervision**: `bg-then-exec` is not enough — if the
  backgrounded sidecar dies, Flask (PID 1) keeps running and HA
  doesn't restart. Options (pick one during implementation):
  - `tini` as PID 1 + a wrapper that starts both children and uses
    `wait -n` to exit the container on first child death; **or**
  - switch to `init: true` and use `s6-overlay` with two services.
    Note: this reverses the current `init: false` decision in
    CLAUDE.md — revisit and document the reason for the flip.

**`app.py`**:

- `GET /whatsapp/status` — calls sidecar `/health`, surfaces
  connected/disconnected + last-seen in the UI.
- Render the QR in the UI when sidecar emits one. Baileys emits QR
  as a string; render server-side via a small `qrcode` dep (Python
  `qrcode[pil]` is simplest — sidecar POSTs the string to app.py,
  app.py serves the PNG). Avoids making the user dig through logs.

**Rollout**:

1. Deploy updated add-on to HA.
2. Pair bot account → Baileys.
3. Add bot account to **one** cleaner group first. Let it observe for
   a week with auto-apply disabled (everything goes to review queue).
4. Review accuracy. Tune prompt / thresholds as needed.
5. Enable auto-apply for high-confidence matches.
6. Add to remaining cleaner groups.

## Out of scope for Phase 3

- Outbound messages from the bot (confirmations, reminders). Deferred
  until read-only operation is proven reliable and trusted.
- Push/email notifications on conflicts — Phase 4.
- Multi-account support (more than one bot WhatsApp number).

## Risks and mitigations

- **Baileys ban risk** → dedicated account on a separate number (not
  personal). Read-only posture for v1 reduces signal to WhatsApp's
  heuristics.
- **Baileys breaking changes** → pin version, budget ~half-day every
  6–12 months for upgrades.
- **Auth state loss** → `/data/whatsapp-auth/` persists across
  rebuilds. Document re-pairing procedure in the add-on README.
- **Haiku parse errors** → review queue catches anything below
  confidence threshold; user can always scroll the real group chat to
  audit what the bot saw.

## Definition of done

- Step 1 complete: fake-inbound tests pass, review queue works,
  auto-apply rule behaves correctly on supplied sample messages.
- Step 2 complete (user-owned): bot WhatsApp Business account exists
  on the SpeakOut number.
- Step 3 complete: sidecar paired, added to one cleaner group, one
  week of real-message observation shows acceptable parse accuracy,
  then rolled out to remaining groups.
- `config.yaml` version bumped, add-on README updated with setup
  steps for the bot account + pairing flow.
