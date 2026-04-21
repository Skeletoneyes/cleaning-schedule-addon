# WhatsApp bridge (test / prod sidecar)

Node sidecar that pairs as a WhatsApp **linked device** and forwards inbound
group messages to the cleaning-tracker add-on's
`POST /internal/whatsapp/inbound` endpoint.

## Test mode vs. production mode

The sidecar code is identical. What differs is which WhatsApp account you
pair against:

- **Test:** your personal WhatsApp. No SIM needed. Pair via QR, messages in
  the allowlisted groups start flowing. Risk: if Meta flags the linked
  device, your personal account eats the ban. Use only to validate the
  pipeline, not for unattended operation.
- **Prod:** a dedicated bot number (Speakout $125/yr unlimited talk+text).
  Same QR flow from the bot's WhatsApp Business install. Ban risk is
  contained to the bot account.

## Setup (test)

Prereqs: Node 20+, the add-on running and reachable from this machine.

1. On the add-on side: set the `whatsapp_shared_secret` option to a random
   string (e.g. `openssl rand -hex 32`). Restart the add-on.
2. The add-on's Flask port (5000) must be reachable from this PC. Either:
   - Run the sidecar on the HA host itself → `HA_URL=http://127.0.0.1:5000`
     works, and loopback bypasses the shared-secret check.
   - Or expose port 5000 on the add-on and point the sidecar at the host's
     LAN IP. Shared secret is required in that case.
3. In this directory:
   ```
   npm install
   cp .env.example .env
   # edit .env: HA_URL, SHARED_SECRET (leave GROUP_ALLOWLIST empty for now)
   ```
4. First run — list group JIDs:
   ```
   npm run list-groups
   ```
   Scan the QR from WhatsApp → Settings → Linked Devices → Link a Device.
   The script will print every group your account is in, then exit. Copy
   the JIDs for the cleaner groups into `GROUP_ALLOWLIST` in `.env`.
5. Normal run:
   ```
   npm start
   ```
   Auth state persists in `./auth/`. Subsequent starts skip the QR.

## Operational notes

- **Own outbound is filtered** (`msg.key.fromMe` → drop). Your replies in
  the cleaner groups don't get forwarded.
- **History replay is dropped by default** (`DROP_HISTORY=1`). On first
  pair Baileys backfills recent messages; we discard anything with a
  timestamp earlier than process start. Flip to `0` for debugging.
- **PC must stay awake.** Sleep = no message delivery. On Windows,
  Settings → System → Power → Screen and sleep → "Never" while plugged in.
- **Re-verification.** WhatsApp occasionally asks the primary device to
  re-confirm linked devices. If the bridge stops receiving messages,
  check WhatsApp on your phone for a linked-device prompt.
- **Ban safety.** Do not send outbound from this sidecar. `sendMessage` is
  not called anywhere in `index.js`. Keeping the bridge read-only is the
  single biggest thing you can do to reduce ban risk.

## Promoting to the dedicated bot number

1. Stop the sidecar, delete `./auth/`.
2. Install WhatsApp Business on the bot phone, register against the
   Speakout number.
3. `npm start` on the sidecar, scan the new QR from the bot phone's
   Linked Devices screen.
4. Re-run `--list-groups` once the bot has been added to the cleaner
   groups, update `GROUP_ALLOWLIST`, restart.
