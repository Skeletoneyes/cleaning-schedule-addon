"""Fire synthetic WhatsApp inbound messages at the running add-on.

Usage (with add-on served on http://127.0.0.1:5000):

    python scripts/whatsapp_fixture.py               # send all fixtures
    python scripts/whatsapp_fixture.py --scenario confirm
    python scripts/whatsapp_fixture.py --url http://127.0.0.1:5000 --scenario unmapped

The endpoint (/internal/whatsapp/inbound) is loopback-only, so this must run
on the same machine as the Flask process.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta


def _now_plus(seconds=0):
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


# Group JIDs kept stable so chat history accumulates across runs.
GROUP_A = "120363000000000001@g.us"
GROUP_B = "120363000000000002@g.us"

# Sender JIDs. MAP = known cleaner (via config or a prior mapping).
# UNMAP = unmapped — triggers the review-queue mapping prompt.
SENDER_MAP_MICHELLE = "5215550000001@s.whatsapp.net"
SENDER_MAP_HOST = "5215550000099@s.whatsapp.net"   # "me/host" in the chat
SENDER_UNMAPPED = "5215559999999@s.whatsapp.net"


SCENARIOS = {
    # Plain confirmation — host proposes a date, cleaner says yes.
    "confirm": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Hi Maria, can you clean on Apr 25? Checkout is 11am."},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Yes, 1pm works. See you then."},
    ],

    # Cleaner declines.
    "decline": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Are you free to clean on May 2?"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Sorry, I'm fully booked that day."},
    ],

    # Ambiguous date — cleaner says "next week" without specifying.
    "ambiguous": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_B,
         "text": "Got a cleaning coming up."},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_B,
         "text": "Sure, let me know the day"},
    ],

    # Unmapped-sender scenario — first message from a new JID.
    "unmapped": [
        {"sender_jid": "5215558888888@s.whatsapp.net", "group_jid": GROUP_A,
         "text": "Hi, this is Patricia. I can do Apr 30 at 10am."},
    ],

    # Unrelated chit-chat — should be marked ignored.
    "chitchat": [
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Good morning! How is your day going?"},
    ],

    # ── Facts-layer scenarios (drawn from the real Itzel chat) ──
    # These exercise the extractor, not just the parser. Inspect results via
    # GET /admin/facts after running.

    # Batch commit: one message commits to two different dates.
    "batch_commit": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Can you do May 19 and May 23?"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Yea I can do it may 19th at 1:00 pm\nMay 23th I can do it at 1:00 pm\nThank you"},
    ],

    # Counter-time: host proposes a window, cleaner counters with a specific time.
    "counter_time": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Are you available to clean on April 20 anytime after 11am?"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Hello guys yes I can do it\nApril 20th at 4:30\nThank you"},
    ],

    # Tentative confirm: yes but will reconfirm tonight after checking agenda.
    "tentative": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Here's the schedule for April 14 at 2pm, please confirm."},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Yes confirmed\n\nI'm already outside working very early, can I confirm to you tonight? I have my agenda at home"},
    ],

    # Delegation: sender commits a different cleaner by proxy.
    "delegation": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Hey Itzel, we just got a booking ending May 1st. Can you come between 11am and 3pm?"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Hello guys I'm so sorry may 1st I'm full but I asked Daria and she said yes. She can go may 1st at 11:00 am"},
    ],

    # Schedule dump + bulk confirm: host dumps a full multi-date schedule,
    # cleaner replies with a single confirmation that covers all of them.
    "schedule_dump": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Here's an updated schedule!\n\nApril 14, 2:00 PM\nApril 20 - 4:30 PM\nMay 7 - 12:00\nMay 13 - 4:30pm\nMay 16 - 11am\nMay 19 - 1:00pm\nMay 23 - 1:00pm"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Yes ✔️ confirmed"},
    ],

    # Dense chitchat: payments, emoji, thanks, door codes — all empty facts.
    "chitchat_dense": [
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Sent payment for today 💪"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Thank you!!"},
        {"sender_jid": SENDER_MAP_HOST, "group_jid": GROUP_A,
         "text": "Code 2468"},
        {"sender_jid": SENDER_UNMAPPED, "group_jid": GROUP_A,
         "text": "Happy Easter 🐣🌸💜"},
    ],
}


def send(url, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url + "/internal/whatsapp/inbound",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e)


def run_scenario(url, name, messages):
    print(f"\n=== {name} ({len(messages)} message(s)) ===")
    for i, base in enumerate(messages):
        payload = dict(base)
        payload.setdefault("id", f"fixture-{name}-{uuid.uuid4().hex[:8]}")
        payload.setdefault("timestamp", _now_plus(i))
        status, body = send(url, payload)
        print(f"  [{status}] {payload['id']} -> {body}")
        # Tiny gap so Haiku sees history in order.
        time.sleep(0.2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:5000")
    p.add_argument("--scenario", choices=list(SCENARIOS) + ["all"], default="all")
    args = p.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for n in names:
        run_scenario(args.url, n, SCENARIOS[n])

    print("\nDone. Open the add-on UI → Review tab to see results.")


if __name__ == "__main__":
    sys.exit(main() or 0)
