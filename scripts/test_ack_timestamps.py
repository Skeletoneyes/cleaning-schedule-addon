"""Acknowledgement evidence must be compared as instants, never as strings.

Found 2026-08-20. `notify_ack` decides whether a cleaner has already been told
about a change, by asking whether a message came strictly AFTER the recorded
commitment. It compared the two raw strings:

    ts = msg["timestamp"]          # "2026-08-20T05:53:19.000Z"  UTC, 241 of 724
    since = commitment["…_at"]     # "2026-08-20T00:00:00"       naive local, 45 of 45
    if not ts or ts <= since: continue

Every message ingested since 2026-04-21 is UTC-Z; every commitment is naive
local. UTC reads seven hours AHEAD of the same instant, so a message sent up to
seven hours BEFORE a commitment compared as coming after it — and the `.000Z`
suffix pushed even identical instants the same way, because "…:19.000Z" sorts
after "…:19".

The direction is what made it serious. It failed OPEN: it manufactured
acknowledgements, marking a cleaner as informed by a message that predates the
thing she was supposed to have been told, and then the notify queue went quiet
on exactly that booking.

Run: python3 scripts/test_ack_timestamps.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import notify_ack  # noqa: E402


class AsInstant(unittest.TestCase):
    def test_the_exact_pair_that_was_wrong(self):
        """A message at 22:00 Vancouver vs a commitment written at 00:00 the
        next morning — two hours LATER in real time. String comparison said the
        message came after. It did not."""
        msg = notify_ack._as_instant("2026-08-20T05:00:00.000Z")   # 22:00 Aug 19 local
        commit = notify_ack._as_instant("2026-08-20T00:00:00")     # 00:00 Aug 20 local
        self.assertLessEqual(msg, commit)
        # and the old behaviour, preserved here so the regression is legible
        self.assertFalse("2026-08-20T05:00:00.000Z" <= "2026-08-20T00:00:00")

    def test_both_stored_shapes_parse(self):
        self.assertIsNotNone(notify_ack._as_instant("2026-08-20T05:53:19.000Z"))
        self.assertIsNotNone(notify_ack._as_instant("2026-07-30T12:35:00"))

    def test_a_naive_stamp_is_read_as_local_not_utc(self):
        naive = notify_ack._as_instant("2026-08-20T12:00:00")
        self.assertIsNotNone(naive.tzinfo, "naive stamp left without a zone")
        self.assertEqual(naive.utcoffset(), timedelta(hours=-7), "August is PDT")

    def test_winter_uses_pst_not_a_frozen_summer_offset(self):
        """A fixed offset would be wrong for half the year — the same shape as
        the bug being fixed."""
        self.assertEqual(
            notify_ack._as_instant("2026-01-15T12:00:00").utcoffset(),
            timedelta(hours=-8),
        )

    def test_identical_instants_do_not_resolve_to_after(self):
        """The `.000Z` suffix used to break the tie in the unsafe direction."""
        a = notify_ack._as_instant("2026-08-20T19:00:00.000Z")
        b = notify_ack._as_instant("2026-08-20T12:00:00")  # same instant, PDT
        self.assertEqual(a, b)
        self.assertLessEqual(a, b, "an equal instant must not count as strictly after")

    def test_a_genuinely_later_message_still_counts(self):
        """The fix must not break the feature: a message that really does come
        after the commitment is still valid evidence."""
        commit = notify_ack._as_instant("2026-08-20T00:00:00")      # 00:00 local
        later = notify_ack._as_instant("2026-08-20T18:00:00.000Z")  # 11:00 local
        self.assertGreater(later, commit)

    def test_unparseable_is_none_not_long_ago(self):
        """Fail closed. A malformed stamp must never satisfy strictly-after."""
        for bad in ("", None, "garbage", "2026-13-45T99:99:99"):
            self.assertIsNone(notify_ack._as_instant(bad), f"{bad!r} parsed")

    def test_the_comparison_site_uses_instants(self):
        """Guards against a future edit reverting to string comparison."""
        src = (Path(__file__).resolve().parent.parent
               / "cleaning-tracker" / "notify_ack.py").read_text()
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("if ts is None or since_dt is None or ts <= since_dt", code)
        self.assertIn("ts_at <= since_dt", code,
                      "the gate no longer compares instants")
        self.assertIn('best["at"]', code,
                      "ordering must use the instant, not the raw string")


class EvidenceGate(unittest.TestCase):
    """End-to-end through find_ack_evidence with the real live shapes."""

    def _booking(self, communicated_at):
        return {
            "end": "2026-09-10", "cleaner": "Itzel", "clean_time": "11:00:00",
            "cleaner_commitment": {"cleaner": "Darya", "date": "2026-09-10",
                                   "clean_time": "11:00:00",
                                   "communicated_at": communicated_at},
        }

    def _run(self, msg_ts, communicated_at):
        messages_by_id = {"m1": {"id": "m1", "timestamp": msg_ts, "group": "g-darya",
                                 "text": "Itzel is doing Sept 10"}}
        facts = {"m1": {"facts": [{"kind": "schedule_assertion",
                                   "target_date": "2026-09-10", "cleaner": "Itzel",
                                   "confidence": 0.95,
                                   "evidence": "Itzel is doing Sept 10"}]}}
        return notify_ack.find_ack_evidence(
            self._booking(communicated_at), facts, messages_by_id,
            {"Darya": "g-darya", "Itzel": "g-itzel"},
        )

    def test_a_message_predating_the_commitment_is_not_evidence(self):
        """The live failure: a UTC message at 22:00 the previous evening against
        a commitment written after midnight. String comparison accepted it."""
        out = self._run("2026-08-20T05:00:00.000Z", "2026-08-20T00:00:00")
        self.assertNotIn("displaced", out["sides"],
                         "a message that predates the commitment was accepted")

    def test_a_genuinely_later_message_is_still_accepted(self):
        """The fix must not break the feature it guards."""
        out = self._run("2026-08-20T18:00:00.000Z", "2026-08-20T00:00:00")
        self.assertIn("displaced", out["sides"])
        self.assertEqual(out["sides"]["displaced"]["verdict"], "told-moved")

    def test_describe_still_renders_the_timestamp(self):
        """`timestamp` stays a STRING for display — the comparison instant is a
        separate key. Storing the datetime there crashed describe()'s slice."""
        out = self._run("2026-08-20T18:00:00.000Z", "2026-08-20T00:00:00")
        self.assertIsInstance(out["sides"]["displaced"]["timestamp"], str)
        text = notify_ack.describe("2026-09-10", out)
        self.assertIn("2026-08-20", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
