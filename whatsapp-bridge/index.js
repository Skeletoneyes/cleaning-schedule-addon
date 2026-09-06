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
const HEARTBEAT_INTERVAL_SEC = opts.heartbeat_interval_sec ?? parseInt(process.env.HEARTBEAT_INTERVAL_SEC || "60", 10);

const log = P({ level: "info" });
// Self-reported, so it proves nothing on its own — but it is still the
// cheapest way to tell, from the heartbeat alone, which build is running.
// ⚠️ package.json's version and config.yaml's must be bumped TOGETHER. They
// drifted once already (package.json sat at 1.0.0 while the add-on shipped
// 1.3.0), which makes this field worse than useless: it reports a number
// Supervisor has never heard of, so a stale deploy reads as a fresh one.
const BRIDGE_VERSION = require("./package.json").version;

// ---------------------------------------------------------------------------
// Delivery watermark.
//
// Live mode used to discard anything timestamped before THIS PROCESS started.
// That is correct for a restart thirty seconds later and catastrophic for one
// five days later: on 2026-07-28 the bridge died, and when it came back every
// message WhatsApp had queued in the meantime was dropped on sight — including
// a cleaner reassignment for a booking two days out. The gap was unrecoverable
// and, worse, invisible.
//
// The watermark replaces "newer than boot" with "newer than the last message I
// actually forwarded", persisted across restarts. Re-forwarding is safe because
// the tracker deduplicates on message id server-side, so the failure mode of
// being too generous is a no-op write, while the failure mode of being too
// strict is permanent silent data loss. Bounded by MAX_REPLAY_DAYS so a corrupt
// or ancient watermark can't trigger a full-history replay.
const WATERMARK_FILE = IN_HA ? "/data/watermark.json" : (process.env.WATERMARK_FILE || "./watermark.json");
const MAX_REPLAY_DAYS = parseInt(process.env.MAX_REPLAY_DAYS || "14", 10);

function readWatermark() {
  try {
    const raw = JSON.parse(fs.readFileSync(WATERMARK_FILE, "utf8"));
    const ts = Number(raw.last_ts || 0);
    if (!Number.isFinite(ts) || ts <= 0) return null;
    return ts;
  } catch {
    return null;
  }
}

function writeWatermark(ts) {
  try {
    fs.writeFileSync(WATERMARK_FILE, JSON.stringify({ last_ts: ts, updated: new Date().toISOString() }));
  } catch (err) {
    log.error({ err: err.message }, "watermark write failed");
  }
}

const floorSec = STARTED_AT_SEC - MAX_REPLAY_DAYS * 86400;
const storedWatermark = readWatermark();
// No watermark yet (fresh install) → fall back to boot time, the old behaviour.
let watermark = storedWatermark === null ? STARTED_AT_SEC : Math.max(storedWatermark, floorSec);
if (storedWatermark !== null) {
  const ageH = ((STARTED_AT_SEC - watermark) / 3600).toFixed(1);
  log.info({ watermark, age_hours: Number(ageH) }, "resuming from delivery watermark");
}

// ---------------------------------------------------------------------------
// Sensor counters.
//
// This bridge used to raise its own alarms — five kinds, straight to a Home
// Assistant persistent notification. They were deleted on 2026-09-06. Not
// because they were wrong, but because there were twelve bridge-health alerts
// across three channels all answering one question, and on 2026-09-05 five of
// them fired or would have fired for a single fault at five different
// latencies, with the fastest one landing on a panel nobody reads.
//
// The bridge is now a pure sensor: it counts what it observes and reports it,
// and every decision is made in one place, in the tracker. Nothing here
// interprets, thresholds, or escalates.
//
// Why counters and not just a connection flag: a socket can be open while the
// pipeline is broken. If a reconnect fails to re-attach the messages.upsert
// handler — a known Baileys shape — the connection reports open forever and
// nothing is ever forwarded. `socket_events_seen` rising while `forwarded_ok`
// stays flat is POSITIVE evidence of that, which is a different thing from
// inferring breakage from a quiet chat. Josh's ruling was that absence proves
// nothing; a ratio between two things we watched happen is not an absence.
// ---------------------------------------------------------------------------

// Regenerated every process start. Lets the tracker tell "the same bridge is
// still running" from "it restarted and I am talking to a different process" —
// which is how a duplicate or stale instance would otherwise stay invisible.
const BOOT_ID = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

const counters = {
  socket_events_seen: 0,   // every group message Baileys handed us, pre-filter
  allowlist_matched: 0,    // ...of those, the ones in a cleaner group
  forwarded_ok: 0,
  forward_failed: 0,
  decrypt_failures: 0,
  last_forward_ok_at: null,
};

// Baileys logs decrypt failures through the logger we hand it — intercept them
// there, since it exposes no public event for this.
const DECRYPT_ERR_RE = /failed to decrypt|MessageCounterError|Bad MAC|No matching sessions|No SenderKeyRecord|SessionError/i;
const REMOTE_JID_RE = /"remoteJid"\s*:\s*"([^"]+)"/;

// Count only failures in groups this bridge actually forwards. The account is
// in ~60 personal groups; a stale sender-key in an unrelated chat costs the
// cleaning schedule nothing, and counting it would teach the tracker to cry
// wolf about the cleaning channel. Unattributable failures (no parseable
// remoteJid) DO count — this detector exists because silent loss went
// unnoticed for three months, so anything we cannot rule out stays loud.
function noteDecryptFailure(flat) {
  const jid = (flat.match(REMOTE_JID_RE) || [])[1] || "";
  const inScope = !jid || GROUP_ALLOWLIST.size === 0 || GROUP_ALLOWLIST.has(jid);
  if (!inScope) {
    log.warn({ jid }, "decrypt failure in non-allowlisted group — not counted");
    return;
  }
  counters.decrypt_failures += 1;
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
      counters.forward_failed += 1;
    } else {
      counters.forwarded_ok += 1;
      counters.last_forward_ok_at = Math.floor(Date.now() / 1000);
      log.info({ id: payload.id, group: payload.group_jid, from_me: payload.from_me }, "forwarded");
    }
  } catch (err) {
    log.error({ err: err.message, id: payload.id }, "forward threw");
    counters.forward_failed += 1;
  }
}

// ---------------------------------------------------------------------------
// Reconnect control.
//
// The close handler used to call start() directly. Every call built a NEW
// socket with its OWN close handler while the old socket stayed alive, so a
// flapping connection multiplied connections instead of retrying one — and each
// of those re-ran the connect path. On 2026-07-28 that produced four concurrent
// sockets within ten seconds (four "switching to live mode" lines in the log),
// after which the process died and stayed dead for five days.
//
// Three properties fix it, and all three are needed: only one reconnect may be
// in flight (single-flight), the dead socket is torn down before a new one is
// built (no orphan handlers), and attempts back off so a server-side refusal is
// not hammered.
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 60000;
let reconnectPending = false;
let reconnectAttempts = 0;
let liveSock = null;

function teardown(sock) {
  if (!sock) return;
  try { sock.ev.removeAllListeners(); } catch {}
  try { sock.end(undefined); } catch {}
}

function scheduleReconnect(deadSock) {
  // Only the socket that is actually live may trigger a reconnect. A late close
  // event from a socket we already replaced must not queue a second one.
  if (deadSock && liveSock && deadSock !== liveSock) {
    log.info("ignoring close from a superseded socket");
    return;
  }
  teardown(deadSock);
  if (deadSock === liveSock) liveSock = null;
  if (reconnectPending) {
    log.info("reconnect already scheduled — not queueing another");
    return;
  }
  reconnectPending = true;
  const delay = Math.min(RECONNECT_BASE_MS * 2 ** reconnectAttempts, RECONNECT_MAX_MS);
  reconnectAttempts += 1;
  log.info({ delay_ms: delay, attempt: reconnectAttempts }, "reconnecting");
  setTimeout(() => {
    reconnectPending = false;
    start().catch((err) => {
      log.error({ err: err.message }, "reconnect failed");
      scheduleReconnect(null);
    });
  }, delay);
}

// ---------------------------------------------------------------------------
// Link-state heartbeat.
//
// On 2026-09-05 the bridge went logged-out at 11:47 and sat that way for 22
// hours before anyone found out. Two things had to fail together for that to
// go unnoticed: the tracker's own container-state watchdog restarted the
// add-on 263 times without ever concluding restarting was useless (a running
// container with a dead WhatsApp socket looks perfectly healthy to it — see
// bridge_watchdog.py), and the ONLY other signal the tracker had was message
// traffic, which cannot tell "the bridge is dead" apart from "nobody's said
// anything in three days" — the chat really had been quiet since 2026-09-02,
// three days before the container ever went unhealthy. Traffic answers "did
// anyone say anything"; it can never answer "can this pipe carry a message
// right now".
//
// So the bridge reports its own Baileys connection state on a schedule,
// independent of whether anyone is talking — this is true even in an empty
// room, which message traffic structurally cannot be. It's a push, not a
// poll, because this process runs no HTTP server for the tracker to ask.
// Sent on every connection-state change AND on a fixed interval: the interval
// is what turns "the last thing we heard was open" into an actual deadman
// switch on the tracker side. A push that only fires on change is silent in
// exactly the case that matters most — this process being frozen or killed
// outright, with no close event ever firing.
let linkState = "closed";
let connectedSince = null;
let lastCloseCode = null;

async function postHeartbeat() {
  if (!HA_URL) {
    // Nothing configured to report to — same as forward()'s HA_URL guard,
    // not a bridge failure.
    return;
  }
  const body = {
    connection: linkState,
    connected_since: connectedSince,
    last_close_code: lastCloseCode,
    reconnect_attempts: reconnectAttempts,
    boot_id: BOOT_ID,
    allowlist_size: GROUP_ALLOWLIST.size,
    version: BRIDGE_VERSION,
    sent_at: Math.floor(Date.now() / 1000),
    ...counters,
  };
  // CRITICAL: a heartbeat failure must never throw into the message path,
  // never crash the process, and never block forwarding — it is purely a
  // side channel. Every failure mode below is caught and logged, nothing is
  // re-thrown.
  try {
    const headers = { "Content-Type": "application/json" };
    if (SHARED_SECRET) headers["X-Shared-Secret"] = SHARED_SECRET;
    // Explicit timeout, well under the beat interval. A `catch` protects
    // against a tracker that REFUSES; it does nothing about one that simply
    // never answers, and without this those requests pile up forever.
    const res = await fetch(`${HA_URL}/internal/whatsapp/heartbeat`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(Math.max(2, Math.floor(HEARTBEAT_INTERVAL_SEC / 4)) * 1000),
    });
    if (!res.ok) {
      log.warn({ status: res.status }, "heartbeat post failed");
    }
  } catch (err) {
    // Deliberately NOT retried. A heartbeat is a sample, not a message: a beat
    // generated fifty minutes ago and delivered on a backoff would be stamped
    // fresh on arrival and paper over the very outage it was meant to reveal.
    // Drop it; the next tick is seconds away.
    log.warn({ err: err.message }, "heartbeat post threw — dropped, not retried");
  }
}

// Fixed schedule, independent of connection events — this is the deadman
// half of the design. `.unref()` so a pending interval never keeps the
// process alive past a clean shutdown.
setInterval(() => { postHeartbeat(); }, Math.max(5, HEARTBEAT_INTERVAL_SEC) * 1000).unref();

// One beat at boot, before any connection is attempted. A bridge that starts
// and never manages to connect emits no close event and no open event; without
// this it would be indistinguishable from a bridge that was never started, and
// the tracker would sit on a stale "open" from the previous process.
postHeartbeat();

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

  liveSock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log("\nScan this QR with WhatsApp → Settings → Linked Devices:\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      // A clean connection resets the backoff; otherwise one bad night leaves
      // every later retry stuck at the 60s ceiling.
      reconnectAttempts = 0;
      log.info("connected");
      // Edge-triggered beat. The interval alone would leave the tracker
      // believing the link was down for up to one full period after it
      // recovered, which turns every brief blip into a spurious outage.
      linkState = "open";
      connectedSince = Math.floor(Date.now() / 1000);
      lastCloseCode = null;
      postHeartbeat();
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
      // Report the drop before doing anything else with it. The logged-out
      // path below calls process.exit(1), so a beat sent after that branch
      // would never leave the process — and logged-out is precisely the
      // case the tracker most needs to hear about.
      linkState = "closed";
      connectedSince = null;
      lastCloseCode = code ?? null;
      await postHeartbeat();
      if (shouldReconnect) {
        scheduleReconnect(sock);
      } else {
        // No alarm is raised here any more. The heartbeat above already told
        // the tracker `connection: "closed"` with `last_close_code: 401`, and
        // the tracker decides what that means and who to tell. This used to
        // post straight to a Home Assistant panel — which is where it sat,
        // correctly and uselessly, for the 22 hours of 2026-09-05.
        log.error({ code }, "logged out — delete /data/auth and restart to re-pair");
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
      // Counted BEFORE the allowlist filter: proves this handler is still
      // attached and WhatsApp is still delivering, independently of whether
      // the cleaners happen to be talking.
      counters.socket_events_seen += 1;
      if (GROUP_ALLOWLIST.size > 0 && !GROUP_ALLOWLIST.has(remoteJid)) continue;
      counters.allowlist_matched += 1;

      const ts = Number(msg.messageTimestamp || 0);

      if (!backfillDone) {
        if (!backfillBuf.has(remoteJid)) backfillBuf.set(remoteJid, []);
        backfillBuf.get(remoteJid).push({ msg, ts });
        continue;
      }

      if (type !== "notify") continue;
      // Watermark, not boot time — see the WATERMARK_FILE comment above. A
      // message older than what we last forwarded is a genuine duplicate; a
      // message newer than it but older than this process is the queue WhatsApp
      // held while we were down, and it is exactly what we must not drop.
      if (ts && ts < watermark) continue;
      if (seenIds.has(msg.key.id)) continue;
      seenIds.add(msg.key.id);

      const payload = buildPayload(msg);
      if (payload) {
        await forward(payload);
        if (ts && ts > watermark) {
          watermark = ts;
          writeWatermark(ts);
        }
      }
    }
  });
}

start().catch((err) => {
  log.error({ err: err.message }, "fatal");
  process.exit(1);
});
