/**
 * WhatsApp → cleaning-tracker bridge.
 *
 * Pairs as a linked device against an existing WhatsApp account (personal
 * for test, dedicated bot number for production). On inbound group messages
 * matching GROUP_ALLOWLIST, POSTs to the add-on's /internal/whatsapp/inbound
 * endpoint with a shared-secret header.
 */

require("dotenv").config();

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const P = require("pino");
const qrcode = require("qrcode-terminal");

const HA_URL = (process.env.HA_URL || "").replace(/\/$/, "");
const SHARED_SECRET = process.env.SHARED_SECRET || "";
const GROUP_ALLOWLIST = new Set(
  (process.env.GROUP_ALLOWLIST || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
);
const AUTH_DIR = process.env.AUTH_DIR || "./auth";
const DROP_HISTORY = process.env.DROP_HISTORY !== "0";
const BACKFILL_PER_GROUP = parseInt(process.env.BACKFILL_PER_GROUP || "0", 10);
const BACKFILL_WINDOW_MS = parseInt(process.env.BACKFILL_WINDOW_MS || "15000", 10);
const LIST_GROUPS = process.argv.includes("--list-groups");
const STARTED_AT_SEC = Math.floor(Date.now() / 1000);

const log = P({ level: "info" });

function extractText(msg) {
  const m = msg.message;
  if (!m) return "";
  return (
    m.conversation ||
    m.extendedTextMessage?.text ||
    m.imageMessage?.caption ||
    m.videoMessage?.caption ||
    ""
  );
}

async function forward(payload) {
  if (!HA_URL) {
    log.warn("HA_URL not set — dropping forward");
    return;
  }
  try {
    const res = await fetch(`${HA_URL}/internal/whatsapp/inbound`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Shared-Secret": SHARED_SECRET,
      },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      log.error({ status: res.status, id: payload.id }, "forward failed");
    } else {
      log.info({ id: payload.id, group: payload.group_jid }, "forwarded");
    }
  } catch (err) {
    log.error({ err: err.message, id: payload.id }, "forward threw");
  }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: P({ level: "warn" }),
    printQRInTerminal: false,
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log("\nScan this QR with WhatsApp → Settings → Linked Devices:\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      log.info("connected");
      if (LIST_GROUPS) {
        sock.groupFetchAllParticipating().then((groups) => {
          console.log("\nGroups visible to this account:");
          for (const [jid, g] of Object.entries(groups)) {
            console.log(`  ${jid}  —  ${g.subject}`);
          }
          console.log("\nCopy the JIDs you want to track into GROUP_ALLOWLIST in .env, then restart without --list-groups.\n");
          process.exit(0);
        });
      }
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      log.warn({ code, shouldReconnect }, "disconnected");
      if (shouldReconnect) start();
      else {
        log.error("logged out — delete auth/ and re-pair");
        process.exit(1);
      }
    }
  });

  const backfillEnabled = BACKFILL_PER_GROUP > 0;
  const backfillBuf = new Map(); // groupJid -> array of {msg, ts}
  let backfillDone = !backfillEnabled;
  const seenIds = new Set();

  function buildPayload(msg) {
    const remoteJid = msg.key.remoteJid || "";
    const ts = Number(msg.messageTimestamp || 0);
    const text = extractText(msg).trim();
    if (!text) return null;
    const sender = msg.key.participant || remoteJid;
    return {
      id: msg.key.id,
      timestamp: new Date(ts * 1000).toISOString(),
      sender_jid: sender,
      group_jid: remoteJid,
      text,
    };
  }

  if (backfillEnabled) {
    setTimeout(async () => {
      backfillDone = true;
      let total = 0;
      for (const [groupJid, buf] of backfillBuf) {
        buf.sort((a, b) => a.ts - b.ts);
        const slice = buf.slice(-BACKFILL_PER_GROUP);
        for (const { msg } of slice) {
          if (seenIds.has(msg.key.id)) continue;
          seenIds.add(msg.key.id);
          const payload = buildPayload(msg);
          if (payload) {
            await forward(payload);
            total++;
          }
        }
        log.info({ group: groupJid, forwarded: slice.length, buffered: buf.length }, "backfill");
      }
      backfillBuf.clear();
      log.info({ total }, "backfill complete — switching to live mode");
    }, BACKFILL_WINDOW_MS);
  }

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    for (const msg of messages) {
      if (!msg.message) continue;
      if (msg.key.fromMe) continue;

      const remoteJid = msg.key.remoteJid || "";
      if (!remoteJid.endsWith("@g.us")) continue;
      if (GROUP_ALLOWLIST.size > 0 && !GROUP_ALLOWLIST.has(remoteJid)) continue;

      const ts = Number(msg.messageTimestamp || 0);

      if (!backfillDone) {
        if (!backfillBuf.has(remoteJid)) backfillBuf.set(remoteJid, []);
        backfillBuf.get(remoteJid).push({ msg, ts });
        continue;
      }

      if (type !== "notify") continue;
      if (DROP_HISTORY && ts && ts < STARTED_AT_SEC) continue;
      if (seenIds.has(msg.key.id)) continue;
      seenIds.add(msg.key.id);

      const payload = buildPayload(msg);
      if (payload) await forward(payload);
    }
  });
}

start().catch((err) => {
  log.error({ err: err.message }, "fatal");
  process.exit(1);
});
