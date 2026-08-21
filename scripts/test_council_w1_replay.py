"""ISC-350 — a sanitized replay of the live Aug-16-dismissal / Aug-21-refinding
sequence that ISC-349's subject-scoped dismissal was built to fix.

Synthetic uids, no WhatsApp message text (public repo — ISC-41's standing
caveat). The shape being replayed: on 2026-08-16 Josh adjudicated a contest
over one booking and dismissed the findings that raised it — old-style ids,
no `booking_uid` field. Five days later the reconciler's OWN latest-wins
fixes made the same booking resurface as `changed_mind:*` findings — a
different id, the same booking, no new signal — and those were correctly
swallowed by the subject-scoped dismissal (ISC-349). Then two NEW messages
arrive after the dismissal, genuinely reopening the subject, and exactly one
finding should survive: the one saying a message is waiting, not the old
contest.

Run: python3 scripts/test_council_w1_replay.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import reconcile  # noqa: E402

DATE = "2026-08-21"
TODAY = date(2026, 8, 21)
TODAY_STR = "2026-08-21"

UID_A = "aaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb@airbnb.com"
UID_B = "cccccccccccc-dddddddddddddddddddddddddddddddd@airbnb.com"

DISMISSED_AT = "2026-08-16T10:51:11"  # naive local — matches real dismissal shape


def fact(kind, cleaner, target_date=DATE, tentative=False):
    return {"kind": kind, "target_date": target_date, "cleaner": cleaner,
            "tentative": tentative, "confidence": 0.9, "evidence": ""}


# Three Aug-16 dismissals, legacy shape: no `booking_uid` field, subject
# recoverable only by parsing UID_A out of the key itself.
DISMISSED_FINDINGS = {
    f"contested_cleaner:{UID_A}:Darya": {
        "dismissed_at": DISMISSED_AT, "reason": "adjudicated 2026-08-16 — Itzel keeps it",
    },
    f"decline_still_assigned:{UID_A}:Itzel": {
        "dismissed_at": DISMISSED_AT, "reason": "adjudicated 2026-08-16 — superseded by later confirm",
    },
    f"schedule_mismatch:{UID_A}:Darya": {
        "dismissed_at": DISMISSED_AT, "reason": "adjudicated 2026-08-16 — mis-extraction",
    },
}


def base_bookings():
    return {
        UID_A: {"end": DATE, "start": "2026-08-19", "status": "active", "type": "airbnb",
                "cleaner": "Itzel", "clean_time": "11:00:00"},
        UID_B: {"end": DATE, "start": "2026-08-19", "status": "cancelled", "type": "airbnb",
                "cleaner": "Darya"},
    }


def old_evidence():
    """Messages timestamped BEFORE the Aug 16 dismissal — the evidence the
    dismissal was actually made over. Darya confirm-then-decline, Itzel
    decline-then-confirm, both settling before the cutoff."""
    messages = [
        {"id": "m_darya_confirm_old", "timestamp": "2026-08-01T00:00:00", "review_state": "auto"},
        {"id": "m_darya_decline_old", "timestamp": "2026-08-10T00:00:00", "review_state": "auto"},
        {"id": "m_itzel_decline_old", "timestamp": "2026-08-02T00:00:00", "review_state": "auto"},
        {"id": "m_itzel_confirm_old", "timestamp": "2026-08-11T00:00:00", "review_state": "auto"},
    ]
    message_facts = {
        "m_darya_confirm_old": {"facts": [fact("confirm", "Darya")]},
        "m_darya_decline_old": {"facts": [fact("decline", "Darya")]},
        "m_itzel_decline_old": {"facts": [fact("decline", "Itzel")]},
        "m_itzel_confirm_old": {"facts": [fact("confirm", "Itzel")]},
    }
    return messages, message_facts


def new_evidence():
    """Two Aug-18 messages — AFTER the dismissal cutoff — both about the Aug
    21 cleaning. One failed extraction but kept its facts from an earlier
    successful pass (ISC-351: undecided, not unread); one is a plain pending
    message. Neither carries confirm/decline facts of its own — an `unclear`
    fact is enough to name the cleaning without perturbing the latest-wins
    detectors, which is the point: these two messages reopen the subject
    purely by arriving, not by asserting anything new about the contest."""
    messages = [
        {"id": "m_pending_error", "timestamp": "2026-08-18T09:00:00", "review_state": "pending",
         "parse_error": "Anthropic API error: 429", "parsed": False, "sender_name_raw": "Itzel"},
        {"id": "m_pending_plain", "timestamp": "2026-08-18T14:00:00", "review_state": "pending"},
    ]
    message_facts = {
        "m_pending_error": {"facts": [fact("unclear", "Itzel")]},
        "m_pending_plain": {"facts": [fact("unclear", "Itzel")]},
    }
    return messages, message_facts


def build_data(include_new=False):
    messages, message_facts = old_evidence()
    if include_new:
        new_msgs, new_facts = new_evidence()
        messages = messages + new_msgs
        message_facts = {**message_facts, **new_facts}
    return {
        "bookings": base_bookings(),
        "messages": messages,
        "message_facts": message_facts,
        "dismissed_findings": DISMISSED_FINDINGS,
    }


class Case1OldEvidenceOnlyStaysDismissed(unittest.TestCase):
    """Only the evidence the Aug 16 dismissal actually covered exists. The
    reconciler's own latest-wins fixes re-derive the same settled state as
    `changed_mind` findings (a different id than any of the three dismissed
    ones), but their evidence predates the cutoff — subject-dismissed."""

    def test_no_finding_about_booking_a_survives(self):
        result = reconcile.run(build_data(include_new=False), [], today=TODAY)
        surviving = [f for f in result["findings"] if f.get("booking_uid") == UID_A]
        self.assertEqual(surviving, [],
                         "findings re-derived from evidence that predates the Aug-16 dismissal "
                         "must stay subject-dismissed, whatever new id they're minted under")
        self.assertGreaterEqual(result["counts"]["dismissed"], 1)

    def test_the_changed_mind_findings_exist_pre_filter_proving_this_is_a_real_test(self):
        """Guards against a vacuous pass — the findings genuinely fire (and
        would appear without the dismissal), they're just filtered."""
        result = reconcile.run(build_data(include_new=False), [], today=TODAY)
        raw_about_a = [f for f in result["findings_raw"] if f.get("booking_uid") == UID_A]
        self.assertEqual(len(raw_about_a), 1, "old evidence must still resolve to one merged finding")
        self.assertEqual(raw_about_a[0]["kind"], "changed_mind")


class Case2NewMessagesReopenTheSubject(unittest.TestCase):
    """Two messages dated after the dismissal arrive. Exactly one finding
    about booking A survives, and it is the waiting-message finding, not a
    resurrected contest."""

    def test_exactly_one_finding_about_booking_a_survives(self):
        result = reconcile.run(build_data(include_new=True), [], today=TODAY)
        surviving = [f for f in result["findings"] if f.get("booking_uid") == UID_A]
        self.assertEqual(len(surviving), 1)

    def test_the_survivor_is_an_unread_or_undecided_message_kind(self):
        result = reconcile.run(build_data(include_new=True), [], today=TODAY)
        primary = next(f for f in result["findings"] if f.get("booking_uid") == UID_A)
        self.assertIn(primary["kind"], ("undecided_message", "unread_message"))

    def test_the_decision_is_never_adjudicate(self):
        result = reconcile.run(build_data(include_new=True), [], today=TODAY)
        primary = next(f for f in result["findings"] if f.get("booking_uid") == UID_A)
        self.assertIn(primary["decision"], ("approve", "investigate"))
        self.assertNotEqual(primary["decision"], "adjudicate",
                            "a settled contest resurfacing as unread noise must never read "
                            "as a contradiction demanding adjudication")

    def test_why_leads_with_the_waiting_message_not_the_contest(self):
        result = reconcile.run(build_data(include_new=True), [], today=TODAY)
        primary = next(f for f in result["findings"] if f.get("booking_uid") == UID_A)
        self.assertTrue(
            primary["why"].startswith(f"a message about the {DATE} cleaning is waiting"),
            f"why did not lead with the waiting-message phrasing: {primary['why']!r}")
        self.assertNotIn("confirmed for", primary["why"])
        self.assertNotIn("declined", primary["why"].split(" · ")[0])

    def test_the_new_messages_are_the_evidence_that_reopened_it(self):
        result = reconcile.run(build_data(include_new=True), [], today=TODAY)
        primary = next(f for f in result["findings"] if f.get("booking_uid") == UID_A)
        self.assertTrue(
            {"m_pending_error", "m_pending_plain"} & set(primary["evidence"]),
            "the merged survivor must carry the new evidence that reopened the subject")


if __name__ == "__main__":
    unittest.main(verbosity=2)
