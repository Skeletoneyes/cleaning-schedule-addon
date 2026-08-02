"""Unit tests for review-queue expiry and the prompt date header.

The queue never aged anything out: 16 items pending on 2026-08-02, 14 about
dates already gone. A queue that only grows stops being a queue — the items
needing a decision are the ones buried in it.

Run: python3 scripts/test_review_expiry.py
"""
from __future__ import annotations

import ast
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"


def _extract(names, ns):
    tree = ast.parse((APP_DIR / "app.py").read_text(encoding="utf-8"))
    found = {n.name: n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names}
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"not found in app.py: {sorted(missing)}")
    for n in names:
        exec(compile(ast.Module(body=[found[n]], type_ignores=[]), "app.py", "exec"), ns)
    return ns


NS = {"date": date, "timedelta": timedelta,
      "cleaning_date_for": lambda b: b.get("end")}
_extract(["_review_subject_date", "_date_header"], NS)
subject_date = NS["_review_subject_date"]
date_header = NS["_date_header"]

TODAY = date(2026, 8, 2)


class SubjectDateTests(unittest.TestCase):
    """What a review item is ABOUT decides whether it is stale — not when it
    was sent."""

    BOOKINGS = {"b-aug": {"end": "2026-08-14"}, "b-jun": {"end": "2026-06-10"}}

    def test_prefers_the_booking_date_over_the_send_date(self):
        msg = {"timestamp": "2026-06-28T10:00:00",
               "haiku_result": {"booking_uid": "b-aug"}}
        self.assertEqual(subject_date(msg, self.BOOKINGS), "2026-08-14")

    def test_a_june_message_about_an_august_booking_is_not_stale(self):
        """The regression this ordering prevents: aging on the send date would
        retire a live commitment made well in advance."""
        msg = {"timestamp": "2026-06-28T10:00:00",
               "haiku_result": {"booking_uid": "b-aug"}}
        cutoff = (TODAY - timedelta(days=7)).isoformat()
        self.assertGreater(subject_date(msg, self.BOOKINGS), cutoff)

    def test_falls_back_to_the_send_date_when_no_booking_matched(self):
        msg = {"timestamp": "2026-06-28T10:00:00", "haiku_result": {"booking_uid": None}}
        self.assertEqual(subject_date(msg, self.BOOKINGS), "2026-06-28")

    def test_unknown_booking_uid_falls_back(self):
        msg = {"timestamp": "2026-06-28T10:00:00", "haiku_result": {"booking_uid": "gone"}}
        self.assertEqual(subject_date(msg, self.BOOKINGS), "2026-06-28")

    def test_no_timestamp_and_no_booking_is_none_not_a_crash(self):
        self.assertIsNone(subject_date({"haiku_result": {}}, self.BOOKINGS))

    def test_live_timestamp_shape_is_handled(self):
        msg = {"timestamp": "2026-07-28T21:08:38.000Z", "haiku_result": {}}
        self.assertEqual(subject_date(msg, {}), "2026-07-28")


class ExpiryRuleTests(unittest.TestCase):
    """The rule itself: subject date strictly older than today - N days."""

    def _stale(self, subj, days=7):
        return subj < (TODAY - timedelta(days=days)).isoformat()

    def test_a_week_old_is_kept_and_older_is_retired(self):
        self.assertFalse(self._stale("2026-07-26"), "exactly the cutoff stays")
        self.assertTrue(self._stale("2026-07-25"), "a day past the cutoff goes")

    def test_future_dates_are_never_retired(self):
        self.assertFalse(self._stale("2026-08-14"))

    def test_the_real_backlog_shape(self):
        """The 2026-06-28 and 2026-07-04 items Josh was staring at."""
        for d in ("2026-06-28", "2026-07-04", "2026-07-08", "2026-07-14"):
            self.assertTrue(self._stale(d), f"{d} should have been retired")


class DateHeaderTests(unittest.TestCase):
    def test_states_today_with_its_weekday(self):
        h = date_header({"timestamp": "2026-08-02T09:00:00"}, TODAY)
        self.assertIn("TODAY IS 2026-08-02", h)
        self.assertIn("Sunday", h)

    def test_a_backfilled_message_is_anchored_to_its_send_date(self):
        """Processing a January message in August must not re-date it. Stating
        only 'today' would silently shift an entire backfill."""
        h = date_header({"timestamp": "2026-01-03T12:00:00"}, TODAY)
        self.assertIn("SENT ON 2026-01-03", h)
        self.assertIn("against the SEND date", h)

    def test_a_same_day_message_gets_the_simple_instruction(self):
        h = date_header({"timestamp": "2026-08-02T09:00:00"}, TODAY)
        self.assertNotIn("SENT ON", h)
        self.assertIn("against today", h)

    def test_missing_or_broken_timestamp_still_yields_today(self):
        for ts in (None, "", "garbage"):
            h = date_header({"timestamp": ts}, TODAY)
            self.assertIn("TODAY IS 2026-08-02", h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
