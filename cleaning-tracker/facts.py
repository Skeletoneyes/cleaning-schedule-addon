"""WhatsApp message → structured scheduling facts.

Distinct from the parse step in app.py. Parse decides routing for a single
booking (confirm / decline / none, with one booking_uid). Facts extracts every
scheduling assertion a message carries, from BOTH sides of the chat (cleaner
confirming/declining, or schedule-owner asserting the planned schedule). A
single message can emit many facts — e.g. a schedule dump over 30 dates
becomes 30 schedule_assertion facts.

Extraction is versioned. Bump FACTS_PROMPT_VERSION when the prompt or schema
changes, then POST /admin/reprocess-facts to bring older messages forward.
The reconciler reads only current-version facts, so half-reprocessed state
is safe.
"""

import json
import random
import time
from datetime import datetime

import requests

FACTS_MODEL = "claude-haiku-4-5-20251001"
FACTS_PROMPT_VERSION = "facts-v2"

# Anthropic free/standard tiers rate-limit by tokens-per-minute and
# requests-per-minute. For bulk backfill we pace conservatively and retry
# transient errors.
_MAX_RETRIES = 5
_RETRY_INITIAL_DELAY = 2.0  # seconds, doubles on each retry
_RETRY_MAX_DELAY = 30.0

VALID_KINDS = {
    "confirm",
    "decline",
    "time_proposal",
    "date_proposal",
    "schedule_assertion",
    "unclear",
}


def _sender_role(sender_label, known_cleaners):
    """Heuristic: is this sender one of the known cleaners, or the host?

    JID-style senders are matched against lowercased cleaner names in the
    label. For free-form names (paste ingest), match on substring.
    """
    if not sender_label:
        return "unknown"
    s = sender_label.lower()
    for name in known_cleaners:
        if name and name.lower() in s:
            return f"cleaner:{name}"
    return "host"


def _build_prompt(msg, history, known_cleaners, labels):
    history_lines = []
    for h in history:
        grp = labels.get(h.get("group")) or h.get("group") or "unknown-group"
        sender_label = h.get("sender_name_raw") or h.get("sender") or "unknown"
        role = _sender_role(sender_label, known_cleaners)
        history_lines.append(
            f"[{h.get('timestamp','')}] ({grp}) {sender_label} <{role}>: {h.get('text','')}"
        )
    history_text = "\n".join(history_lines) if history_lines else "(no prior messages)"
    this_group = labels.get(msg.get("group")) or msg.get("group") or "unknown-group"
    target_sender = msg.get("sender_name_raw") or msg.get("sender", "unknown")
    target_role = _sender_role(target_sender, known_cleaners)

    return f"""You extract structured scheduling facts from a single WhatsApp message in a house-cleaning group chat.

Roles:
- HOST: the property owner / schedule maker. Asserts "I would like X on date D" or posts a planned schedule.
- CLEANER: one of the known cleaners {json.dumps(known_cleaners)}. Replies to the host with confirms / declines / counter-proposals.

Prior messages across all groups (oldest first). Each line is tagged `<cleaner:Name>` or `<host>` — use this to decide fact kind. Use the full archive to resolve "yes", "that date", "I can do it", and to recognize when a message quotes or re-posts an earlier list:
---
{history_text}
---

Target message (from {target_sender} <{target_role}> in group "{this_group}" at {msg.get('timestamp','')}):
{msg.get('text','')}

Emit every concrete scheduling fact this message asserts. Resolve dates and times to absolute values using the prior context. CONSIDER BOTH SIDES — cleaners AND the host.

Fact kinds — choose by the sender's role:
- schedule_assertion: HOST ONLY. Host states the planned (date, time, cleaner). Example: "Airbnb downstairs: March 30 3:30pm (booked), April 14 2pm (booked)". Never emit schedule_assertion for a cleaner's message.
- confirm: CLEANER accepts a specific date (explicit "yes" or implicit "see you Tuesday"). Subject = the cleaner accepting.
- decline: CLEANER refuses a specific date ("sorry I'm full").
- time_proposal: CLEANER proposes a specific clock time for a date (emit alongside confirm if date is accepted but time differs from host's request).
- date_proposal: CLEANER proposes a different specific date.
- unclear: commitment-shaped language that cannot be resolved with the given context.

CRITICAL — re-posted lists:
When a cleaner message quotes the HOST's earlier list of dates and adds per-row times or notes, it is a bulk CONFIRM (not a schedule_assertion). One `confirm` per row the cleaner answered, one `time_proposal` per row where the cleaner set a time. Rows the cleaner marks as unavailable ("I'm full") emit `decline` for that row. Rows the cleaner did not resolve should be skipped.

CRITICAL — narrative vs fact:
Skip messages that narrate a past incident, ask a favor, or discuss money / thanks / door codes / greetings. Example: "Daria was unable to clean because of this" is a past-tense narration, NOT a decline fact (the fact, if any, was earlier). Example: "we are going to pay double rate since it's last minute ($120)" is payment talk, not scheduling.

Emit one fact per date referenced. A single message can span many dates (a multi-line schedule or reply). Multi-topic messages: extract each date's fact independently; ignore unrelated chitchat inside the same message.

Set `cleaner` to the SUBJECT of the fact — the cleaner the fact is about. Usually the sender (if they are a cleaner); for a host's schedule_assertion, only set cleaner if the host names one explicitly for that row.

Set `tentative` to true only when the speaker explicitly flags uncertainty ("but let me check my agenda tonight and reconfirm"). Do NOT set tentative=true merely because you are uncertain.

Dates: absolute ISO "YYYY-MM-DD". Resolve relative terms ("today", "tomorrow", weekday names) against the message timestamp. If year is missing, pick the nearest plausible year. Times: 24-hour "HH:MM". Unknown → null.

Return ONLY valid JSON, no prose, no code fences:
{{"facts":[{{"kind":"...","target_date":"YYYY-MM-DD or null","target_time":"HH:MM or null","cleaner":"canonical name or null","confidence":0.0,"tentative":false,"evidence":"short quote from the message"}}]}}"""


def extract_facts(api_key, msg, history, known_cleaners, labels):
    """Call Haiku to extract scheduling facts from one message.

    Returns (facts_list, error). facts_list may be empty (chitchat); that's a
    successful extraction. Only `error is not None` indicates the caller
    should retry later.
    """
    if not api_key:
        return None, "No Anthropic API key configured."

    prompt = _build_prompt(msg, history, known_cleaners, labels)
    last_err = None
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": FACTS_MODEL,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            status = resp.status_code
            if status == 429 or 500 <= status < 600:
                last_err = f"Anthropic {status}: {resp.text[:200]}"
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if retry_after else delay
                time.sleep(min(wait + random.uniform(0, 0.5), _RETRY_MAX_DELAY))
                delay = min(delay * 2, _RETRY_MAX_DELAY)
                continue
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(text)
            break
        except requests.exceptions.HTTPError as e:
            return None, f"Anthropic API error: {e.response.status_code} - {e.response.text[:200]}"
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"Network error: {e}"
            time.sleep(min(delay + random.uniform(0, 0.5), _RETRY_MAX_DELAY))
            delay = min(delay * 2, _RETRY_MAX_DELAY)
            continue
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            return None, f"Failed to parse LLM response: {e}"
        except Exception as e:
            return None, f"Error calling Anthropic API: {e}"
    else:
        return None, last_err or "Exhausted retries"

    raw = parsed.get("facts") or []
    cleaned = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        kind = f.get("kind")
        if kind not in VALID_KINDS:
            continue
        try:
            confidence = float(f.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        cleaned.append({
            "kind": kind,
            "target_date": f.get("target_date") or None,
            "target_time": f.get("target_time") or None,
            "cleaner": f.get("cleaner") or None,
            "confidence": confidence,
            "tentative": bool(f.get("tentative", False)),
            "evidence": (f.get("evidence") or "")[:200],
        })
    return cleaned, None


def build_record(facts, reported_by_jid):
    """Wrap a facts list in the stored record shape.

    Always includes version stamps, so an empty facts list still records that
    we processed the message at this prompt version.
    """
    return {
        "facts": facts,
        "reported_by_jid": reported_by_jid,
        "model_version": FACTS_MODEL,
        "prompt_version": FACTS_PROMPT_VERSION,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
    }
