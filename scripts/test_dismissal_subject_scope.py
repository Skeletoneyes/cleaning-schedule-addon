"""Subject-scoped dismissals with an evidence cutoff (ISC-349), and dismissed
members deprioritized during primary selection (ISC-335).

Before ISC-349, a dismissal only ever matched by exact finding id. Detectors
also MINT new ids over old evidence — the Aug 16 dismissals named
`contested_cleaner`/`decline_still_assigned` ids, and five days later the same
adjudicated contest returned as `changed_mind:*`, a different id over the same
booking with no new signal. `filter_and_sort` now also dismisses a finding
when a dismissal on the same `booking_uid` postdates ALL of its evidence —
and re-opens the subject the moment a message arrives after `dismissed_at`.

Before ISC-335, `resolve_subjects` picked the primary by decision rank alone.
A dismissal made last month could out-rank a live finding for the SAME
booking and become its mouthpiece — which, via `_is_dismissed`'s primary
check, silently swallowed live findings that didn't exist when the dismissal
was written. Dismissed member ids now sort last in primary selection.

Run: python3 scripts/test_dismissal_subject_scope.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import reconcile  # noqa: E402

UID = "b-aug21@airbnb.com"
# Legacy-shaped uid embedded in a finding id, matching reconcile._UID_IN_ID_RE
# (hex-hex@airbnb.com) — exactly the shape real detector ids use.
UID_LEGACY = "1418fb94e984-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@airbnb.com"

BOOKINGS = {UID: {"end": "2026-08-21", "status": "active", "type": "airbnb",
                  "cleaner": "Itzel"}}

DISMISSED_AT = "2026-08-16T10:51:11"  # naive local (America/Vancouver)


def finding(fid, uid=UID, kind="changed_mind", decision_kind=None, severity="informational",
            evidence=None, evidence_latest="__unset__", cleaner="Itzel", detector="fact_timeline",
            why="w"):
    """A minimal finding. `evidence_latest="__unset__"` (default) means the
    field is absent entirely — real findings without full evidence coverage
    (e.g. drift) never get it stamped, and that absence is itself a case we
    test (d)."""
    out = {"id": fid, "detector": detector, "kind": kind, "severity": severity,
           "booking_uid": uid, "cleaner": cleaner, "date": "2026-08-21", "why": why,
           "evidence": evidence or []}
    if evidence_latest != "__unset__":
        out["evidence_latest"] = evidence_latest
    return out


class SubjectScopedFilter(unittest.TestCase):
    """(a) and (b): a dismissal WITH booking_uid filters (or doesn't) a
    differently-id'd finding on the same booking, purely by evidence_latest
    vs dismissed_at."""

    def test_a_later_finding_predating_the_cutoff_is_subject_dismissed(self):
        dismissed = {"contested_cleaner:b-aug21@airbnb.com:Darya": {
            "booking_uid": UID, "dismissed_at": DISMISSED_AT, "reason": "adjudicated",
        }}
        f = finding("changed_mind:Itzel:2026-08-21",
                     evidence_latest="2026-08-15T04:00:07.000Z",  # before Aug 16 10:51 local
                     evidence=["m1"])
        out = reconcile.filter_and_sort({"findings_raw": [f]}, dismissed)
        self.assertEqual(out["findings"], [],
                         "evidence predating the dismissal cutoff must be subject-dismissed "
                         "even though the finding's own id was never dismissed")
        self.assertEqual(out["counts"]["dismissed"], 1)

    def test_b_evidence_after_the_cutoff_reopens_the_subject(self):
        dismissed = {"contested_cleaner:b-aug21@airbnb.com:Darya": {
            "booking_uid": UID, "dismissed_at": DISMISSED_AT, "reason": "adjudicated",
        }}
        f = finding("changed_mind:Itzel:2026-08-21",
                     evidence_latest="2026-08-17T04:00:07.000Z",  # after Aug 16 10:51 local
                     evidence=["m1"])
        out = reconcile.filter_and_sort({"findings_raw": [f]}, dismissed)
        self.assertEqual([x["id"] for x in out["findings"]], [f["id"]],
                         "a message arriving after dismissed_at must re-open the subject")


class LegacyDismissalKey(unittest.TestCase):
    """(c): a legacy dismissal record (no booking_uid field) still resolves
    its subject by parsing the uid out of the dismissed finding's own id."""

    def test_dismissal_subject_parses_uid_out_of_the_legacy_key(self):
        rec = {"dismissed_at": DISMISSED_AT, "reason": "adjudicated 2026-08-16"}
        key = f"contested_cleaner:{UID_LEGACY}:Darya"
        self.assertEqual(reconcile._dismissal_subject(key, rec), UID_LEGACY)

    def test_the_subject_rule_still_applies_via_the_parsed_uid(self):
        dismissed = {
            f"contested_cleaner:{UID_LEGACY}:Darya": {"dismissed_at": DISMISSED_AT,
                                                        "reason": "adjudicated"},
            f"decline_still_assigned:{UID_LEGACY}:Itzel": {"dismissed_at": DISMISSED_AT,
                                                             "reason": "adjudicated"},
        }
        f = finding("changed_mind:Itzel:2026-08-21", uid=UID_LEGACY,
                     evidence_latest="2026-08-15T04:00:07.000Z", evidence=["m1"])
        out = reconcile.filter_and_sort({"findings_raw": [f]}, dismissed)
        self.assertEqual(out["findings"], [],
                         "a legacy dismissal (no booking_uid field) must still filter by "
                         "the uid embedded in its own key")


class NoEvidenceLatestNeverSubjectDismissed(unittest.TestCase):
    """(d): a finding with no evidence at all (drift) carries no timestamp to
    compare and must never be subject-dismissed, even for a dismissed booking."""

    def test_a_driftfinding_with_empty_evidence_survives(self):
        dismissed = {"contested_cleaner:b-aug21@airbnb.com:Darya": {
            "booking_uid": UID, "dismissed_at": DISMISSED_AT, "reason": "adjudicated",
        }}
        drift = finding("drift:b-aug21@airbnb.com:unassigned", kind="drift_unassigned",
                        severity="needs-attention", detector="drift", evidence=[])
        # No evidence_latest key at all — mirrors what run() actually produces
        # for a drift finding, since `ev` is empty and the stamp loop only
        # sets the key when `ev` is truthy.
        self.assertNotIn("evidence_latest", drift)
        out = reconcile.filter_and_sort({"findings_raw": [drift]}, dismissed)
        self.assertEqual([x["id"] for x in out["findings"]], [drift["id"]],
                         "an evidence-free finding must never be subject-dismissed")


class InstantNotStringComparison(unittest.TestCase):
    """(e): the two stored timestamp shapes are not string-comparable. This
    constructs a pair where naive-string comparison and instant comparison
    disagree, and only the instant-correct answer is right."""

    def test_naive_local_dismissed_at_vs_utc_evidence_latest(self):
        # dismissed_at "02:00" naive-local (America/Vancouver, PDT = UTC-7)
        # is the INSTANT 09:00 UTC. evidence_latest "08:30Z" is 08:30 UTC —
        # 30 minutes BEFORE that instant, so the evidence predates the
        # dismissal and must be subject-dismissed.
        #
        # A raw string comparison gets this backwards: "...T02:00:00" sorts
        # BEFORE "...T08:30:00.000Z" lexically (the digit '0' < '8' right
        # after "T"), which would read as "evidence is newer than the
        # dismissal" — the wrong answer, and the opposite of what the
        # instant comparison below requires.
        dismissed_at = "2026-08-16T02:00:00"
        evidence_latest = "2026-08-16T08:30:00.000Z"
        self.assertLess(dismissed_at, evidence_latest,
                        "sanity check: string order must indeed disagree with instant order "
                        "for this test to prove anything")
        cutoff_instant = reconcile.as_instant(dismissed_at)
        evidence_instant = reconcile.as_instant(evidence_latest)
        self.assertGreater(cutoff_instant, evidence_instant,
                           "sanity check: the real instants must disagree with the strings")

        dismissed = {"contested_cleaner:b-aug21@airbnb.com:Darya": {
            "booking_uid": UID, "dismissed_at": dismissed_at, "reason": "adjudicated",
        }}
        f = finding("changed_mind:Itzel:2026-08-21", evidence_latest=evidence_latest,
                     evidence=["m1"])
        out = reconcile.filter_and_sort({"findings_raw": [f]}, dismissed)
        self.assertEqual(out["findings"], [],
                         "comparison must use as_instant, not the raw timestamp strings")


class SubjectAwarePrimarySelection(unittest.TestCase):
    """(f), ISC-335: dismissed member ids sort last in resolve_subjects, so a
    dismissal cannot become the mouthpiece for live findings absorbed under
    it — and a group where every member is dismissed still filters entirely."""

    def _facts_vs_bookings_style(self, fid, cleaner, kind="contested_cleaner",
                                 severity="needs-attention"):
        return finding(fid, kind=kind, severity=severity, cleaner=cleaner,
                       detector="facts_vs_bookings", evidence=["m-old"],
                       why=f"{cleaner} confirmed but booking is assigned to Itzel")

    def test_a_live_member_becomes_primary_over_a_higher_ranked_dismissed_one(self):
        """Three members on one booking: the highest-decision-rank member
        (`contested_cleaner`, adjudicate) is dismissed; two live members
        (`changed_mind` at approve, and an `unread_message`-style finding at
        investigate) are not. Without ISC-335 the dismissed adjudicate-rank
        member would sort first by decision rank alone and become primary —
        making the whole merged finding inert via `_is_dismissed`'s primary
        rule even though live signal exists."""
        dismissed_id = "contested_cleaner:b-aug21@airbnb.com:Darya"
        contested = self._facts_vs_bookings_style(dismissed_id, "Darya")
        changed = finding("changed_mind:Itzel:2026-08-21", uid=None, kind="changed_mind",
                          severity="informational", evidence=["m-new"],
                          why="Itzel said decline then confirm for 2026-08-21; latest is confirm")
        unread = finding("unread:m-new2", uid=None, kind="undecided_message",
                         severity="suggest", detector="unread_messages", cleaner=None,
                         evidence=["m-new2"], why="a message about the 2026-08-21 cleaning is waiting")

        out = reconcile.resolve_subjects([contested, changed, unread], BOOKINGS,
                                         {dismissed_id: {}})
        merged = [x for x in out if x.get("booking_uid") == UID]
        self.assertEqual(len(merged), 1)
        primary = merged[0]
        self.assertNotEqual(primary["id"], dismissed_id,
                            "the dismissed member became primary despite live members existing")
        self.assertIn(dismissed_id, primary["absorbed"])

        # And the merged finding survives filtering: not every absorbed id is
        # dismissed (only the one), and the primary's own id was never
        # dismissed.
        survived = reconcile.filter_and_sort({"findings_raw": out}, {dismissed_id: {}})
        self.assertIn(primary["id"], [x["id"] for x in survived["findings"]],
                     "a live primary absorbing one dismissed member must survive")

    def test_when_every_member_is_dismissed_the_merged_finding_filters_out(self):
        id_a = "contested_cleaner:b-aug21@airbnb.com:Darya"
        id_b = "decline_still_assigned:b-aug21@airbnb.com:Itzel"
        a = self._facts_vs_bookings_style(id_a, "Darya")
        b = self._facts_vs_bookings_style(id_b, "Itzel", kind="decline_still_assigned")
        dismissed = {id_a: {}, id_b: {}}

        out = reconcile.resolve_subjects([a, b], BOOKINGS, dismissed)
        merged = [x for x in out if x.get("booking_uid") == UID]
        self.assertEqual(len(merged), 1)

        survived = reconcile.filter_and_sort({"findings_raw": merged}, dismissed)
        self.assertEqual(survived["findings"], [],
                         "a group where every member is dismissed must filter out entirely")


class RegressionDirectAndAllAbsorbedDismissal(unittest.TestCase):
    """(g): the two dismissal rules that predate ISC-349/ISC-335 still work
    unchanged — a direct id match, and every absorbed id dismissed."""

    def test_a_findings_own_id_being_dismissed_still_filters_it(self):
        f = finding("changed_mind:Itzel:2026-08-21", evidence=["m1"])
        out = reconcile.filter_and_sort({"findings_raw": [f]}, {f["id"]: {}})
        self.assertEqual(out["findings"], [])

    def test_every_absorbed_id_dismissed_still_filters_the_merged_primary(self):
        primary = dict(finding("primary-id", evidence=["m1", "m2"]))
        primary["absorbed"] = ["absorbed-1", "absorbed-2"]
        out = reconcile.filter_and_sort({"findings_raw": [primary]},
                                        {"absorbed-1": {}, "absorbed-2": {}})
        self.assertEqual(out["findings"], [],
                         "every absorbed id dismissed must still silence the primary")

    def test_a_partial_absorbed_dismissal_does_not_filter_the_primary(self):
        primary = dict(finding("primary-id-2", evidence=["m1", "m2"]))
        primary["absorbed"] = ["absorbed-a", "absorbed-b"]
        out = reconcile.filter_and_sort({"findings_raw": [primary]}, {"absorbed-a": {}})
        self.assertEqual([x["id"] for x in out["findings"]], ["primary-id-2"])




class ReviewQueueExemption(unittest.TestCase):
    """Code-review 2026-08-21, confirmed by live repro: a pending message's
    finding has the message's own arrival as its only evidence, so a message
    sitting unprocessed since BEFORE an unrelated dismissal on the same
    booking was muted forever by the subject rule — the row-stuck-in-pending
    failure the unread detector exists to end. undecided_message and
    unread_message are exempt: their repair is accept/ignore, not
    adjudication. An explicit id dismissal still silences them."""

    DISMISSED = {"contested_cleaner:b-aug21@airbnb.com:Darya": {
        "booking_uid": UID, "dismissed_at": DISMISSED_AT, "reason": "adjudicated"}}

    def _msg_finding(self, kind):
        return finding(f"unread:m-old", kind=kind, detector="unread_messages",
                       severity="needs-attention",
                       evidence=["m-old"], evidence_latest="2026-08-15T04:00:07.000Z")

    def test_undecided_message_predating_the_cutoff_still_surfaces(self):
        out = reconcile.filter_and_sort(
            {"findings_raw": [self._msg_finding("undecided_message")]}, self.DISMISSED)
        self.assertEqual(len(out["findings"]), 1)

    def test_unread_message_predating_the_cutoff_still_surfaces(self):
        out = reconcile.filter_and_sort(
            {"findings_raw": [self._msg_finding("unread_message")]}, self.DISMISSED)
        self.assertEqual(len(out["findings"]), 1)

    def test_explicit_id_dismissal_still_silences_a_message_finding(self):
        d = dict(self.DISMISSED)
        d["unread:m-old"] = {"dismissed_at": DISMISSED_AT, "reason": "handled"}
        out = reconcile.filter_and_sort(
            {"findings_raw": [self._msg_finding("undecided_message")]}, d)
        self.assertEqual(out["findings"], [])

    def test_non_message_kinds_remain_subject_to_the_cutoff(self):
        """The exemption is surgical — changed_mind on old evidence stays dismissed."""
        f = finding("changed_mind:Itzel:2026-08-21",
                    evidence=["m1"], evidence_latest="2026-08-15T04:00:07.000Z")
        out = reconcile.filter_and_sort({"findings_raw": [f]}, self.DISMISSED)
        self.assertEqual(out["findings"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
