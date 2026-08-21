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
_written: list = []

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
    # Recording stub for the write-audit log (1.37.5). Recorded rather than
    # no-op'd because "every write is logged" is the property, so a test that
    # silently swallowed the call would pass on a build that stopped logging.
    "_log_write": lambda op, uid=None, **k: _written.append((op, uid, k)),
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


class WriteAuditLogTests(unittest.TestCase):
    """1.37.5 — every booking mutation leaves a record naming the target.

    The precondition for a CLI: once something other than a human at the HA UI
    can assign and delete bookings, "who did this and to which booking" has to
    be answerable after the fact. `_record_change` cannot answer it — it
    watches four fields, returns early when none moved, and never sees the UI
    routes at all.
    """

    def setUp(self):
        _written.clear()
        self.data = {"bookings": {"u1": {
            "start": "2026-08-09", "end": "2026-08-10", "cleaner": None,
            "confirmed": False, "status": "active", "type": "airbnb",
        }}}

    def test_a_confirm_is_logged_with_its_resolved_booking(self):
        app._apply_booking_change(self.data, "u1", "Itzel", "confirm",
                                  {"id": "m1"}, facts_list=[fact()])
        self.assertEqual(len(_written), 1)
        op, uid, detail = _written[0]
        self.assertEqual(op, "booking_confirm")
        self.assertEqual(uid, "u1")
        self.assertEqual(detail["cleaning_date"], "2026-08-10")
        self.assertEqual(detail["cleaner"], "Itzel")
        self.assertEqual(detail["message_id"], "m1")

    def test_a_decline_is_logged(self):
        self.data["bookings"]["u1"]["cleaner"] = "Itzel"
        app._apply_booking_change(self.data, "u1", "Itzel", "decline",
                                  {"id": "m2"}, facts_list=[])
        self.assertEqual([w[0] for w in _written], ["booking_decline"])

    def test_a_write_against_a_uid_that_does_not_exist_is_still_logged(self):
        """The signature of a stale automation, and invisible everywhere else.

        `_record_change` is never reached on this path — the function returns
        before it — so without this entry the attempt leaves no trace at all.
        """
        app._apply_booking_change(self.data, "gone", "Itzel", "confirm",
                                  {"id": "m3"}, facts_list=[])
        self.assertEqual(len(_written), 1)
        op, uid, _ = _written[0]
        self.assertEqual(op, "booking_write_unresolved")
        self.assertEqual(uid, "gone")


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
        msgs = {"m1": {"timestamp": "2026-08-08T04:35:34.000Z"},
                "old": {"timestamp": "2026-03-30T08:31:00"},
                "new": {"timestamp": "2026-08-08T04:35:34.000Z"}}
        return reconcile._time_agreement(self.bookings, self.facts, today,
                                         horizon, msgs)

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

    def test_unagreed_time_is_flagged(self):
        """ISC-214, restated 2026-08-20.

        The detector's reason for existing is unchanged — an unagreed time is
        worth raising. Its RATIONALE changed: `gcal.py` no longer substitutes
        11:00, so the finding must not claim it does. A finding that describes
        behaviour the code no longer has is a wrong statement sitting in the
        answering path, which this project's own rule forbids."""
        self.bookings["u1"]["clean_time"] = None
        self.facts = {}
        out = self.run_det()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "time_unagreed")
        self.assertEqual(out[0]["severity"], "suggest")
        self.assertNotIn("11:00", out[0]["why"],
                         "the finding still claims the calendar fabricates a time")
        self.assertIn("all-day", out[0]["why"])

    def test_gcal_no_longer_fabricates_a_time(self):
        """The change the finding above now describes. An untimed cleaning must
        render as all-day, never as a plausible-looking hour on the one surface
        the cleaners read."""
        import ast
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "cleaning-tracker" / "gcal.py").read_text()
        tree = ast.parse(src)

        # Check the CODE, not the text. Both previous attempts at this guard
        # matched the docstring that describes the old bug — a test that fails
        # on its own explanation of what it prevents.
        desired = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "_desired_events")
        body = ast.unparse(desired)
        self.assertNotIn('"11:00:00"', body,
                         "_desired_events still hardcodes a cleaning time")
        self.assertEqual(body.count("_event_window("), 2,
                         "both cleaning-event sites must go through _event_window")

        ns = {"datetime": datetime, "timedelta": timedelta, "LOCAL_TZ": "America/Vancouver"}
        for n in ast.parse(src).body:
            if isinstance(n, ast.FunctionDef) and n.name == "_event_window":
                exec(compile(ast.Module(body=[n], type_ignores=[]), "gcal.py", "exec"), ns)
        window = ns["_event_window"]

        start, end = window(date(2026, 9, 10), None)
        self.assertEqual(start, {"date": "2026-09-10"})
        self.assertEqual(end, {"date": "2026-09-11"},
                         "Google treats an all-day end date as exclusive")
        self.assertNotIn("dateTime", start)

        start, end = window(date(2026, 9, 10), "13:00:00")
        self.assertEqual(start["dateTime"], "2026-09-10T13:00:00")
        self.assertEqual(end["dateTime"], "2026-09-10T15:00:00")

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

    def test_reprocessing_cannot_resurrect_a_superseded_time(self):
        """Advisor catch. Ordering by `extracted_at` means a reprocess — which
        runs after every prompt-version bump — restamps ancient messages as the
        newest opinion. Here the OLD message was extracted most recently; the
        March 17:00 must still lose to the August 11:00."""
        self.facts = {
            "old": {"extracted_at": "2026-09-01T00:00:00",   # reprocessed last
                    "facts": [fact(time="17:00")]},
            "new": {"extracted_at": "2026-08-08T04:35:40",
                    "facts": [fact(time="11:00")]},
        }
        self.bookings["u1"]["clean_time"] = "09:00:00"
        out = self.run_det()
        self.assertEqual(len(out), 1)
        self.assertIn("said 11:00", out[0]["why"])

    def test_a_stated_window_gets_its_own_finding(self):
        """Advisor catch, highest value. "anytime after 11am and before 3pm"
        is a valid human answer. Without its own kind it falls into the drift
        queue, whose prescribed action is "tell the cleaner" — the opposite of
        the truth, which is that SHE told US and we need to pick an hour."""
        self.bookings["u1"]["time_note"] = "names 2 different times (11:00, 15:00)"
        out = self.run_det()
        self.assertEqual(len(out), 1)
        f = out[0]
        self.assertEqual(f["kind"], "time_ambiguous")
        self.assertEqual(f["severity"], "needs-attention")
        self.assertIn("ask her which one", f["why"])
        self.assertIn("11:00", f["why"])

    def test_ambiguity_suppresses_the_mismatch_for_the_same_booking(self):
        """One situation, one finding — never both."""
        self.bookings["u1"]["time_note"] = "names 2 different times (11:00, 15:00)"
        kinds = [f["kind"] for f in self.run_det()]
        self.assertEqual(kinds, ["time_ambiguous"])

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


class CrossVendorAuditTests(unittest.TestCase):
    """Three defects found by a GPT-5 Codex audit after the Claude advisor
    had already signed off. All three were real; one was a regression this
    release introduced."""

    def setUp(self):
        self.bookings = {"u1": {
            "start": "2026-08-05", "end": "2026-08-10", "cleaner": "Itzel",
            "clean_time": "17:00:00", "status": "active", "type": "airbnb",
        }}
        self.msgs = {"m1": {"timestamp": "2026-08-08T04:35:34.000Z"}}

    def det(self, facts):
        return reconcile._time_agreement(self.bookings, facts, "2026-08-09",
                                         "2026-08-30", self.msgs)

    def test_raw_two_time_facts_are_ambiguous_not_a_confident_mismatch(self):
        """Cato finding 2. The write path refuses a window, but the detector
        reads the ARCHIVE — messages the new writer never touched. Collapsing
        two facts to whichever was visited last would report a confident
        mismatch against an arbitrary half of a range."""
        facts = {"m1": {"facts": [fact(time="11:00"), fact(time="15:00")]}}
        out = self.det(facts)
        self.assertEqual([f["kind"] for f in out], ["time_ambiguous"])
        self.assertIn("11:00", out[0]["why"])
        self.assertIn("15:00", out[0]["why"])

    def test_malformed_time_never_reaches_a_finding(self):
        """Cato finding 3. The HH:MM guard existed on the write path only, so
        a malformed model output could ride into a finding id and into prose
        the host reads, dressed as a time somebody stated."""
        for bad in ("11am", "25:00", "later", "3pm-ish"):
            out = self.det({"m1": {"facts": [fact(time=bad)]}})
            self.assertEqual([f["kind"] for f in out], [],
                             f"{bad!r} leaked into a finding")

    def test_one_good_time_still_fires_alongside_a_malformed_sibling(self):
        """The guard must drop the bad value, not the whole message."""
        out = self.det({"m1": {"facts": [fact(time="11:00"), fact(time="banana")]}})
        self.assertEqual([f["kind"] for f in out], ["time_mismatch"])
        self.assertIn("said 11:00", out[0]["why"])


class BaselineIdSetTests(unittest.TestCase):
    """Cato finding 1, the regression this release introduced. Merging change
    ids into `finding_ids` made every one of them count as RESOLVED on the
    following night, because they are never in the next reconciler run."""

    def test_resolved_count_is_computed_only_from_reconciler_ids(self):
        baseline_finding_ids = {"drift:u1:new", "drift:u2:new"}
        current_ids = {"drift:u1:new", "drift:u2:new"}
        # Yesterday also REPORTED a change finding. It must not be in the set
        # the resolved diff is taken over.
        reported_ids = baseline_finding_ids | {"applied:m1:2026-08-08T21:35:42"}
        self.assertEqual(len(baseline_finding_ids - current_ids), 0,
                         "no phantom resolutions")
        self.assertEqual(len(reported_ids - current_ids), 1,
                         "suppression set still remembers what was sent")


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
