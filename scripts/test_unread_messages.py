"""A message the system failed to read must become a finding.

The ISA's first Principle — "Fail loudly, never silently. A broken dependency
must produce a signal a human sees, not a row stuck in pending" — had no
implementation until 2026-08-20. `review_state`, `pending` and `parse_error`
appeared nowhere in reconcile.py, while 75 messages carried parse errors (60
un-retried rate limits) and 67 had been bulk-filed as `ignored`.

The two properties that matter, both asserted below: findings are dated to the
CLEANING, not the message — so they escalate toward the date and retire
naturally after it, unlike `expire_stale_reviews`, which fires seven days after
the cleaning it concerns and cannot be made timely by any setting — and `why`
carries structured fields only, because the VPS payload allowlist protects keys
and not values (ISC-41).

Run: python3 scripts/test_unread_messages.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import reconcile  # noqa: E402

TODAY = date(2026, 8, 20)
T = TODAY.isoformat()


def d(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def msg(mid, state="pending", err=None, parsed=True, ts=None, sender="Itzel"):
    m = {"id": mid, "review_state": state, "parsed": parsed,
         "timestamp": (ts or d(0)) + "T04:00:00.000Z", "sender_name_raw": sender}
    if err:
        m["parse_error"] = err
    return m


def facts(mid, *dates):
    return {mid: {"facts": [{"target_date": x, "kind": "confirm"} for x in dates]}}


class UnreadMessages(unittest.TestCase):
    def run_(self, messages, fr=None):
        return reconcile._unread_messages(messages, fr or {}, T)

    def test_a_pending_message_about_a_future_cleaning_surfaces(self):
        out = self.run_([msg("m1")], facts("m1", d(21)))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], d(21))
        self.assertEqual(out[0]["kind"], "undecided_message")

    def test_a_failed_extraction_is_named_as_such_only_when_no_facts_exist(self):
        """ISC-351: "extraction failed" consults the facts, not just the flag.

        A failed RE-parse writes `parse_error` over a message whose earlier
        extraction succeeded and whose facts still stand — on 2026-08-21 the
        digest told Josh a message's "extraction failed" while 17 facts from
        it sat in the corpus. Facts present = the system read it; the message
        is waiting on a human, and the finding must say so.
        """
        out = self.run_([msg("m1", err="Anthropic API error: 429")], facts("m1", d(21)))
        self.assertEqual(out[0]["kind"], "undecided_message")
        self.assertIn("waiting for a decision", out[0]["why"])

    def test_error_with_no_facts_is_still_a_failed_extraction(self):
        out = self.run_([msg("m1", err="Anthropic API error: 429")],
                        {"m1": {"facts": []}})
        self.assertEqual(out[0]["kind"], "unread_message")
        self.assertIn("never read", out[0]["why"])

    def test_a_message_whose_extraction_failed_entirely_still_surfaces(self):
        """The case that matters most: no facts at all, so there is no
        target_date to key on. Falling back to the message's own day is what
        keeps it from being dropped by the very detector meant to catch it."""
        out = self.run_([msg("m1", err="boom")], {})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], d(0))

    def test_dated_to_the_cleaning_not_the_message(self):
        """The whole reason this beats review expiry."""
        out = self.run_([msg("m1", ts=d(-3))], facts("m1", d(21)))
        self.assertEqual(out[0]["date"], d(21), "dated to the message, not the cleaning")

    def test_urgency_escalates_as_the_cleaning_approaches(self):
        soon = self.run_([msg("m1")], facts("m1", d(3)))
        far = self.run_([msg("m2")], facts("m2", d(40)))
        self.assertEqual(soon[0]["severity"], "needs-attention")
        self.assertEqual(far[0]["severity"], "suggest")

    def test_the_soonest_named_date_wins(self):
        out = self.run_([msg("m1")], facts("m1", d(40), d(5), d(21)))
        self.assertEqual(out[0]["date"], d(5))

    def test_past_cleanings_are_not_surfaced(self):
        self.assertEqual(self.run_([msg("m1", ts=d(-30))], facts("m1", d(-20))), [])

    def test_decided_messages_are_silent(self):
        for state in ("auto", "ignored", "expired"):
            self.assertEqual(self.run_([msg("m1", state=state)], facts("m1", d(10))), [],
                             f"{state} should not surface")

    def test_a_credit_deferred_message_surfaces_even_though_unparsed(self):
        m = msg("m1", state="pending", err="credit balance is too low", parsed=False)
        self.assertEqual(len(self.run_([m], facts("m1", d(10)))), 1)

    def test_ids_are_stable_so_it_alarms_once_not_nightly(self):
        a = self.run_([msg("m1")], facts("m1", d(10)))
        b = self.run_([msg("m1")], facts("m1", d(10)))
        self.assertEqual(a[0]["id"], b[0]["id"])
        self.assertEqual(a[0]["id"], "unread:m1")

    def test_why_never_carries_message_text(self):
        """ISC-41: the VPS allowlist protects keys, not values."""
        m = msg("m1")
        m["text"] = "Yes Sept 10 I can do it at 11:00 — my number is 604-555-0142"
        out = self.run_([m], facts("m1", d(21)))
        for token in ("604-555", "Yes Sept 10", "11:00"):
            self.assertNotIn(token, out[0]["why"])
        self.assertNotIn("quote", out[0])

    def test_it_is_wired_into_run(self):
        """A detector nothing calls is the failure this whole file exists to
        prevent — the 2026-06-11 lesson, 'a shipped detector is worthless if
        nothing runs it'."""
        data = {"bookings": {}, "messages": [msg("m1")], "message_facts": facts("m1", d(10))}
        res = reconcile.run(data, [], today=TODAY)
        kinds = {f["kind"] for f in res["findings"]}
        self.assertIn("undecided_message", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
