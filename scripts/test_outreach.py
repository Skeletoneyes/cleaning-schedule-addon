#!/usr/bin/env python3
"""The digest says where an unassigned cleaning IS, not just that it is short (1.41.0).

On 2026-08-23 the Sept 2 bullet read "active booking has no cleaner assigned —
assign a cleaner". The truth was "Itzel was asked at 08:41 on Aug 22, no reply
yet", and the Pi held that fact: a host `schedule_assertion` at 0.75, cleaner
Itzel, target 2026-09-02. `_schedule_vs_bookings` discards anything under the
0.85 gate because it ASSIGNS on those facts; nothing else read them, so an
unanswered ask was invisible. `_outreach` reads the same facts at a low bar,
never writes, and annotates the drift finding with one of three states.

Run: python3 scripts/test_outreach.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))

import reconcile  # noqa: E402

TODAY = date(2026, 8, 23)
TODAY_S = TODAY.isoformat()
UID = "1418fb94e984-sept2@airbnb.com"
ITZEL_GROUP = "120363285451054712@g.us"
DARYA_GROUP = "120363410469116316@g.us"
HOST = "162379946016995@lid"
ITZEL = "192466460373222@lid"
DARYA = "135712527638545@lid"


def _data(messages, facts, bookings=None):
    return {
        "bookings": bookings or {
            UID: {"start": "2026-08-31", "end": "2026-09-02", "cleaner": None,
                  "status": "active", "type": "airbnb"},
        },
        "messages": messages,
        "message_facts": facts,
        "group_labels": {ITZEL_GROUP: "Itzel", DARYA_GROUP: "Darya"},
        "cleaner_jids": {"Darya": [DARYA], "Itzel": [ITZEL]},
        "dismissed_findings": {},
    }


def _msg(mid, sender, group, ts):
    return {"id": mid, "sender": sender, "group": group, "timestamp": ts, "text": "x"}


def _fact(kind, target, cleaner, conf=0.75, tentative=False):
    return {"kind": kind, "target_date": target, "cleaner": cleaner,
            "confidence": conf, "tentative": tentative, "evidence": "q"}


def _drift_item(uid=UID, end="2026-09-02"):
    return {"uid": uid, "kind": "unassigned", "cleaner": None, "date": end,
            "was": None, "now": (None, end, None)}


def _run(data):
    return reconcile.run(data, [_drift_item()], today=TODAY)


def _merged(result, uid=UID):
    return next(f for f in result["findings"] if f.get("booking_uid") == uid)


ASK = _msg("ask1", HOST, ITZEL_GROUP, "2026-08-22T15:41:55.000Z")
ASK_FACTS = {"ask1": {"facts": [_fact("schedule_assertion", "2026-09-02", "Itzel")]}}


class OutreachStates(unittest.TestCase):
    def test_unasked_when_no_host_fact_names_the_date(self):
        f = _merged(_run(_data([], {})))
        self.assertEqual(f["outreach"]["state"], "unasked")
        self.assertIn("no cleaner has been asked", f["why"])
        self.assertEqual(f["decision"], "investigate")

    def test_asked_when_host_named_date_and_cleaner_silent(self):
        f = _merged(_run(_data([ASK], ASK_FACTS)))
        o = f["outreach"]
        self.assertEqual(o["state"], "asked")
        self.assertEqual(o["cleaner"], "Itzel")
        self.assertEqual(o["asked_on"], "2026-08-22")
        self.assertEqual(o["days_waiting"], 1)
        self.assertIn("waiting on Itzel — asked 2026-08-22 (1 day)", f["why"])
        self.assertIn("booking needs a cleaner", f["why"])
        # Sept 2 is 10 days out and the ask is 1 day old → patience.
        self.assertEqual(f["decision"], "wait")

    def test_the_live_sept2_fact_lights_up_without_reprocess(self):
        """The exact record stored on the Pi on 2026-08-22 (confidence 0.75)."""
        facts = {"ask1": {"facts": [
            {"cleaner": "Itzel", "confidence": 0.75, "evidence": "if you’re free on Sept 2",
             "kind": "schedule_assertion", "target_date": "2026-09-02",
             "target_time": None, "tentative": False}]}}
        f = _merged(_run(_data([ASK], facts)))
        self.assertEqual(f["outreach"]["state"], "asked")

    def test_declined_when_cleaner_said_no_after_the_ask(self):
        msgs = [ASK, _msg("no1", ITZEL, ITZEL_GROUP, "2026-08-22T20:00:00.000Z")]
        facts = dict(ASK_FACTS)
        facts["no1"] = {"facts": [_fact("decline", "2026-09-02", "Itzel", conf=0.9)]}
        f = _merged(_run(_data(msgs, facts)))
        self.assertEqual(f["outreach"]["state"], "declined")
        self.assertIn("Itzel declined 2026-08-22", f["why"])
        self.assertEqual(f["decision"], "investigate")

    def test_confirm_defers_to_existing_approve_path(self):
        msgs = [ASK, _msg("yes1", ITZEL, ITZEL_GROUP, "2026-08-22T20:00:00.000Z")]
        facts = dict(ASK_FACTS)
        facts["yes1"] = {"facts": [_fact("confirm", "2026-09-02", "Itzel", conf=0.9)]}
        res = _run(_data(msgs, facts))
        self.assertFalse(any(f["detector"] == "outreach" for f in res["findings_raw"]))
        f = _merged(res)
        self.assertNotIn("outreach", f)

    def test_old_decline_before_the_ask_does_not_count_as_an_answer(self):
        msgs = [_msg("no0", ITZEL, ITZEL_GROUP, "2026-05-05T10:00:00.000Z"), ASK]
        facts = dict(ASK_FACTS)
        facts["no0"] = {"facts": [_fact("decline", "2026-09-02", "Itzel", conf=0.9)]}
        f = _merged(_run(_data(msgs, facts)))
        self.assertEqual(f["outreach"]["state"], "asked")

    def test_group_label_supplies_the_cleaner_when_host_did_not_name_her(self):
        facts = {"ask1": {"facts": [_fact("schedule_assertion", "2026-09-02", None)]}}
        f = _merged(_run(_data([ASK], facts)))
        self.assertEqual(f["outreach"]["state"], "asked")
        self.assertEqual(f["outreach"]["cleaner"], "Itzel")

    def test_two_cleaners_asked_lists_both_longest_wait_first(self):
        msgs = [_msg("askD", HOST, DARYA_GROUP, "2026-08-20T10:00:00.000Z"), ASK]
        facts = dict(ASK_FACTS)
        facts["askD"] = {"facts": [_fact("schedule_assertion", "2026-09-02", "Darya")]}
        f = _merged(_run(_data(msgs, facts)))
        o = f["outreach"]
        self.assertEqual(o["state"], "asked")
        self.assertEqual(o["cleaner"], "Darya, Itzel")
        self.assertEqual(o["asked_on"], "2026-08-20")
        self.assertEqual(o["days_waiting"], 3)
        self.assertEqual(f["decision"], "investigate")  # 3 days ≥ patience

    def test_one_declined_one_waiting_is_asked(self):
        msgs = [_msg("askD", HOST, DARYA_GROUP, "2026-08-20T10:00:00.000Z"),
                _msg("noD", DARYA, DARYA_GROUP, "2026-08-20T12:00:00.000Z"), ASK]
        facts = dict(ASK_FACTS)
        facts["askD"] = {"facts": [_fact("schedule_assertion", "2026-09-02", "Darya")]}
        facts["noD"] = {"facts": [_fact("decline", "2026-09-02", "Darya", conf=0.9)]}
        f = _merged(_run(_data(msgs, facts)))
        self.assertEqual(f["outreach"]["state"], "asked")
        self.assertEqual(f["outreach"]["cleaner"], "Itzel")


class OutreachGates(unittest.TestCase):
    def test_tentative_host_fact_is_not_an_ask(self):
        facts = {"ask1": {"facts": [_fact("schedule_assertion", "2026-09-02", "Itzel", tentative=True)]}}
        f = _merged(_run(_data([ASK], facts)))
        self.assertEqual(f["outreach"]["state"], "unasked")

    def test_below_outreach_floor_is_not_an_ask(self):
        facts = {"ask1": {"facts": [_fact("schedule_assertion", "2026-09-02", "Itzel", conf=0.3)]}}
        f = _merged(_run(_data([ASK], facts)))
        self.assertEqual(f["outreach"]["state"], "unasked")

    def test_cleaner_authored_assertion_is_not_an_ask(self):
        msg = _msg("ask1", ITZEL, ITZEL_GROUP, "2026-08-22T15:41:55.000Z")
        f = _merged(_run(_data([msg], ASK_FACTS)))
        self.assertEqual(f["outreach"]["state"], "unasked")

    def test_stale_ask_from_a_previous_season_is_ignored(self):
        msg = _msg("ask1", HOST, ITZEL_GROUP, "2026-04-13T11:03:00.000Z")  # 142 days before
        f = _merged(_run(_data([msg], ASK_FACTS)))
        self.assertEqual(f["outreach"]["state"], "unasked")

    def test_no_finding_beyond_the_urgent_window(self):
        bookings = {UID: {"start": "2026-10-17", "end": "2026-10-19", "cleaner": None,
                          "status": "active", "type": "airbnb"}}
        res = reconcile.run(_data([], {}, bookings),
                            [_drift_item(end="2026-10-19")], today=TODAY)
        self.assertFalse(any(f["detector"] == "outreach" for f in res["findings_raw"]))

    def test_assigned_booking_gets_no_outreach(self):
        bookings = {UID: {"start": "2026-08-31", "end": "2026-09-02", "cleaner": "Itzel",
                          "status": "active", "type": "airbnb"}}
        res = reconcile.run(_data([ASK], ASK_FACTS, bookings), [], today=TODAY)
        self.assertFalse(any(f["detector"] == "outreach" for f in res["findings_raw"]))

    def test_fail_closed_floor_never_waits_inside_seven_days(self):
        bookings = {UID: {"start": "2026-08-26", "end": "2026-08-28", "cleaner": None,
                          "status": "active", "type": "airbnb"}}
        facts = {"ask1": {"facts": [_fact("schedule_assertion", "2026-08-28", "Itzel")]}}
        res = reconcile.run(_data([ASK], facts, bookings),
                            [_drift_item(end="2026-08-28")], today=TODAY)
        f = _merged(res)
        self.assertEqual(f["outreach"]["state"], "asked")
        self.assertEqual(f["decision"], "investigate")

    def test_detector_never_reads_message_text(self):
        msg = dict(ASK); msg["text"] = None
        f = _merged(_run(_data([msg], ASK_FACTS)))
        self.assertEqual(f["outreach"]["state"], "asked")


class OutreachProjection(unittest.TestCase):
    def test_projection_carries_closed_shape_only(self):
        f = _merged(_run(_data([ASK], ASK_FACTS)))
        p = reconcile.project_finding_for_vps(f, {})
        self.assertEqual(set(p["outreach"]), {"state", "cleaner", "asked_on", "days_waiting"})
        self.assertEqual(p["outreach"]["state"], "asked")
        self.assertNotIn("patience", p["outreach"])
        self.assertNotIn("evidence", p)
        self.assertEqual(p["decision"], "wait")

    def test_projection_is_none_when_absent_or_malformed(self):
        self.assertIsNone(reconcile.project_finding_for_vps({"id": "x"}, {})["outreach"])
        self.assertIsNone(reconcile.project_finding_for_vps(
            {"id": "x", "outreach": {"state": "bogus"}}, {})["outreach"])

    def test_allowlist_names_outreach(self):
        self.assertIn("outreach", reconcile.VPS_FINDING_FIELDS)

    def test_wait_ranks_between_investigate_and_observe(self):
        r = reconcile.DECISION_RANK
        self.assertLess(r["investigate"], r["wait"])
        self.assertLess(r["wait"], r["observe"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
