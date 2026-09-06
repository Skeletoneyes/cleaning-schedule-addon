---
name: ingest-whatsapp-statements
description: Backfill a gap in the WhatsApp record by reading a chat transcript and writing structured statements to the cleaning tracker. Invoke when the user wants to fill in messages the bridge missed, ingest a chat export, or recover a blind window.
---

# Ingest WhatsApp statements

## What this is for

The bridge occasionally misses a window — it was logged out, the container was
down, or a per-sender key broke and muted somebody. The messages still exist on
the phone. This gets them into the tracker.

**You do the reading. The endpoint does the writing.** That split is the whole
design: judging what "ok" means needs the whole conversation in view, which a
model has and a per-message parser never did; but *writing* has to be
deterministic, idempotent and auditable, which prose never is.

## What it replaced, and do not rebuild it

`POST /admin/ingest-transcript` and the `/admin/ingest` form still exist and are
**deprecated — do not use them**. That path parses three WhatsApp export
formats, spends one model call per message deciding what each meant with only a
rolling window for context, and gates on a cost confirmation. It ran twice in
five months and carried three clock bugs (two now fixed; the third, that its
message ids hash a pre-migration timestamp, is why re-pasting an old transcript
through it still will not dedupe). Removing it is a pending follow-up — it was
left in place rather than deleted on the same day the tracker had already been
taken down once.

⚠️ **If you find yourself writing a transcript parser, stop.** Read the
transcript yourself and emit statements. That is the point.

## Procedure

1. **Get the transcript.** A file outside the repos is tidiest — the public
   add-on repo has had cleaner conversation scrubbed from its history once
   already, and `.gitignore` carries the banner. Pasting into the session is
   equally fine: session transcripts are gitignored and cannot reach the VPS
   projection, and the live pipeline already sends every message to the same
   API, so a paste is not new exposure.

2. **Trim to the window you actually need.** Dedup is good but not free — see
   the tolerance below.

3. **Read it and decide what was said.** One statement per commitment, not per
   line. A message spanning four dates is four statements. Chitchat, thanks,
   door codes and payment talk are not statements.

4. **Dry run first.** `"dry_run": true` reports what would be written and what
   is already captured, and writes nothing.

5. **Write it.** Then read `GET /reconcile/last` (or `POST /reconcile/run`) and
   tell the user what changed. Statements produce *findings*; a human accepts
   them.

## The call

```bash
SSH="ssh -p 22 -i ~/.ssh/id_ed25519_ha root@homeassistant.local"
$SSH "curl -sS -X POST -H 'X-Shared-Secret: <whatsapp_shared_secret>' \
  -H 'Content-Type: application/json' --data-binary @/tmp/statements.json \
  http://172.30.32.1:5000/internal/statements"
```

Auth is loopback, HA ingress, or `X-Shared-Secret` matching the tracker's
`whatsapp_shared_secret` option. ⚠️ Ship the JSON as a **file** with
`--data-binary @path` — inlining it through two layers of shell quoting mangles
the body, and Supervisor answers a mangled body with `403`, which reads exactly
like a permissions problem.

```json
{
  "source": "session-ingest",
  "dry_run": true,
  "statements": [
    {
      "said_at": "2026-09-03T14:05:00",
      "group": "<group jid>",
      "speaker": "Darya",
      "text": "Yes I can do the 18th, around 10",
      "kind": "confirm",
      "target_date": "2026-09-18",
      "target_time": "10:00",
      "cleaner": "Darya",
      "tentative": false,
      "confidence": 0.9
    }
  ]
}
```

- `said_at` — **when it was said**, from the transcript. Naive values are read
  as local time (America/Vancouver) and stored as UTC.
- `text` — mandatory. It is the provenance for the fact; a statement without
  the words that justify it is unauditable.
- `kind` — one of `confirm`, `decline`, `time_proposal`, `date_proposal`,
  `schedule_assertion`, `unclear`. Anything else is rejected.

## Gotchas

- **The batch is all or nothing.** One bad statement rejects the whole call and
  writes nothing. A half-applied backfill cannot be distinguished from a
  complete one afterwards.
- **Dedup is content-based**: same group, same words (whitespace- and
  case-insensitive), within **120 seconds**. That is why trimming matters — a
  message repeated more than two minutes later is treated as a second message,
  because people do repeat themselves.
- **Re-running the same batch is a no-op.** The id is a hash of
  `said_at|speaker|text`.
- **It never writes bookings**, on purpose. Statements → detectors → findings →
  the user decides. Writing bookings from here would bypass the reconciler's
  contradiction checks, which are the only second opinion on whether your
  reading was right. There is a test asserting this.
- **`extracted_at` is not a clock.** It is when extraction ran. Ordering keys
  off the message's own timestamp — see the "One clock" block in `app.py`. A
  transcript pasted today would otherwise stamp every historical line with
  today and let an old statement outrank a newer live one.

## Verify

```bash
$SSH "curl -sS -H 'X-Shared-Secret: <secret>' http://172.30.32.1:5000/admin/facts" 
```

Check the count moved by what you inserted, then run the reconciler and report
the findings. Do not report success from the endpoint's own response alone —
it tells you what it wrote, not what the detectors made of it.
