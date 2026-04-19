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
