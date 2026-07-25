/**
 * WhatsApp → cleaning-tracker bridge (HA add-on).
 *
 * Pairs as a linked device against an existing WhatsApp account. Forwards
 * group messages — including host's own messages — to the cleaning tracker's
 * /internal/whatsapp/inbound endpoint.
 *
 * In HA: reads /data/options.json, stores auth in /data/auth, reaches the
 * cleaning tracker at 127.0.0.1:5000 (loopback — no shared secret needed).
 * Locally: falls back to .env (HA_URL, SHARED_SECRET, GROUP_ALLOWLIST, etc).
 */

const fs = require("fs");

const IN_HA = fs.existsSync("/data/options.json");
let opts = {};
if (IN_HA) {
  try { opts = JSON.parse(fs.readFileSync("/data/options.json", "utf8")); } catch {}
} else {
  require("dotenv").config();
}

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const P = require("pino");
const qrcode = require("qrcode-terminal");

// Bridge runs with host_network: true, so it reaches the tracker via
// 127.0.0.1:5000 — but the tracker is on the docker bridge with port 5000
// mapped, so Docker NATs the source IP to the bridge gateway (172.30.32.1).
// That means the tracker does NOT see us as loopback, so we must present the
// shared secret in HA mode too.
const HA_URL = IN_HA
  ? "http://127.0.0.1:5000"
  : (process.env.HA_URL || "").replace(/\/$/, "");
const SHARED_SECRET = IN_HA
  ? (opts.shared_secret || "")
  : (process.env.SHARED_SECRET || "");

const GROUP_ALLOWLIST = new Set([
  ...(opts.group_allowlist || []),
  ...(process.env.GROUP_ALLOWLIST || "").split(",").map((s) => s.trim()).filter(Boolean),
]);
const AUTH_DIR = IN_HA ? "/data/auth" : (process.env.AUTH_DIR || "./auth");
const BACKFILL_PER_GROUP = opts.backfill_per_group ?? parseInt(process.env.BACKFILL_PER_GROUP || "10", 10);
const BACKFILL_WINDOW_MS = opts.backfill_window_ms ?? parseInt(process.env.BACKFILL_WINDOW_MS || "15000", 10);
const LIST_GROUPS = process.argv.includes("--list-groups");
const STARTED_AT_SEC = Math.floor(Date.now() / 1000);
const TEST_ALARM = !!opts.test_alarm;

const log = P({ level: "info" });

// ---------------------------------------------------------------------------
// Health alarms → HA persistent notifications.
//
// This bridge failed silently for 3 months (libsignal MessageCounterError
// poison loop: decrypt failure → stream crash → reconnect → redeliver → …)
// and nothing surfaced it. These alarms make that class of failure loud.
// Requires homeassistant_api: true in config.yaml (SUPERVISOR_TOKEN env).
// ---------------------------------------------------------------------------
const SUPERVISOR_TOKEN = process.env.SUPERVISOR_TOKEN || "";
const ALARM_COOLDOWN_MS = 6 * 60 * 60 * 1000; // one HA notification per kind per 6h
const _alarmLastSent = {};

async function postAlarm(kind, title, message) {
  const now = Date.now();
  if (_alarmLastSent[kind] && now - _alarmLastSent[kind] < ALARM_COOLDOWN_MS) return;
  _alarmLastSent[kind] = now;
  log.error({ kind, message }, "HEALTH ALARM");
  if (!SUPERVISOR_TOKEN) {
    log.warn("no SUPERVISOR_TOKEN — cannot post HA notification");
    return;
  }
  try {
    const res = await fetch("http://supervisor/core/api/services/persistent_notification/create", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${SUPERVISOR_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        notification_id: `whatsapp_bridge_${kind}`,
        title: `WhatsApp Bridge: ${title}`,
        message,
      }),
    });
    log.info({ kind, status: res.status }, "health alarm posted to HA");
  } catch (err) {
    log.error({ err: err.message, kind }, "health alarm post failed");
  }
}

// Sliding-window event counters: alarm when `threshold` events land within `windowMs`.
function makeWindowCounter(kind, threshold, windowMs, title, messageFn) {
  const events = [];
  return () => {
    const now = Date.now();
    events.push(now);
    while (events.length && events[0] < now - windowMs) events.shift();
    if (events.length >= threshold) postAlarm(kind, title, messageFn(events.length));
  };
}

const countDecryptFailure = makeWindowCounter(
  "decrypt", 5, 10 * 60 * 1000,
  "message decryption failing",
  (n) => `${n} messages failed to decrypt in a cleaning group in the last 10 minutes ` +
    `(libsignal session corruption — the failure mode that silently dropped 3 months ` +
    `of Daria's messages). First CHECK, don't re-pair: open the Log tab and confirm ` +
    `the failures are still arriving and are in an allowlisted group. Only if it ` +
    `persists, re-pair — uninstall + reinstall the add-on to wipe /data/auth, restore ` +
    `options, restart, scan the QR. Re-pairing costs the auth state, so it is the ` +
    `last step, not the first. Messages missed meanwhile are recoverable via chat ` +
    `export → transcript ingest.`
);

const countDisconnect = makeWindowCounter(
  "flapping", 4, 30 * 60 * 1000,
  "connection flapping",
  (n) => `${n} disconnects in the last 30 minutes. Messages arriving during the gaps ` +
    `may be lost. If this persists, check the bridge Log tab for decrypt errors ` +
    `(session corruption → re-pair) or network issues.`
);

const countForwardFailure = makeWindowCounter(
  "forward", 5, 10 * 60 * 1000,
  "cannot reach cleaning tracker",
  (n) => `${n} forwards to the cleaning-tracker add-on failed in the last 10 minutes. ` +
    `Incoming WhatsApp messages are NOT being recorded. Check that the Cleaning ` +
    `Schedule Tracker add-on is running and whatsapp_shared_secret matches.`
);

// Baileys logs decrypt failures through the logger we hand it — intercept them
// there, since it exposes no public event for this.
const DECRYPT_ERR_RE = /failed to decrypt|MessageCounterError|Bad MAC|No matching sessions|No SenderKeyRecord|SessionError/i;
const REMOTE_JID_RE = /"remoteJid"\s*:\s*"([^"]+)"/;

// Only ALARM on failures in groups this bridge actually forwards (same filter
// as the messages.upsert loop below). The account is in ~60 personal groups;
// a stale sender-key in "Property Bros" costs the cleaning schedule nothing,
// but it used to trip a 5-in-10-min alarm whose text says "the exact failure
// mode that dropped 3 months of Daria's messages" — a false alarm that teaches
// you to ignore the real one. Out-of-scope failures are still logged (visible
// in the Log tab), just not escalated to a persistent notification.
// Unattributable failures (no parseable remoteJid) still count: the detector
// exists because silent loss went unnoticed for 3 months, so anything we
// cannot rule out stays loud.
function noteDecryptFailure(flat) {
  const jid = (flat.match(REMOTE_JID_RE) || [])[1] || "";
  const inScope = !jid || GROUP_ALLOWLIST.size === 0 || GROUP_ALLOWLIST.has(jid);
  if (!inScope) {
    log.warn({ jid }, "decrypt failure in non-allowlisted group — not alarming");
    return;
  }
  countDecryptFailure();
}

const baileysLogger = P({
  level: "warn",
  hooks: {
    logMethod(args, method) {
      try {
        const flat = args.map((a) => {
          if (typeof a === "string") return a;
          try { return JSON.stringify(a); } catch { return String(a); }
        }).join(" ");
        if (DECRYPT_ERR_RE.test(flat)) noteDecryptFailure(flat);
      } catch {}
      return method.apply(this, args);
    },
  },
});

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
    const headers = { "Content-Type": "application/json" };
    if (SHARED_SECRET) headers["X-Shared-Secret"] = SHARED_SECRET;
    const res = await fetch(`${HA_URL}/internal/whatsapp/inbound`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      log.error({ status: res.status, id: payload.id }, "forward failed");
      countForwardFailure();
    } else {
      log.info({ id: payload.id, group: payload.group_jid, from_me: payload.from_me }, "forwarded");
    }
  } catch (err) {
    log.error({ err: err.message, id: payload.id }, "forward threw");
    countForwardFailure();
  }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: baileysLogger,
    printQRInTerminal: false,
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Skip Baileys' init queries (fetchProps etc.). They 408 ("init queries
    // Timed Out") on this linked device every connect, and we don't use the
    // server props / feature-flag data — only live message forwarding. This
    // suppresses the recurring timeout without affecting forwarding.
    fireInitQueries: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log("\nScan this QR with WhatsApp → Settings → Linked Devices:\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      log.info("connected");
      if (TEST_ALARM) {
        postAlarm("test", "test alarm", "Bridge health-alarm path works. Turn the test_alarm option back off.");
      }
      // Always log visible groups so you can populate group_allowlist on first install.
      try {
        const groups = await sock.groupFetchAllParticipating();
        console.log("\nVisible groups (copy JIDs into the group_allowlist add-on option):");
        for (const [jid, g] of Object.entries(groups)) {
          console.log(`  ${jid}  —  ${g.subject}`);
        }
        console.log();
      } catch {}
      if (LIST_GROUPS) process.exit(0);
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      log.warn({ code, shouldReconnect }, "disconnected");
      if (shouldReconnect) {
        countDisconnect();
        start();
      } else {
        log.error("logged out — delete /data/auth and restart to re-pair");
        await postAlarm(
          "logged_out",
          "LOGGED OUT — re-pair required",
          "WhatsApp unlinked this device. NO messages are being captured. Reinstall " +
          "the WhatsApp Bridge add-on (wipes /data/auth), restart, and scan the QR " +
          "from the Log tab."
        );
        process.exit(1);
      }
    }
  });

  const backfillEnabled = BACKFILL_PER_GROUP > 0;
  const backfillBuf = new Map();
  let backfillDone = !backfillEnabled;
  const seenIds = new Set();

  function buildPayload(msg) {
    const remoteJid = msg.key.remoteJid || "";
    const ts = Number(msg.messageTimestamp || 0);
    const text = extractText(msg).trim();
    if (!text) return null;
    const fromMe = !!msg.key.fromMe;
    const sender = fromMe
      ? (sock.user?.id || "me@s.whatsapp.net")
      : (msg.key.participant || remoteJid);
    return {
      id: msg.key.id,
      timestamp: new Date(ts * 1000).toISOString(),
      sender_jid: sender,
      from_me: fromMe,
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
      if (ts && ts < STARTED_AT_SEC) continue;
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
