"""Unit tests for booking routing — the rule that decides which cleaning a
message touches, and whether it may write unattended.

## The history this file carries

**2026-08-05.** Itzel messaged "Hello guys I'm here" at 12:12 local, walking
into the cleaning for the stay that CHECKED OUT that day. It was auto-applied,
at 0.90 confidence, to the stay that CHECKED IN that day — marking the Aug 10
cleaning confirmed on the strength of a message about Aug 5. An audit found
**16 of 48** auto-applied confirmations had done the same thing, because 53% of
cleanings here fall on a day that is also the next guest's check-in.

Two causes were found, and only the second one has since been closed properly:

  A. The candidate list handed the model RESERVATIONS (two dates each, labelled
     "Aug 05 → Aug 10"), so on a turnover day two rows displayed the same date.
     The 1.35.0 fix reshaped it to one date per row.
  B. Nothing checked the answer. `confidence` is the model's certainty about
     its reading of the sentence, not about the row it picked, so a confident
     wrong answer had nothing to fail against.

**2026-08-20.** Cause A came back in a different costume. Itzel wrote "Yes Sept
10 I can do it at 11:00"; the model returned the right action, the right date,
the right cleaner and 0.90 confidence — and the booking uid **without its
`@airbnb.com` suffix**. The lookup returned None, the write was refused, and
because the branch that explains a refusal also needed the booking it was
explaining about, the hold was recorded with no reason at all. 79 of 81 uids in
the archive resolved; one was truncated, one was invented.

The fix is not a better gate on the uid. It is that **the model is no longer
asked for a uid, and is no longer shown the booking list at all.** It reports
what was said; `_route_from_facts` resolves the stated date against the data.
So cause A is now structurally impossible rather than guarded, and cause B is
answered by a resolution that either finds exactly one booking or refuses.

That is why `upcoming_booking_list` and `_auto_apply_decision` no longer exist,
and why this file tests `_route_from_facts` instead. Do not reintroduce a
model-supplied identifier.

Same harness as before: the real function source is pulled out of app.py with
`ast` and exec'd against injected fakes, so a rename or reshape fails loudly
here instead of passing against a stale copy.

Run: python3 scripts/test_cleaning_match.py
"""
from __future__ import annotations

import ast
import sys
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
    "ROUTE_CONFIDENCE": 0.85,
}
_extract(["cleaning_date_for", "_routable_bookings_by_date", "_route_from_facts",
          "_synthesize_result"], NS)
route = NS["_route_from_facts"]
synth = NS["_synthesize_result"]

KNOWN = ["Itzel", "Darya"]
TODAY = "2026-08-20"

# The real shape of the two bookings that collided on 2026-08-05.
CHECKING_OUT = "1418fb94e984-out@airbnb.com"   # Aug 03 → Aug 05, cleaned Aug 5
CHECKING_IN = "1418fb94e984-in@airbnb.com"     # Aug 05 → Aug 10, cleaned Aug 10

TURNOVER = {
    CHECKING_OUT: {"start": "2026-08-03", "end": "2026-08-05", "status": "active",
                   "type": "airbnb", "cleaner": "Itzel"},
    CHECKING_IN: {"start": "2026-08-05", "end": "2026-08-10", "status": "active",
                  "type": "airbnb", "cleaner": "Itzel"},
}

SEPT = {
    "1418fb94e984-sept10@airbnb.com": {"start": "2026-09-08", "end": "2026-09-10",
                                       "status": "active", "type": "airbnb", "cleaner": None},
    "1418fb94e984-sept14@airbnb.com": {"start": "2026-09-10", "end": "2026-09-14",
                                       "status": "active", "type": "airbnb", "cleaner": "Itzel"},
}


def fact(kind="confirm", d="2026-09-10", cleaner="Itzel", conf=0.95,
         tentative=False, time=None, ev="Sept 10- 11:00"):
    return {"kind": kind, "target_date": d, "cleaner": cleaner, "confidence": conf,
            "tentative": tentative, "target_time": time, "evidence": ev}


class TurnoverDay(unittest.TestCase):
    """Cause A, the 2026-08-05 wrong-row bug, at its source."""

    def test_a_turnover_day_resolves_to_the_checkout_not_the_checkin(self):
        """Aug 5 is both a checkout and a check-in. Only the checkout is a
        cleaning, so only one booking can ever match — the collision the model
        used to resolve by guessing no longer reaches it."""
        d, blocks = route([fact(d="2026-08-05")], TURNOVER, "Itzel", KNOWN, "2026-08-01")
        self.assertEqual([x["uid"] for x in d], [CHECKING_OUT])
        self.assertEqual(blocks, [])

    def test_the_checkin_stay_is_reachable_only_by_its_own_cleaning_date(self):
        d, _ = route([fact(d="2026-08-10")], TURNOVER, "Itzel", KNOWN, "2026-08-01")
        self.assertEqual([x["uid"] for x in d], [CHECKING_IN])


class TheSeptTenCase(unittest.TestCase):
    """The 2026-08-20 regression, end to end."""

    def test_the_message_that_started_this_now_routes(self):
        facts = [fact(kind="unclear", d="2026-09-27", conf=0.4),
                 fact(kind="confirm", d="2026-09-10", conf=0.9,
                      ev="Yes Sept 10 I can do it at 11:00")]
        d, blocks = route(facts, SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["uid"], "1418fb94e984-sept10@airbnb.com")
        self.assertEqual(d[0]["action"], "confirm")

    def test_no_identifier_is_ever_read_from_the_model(self):
        """The regression guard. If a uid ever reappears in the fact schema it
        must not be trusted — routing is by date, resolved against the data."""
        f = fact()
        f["booking_uid"] = "totally-made-up-uid"
        d, _ = route([f], SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d[0]["uid"], "1418fb94e984-sept10@airbnb.com")


class RoutingRules(unittest.TestCase):
    def test_a_reposted_list_decides_many_bookings_in_one_message(self):
        """The dominant real-chat pattern. The old one-booking-per-message
        contract could express only the first of these."""
        d, _ = route([fact(d="2026-09-10"), fact(d="2026-09-14")],
                     SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(len(d), 2)

    def test_past_dates_are_ignored(self):
        d, blocks = route([fact(d="2026-08-05")], TURNOVER, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertEqual(blocks, [])

    def test_an_unmapped_sender_never_writes(self):
        d, blocks = route([fact()], SEPT, None, KNOWN, TODAY)
        self.assertEqual(d, [])

    def test_the_host_never_writes(self):
        """Host schedule assertions are the reconciler's business. He is
        stating a plan, not accepting one."""
        d, _ = route([{"kind": "schedule_assertion", "target_date": "2026-09-10",
                       "cleaner": "Itzel", "confidence": 0.95}],
                     SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])

    def test_a_cleaner_cannot_confirm_on_someone_elses_behalf(self):
        """"Itzel told me she's taking the 17th" is testimony. The facts prompt
        attributes it to the SUBJECT, so without this check Darya could confirm
        a booking for Itzel, who never spoke."""
        d, blocks = route([fact(cleaner="Itzel")], SEPT, "Darya", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertIn("was sent by Darya", blocks[0])

    def test_tentative_is_held(self):
        d, blocks = route([fact(tentative=True)], SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertIn("tentative", blocks[0])

    def test_low_confidence_is_held(self):
        d, blocks = route([fact(conf=0.5)], SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])

    def test_a_date_with_no_booking_is_held_not_invented(self):
        d, blocks = route([fact(d="2026-12-25")], SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertIn("no active cleaning on 2026-12-25", blocks)

    def test_an_ambiguous_date_is_a_question_for_a_human(self):
        """Exactly one date in the two-year archive carries two bookings. When
        it happens the old design let the model pick one silently."""
        ambiguous = dict(SEPT)
        ambiguous["manual-extra"] = {"start": "2026-09-10", "end": "2026-09-10",
                                     "status": "active", "type": "manual_cleaning",
                                     "cleaner": None}
        d, blocks = route([fact(d="2026-09-10")], ambiguous, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertIn("2 cleanings share 2026-09-10", blocks[0])

    def test_cancelled_and_custom_stays_are_not_routable(self):
        bookings = {"c": {"end": "2026-09-10", "status": "cancelled", "type": "airbnb"},
                    "s": {"end": "2026-09-10", "status": "active", "type": "custom_stay"}}
        d, blocks = route([fact()], bookings, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertIn("no active cleaning", blocks[0])

    def test_a_message_that_both_confirms_and_declines_one_date_writes_nothing(self):
        d, blocks = route([fact(kind="confirm"), fact(kind="decline")],
                          SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d, [])
        self.assertIn("both confirms and declines", blocks[-1])

    def test_a_decline_routes(self):
        d, _ = route([fact(kind="decline", d="2026-09-14")], SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual(d[0]["action"], "decline")

    def test_chitchat_yields_nothing_and_blocks_nothing(self):
        """"Okay! Thank you!" must produce neither a write nor a review item.
        The old contract had no honest bucket for it and rounded it to confirm."""
        d, blocks = route([], SEPT, "Itzel", KNOWN, TODAY)
        self.assertEqual((d, blocks), ([], []))


class SynthesizedResult(unittest.TestCase):
    """The Review tab, /review/accept and _review_subject_date all read this."""

    def test_uid_always_resolves_because_code_chose_it(self):
        d, _ = route([fact()], SEPT, "Itzel", KNOWN, TODAY)
        res = synth(d, [], "Itzel")
        self.assertIn(res["booking_uid"], SEPT)
        self.assertEqual(res["cleaning_date"], "2026-09-10")
        self.assertEqual(res["source"], "facts")

    def test_no_decision_reports_the_reason_rather_than_nothing(self):
        d, blocks = route([fact(d="2026-12-25")], SEPT, "Itzel", KNOWN, TODAY)
        res = synth(d, blocks, "Itzel")
        self.assertEqual(res["action"], "none")
        self.assertIn("2026-12-25", res["reason"])

    def test_a_multi_date_message_says_so(self):
        d, _ = route([fact(d="2026-09-10"), fact(d="2026-09-14")],
                     SEPT, "Itzel", KNOWN, TODAY)
        self.assertIn("+1 more", synth(d, [], "Itzel")["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
