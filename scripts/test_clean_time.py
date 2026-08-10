#!/usr/bin/env python3
"""Tests for wiring a cleaner's STATED time through to the booking (1.36.0).

The defect these exist for: on 2026-08-08 Itzel wrote "see you on Monday at
11:00 am", the facts layer extracted `target_time: "11:00"` perfectly, and the
confirm path wrote `confirmed = True` and nothing else. The booking kept a
5:00pm agreed back in March and the shared Google Calendar showed 5:00 PM for a
cleaning that happened at 11:00.

`test_the_real_august_message` replays that exact message. It is the antecedent
criterion (ISA ISC-231) — the bug reproduced as a passing test.

Same harness as test_cleaning_match.py: pull the real function source out of
app.py with `ast` and exec it against injected fakes, so a rename or reshape
fails loudly here instead of passing against a stale copy.

Run: python3 scripts/test_clean_time.py
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"
sys.path.insert(0, str(APP_DIR))

import reconcile  # noqa: E402  (pure module — no flask, safe to import)


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


_recorded: list = []

APP = {
    "date": date, "datetime": datetime, "timedelta": timedelta, "re": re,
    # Mirrors the real constants; if app.py's values drift, the assertions
    # below about host-authored kinds are what catch it.
    "CLEANER_TIME_KINDS": ("confirm", "time_proposal"),
    "_HHMM_RE": re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$"),
    "_CHANGE_LABELS": {"cleaner": "cleaner", "clean_time": "time",
                       "confirmed": "confirmed", "end": "cleaning date"},
    # Recording stub — `_record_change` is exercised by its own suite.
    "_record_change": lambda *a, **k: _recorded.append(a),
}
_extract(["_stated_clean_time", "cleaning_date_for", "ack_notified",
          "_apply_booking_change", "_change_findings", "_recent_changes"], APP)


class _AppShim:
    """Attribute access over the exec-ed namespace, so tests read naturally."""
    def __getattr__(self, name):
        try:
            return APP[name]
        except KeyError as e:
            raise AttributeError(name) from e
    def __setattr__(self, name, value):
        APP[name] = value


app = _AppShim()


def fact(kind="confirm", date="2026-08-10", time="11:00", cleaner="Itzel",
         tentative=False):
    return {"kind": kind, "target_date": date, "target_time": time,
            "cleaner": cleaner, "tentative": tentative, "confidence": 0.95,
            "evidence": "see you on Monday at 11:00 am"}


class StatedCleanTimeTests(unittest.TestCase):
    """`_stated_clean_time` — the pure chooser."""

    def test_the_real_august_message(self):
        """ISC-231 antecedent: the exact fact record from the live archive."""
        real = [{
            "kind": "confirm", "cleaner": "Itzel", "target_date": "2026-08-10",
            "target_time": "11:00", "confidence": 0.95, "tentative": False,
            "evidence": "see you on Monday at 11:00 am",
        }]
        self.assertEqual(app._stated_clean_time(real, "2026-08-10"),
                         ("11:00:00", None))

    def test_silence_is_not_a_problem(self):
        """No time mentioned is the common case and must not be an error."""
        self.assertEqual(app._stated_clean_time([], "2026-08-10"), (None, None))
        self.assertEqual(app._stated_clean_time(None, "2026-08-10"), (None, None))
        self.assertEqual(
            app._stated_clean_time([fact(time=None)], "2026-08-10"), (None, None))

    def test_host_assertions_never_write(self):
        """ISC-206: `schedule_assertion` is host-authored — the host restating a
        plan is not the cleaner agreeing to it. 84 of 235 timed facts in the
        live archive are this kind, so it is the dominant wrong answer."""
        self.assertEqual(
            app._stated_clean_time([fact(kind="schedule_assertion")], "2026-08-10"),
            (None, None))

    def test_unclear_never_writes(self):
        """ISC-206: `unclear` carries a time 5x in the archive and by
        definition means the extractor could not tell what was meant."""
        self.assertEqual(app._stated_clean_time([fact(kind="unclear")], "2026-08-10"),
                         (None, None))

    def test_time_proposal_does_write(self):
        """A cleaner proposing a time is her naming one."""
        self.assertEqual(
            app._stated_clean_time([fact(kind="time_proposal", time="13:30")],
                                   "2026-08-10"),
            ("13:30:00", None))

    def test_tentative_never_writes(self):
        """ISC-207."""
        self.assertEqual(app._stated_clean_time([fact(tentative=True)], "2026-08-10"),
                         (None, None))

    def test_other_dates_are_ignored(self):
        """A time for Tuesday must not land on Monday's booking."""
        self.assertEqual(
            app._stated_clean_time([fact(date="2026-08-11")], "2026-08-10"),
            (None, None))

    def test_a_range_is_not_a_time(self):
        """ISC-208. Real archive case: "anytime after 11am and before 3pm"
        extracted as TWO facts, 11:00 and 15:00. Picking an end would invent an
        agreement, so this must refuse AND say why."""
        got, why = app._stated_clean_time(
            [fact(time="11:00"), fact(time="15:00")], "2026-08-10")
        self.assertIsNone(got)
        self.assertIn("2 different times", why)
        self.assertIn("11:00", why)
        self.assertIn("15:00", why)

    def test_duplicate_identical_times_are_not_a_range(self):
        """Two facts agreeing on 11:00 is agreement, not ambiguity."""
        self.assertEqual(
            app._stated_clean_time([fact(), fact()], "2026-08-10"),
            ("11:00:00", None))

    def test_garbage_time_is_refused_not_written(self):
        """ISC-209. 235/235 archive samples parse clean; that is a fact about
        the model's habit, not a guarantee about the next one."""
        for bad in ("11am", "25:00", "11:60", "", "1100", "11:0"):
            got, why = app._stated_clean_time([fact(time=bad)], "2026-08-10")
            self.assertIsNone(got, f"{bad!r} should be refused")
            if bad:
                self.assertIn("unparseable", why)

    def test_midnight_and_end_of_day_are_valid(self):
        self.assertEqual(app._stated_clean_time([fact(time="00:00")], "2026-08-10"),
                         ("00:00:00", None))
        self.assertEqual(app._stated_clean_time([fact(time="23:59")], "2026-08-10"),
                         ("23:59:00", None))

    def test_malformed_fact_entries_do_not_raise(self):
        """Facts come from a model; a non-dict entry must degrade, not crash."""
        self.assertEqual(
            app._stated_clean_time(["nonsense", None, fact()], "2026-08-10"),
            ("11:00:00", None))


class ApplyBookingChangeTests(unittest.TestCase):
    """The write path — including what must NOT happen."""

    def setUp(self):
        self.data = {"bookings": {"u1": {
            "start": "2026-08-05", "end": "2026-08-10", "cleaner": "Itzel",
            "clean_time": "17:00:00", "confirmed": False, "status": "active",
            "type": "airbnb",
        }}}
        self.msg = {"id": "m1", "timestamp": "2026-08-08T04:35:34.000Z"}

    def _apply(self, facts):
        app._apply_booking_change(self.data, "u1", "Itzel", "confirm",
                                  self.msg, facts_list=facts)
        return self.data["bookings"]["u1"]

    def test_revision_replaces_the_standing_agreement(self):
        """ISC-204, Josh's ruling: 17:00 agreed in March yields to 11:00."""
        b = self._apply([fact()])
        self.assertEqual(b["clean_time"], "11:00:00")
        self.assertTrue(b["confirmed"])

    def test_commitment_records_the_new_time_not_the_old(self):
        """ISC-210. This is the specific bug: `ack_notified` stamps CURRENT
        truth, so writing the time after it would record 'Itzel agreed to
        17:00' on the very message where she said 11:00."""
        b = self._apply([fact()])
        self.assertEqual(b["cleaner_commitment"]["clean_time"], "11:00:00")
        self.assertEqual(b["cleaner_commitment"]["communicated_via"], "whatsapp")

    def test_no_stated_time_leaves_the_booking_time_alone(self):
        """ISC-205: silence must not blank a good value."""
        b = self._apply([])
        self.assertEqual(b["clean_time"], "17:00:00")
        self.assertTrue(b["confirmed"])
        self.assertEqual(b["cleaner_commitment"]["clean_time"], "17:00:00")

    def test_unusable_time_does_not_ratify_the_old_one(self):
        """ISC-211. She said something about the time we cannot act on. The
        old value must NOT be stamped as agreed — that would silence the
        notify queue on precisely the booking that just became doubtful."""
        b = self._apply([fact(time="11:00"), fact(time="15:00")])
        self.assertEqual(b["clean_time"], "17:00:00")   # unchanged
        self.assertNotIn("cleaner_commitment", b)       # NOT ratified
        self.assertIn("2 different times", b["time_note"])

    def test_time_note_clears_once_resolved(self):
        self._apply([fact(time="11:00"), fact(time="15:00")])
        self.assertIn("time_note", self.data["bookings"]["u1"])
        b = self._apply([fact(time="11:00")])
        self.assertNotIn("time_note", b)
        self.assertEqual(b["clean_time"], "11:00:00")

    def test_booking_with_no_time_gets_one(self):
        self.data["bookings"]["u1"]["clean_time"] = None
        b = self._apply([fact(time="12:30")])
        self.assertEqual(b["clean_time"], "12:30:00")

    def test_decline_path_is_untouched(self):
        """Anti-regression: the decline branch must not gain time behaviour."""
        app._apply_booking_change(self.data, "u1", "Itzel", "decline",
                                  self.msg, facts_list=[fact()])
        b = self.data["bookings"]["u1"]
        self.assertIsNone(b["cleaner"])
        self.assertFalse(b["confirmed"])
        self.assertEqual(b["clean_time"], "17:00:00")

    def test_omitting_facts_entirely_still_works(self):
        """Callers that pass no facts must behave exactly as before."""
        app._apply_booking_change(self.data, "u1", "Itzel", "confirm", self.msg)
        self.assertEqual(self.data["bookings"]["u1"]["clean_time"], "17:00:00")
        self.assertTrue(self.data["bookings"]["u1"]["confirmed"])


class TimeAgreementDetectorTests(unittest.TestCase):
    """`reconcile._time_agreement` — the probe that was entirely absent."""

    def setUp(self):
        self.bookings = {"u1": {
            "start": "2026-08-05", "end": "2026-08-10", "cleaner": "Itzel",
            "clean_time": "17:00:00", "status": "active", "type": "airbnb",
        }}
        self.facts = {"m1": {"extracted_at": "2026-08-08T04:35:40",
                             "facts": [fact()]}}

    def run_det(self, today="2026-08-09", horizon="2026-08-30"):
        return reconcile._time_agreement(self.bookings, self.facts, today, horizon)

    def test_mismatch_is_needs_attention(self):
        """ISC-213: the finding that would have caught Aug 10 on Aug 8."""
        out = self.run_det()
        self.assertEqual(len(out), 1)
        f = out[0]
        self.assertEqual(f["kind"], "time_mismatch")
        self.assertEqual(f["severity"], "needs-attention")
        self.assertEqual(f["date"], "2026-08-10")
        self.assertIn("11:00", f["why"])
        self.assertIn("17:00", f["why"])

    def test_agreement_is_silent(self):
        self.bookings["u1"]["clean_time"] = "11:00:00"
        self.assertEqual(self.run_det(), [])

    def test_unagreed_time_is_flagged_because_gcal_invents_one(self):
        """ISC-214: gcal.py substitutes 11:00:00, so absence renders on the
        shared calendar as an agreed-looking hour."""
        self.bookings["u1"]["clean_time"] = None
        self.facts = {}
        out = self.run_det()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "time_unagreed")
        self.assertEqual(out[0]["severity"], "suggest")
        self.assertIn("11:00 AM", out[0]["why"])

    def test_unagreed_beyond_the_horizon_is_quiet(self):
        """ISC-215: a booking nine months out is true and unimportant today."""
        self.bookings["u1"]["clean_time"] = None
        self.bookings["u1"]["end"] = "2027-05-16"
        self.facts = {}
        self.assertEqual(self.run_det(), [])

    def test_host_assertion_does_not_manufacture_a_mismatch(self):
        """ISC-206 on the read side: the host restating 11:00 while the
        booking says 17:00 is not the cleaner disagreeing."""
        self.facts = {"m1": {"extracted_at": "2026-08-08T04:35:40",
                             "facts": [fact(kind="schedule_assertion")]}}
        self.assertEqual(self.run_det(), [])

    def test_latest_statement_wins(self):
        self.facts = {
            "old": {"extracted_at": "2026-03-30T08:31:00",
                    "facts": [fact(time="17:00")]},
            "new": {"extracted_at": "2026-08-08T04:35:40",
                    "facts": [fact(time="11:00")]},
        }
        out = self.run_det()
        self.assertEqual(len(out), 1)
        self.assertIn("said 11:00", out[0]["why"])

    def test_past_and_cancelled_are_ignored(self):
        self.bookings["u1"]["status"] = "cancelled"
        self.assertEqual(self.run_det(), [])
        self.bookings["u1"]["status"] = "active"
        self.assertEqual(self.run_det(today="2026-09-01",
                                      horizon="2026-09-22"), [])

    def test_unassigned_booking_is_not_a_time_problem(self):
        """Unassigned is the drift detector's job, not this one's."""
        self.bookings["u1"]["cleaner"] = None
        self.bookings["u1"]["clean_time"] = None
        self.assertEqual(self.run_det(), [])

    def test_ids_are_stable_across_runs(self):
        """ISC-216: alarm once, dismissible."""
        self.assertEqual([f["id"] for f in self.run_det()],
                         [f["id"] for f in self.run_det()])

    def test_why_carries_no_raw_message_text(self):
        """ISC-218: the evidence quote must never ride into `why`, which
        crosses to the VPS."""
        for f in self.run_det():
            self.assertNotIn("see you on Monday", f["why"])
            self.assertNotIn("quote", f)

    def test_detector_is_pure(self):
        """ISC-217: identical inputs, identical output, no clock read."""
        a, b = self.run_det(), self.run_det()
        self.assertEqual(a, b)


class ChangeFindingCleanerTests(unittest.TestCase):
    """ISC-220: the hardcoded null that became 'no cleaner assigned'."""

    def test_finding_carries_the_real_cleaner(self):
        entry = {"at": "2026-08-08T04:35:42", "booking_uid": "u1",
                 "cleaning_date": "2026-08-10", "action": "confirm",
                 "source": "whatsapp-auto", "message_id": "m1",
                 "changed": {"confirmed": {"from": False, "to": True}}}
        orig = app._recent_changes
        app._recent_changes = lambda hours=24, now=None: [entry]
        try:
            out = app._change_findings(
                "2026-08-09", bookings={"u1": {"cleaner": "Itzel"}})
        finally:
            app._recent_changes = orig
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["cleaner"], "Itzel")
        self.assertEqual(out[0]["severity"], "informational")


if __name__ == "__main__":
    unittest.main(verbosity=2)
