"""Unit tests for closing the notify loop from host messages.

This is the highest-risk rule in the system: a false positive reports a cleaner
as informed when she is not, which ends with someone outside a locked house.
So most of these tests are about what it must REFUSE to accept.

Run: python3 scripts/test_notify_ack.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import notify_ack as na  # noqa: E402

ITZEL_CHAT = "120363285451054712@g.us"
DARYA_CHAT = "120363410469116316@g.us"
GROUPS = {"Itzel": ITZEL_CHAT, "Darya": DARYA_CHAT}
CHANGED_AT = "2026-07-26T19:00:30"


def booking(cleaner="Darya", clean_time="12:00:00", commit_cleaner="Itzel",
            commit_time="17:00:00", end="2026-08-03"):
    return {"end": end, "cleaner": cleaner, "clean_time": clean_time, "status": "active",
            "cleaner_commitment": {"cleaner": commit_cleaner, "clean_time": commit_time,
                                   "date": end, "communicated_at": CHANGED_AT}}


def msg(mid, chat, ts, text="x"):
    return {"id": mid, "group": chat, "timestamp": ts, "text": text}


def facts(mid, cleaner, date="2026-08-03", time=None, kind="schedule_assertion", ev="quote"):
    return {mid: {"facts": [{"kind": kind, "target_date": date, "cleaner": cleaner,
                             "target_time": time, "evidence": ev}]}}


class AckTests(unittest.TestCase):
    def test_the_real_case_both_sides_told(self):
        """Itzel told it moved, Darya told she has it — the Aug 3 scenario."""
        m = {**msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
             **{}}
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", "Darya", ev="I will contact Daria to ask"),
             **facts("b", "Darya", time="11:00", ev="we're looking for Monday August 3")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["sides"]["displaced"]["cleaner"], "Itzel")
        self.assertEqual(ev["sides"]["assigned"]["cleaner"], "Darya")

    def test_telling_only_the_new_cleaner_is_not_enough(self):
        """The displaced cleaner is the one who turns up unexpectedly."""
        messages = {"b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        ev = na.find_ack_evidence(booking(), facts("b", "Darya"), messages, GROUPS)
        self.assertFalse(ev["ok"])
        self.assertTrue(any("Itzel" in x for x in ev["missing"]))

    def test_telling_only_the_displaced_cleaner_is_not_enough(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00")}
        ev = na.find_ack_evidence(booking(), facts("a", "Darya"), messages, GROUPS)
        self.assertFalse(ev["ok"])
        self.assertTrue(any("Darya" in x for x in ev["missing"]))

    def test_a_message_in_the_wrong_chat_tells_nobody(self):
        """Telling Darya about Itzel does not inform Itzel."""
        messages = {"a": msg("a", DARYA_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", "Darya"), **facts("b", "Darya")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        self.assertFalse(ev["ok"])

    def test_a_message_predating_the_change_proves_nothing(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-01T08:00:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-01T09:00:00")}
        f = {**facts("a", "Darya"), **facts("b", "Darya")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        self.assertFalse(ev["ok"])

    def test_reaffirming_the_old_plan_is_the_opposite_of_an_ack(self):
        """'Still good for Aug 3 at 5?' to Itzel must never clear the item — it
        is the host restating the stale arrangement."""
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", "Itzel", time="17:00", ev="still good for the 3rd at 5?"),
             **facts("b", "Darya")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        self.assertFalse(ev["ok"])
        self.assertTrue(any("OLD arrangement" in x for x in ev["missing"]))

    def test_an_assertion_naming_nobody_is_insufficient(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", None), **facts("b", "Darya")}
        self.assertFalse(na.find_ack_evidence(booking(), f, messages, GROUPS)["ok"])

    def test_wrong_date_is_ignored(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", "Darya", date="2026-08-05"), **facts("b", "Darya")}
        self.assertFalse(na.find_ack_evidence(booking(), f, messages, GROUPS)["ok"])

    def test_a_confirm_fact_is_not_a_host_telling_anyone(self):
        """Only host schedule_assertions count; a cleaner's own confirm is a
        different event entirely."""
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", "Darya", kind="confirm"), **facts("b", "Darya")}
        self.assertFalse(na.find_ack_evidence(booking(), f, messages, GROUPS)["ok"])

    def test_time_only_change_needs_just_the_one_cleaner(self):
        """Same cleaner, new time: only she needs telling."""
        b = booking(cleaner="Itzel", clean_time="11:00:00",
                    commit_cleaner="Itzel", commit_time="17:00:00")
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00")}
        f = facts("a", "Itzel", time="11:00", ev="let's do 11 instead")
        ev = na.find_ack_evidence(b, f, messages, GROUPS)
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["sides"]["displaced"]["verdict"], "told-current")

    def test_latest_qualifying_message_wins(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:00:00"),
                    "a2": msg("a2", ITZEL_CHAT, "2026-07-31T08:00:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**facts("a", "Darya", ev="first"), **facts("a2", "Darya", ev="second"),
             **facts("b", "Darya")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        self.assertEqual(ev["sides"]["displaced"]["quote"], "second")


class DescribeTests(unittest.TestCase):
    EV = {"sides": {"displaced": {"cleaner": "Itzel", "verdict": "told-moved",
                                  "timestamp": "2026-07-30T08:53:00", "quote": "I'll ask Darya"}}}

    def test_names_the_timestamp_and_quote(self):
        out = na.describe("2026-08-03", self.EV)
        self.assertIn("2026-07-30 08:53", out)
        self.assertIn("I'll ask Darya", out)
        self.assertIn("Itzel", out)

    def test_quotes_can_be_suppressed(self):
        out = na.describe("2026-08-03", self.EV, include_quotes=False)
        self.assertNotIn("I'll ask Darya", out)
        self.assertIn("2026-07-30 08:53", out, "the timestamp must survive redaction")




class SelfDeclineTests(unittest.TestCase):
    """A cleaner who declined the date already knows. Telling her would be
    repeating her own words back to her — and this was the real Aug 3 case."""

    def _decline(self, mid, cleaner="Itzel", date="2026-08-03", ev="I'm not available before 3pm on Monday"):
        return {mid: {"facts": [{"kind": "decline", "target_date": date,
                                 "cleaner": cleaner, "evidence": ev}]}}

    def test_her_own_decline_settles_her_side(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**self._decline("a"), **facts("b", "Darya")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["sides"]["displaced"]["verdict"], "declined-herself")

    def test_the_incoming_cleaner_still_has_to_be_told(self):
        """Itzel declining says nothing about whether Darya knows she is on."""
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00")}
        ev = na.find_ack_evidence(booking(), self._decline("a"), messages, GROUPS)
        self.assertFalse(ev["ok"])
        self.assertTrue(any("Darya" in x for x in ev["missing"]))

    def test_someone_elses_decline_does_not_count(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**self._decline("a", cleaner="Darya"), **facts("b", "Darya")}
        self.assertFalse(na.find_ack_evidence(booking(), f, messages, GROUPS)["ok"])

    def test_a_decline_for_another_date_does_not_count(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**self._decline("a", date="2026-08-10"), **facts("b", "Darya")}
        self.assertFalse(na.find_ack_evidence(booking(), f, messages, GROUPS)["ok"])

    def test_a_decline_predating_the_commitment_does_not_count(self):
        """She declined, then was re-committed anyway — the later commitment is
        the live fact."""
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-01T08:00:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**self._decline("a"), **facts("b", "Darya")}
        self.assertFalse(na.find_ack_evidence(booking(), f, messages, GROUPS)["ok"])

    def test_the_description_says_why_not_just_that(self):
        messages = {"a": msg("a", ITZEL_CHAT, "2026-07-30T08:53:00"),
                    "b": msg("b", DARYA_CHAT, "2026-07-30T09:57:00")}
        f = {**self._decline("a"), **facts("b", "Darya")}
        ev = na.find_ack_evidence(booking(), f, messages, GROUPS)
        out = na.describe("2026-08-03", ev)
        self.assertIn("declined", out)
        self.assertIn("already knows", out)
        self.assertIn("not available before 3pm", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
