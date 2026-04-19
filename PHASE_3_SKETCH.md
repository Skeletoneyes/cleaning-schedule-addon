# Phase 3 Plan — WhatsApp automation

## Route

**Baileys sidecar** (unofficial linked-device). Route A (WhatsApp Cloud
API) is rejected: it can't participate in the existing group chats
(Me + Michelle + cleaner), and the user's explicit requirement is that
the bot only operates in visible, human-auditable chats.

## Three-step rollout

### Step 1 — Build against fake data

Everything except the Baileys link is built and tested first, using
synthetic inbound messages. Zero WhatsApp risk.

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

### Step 2 — User procures the number and account

User, not assistant:

1. Buy SpeakOut Wireless SIM at 7-Eleven ($25 for 365 days).
2. Put it in a spare/used Android phone.
3. Install WhatsApp Business; register with the new number; receive
   SMS OTP on the phone.
4. Set display name / photo for the bot account (e.g. "Cleaning Bot").

Nothing technical on the add-on side during this step.

### Step 3 — Wire Baileys to the supplied account

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
