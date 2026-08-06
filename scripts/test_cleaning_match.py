"""Unit tests for cleaning-vs-reservation matching and the auto-apply gate.

Covers the 2026-08-06 fix. On 2026-08-05 Itzel messaged "Hello guys I'm here"
at 12:12 local, on her way into the cleaning for the stay that CHECKED OUT
that day. It was auto-applied, at 0.90 confidence, to the stay that CHECKED IN
that day — marking the Aug 10 cleaning confirmed on the strength of a message
about Aug 5. An audit of the archive found 16 of 48 auto-applied confirmations
had done the same thing, because 53% of cleanings here fall on a day that is
also the next guest's check-in.

Two causes, one test file:
  A. The candidate list handed the model RESERVATIONS (two dates each,
     labelled "Aug 05 → Aug 10"), so on a turnover day two rows displayed the
     same date. It now hands over CLEANINGS — one date per row.
  B. Nothing checked the answer. `confidence` is the model's certainty about
     its reading of the sentence, not about the row it picked, so a confident
     wrong answer had nothing to fail against. The model now states the
     cleaning day separately and the gate requires the two to agree.

Same harness as test_parser_context.py: pull the real function source out of
app.py with `ast` and exec it against injected fakes, so a rename or reshape
fails loudly here instead of passing against a stale copy.

Run: python3 scripts/test_cleaning_match.py
"""
from __future__ import annotations

import ast
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"
sys.path.insert(0, str(APP_DIR))


def _extract(names, ns):
    src = (APP_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names}
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"function(s) not found in app.py: {sorted(missing)}")
    for name in names:
        exec(compile(ast.Module(body=[found[name]], type_ignores=[]), "app.py", "exec"), ns)
    return ns


NS = {
    "date": date, "datetime": datetime, "timedelta": timedelta,
    "AUTO_APPLY_CONFIDENCE": 0.85,
    # Only LOCAL_TZ is reached for; a stub keeps the test off gcal's imports.
    "gcal_mod": types.SimpleNamespace(LOCAL_TZ="America/Vancouver"),
}
_extract(["_msg_local_day", "_relative_day", "upcoming_booking_list",
          "_auto_apply_decision", "_date_header"], NS)

msg_local_day = NS["_msg_local_day"]
booking_list = NS["upcoming_booking_list"]
decide = NS["_auto_apply_decision"]
date_header = NS["_date_header"]

# The real shape of the two bookings that collided, from the live snapshot.
CHECKING_OUT = "1418fb94e984-out@airbnb.com"   # Aug 03 → Aug 05, cleaned Aug 5
CHECKING_IN = "1418fb94e984-in@airbnb.com"     # Aug 05 → Aug 10, cleaned Aug 10

TURNOVER = {
    CHECKING_OUT: {"start": "2026-08-03", "end": "2026-08-05",
                   "status": "active", "cleaner": "Itzel"},
    CHECKING_IN: {"start": "2026-08-05", "end": "2026-08-10",
                  "status": "active", "cleaner": "Itzel"},
}


class CandidateListTests(unittest.TestCase):
    """A: the model can only match on a date it is actually shown."""

    def test_a_turnover_day_appears_once_not_twice(self):
        """The whole bug in one assertion. Aug 5 is a checkout AND a check-in;
        exactly one row may carry it, and it must be the cleaning."""
        rows = booking_list(TURNOVER, anchor=date(2026, 8, 5))
        on_aug5 = [r for r in rows if r["cleaning_date"] == "2026-08-05"]
        self.assertEqual(len(on_aug5), 1)
        self.assertEqual(on_aug5[0]["uid"], CHECKING_OUT)

    def test_check_in_dates_are_not_emitted_at_all(self):
        """Aug 03 and Aug 05 are check-ins. Neither may appear as a value
        anywhere in a row — not as a field, not inside a label."""
        rows = booking_list(TURNOVER, anchor=date(2026, 8, 5))
        for row in rows:
            self.assertNotIn("checkin", row)
            self.assertNotIn("checkout", row)
            self.assertNotIn("label", row)
            self.assertNotIn("2026-08-03", str(row))

    def test_todays_cleaning_is_labelled_today(self):
        rows = booking_list(TURNOVER, anchor=date(2026, 8, 5))
        by_uid = {r["uid"]: r for r in rows}
        self.assertEqual(by_uid[CHECKING_OUT]["when"], "today")
        self.assertEqual(by_uid[CHECKING_IN]["when"], "in 5 days")

    def test_relative_wording_across_the_window(self):
        rel = NS["_relative_day"]
        self.assertEqual(rel(0), "today")
        self.assertEqual(rel(1), "tomorrow")
        self.assertEqual(rel(-1), "yesterday")
        self.assertEqual(rel(5), "in 5 days")
        self.assertEqual(rel(-3), "3 days ago")

    def test_the_anchor_moves_the_window_for_backfill(self):
        """A February message reprocessed in August must see February's
        candidates. Anchoring on today instead offered it a random summer
        booking — five archived messages were applied that way."""
        feb = {"feb": {"start": "2026-02-01", "end": "2026-02-04",
                       "status": "active", "cleaner": "Itzel"}}
        self.assertEqual(booking_list(feb, anchor=date(2026, 8, 5)), [])
        rows = booking_list(feb, anchor=date(2026, 2, 3))
        self.assertEqual([r["uid"] for r in rows], ["feb"])
        self.assertEqual(rows[0]["when"], "tomorrow")

    def test_window_bounds_hold_against_the_anchor(self):
        b = {
            "old": {"start": "2026-07-25", "end": "2026-08-01",
                    "status": "active", "cleaner": None},   # 4 days back — out
            "edge": {"start": "2026-07-25", "end": "2026-08-02",
                     "status": "active", "cleaner": None},  # 3 days back — in
            "far": {"start": "2026-10-01", "end": "2026-10-06",
                    "status": "active", "cleaner": None},   # 62 days on — out
        }
        uids = {r["uid"] for r in booking_list(b, anchor=date(2026, 8, 5))}
        self.assertEqual(uids, {"edge"})

    def test_cancelled_and_complete_bookings_are_never_candidates(self):
        b = {
            "gone": {"start": "2026-08-03", "end": "2026-08-05",
                     "status": "cancelled", "cleaner": "Itzel"},
            "done": {"start": "2026-08-03", "end": "2026-08-05",
                     "status": "complete", "cleaner": "Itzel"},
        }
        self.assertEqual(booking_list(b, anchor=date(2026, 8, 5)), [])

    def test_malformed_dates_are_skipped_not_raised(self):
        b = {"bad": {"start": "2026-08-03", "end": "not-a-date",
                     "status": "active", "cleaner": None},
             "nodate": {"start": "2026-08-03", "status": "active", "cleaner": None}}
        self.assertEqual(booking_list(b, anchor=date(2026, 8, 5)), [])

    def test_rows_are_sorted_by_cleaning_day(self):
        rows = booking_list(TURNOVER, anchor=date(2026, 8, 5))
        self.assertEqual([r["cleaning_date"] for r in rows],
                         ["2026-08-05", "2026-08-10"])


class LocalDayTests(unittest.TestCase):
    """The anchor is a LOCAL day. Live timestamps are UTC."""

    def test_the_real_message_resolves_to_its_local_day(self):
        """19:12Z on Aug 5 is 12:12 PDT on Aug 5 — the actual message."""
        self.assertEqual(msg_local_day({"timestamp": "2026-08-05T19:12:30.000Z"}),
                         date(2026, 8, 5))

    def test_an_evening_message_does_not_roll_into_tomorrow(self):
        """18:00 PDT is 01:00Z the NEXT day. Slicing ts[:10] would anchor
        "today" one day late and every relative term with it."""
        self.assertEqual(msg_local_day({"timestamp": "2026-08-06T01:00:00.000Z"}),
                         date(2026, 8, 5))

    def test_naive_backfill_timestamps_pass_through_as_local(self):
        self.assertEqual(msg_local_day({"timestamp": "2026-07-28T14:08:00"}),
                         date(2026, 7, 28))

    def test_garbage_returns_the_default_not_an_exception(self):
        for bad in (None, "", "nope", "2026-13-99T00:00:00"):
            self.assertIsNone(msg_local_day({"timestamp": bad}))
        self.assertEqual(msg_local_day({}, default=date(2026, 1, 1)), date(2026, 1, 1))

    def test_the_prompt_header_agrees_with_the_candidate_list(self):
        """Both must call the same day "today" — the gate compares against a
        date the model derived from the header."""
        msg = {"timestamp": "2026-08-06T01:00:00.000Z"}   # Aug 5 local
        header = date_header(msg, today=date(2026, 8, 20))
        self.assertIn("SENT ON 2026-08-05", header)


class AutoApplyGateTests(unittest.TestCase):
    """B: the answer gets checked, not just scored."""

    KNOWN = ["Itzel", "Darya"]

    def _result(self, **over):
        base = {"action": "confirm", "confidence": 0.95, "cleaning_date": "2026-08-05"}
        base.update(over)
        return base

    def test_the_original_failure_is_now_held(self):
        """0.90 confidence, known cleaner, real booking — and still wrong,
        because the day it named is not the day that booking is cleaned."""
        auto, block = decide(
            self._result(confidence=0.90, cleaning_date="2026-08-05"),
            TURNOVER[CHECKING_IN], "Itzel", "Itzel", self.KNOWN)
        self.assertFalse(auto)
        self.assertIn("2026-08-05", block)
        self.assertIn("2026-08-10", block)

    def test_agreement_still_auto_applies(self):
        auto, block = decide(self._result(), TURNOVER[CHECKING_OUT],
                             "Itzel", "Itzel", self.KNOWN)
        self.assertTrue(auto)
        self.assertIsNone(block)

    def test_a_missing_cleaning_date_fails_closed(self):
        """Old cached results and schema-ignoring replies are exactly the
        cases not to trust — absence must not read as agreement."""
        for missing in (None, "", "   "):
            auto, block = decide(self._result(cleaning_date=missing),
                                 TURNOVER[CHECKING_OUT], "Itzel", "Itzel", self.KNOWN)
            self.assertFalse(auto, f"cleaning_date={missing!r} must not auto-apply")
            self.assertIn("(unstated)", block)

    def test_declines_are_gated_the_same_way(self):
        auto, _ = decide(self._result(action="decline", cleaning_date="2026-08-10"),
                         TURNOVER[CHECKING_OUT], "Itzel", "Itzel", self.KNOWN)
        self.assertFalse(auto)
        auto, _ = decide(self._result(action="decline"),
                         TURNOVER[CHECKING_OUT], "Itzel", "Itzel", self.KNOWN)
        self.assertTrue(auto)

    def test_every_pre_existing_gate_still_bites(self):
        ok = (TURNOVER[CHECKING_OUT], "Itzel", "Itzel", self.KNOWN)
        self.assertFalse(decide(self._result(confidence=0.84), *ok)[0])
        self.assertFalse(decide(self._result(), TURNOVER[CHECKING_OUT],
                                None, "Itzel", self.KNOWN)[0])   # unmapped sender
        self.assertFalse(decide(self._result(), TURNOVER[CHECKING_OUT],
                                "Itzel", "Nobody", self.KNOWN)[0])  # unknown cleaner
        self.assertFalse(decide(self._result(), None,
                                "Itzel", "Itzel", self.KNOWN)[0])   # no such booking

    def test_non_actionable_messages_are_not_blocked_they_are_ignored(self):
        auto, block = decide({"action": "none"}, None, "Itzel", "Itzel", self.KNOWN)
        self.assertFalse(auto)
        self.assertIsNone(block, "chit-chat must not produce a review-tab warning")

    def test_an_unknown_booking_produces_no_date_complaint(self):
        """With no booking there is nothing to disagree with — the review card
        already says the uid was unknown."""
        auto, block = decide(self._result(), None, "Itzel", "Itzel", self.KNOWN)
        self.assertFalse(auto)
        self.assertIsNone(block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
