"""Findings about one cleaning must resolve to one statement, ranked by what
the reader has to do.

On 2026-08-20 the digest told Josh "Sept 10 — booking needs a cleaner" while
holding, two severity tiers lower in the same payload, "Itzel said decline then
confirm for 2026-09-10; latest is confirm". Five findings, four detectors, no
join. Severity was a string literal fixed at each detector's emit site — a
property of the function that spoke, never of the finding — so `_drift`, whose
whole input is one booking dict and whose evidence is a hardcoded `[]`, was
pinned to the top permanently. The nightly repeat filter then keyed on that
same severity, so the only line that survived past night one was the one
guaranteed to carry nothing.

Run: python3 scripts/test_finding_resolution.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import reconcile  # noqa: E402

UID = "b-sept10@airbnb.com"
BOOKINGS = {UID: {"end": "2026-09-10", "status": "active", "type": "airbnb", "cleaner": None},
            "b-other@airbnb.com": {"end": "2026-10-01", "status": "active",
                                   "type": "airbnb", "cleaner": None}}


def f(fid, kind, severity, uid=UID, cleaner=None, why="w", evidence=None, quote=None,
      d="2026-09-10", detector="facts_vs_bookings"):
    out = {"id": fid, "detector": detector, "kind": kind, "severity": severity,
           "booking_uid": uid, "cleaner": cleaner, "date": d, "why": why,
           "evidence": evidence or []}
    if quote:
        out["quote"] = quote
    return out


# The five real findings from the live 2026-08-20 reconcile.
SEPT10 = [
    f("drift:u:unassigned", "drift_unassigned", "needs-attention",
      why="booking needs a cleaner", detector="drift"),
    f("unrec:u:Itzel", "unrecorded_confirmation", "suggest", cleaner="Itzel",
      why="Itzel confirmed for 2026-09-10 but booking is unassigned",
      evidence=["m1"], quote="Sept 10- 11:00"),
    f("sched:u:Darya", "schedule_unassigned", "suggest", cleaner="Darya",
      why="host scheduled Darya for 2026-09-10 but booking is unassigned", evidence=["m2"],
      detector="schedule_vs_bookings"),
    f("changed:Itzel:2026-09-10", "changed_mind", "informational", uid=None, cleaner="Itzel",
      detector="fact_timeline",
      why="Itzel said decline then confirm for 2026-09-10; latest is confirm",
      evidence=["m3"], quote="Yes Sept 10 I can do it at 11:00"),
]


class Resolution(unittest.TestCase):
    def setUp(self):
        self.out = reconcile.resolve_subjects(SEPT10, BOOKINGS)
        self.sept = [x for x in self.out if x.get("booking_uid") == UID]

    def test_five_findings_about_one_cleaning_become_one(self):
        self.assertEqual(len(self.sept), 1)

    def test_a_dateless_finding_is_joined_by_its_date(self):
        """`changed_mind` carries booking_uid None. It is the finding that held
        the answer, and never being joined to a booking is precisely why the
        answer never met the question."""
        self.assertIn("changed:Itzel:2026-09-10", [x["id"] for x in self.sept] +
                      (self.sept[0].get("absorbed") or []))

    def test_the_merged_finding_carries_the_booking(self):
        """No uid means no one-tap action, which is information arriving unusable."""
        self.assertEqual(self.sept[0]["booking_uid"], UID)

    def test_severity_is_the_max_of_the_group_not_the_primarys(self):
        """The repeat filter keys on needs-attention. A merged finding that
        inherited `informational` would be reported once and never again."""
        self.assertEqual(self.sept[0]["severity"], "needs-attention")

    def test_the_answer_leads(self):
        self.assertTrue(self.sept[0]["why"].startswith("Itzel said decline then confirm"))

    def test_the_quote_survives_the_merge(self):
        self.assertEqual(self.sept[0]["quote"], "Yes Sept 10 I can do it at 11:00")

    def test_no_evidence_is_lost(self):
        self.assertEqual(set(self.sept[0]["evidence"]), {"m1", "m2", "m3"})

    def test_absorbed_ids_are_recorded(self):
        self.assertEqual(len(self.sept[0]["absorbed"]), 3)

    def test_the_why_does_not_become_a_run_on(self):
        """Read on a phone. Concatenating every clause fails the same way five
        separate lines did."""
        self.assertLessEqual(self.sept[0]["why"].count(" · "), 3)

    def test_the_reader_is_told_to_approve_not_to_investigate(self):
        self.assertEqual(self.sept[0]["decision"], "approve")

    def test_health_findings_are_never_merged_into_a_cleaning(self):
        health = {"id": "stale_push", "detector": "gcal_push_health", "kind": "stale_push",
                  "severity": "needs-attention", "booking_uid": None, "cleaner": None,
                  "date": "2026-09-10", "why": "no push in 26h", "evidence": []}
        out = reconcile.resolve_subjects(SEPT10 + [health], BOOKINGS)
        self.assertIn("stale_push", [x["id"] for x in out],
                      "a health finding was absorbed into a cleaning")

    def test_calendar_projection_findings_are_not_merged_away(self):
        """A stale calendar event has a different repair from an unassigned
        booking. Folding it in would hide a broken calendar behind a solved one."""
        gcal = {"id": "gcal_stale:u", "detector": "bookings_vs_gcal",
                "kind": "gcal_stale_event", "severity": "suggest", "booking_uid": UID,
                "cleaner": "Itzel", "date": "2026-09-10", "why": "event is stale",
                "evidence": []}
        out = reconcile.resolve_subjects(SEPT10 + [gcal], BOOKINGS)
        self.assertIn("gcal_stale_event", {x["kind"] for x in out})

    def test_an_ambiguous_date_does_not_join_by_date(self):
        """Two bookings share the date, so a dateless finding cannot be
        attributed to either — guessing is what the old design did."""
        two = dict(BOOKINGS)
        two["b-dup@airbnb.com"] = {"end": "2026-09-10", "status": "active",
                                   "type": "airbnb", "cleaner": None}
        out = reconcile.resolve_subjects(SEPT10, two)
        self.assertIn("changed:Itzel:2026-09-10", [x["id"] for x in out])


class Ranking(unittest.TestCase):
    def test_the_informed_finding_outranks_the_ignorant_one(self):
        """The whole defect, as one assertion."""
        ranked = reconcile.filter_and_sort(
            {"findings_raw": reconcile.resolve_subjects(SEPT10, BOOKINGS)}, {})["findings"]
        first = ranked[0]
        self.assertEqual(first.get("decision"), "approve")
        self.assertNotEqual(first["kind"], "drift_unassigned",
                            "an evidence-free finding ranked above one holding the answer")

    def test_a_dismissal_survives_the_merge(self):
        """Ids a human dismissed before resolution existed may now be absorbed.
        Keying only on the primary would make them all inert at once."""
        merged = reconcile.resolve_subjects(SEPT10, BOOKINGS)
        primary = next(f for f in merged if f.get("booking_uid") == UID)
        all_ids = {primary["id"], *primary["absorbed"]}
        out = reconcile.filter_and_sort({"findings_raw": merged}, {i: {} for i in all_ids})
        self.assertNotIn(primary["id"], [f["id"] for f in out["findings"]])

    def test_a_partial_dismissal_does_not_silence_the_group(self):
        merged = reconcile.resolve_subjects(SEPT10, BOOKINGS)
        primary = next(f for f in merged if f.get("booking_uid") == UID)
        out = reconcile.filter_and_sort({"findings_raw": merged},
                                        {primary["absorbed"][0]: {}})
        self.assertIn(primary["id"], [f["id"] for f in out["findings"]])

    def test_unknown_kinds_fail_toward_asking(self):
        self.assertEqual(reconcile._decision_of("something_new_nobody_mapped"), "investigate")




class SupersededStatements(unittest.TestCase):
    """A cleaner's LATEST word wins. Found on the 1.37.1 deploy.

    Itzel declined Sept 10 on May 5, then confirmed it on Aug 15 and Aug 20.
    The moment the booking was assigned to her, `_facts_vs_bookings` raised
    `decline_still_assigned` at needs-attention from the May fact — while
    `_fact_timeline`, one detector over, correctly reported "latest is
    confirm". The reconciler contradicting itself is the failure it exists to
    catch. `dismissed_findings` carries a hand-written note about this exact
    shape from 2026-08-03.
    """

    BOOKINGS = {UID: {"end": "2026-09-10", "status": "active", "type": "airbnb",
                      "cleaner": "Itzel"}}

    def _facts(self):
        return {
            "old": {"facts": [{"kind": "decline", "target_date": "2026-09-10",
                               "cleaner": "Itzel", "confidence": 0.95,
                               "evidence": "Sept 10 - I'm full"}]},
            "new": {"facts": [{"kind": "confirm", "target_date": "2026-09-10",
                               "cleaner": "Itzel", "confidence": 0.9,
                               "evidence": "Yes Sept 10 I can do it at 11:00"}]},
        }

    MSGS = {"old": {"timestamp": "2026-05-05T20:54:00"},
            "new": {"timestamp": "2026-08-20T05:01:37.000Z"}}

    def test_a_superseded_decline_does_not_fire(self):
        out = reconcile._facts_vs_bookings(
            self.BOOKINGS, self._facts(), "2026-08-20", self.MSGS)
        self.assertEqual([f["kind"] for f in out], [],
                         "a decline superseded by a later confirm still fired")

    def test_a_standing_decline_still_fires(self):
        """The detector must keep working when the decline IS the latest word."""
        facts = self._facts()
        msgs = {"old": {"timestamp": "2026-08-20T05:01:37.000Z"},
                "new": {"timestamp": "2026-05-05T20:54:00"}}
        out = reconcile._facts_vs_bookings(self.BOOKINGS, facts, "2026-08-20", msgs)
        self.assertEqual([f["kind"] for f in out], ["decline_still_assigned"])

    def test_a_tentative_statement_is_never_the_latest_word(self):
        facts = self._facts()
        facts["new"]["facts"][0]["tentative"] = True
        out = reconcile._facts_vs_bookings(
            self.BOOKINGS, facts, "2026-08-20", self.MSGS)
        self.assertEqual([f["kind"] for f in out], ["decline_still_assigned"],
                         "a tentative confirm was allowed to supersede a decline")


class EveryFindingCarriesADecision(unittest.TestCase):
    def test_watchdog_kinds_are_mapped_explicitly(self):
        """Findings merged after run() never pass through resolve_subjects, so
        app.py stamps them. The mapping must be explicit for both paths."""
        for kind in ("bridge_down", "bridge_blind_window", "bridge_watchdog_error"):
            self.assertIn(kind, reconcile._KIND_DECISION, f"{kind} unmapped")

    def test_app_stamps_merged_findings(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "cleaning-tracker" / "app.py").read_text()
        self.assertIn("decision=reconcile_mod._decision_of(f[\"kind\"])", src,
                      "watchdog findings reach the digest without a decision")

    def test_a_contradiction_about_time_is_adjudicate_not_investigate(self):
        self.assertEqual(reconcile._decision_of("time_mismatch"), "adjudicate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
