#!/usr/bin/env python3
"""Distance decides urgency, and one list decides order (1.37.5).

Two changes with one shape: a value that should be derived was instead fixed at
the site that emitted it, and a list that should have been re-derived was
instead maintained by hand alongside the real one.

`_drift` stamped every finding `needs-attention` regardless of date. Live on
2026-08-20 that produced ten drift findings, nine of them bookings two to
twelve months out, and a counts badge reading 14 needs-attention on a day when
five things actually needed a human. Josh's rule: "for cleanings where nobody
is assigned it's only a problem if that's less than one month from today."

The watchdog findings were prepended straight onto `result["findings"]` after
`filter_and_sort` had already run, so they skipped the decision ranking they
had just been stamped for AND the dismissed filter. `bridge_blind_window` held
line 1 of every digest above the one-tap actions, and no dismissal could clear
it. Those were tracked as two problems; they were one.

Run: python3 scripts/test_drift_severity.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))

import reconcile  # noqa: E402


TODAY = "2026-08-20"


def _item(kind="unassigned", when="2026-09-10", uid="u1", cleaner=None):
    return {"kind": kind, "uid": uid, "date": when, "cleaner": cleaner}


class DriftSeverityTests(unittest.TestCase):
    """`_drift` — severity derived from the date, not typed at the emit site."""

    def _sev(self, **kw):
        return reconcile._drift([_item(**kw)], TODAY)[0]["severity"]

    def test_an_unassigned_cleaning_inside_a_month_needs_attention(self):
        self.assertEqual(self._sev(when="2026-09-10"), "needs-attention")

    def test_the_boundary_day_is_inside(self):
        """Exactly 30 days out still counts. A rule whose edge is ambiguous
        gets re-litigated every time it fires."""
        self.assertEqual(self._sev(when="2026-09-19"), "needs-attention")

    def test_one_day_past_the_boundary_is_demoted(self):
        self.assertEqual(self._sev(when="2026-09-20"), "suggest")

    def test_the_far_future_bookings_that_produced_the_bad_badge(self):
        """The nine live on 2026-08-20, every one of them demoted."""
        for when in ("2026-10-19", "2027-01-04", "2027-05-05", "2027-05-12",
                     "2027-05-16", "2027-06-06", "2027-07-01", "2027-07-09",
                     "2027-08-08"):
            self.assertEqual(self._sev(when=when), "suggest", when)

    def test_a_past_due_unassigned_cleaning_is_the_most_urgent_case(self):
        """`days` goes negative here. An off-by-one that read it as 'far away'
        would silence the one finding that cannot wait."""
        self.assertEqual(self._sev(when="2026-08-19"), "needs-attention")

    def test_the_rule_applies_to_unassigned_only(self):
        """Josh ruled on bookings with NOBODY on them. `drift_new` and
        `drift_changed` describe a cleaner who IS assigned and may not have
        been told — a different question, and not one he has answered."""
        for kind in ("new", "changed", "cancelled"):
            self.assertEqual(self._sev(kind=kind, when="2027-05-05"),
                             "needs-attention", kind)

    def test_without_a_reference_date_nothing_is_demoted(self):
        """Fail toward shouting. A caller that forgets to pass today must not
        silently downgrade the whole list."""
        self.assertEqual(reconcile._drift([_item(when="2027-05-05")])[0]["severity"],
                         "needs-attention")

    def test_an_unparseable_date_is_not_demoted(self):
        self.assertEqual(self._sev(when="not-a-date"), "needs-attention")

    def test_run_passes_today_through(self):
        """The wiring, not the rule: `_drift` is only correct if `run()` hands
        it the date. Nothing else in this file would notice if it stopped."""
        out = reconcile.run(
            {"bookings": {}, "message_facts": {}, "messages": []},
            [_item(when="2027-05-05")],
            today=date.fromisoformat(TODAY),
        )
        drift = [f for f in out["findings_raw"] if f["kind"] == "drift_unassigned"]
        self.assertEqual([f["severity"] for f in drift], ["suggest"])


# `filter_and_sort` reads the real clock for its STALE_DAYS cutoff, so these
# fixtures are dated relative to it. Pinning them to 2026-08-20 like the
# `_drift` tests above would pass today and silently start failing six days
# later — a test that expires is worse than no test, because it fails for a
# reason unrelated to the thing it guards.
_REAL_TODAY = date.today()


def _wd(fid="bridge_blind_window", kind="bridge_blind_window"):
    """Shaped like bridge_watchdog.findings() output — dated today, no uid."""
    return {"id": fid, "detector": "bridge_watchdog", "kind": kind,
            "severity": "needs-attention", "booking_uid": None, "cleaner": None,
            "date": _REAL_TODAY.isoformat(), "why": "the bridge may be down",
            "evidence": []}


def _approve(fid="changed_mind:u1"):
    return {"id": fid, "detector": "fact_timeline", "kind": "changed_mind",
            "severity": "needs-attention", "booking_uid": "u1", "cleaner": "Itzel",
            "date": (_REAL_TODAY + timedelta(days=21)).isoformat(),
            "why": "latest is confirm", "evidence": ["x"], "decision": "approve"}


class WatchdogFindingsGoThroughTheRankingTests(unittest.TestCase):
    """The property app.py's merge site now relies on instead of hand-merging.

    These assert `filter_and_sort` behaves correctly when a watchdog finding is
    present in `findings_raw` — which is the whole of the fix, since the merge
    site's job is now only to put it there.
    """

    def _sorted(self, raw, dismissed=None):
        return reconcile.filter_and_sort(
            {"findings_raw": raw}, dismissed or {})

    def test_a_one_tap_action_outranks_a_go_and_look(self):
        """The live 2026-08-20 ordering, inverted. `bridge_blind_window` ranks
        `investigate`; `changed_mind` ranks `approve` and held the answer."""
        wd = dict(_wd(), decision=reconcile._decision_of("bridge_blind_window"))
        out = self._sorted([wd, _approve()])
        self.assertEqual([f["id"] for f in out["findings"]],
                         ["changed_mind:u1", "bridge_blind_window"])

    def test_a_dismissed_watchdog_finding_stays_dismissed(self):
        """ISC-236. It re-appeared on every run because the merge happened
        after the filter, so no button could ever clear it."""
        wd = dict(_wd(), decision="investigate")
        out = self._sorted([wd, _approve()], {"bridge_blind_window": "noise"})
        self.assertEqual([f["id"] for f in out["findings"]], ["changed_mind:u1"])
        self.assertEqual(out["counts"]["dismissed"], 1)

    def test_counts_are_derived_from_the_same_list_that_is_rendered(self):
        """They were incremented by hand at the merge site — a third copy of a
        rule that already existed once."""
        wd = dict(_wd(), decision="investigate")
        out = self._sorted([wd, _approve()])
        self.assertEqual(out["counts"]["total"], len(out["findings"]))
        self.assertEqual(out["counts"]["needs-attention"], 2)

    def test_a_watchdog_finding_dated_today_survives_the_stale_filter(self):
        """The property the old hand-merge existed to protect. It holds without
        a second list, because every watchdog finding is dated today at its own
        emit site — but if that ever changes, this fails here."""
        wd = dict(_wd(), decision="investigate")
        out = reconcile.filter_and_sort({"findings_raw": [wd]}, {})
        self.assertEqual(len(out["findings"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=0)
