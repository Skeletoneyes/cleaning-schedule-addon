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
from datetime import datetime

import requests

FACTS_MODEL = "claude-haiku-4-5-20251001"
FACTS_PROMPT_VERSION = "facts-v1"

VALID_KINDS = {
    "confirm",
    "decline",
    "time_proposal",
    "date_proposal",
    "schedule_assertion",
    "unclear",
}


def _build_prompt(msg, history, known_cleaners, labels):
    history_lines = []
    for h in history:
        grp = labels.get(h.get("group")) or h.get("group") or "unknown-group"
        sender_label = h.get("sender") or "unknown"
        history_lines.append(
            f"[{h.get('timestamp','')}] ({grp}) {sender_label}: {h.get('text','')}"
        )
    history_text = "\n".join(history_lines) if history_lines else "(no prior messages)"
    this_group = labels.get(msg.get("group")) or msg.get("group") or "unknown-group"

    return f"""You extract structured scheduling facts from a single WhatsApp message in a house-cleaning group chat.

Known cleaners: {json.dumps(known_cleaners)}

Prior messages across all groups (oldest first). Use these to resolve references like "that date", "yes", "tomorrow", or bulk confirmations that point back to a schedule dump:
---
{history_text}
---

Target message (from {msg.get('sender','unknown')} in group "{this_group}" at {msg.get('timestamp','')}):
{msg.get('text','')}

Emit every concrete scheduling fact this message asserts, resolving dates and times to absolute values using the prior context. The sender may be a cleaner (confirming / declining / counter-proposing) or the schedule owner / host (asserting the planned schedule). Consider BOTH sides.

Fact kinds:
- confirm: a cleaner accepts an assignment on a specific date (explicit "yes" or implicit like "see you Tuesday")
- decline: a cleaner refuses a specific date (e.g. "sorry I'm full that day")
- time_proposal: a cleaner proposes a specific clock time for a specific date (emit alongside confirm if they accept the date but counter on the time)
- date_proposal: a cleaner proposes a different specific date
- schedule_assertion: the schedule owner (host) states the planned date + cleaner + optional time (e.g. a schedule dump listing "May 4 - Daria, May 7 - 12:00")
- unclear: commitment-shaped language you cannot resolve with the given context

Emit one fact per date referenced. A single message can emit many facts:
- A schedule dump listing 30 dates → 30 schedule_assertion facts.
- A reply "yes confirmed" that clearly points at a prior 30-date dump → 30 confirm facts for those dates.
- "I can do May 19 at 1pm and May 23 at 1pm" → 2 confirm facts (plus time_proposal if the times differ from what was originally asked).

Set `cleaner` to the SUBJECT of the fact — the cleaner the fact is about. Usually that's the sender (if they are a cleaner), but can differ when someone commits on behalf of another, e.g. "I asked Daria, she can go May 1st at 11:00" emits a confirm with cleaner=Daria. Resolve names to the canonical cleaner roster above.

Set `tentative` to true when the commitment is provisional (e.g. "yes confirmed, but let me check my agenda tonight and reconfirm").

If the message is pure chitchat, logistics, payment acknowledgment, door codes, greetings, thanks, or otherwise has no scheduling content, return an empty facts array.

Dates must be absolute ISO "YYYY-MM-DD". If the year is unstated, pick the nearest plausible year given the message timestamp. Times must be 24-hour "HH:MM". Unknown date or time → null.

Return ONLY valid JSON, no other text:
{{"facts":[{{"kind":"...","target_date":"YYYY-MM-DD or null","target_time":"HH:MM or null","cleaner":"canonical name or null","confidence":0.0,"tentative":false}}]}}"""


def extract_facts(api_key, msg, history, known_cleaners, labels):
    """Call Haiku to extract scheduling facts from one message.

    Returns (facts_list, error). facts_list may be empty (chitchat); that's a
    successful extraction. Only `error is not None` indicates the caller
    should retry later.
    """
    if not api_key:
        return None, "No Anthropic API key configured."

    prompt = _build_prompt(msg, history, known_cleaners, labels)
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
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        parsed = json.loads(text)
    except requests.exceptions.HTTPError as e:
        return None, f"Anthropic API error: {e.response.status_code} - {e.response.text[:200]}"
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        return None, f"Failed to parse LLM response: {e}"
    except Exception as e:
        return None, f"Error calling Anthropic API: {e}"

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
