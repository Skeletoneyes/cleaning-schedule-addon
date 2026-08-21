"""ISC-352 (coherent schedule states downgrade `changed_mind`) and ISC-353
(a coherent date must not drag down an unread member's urgency).

Before this, a `changed_mind` finding — a cleaner's timeline showing both a
confirm and a decline — was treated as a conflict to adjudicate regardless of
which statement was LATEST. Live on 2026-08-21: Darya's latest decline
belonged to a cancelled duplicate booking while Itzel, the actual assignee,
had confirmed. The digest called that "conflicting signals" for 8 days
because nothing checked whether the CURRENT booking state already agreed with
everyone's most recent word.

`_coherent_dates` answers that question; `run()` stamps a `decision_override`
of "observe" on any `changed_mind` whose date is coherent — but only the
DECISION, never the severity, so a genuinely unread message merged into the
same booking still reads as needs-attention (ISC-353).

Run: python3 scripts/test_schedule_coherence.py
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
UID = "b-aug21@airbnb.com"


def booking(cleaner, status="active", btype="airbnb", end=DATE, start="2026-08-19"):
    return {"end": end, "start": start, "status": status, "type": btype, "cleaner": cleaner}


def fact(kind, cleaner, target_date=DATE, tentative=False):
    return {"kind": kind, "target_date": target_date, "cleaner": cleaner,
            "tentative": tentative, "confidence": 0.9, "evidence": ""}


def facts_and_msgs(*entries):
    """entries: (msg_id, timestamp, fact_dict) tuples. Returns (facts_records,
    messages_by_id) in the shapes reconcile.py expects."""
    facts_records, msgs = {}, {}
    for msg_id, ts, f in entries:
        facts_records.setdefault(msg_id, {"facts": []})["facts"].append(f)
        msgs[msg_id] = {"timestamp": ts}
    return facts_records, msgs


class CoherentDatesUnit(unittest.TestCase):
    """Direct tests of `_coherent_dates`, each proving latest-wins by giving
    the LOSING cleaner an older statement of the opposite kind."""

    def test_a_assignee_confirms_latest_non_assignee_declines_latest_is_coherent(self):
        bookings = {UID: booking("Itzel")}
        facts, msgs = facts_and_msgs(
            ("old_itzel", "2026-08-01T00:00:00", fact("decline", "Itzel")),
            ("new_itzel", "2026-08-15T00:00:00", fact("confirm", "Itzel")),
            ("old_darya", "2026-08-01T00:00:00", fact("confirm", "Darya")),
            ("new_darya", "2026-08-15T00:00:00", fact("decline", "Darya")),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertIn(DATE, coherent)

    def test_b_assignees_latest_is_decline_is_not_coherent(self):
        bookings = {UID: booking("Itzel")}
        facts, msgs = facts_and_msgs(
            ("old_itzel", "2026-08-01T00:00:00", fact("confirm", "Itzel")),
            ("new_itzel", "2026-08-15T00:00:00", fact("decline", "Itzel")),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertNotIn(DATE, coherent)

    def test_c_non_assignees_latest_is_confirm_contested_is_not_coherent(self):
        bookings = {UID: booking("Itzel")}
        facts, msgs = facts_and_msgs(
            ("assignee", "2026-08-15T00:00:00", fact("confirm", "Itzel")),
            ("old_darya", "2026-08-01T00:00:00", fact("decline", "Darya")),
            ("new_darya", "2026-08-16T00:00:00", fact("confirm", "Darya")),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertNotIn(DATE, coherent)

    def test_d1_unassigned_booking_is_not_coherent(self):
        bookings = {UID: booking(None)}
        facts, msgs = facts_and_msgs(
            ("m1", "2026-08-15T00:00:00", fact("confirm", "Itzel")),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertNotIn(DATE, coherent)

    def test_d2_no_booking_on_the_date_is_not_coherent(self):
        bookings = {}
        facts, msgs = facts_and_msgs(
            ("m1", "2026-08-15T00:00:00", fact("confirm", "Itzel")),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertNotIn(DATE, coherent)

    def test_d3_two_bookings_on_the_same_date_is_not_coherent(self):
        bookings = {UID: booking("Itzel"), "b-dup@airbnb.com": booking("Itzel")}
        facts, msgs = facts_and_msgs(
            ("m1", "2026-08-15T00:00:00", fact("confirm", "Itzel")),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertNotIn(DATE, coherent,
                         "an ambiguous date must not resolve to any assignee")

    def test_e_a_tentative_latest_statement_is_ignored_the_older_one_governs(self):
        """The newest fact (a decline) is tentative — a provisional statement,
        not a commitment — and must be skipped entirely, leaving the older
        non-tentative confirm as the operative one."""
        bookings = {UID: booking("Itzel")}
        facts, msgs = facts_and_msgs(
            ("old", "2026-08-01T00:00:00", fact("confirm", "Itzel", tentative=False)),
            ("new_tentative", "2026-08-19T00:00:00", fact("decline", "Itzel", tentative=True)),
        )
        coherent = reconcile._coherent_dates(bookings, facts, msgs, TODAY_STR)
        self.assertIn(DATE, coherent,
                      "a tentative newest statement must not override the standing confirm")


class EndToEndThroughRun(unittest.TestCase):
    """(f): the live 2026-08-21 topology, rebuilt with synthetic ids. One
    active booking (Itzel), one cancelled duplicate (was Darya's), both
    cleaners changed their minds, and one pending message with facts is
    still waiting for a decision."""

    UID_ACTIVE = "b-active-aug21@airbnb.com"
    UID_CANCELLED = "b-cancelled-aug21@airbnb.com"

    def _data(self):
        bookings = {
            self.UID_ACTIVE: booking("Itzel", status="active"),
            self.UID_CANCELLED: booking("Darya", status="cancelled"),
        }
        messages = [
            {"id": "m_darya_confirm", "timestamp": "2026-08-01T00:00:00", "review_state": "auto"},
            {"id": "m_darya_decline", "timestamp": "2026-08-16T00:00:00", "review_state": "auto"},
            {"id": "m_itzel_decline", "timestamp": "2026-08-02T00:00:00", "review_state": "auto"},
            {"id": "m_itzel_confirm", "timestamp": "2026-08-18T00:00:00", "review_state": "auto"},
            {"id": "m_pending", "timestamp": "2026-08-19T00:00:00", "review_state": "pending",
             "parse_error": "Anthropic API error: 429", "parsed": False,
             "sender_name_raw": "Itzel"},
        ]
        message_facts = {
            "m_darya_confirm": {"facts": [fact("confirm", "Darya")]},
            "m_darya_decline": {"facts": [fact("decline", "Darya")]},
            "m_itzel_decline": {"facts": [fact("decline", "Itzel")]},
            "m_itzel_confirm": {"facts": [fact("confirm", "Itzel")]},
            # A message the system failed to READ but did manage to extract
            # facts from on an earlier attempt (ISC-351) — still undecided,
            # never "unread".
            "m_pending": {"facts": [fact("unclear", "Itzel")]},
        }
        return {"bookings": bookings, "messages": messages, "message_facts": message_facts}

    def test_the_merged_finding_is_not_adjudicate_and_the_primary_is_the_pending_message(self):
        result = reconcile.run(self._data(), [], today=TODAY)
        merged = [f for f in result["findings"] if f.get("booking_uid") == self.UID_ACTIVE]
        self.assertEqual(len(merged), 1, "everything about the Aug 21 cleaning must resolve to one line")
        primary = merged[0]

        self.assertNotEqual(primary["decision"], "adjudicate",
                            "a coherent schedule story must not read as a contradiction to settle")
        self.assertIn(primary["kind"], ("undecided_message", "unread_message"))
        self.assertEqual(primary["decision"], "approve")

        # ISC-353: the merged severity is the max of the group. The unread
        # message contributes needs-attention; both changed_mind members were
        # downgraded to `observe` in DECISION but never in severity — and the
        # merge must not let the downgrade leak into severity either.
        self.assertEqual(primary["severity"], "needs-attention",
                         "the unread member's urgency must survive the changed_mind downgrade")

    def _raw_changed_mind_with_override_applied(self, data):
        """`findings_raw` on the run() result is POST-merge — individual
        changed_mind members are absorbed into one primary and their own
        `decision_override` is no longer separately visible there. To see
        the per-member override this reproduces run()'s own two-line
        sequence (lines 162-165 of reconcile.py) directly over the real
        detector + coherence functions — no reimplementation, same calls."""
        bookings = data["bookings"]
        facts_records = data["message_facts"]
        messages_by_id = {m["id"]: m for m in data["messages"] if m.get("id")}
        raw = reconcile._fact_timeline(facts_records, messages_by_id, TODAY_STR)
        coherent = reconcile._coherent_dates(bookings, facts_records, messages_by_id, TODAY_STR)
        for f in raw:
            if f.get("kind") == "changed_mind" and f.get("date") in coherent:
                f["decision_override"] = "observe"
        return raw

    def test_both_changed_mind_findings_carry_the_observe_override(self):
        changed = self._raw_changed_mind_with_override_applied(self._data())
        self.assertEqual({f["cleaner"] for f in changed}, {"Itzel", "Darya"})
        for f in changed:
            self.assertEqual(f.get("decision_override"), "observe",
                             f"{f['cleaner']}'s changed_mind must be downgraded on a coherent date")

    def test_darya_said_confirm_then_decline_itzel_said_decline_then_confirm(self):
        """End-to-end: both underlying statements survive the merge and are
        readable in the surviving primary's `why`."""
        result = reconcile.run(self._data(), [], today=TODAY)
        primary = next(f for f in result["findings"] if f.get("booking_uid") == self.UID_ACTIVE)
        self.assertIn("Darya said confirm then decline", primary["why"])
        self.assertIn("Itzel said decline then confirm", primary["why"])


class ChangedMindOnAnIncoherentDateKeepsApprove(unittest.TestCase):
    """(g): when the date is NOT coherent, no override is applied and the
    kind's default decision (`approve`) stands."""

    def test_no_override_when_the_assignee_has_not_settled_on_confirm(self):
        uid = "b-incoherent@airbnb.com"
        d = "2026-08-25"
        bookings = {uid: {"end": d, "start": "2026-08-23", "status": "active",
                          "type": "airbnb", "cleaner": "Itzel"}}
        messages = [
            {"id": "m1", "timestamp": "2026-08-10T00:00:00", "review_state": "auto"},
            {"id": "m2", "timestamp": "2026-08-20T00:00:00", "review_state": "auto"},
        ]
        message_facts = {
            "m1": {"facts": [fact("confirm", "Itzel", target_date=d)]},
            "m2": {"facts": [fact("decline", "Itzel", target_date=d)]},  # latest is decline
        }
        messages_by_id = {m["id"]: m for m in messages}

        # `_coherent_dates` must not mark this date coherent (assignee's
        # latest is decline, not confirm — case b, re-proven inline).
        coherent = reconcile._coherent_dates(bookings, message_facts, messages_by_id, TODAY_STR)
        self.assertNotIn(d, coherent)

        # Individual (pre-merge) changed_mind finding never receives the
        # override, exactly mirroring run()'s own override loop.
        raw = reconcile._fact_timeline(message_facts, messages_by_id, TODAY_STR)
        changed = [f for f in raw if f["kind"] == "changed_mind"]
        self.assertEqual(len(changed), 1)
        for f in raw:
            if f.get("kind") == "changed_mind" and f.get("date") in coherent:
                f["decision_override"] = "observe"
        self.assertNotIn("decision_override", changed[0],
                         "an incoherent date must never receive the observe override")
        self.assertEqual(reconcile._decision_for(changed[0]), "approve")

        # And end-to-end: run() correctly promotes the real contradiction
        # (decline_still_assigned, adjudicate) to primary instead — proving
        # the changed_mind signal was never silently discarded, merely
        # outranked by a genuine adjudication on this incoherent date.
        data = {"bookings": bookings, "messages": messages, "message_facts": message_facts}
        result = reconcile.run(data, [], today=TODAY)
        primary = next(f for f in result["findings"] if f.get("booking_uid") == uid)
        self.assertEqual(primary["kind"], "decline_still_assigned")
        self.assertEqual(primary["decision"], "adjudicate")
        self.assertIn("changed_mind:Itzel:2026-08-25", primary["absorbed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
