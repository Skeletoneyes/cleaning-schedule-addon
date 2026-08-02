"""Unit tests for parser context windows and the cross-chat facts digest.

Covers the 2026-08-02 change: fact extraction could only ever see one chat,
while its prompt told the model it was seeing all of them — so a cleaning
arranged across both threads (one cleaner released in hers, another asked in
hers) was invisible to the conflict detector.

Same harness as test_attestation.py: pull the real function source out of
app.py with `ast` and exec it against injected fakes, so a rename or reshape
fails loudly here instead of passing against a stale copy.

Run: python3 scripts/test_parser_context.py
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

import facts as facts_mod  # noqa: E402  (pure stdlib + requests)


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
    "FACTS_HISTORY_WINDOW": 30, "FACTS_HISTORY_DAYS": 45, "FACTS_HISTORY_MAX": 120,
    "PARSE_HISTORY_WINDOW": 50, "PARSE_HISTORY_DAYS": 30, "PARSE_HISTORY_MAX": 150,
    "CROSS_FACTS_BACK_DAYS": 7, "CROSS_FACTS_FWD_DAYS": 150, "CROSS_FACTS_MAX_LINES": 80,
}
_extract(["_msg_day", "_window_by_count_or_days", "_facts_history",
          "_cross_chat_facts", "_parse_history"], NS)

NS2 = _extract(["_sender_roles"], dict(NS))

msg_day = NS["_msg_day"]
window = NS["_window_by_count_or_days"]
facts_history = NS["_facts_history"]
cross_chat_facts = NS["_cross_chat_facts"]

ITZEL = "120363285451054712@g.us"
DARIA = "120363410469116316@g.us"


def m(day, group=ITZEL, text="x", mid=None, hour=12):
    return {"id": mid or f"{group[:6]}-{day}-{hour}", "group": group,
            "timestamp": f"{day}T{hour:02d}:00:00", "text": text}


class MsgDayTests(unittest.TestCase):
    def test_handles_both_stored_timestamp_shapes(self):
        """Live messages carry `...T21:08:38.000Z`; backfilled ones don't."""
        self.assertEqual(msg_day({"timestamp": "2026-07-28T21:08:38.000Z"}), date(2026, 7, 28))
        self.assertEqual(msg_day({"timestamp": "2026-07-28T14:08:00"}), date(2026, 7, 28))

    def test_garbage_is_none_not_an_exception(self):
        for bad in (None, "", "nope", "2026-13-99T00:00:00"):
            self.assertIsNone(msg_day({"timestamp": bad}))


class WindowTests(unittest.TestCase):
    def test_quiet_chat_gets_time_based_reach(self):
        """Daria: ~96 messages since March. A count window would reach back
        months, but the point is that the DAYS window must win when it is
        larger, not that either is always right."""
        target = m("2026-08-02")
        prior = [m(f"2026-07-{d:02d}") for d in range(20, 32)]  # 12 msgs in 12 days
        got = window(prior, count=3, days=45, hard_max=120, target=target)
        self.assertEqual(len(got), 12, "45 days should beat a 3-message count")

    def test_the_count_is_a_floor_never_a_ceiling(self):
        """The count must never SHRINK the window — that was the old bug, where
        a busy chat's fixed count cut its reach to about a fortnight. When many
        messages fall inside the day window, all of them are kept (up to
        hard_max); when few do, the count still guarantees a minimum."""
        target = m("2026-08-02")

        # Busy: 80 messages in one day, count of 50 — the day window is larger.
        busy = [m("2026-08-01", hour=h % 24, mid=f"busy-{h}") for h in range(80)]
        self.assertEqual(len(window(busy, count=50, days=1, hard_max=150, target=target)), 80)

        # Sparse: 5 messages spread over a year, tiny day window — count wins.
        sparse = [m(f"2025-{mo:02d}-01", mid=f"sp-{mo}") for mo in range(1, 6)]
        self.assertEqual(len(window(sparse, count=50, days=1, hard_max=150, target=target)), 5)

    def test_hard_max_caps_a_runaway_window(self):
        target = m("2026-08-02")
        prior = [m(f"2026-07-{d:02d}", hour=h, mid=f"c-{d}-{h}")
                 for d in range(1, 31) for h in range(10)]
        got = window(prior, count=50, days=45, hard_max=120, target=target)
        self.assertEqual(len(got), 120, "token cost must stay bounded")

    def test_returns_the_most_recent_not_the_oldest(self):
        target = m("2026-08-02")
        prior = [m(f"2026-07-{d:02d}") for d in range(1, 31)]
        got = window(prior, count=3, days=0, hard_max=120, target=target)
        self.assertEqual([g["timestamp"][:10] for g in got],
                         ["2026-07-28", "2026-07-29", "2026-07-30"])

    def test_output_is_chronological(self):
        target = m("2026-08-02")
        prior = list(reversed([m(f"2026-07-{d:02d}") for d in range(20, 30)]))
        got = window(prior, count=50, days=45, hard_max=120, target=target)
        self.assertEqual(got, sorted(got, key=lambda x: x["timestamp"]))


class FactsHistoryTests(unittest.TestCase):
    def test_stays_within_one_chat(self):
        """Raw history remains same-chat — cross-chat arrives as extracted
        facts instead, because this runs on every message."""
        target = m("2026-08-02", group=DARIA)
        prior = [m("2026-08-01", group=ITZEL), m("2026-08-01", group=DARIA)]
        got = facts_history(prior, target)
        self.assertTrue(all(g["group"] == DARIA for g in got))
        self.assertEqual(len(got), 1)

    def test_excludes_messages_after_the_target(self):
        target = m("2026-07-15", group=DARIA)
        prior = [m("2026-07-20", group=DARIA), m("2026-07-10", group=DARIA)]
        got = facts_history(prior, target)
        self.assertEqual([g["timestamp"][:10] for g in got], ["2026-07-10"])


def _facts_data(extracted_at="2026-03-30T10:00:00", target_date="2026-08-03",
                cleaner="Itzel", kind="confirm", time_="17:00", group=ITZEL):
    return {
        "messages": [{"id": "msg1", "group": group, "timestamp": extracted_at}],
        "group_labels": {ITZEL: "Itzel", DARIA: "Daria"},
        "message_facts": {
            "msg1": {"extracted_at": extracted_at, "facts": [{
                "kind": kind, "target_date": target_date, "target_time": time_,
                "cleaner": cleaner, "confidence": 0.95,
            }]},
        },
    }


class CrossChatFactsTests(unittest.TestCase):
    def test_the_aug_3_case(self):
        """The scenario that motivated this: Itzel committed to Aug 3 back in
        March in her chat; a message now arriving in Daria's chat must be able
        to see that commitment."""
        data = _facts_data()
        target = m("2026-08-02", group=DARIA)
        rows = cross_chat_facts(data, target)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cleaner"], "Itzel")
        self.assertEqual(rows[0]["date"], "2026-08-03")
        self.assertEqual(rows[0]["time"], "17:00")
        self.assertEqual(rows[0]["chat"], "Itzel")

    def test_excludes_facts_from_the_targets_own_chat(self):
        """Those are already in the raw history — repeating them wastes tokens
        and invites the model to double-count."""
        data = _facts_data(group=DARIA)
        rows = cross_chat_facts(data, m("2026-08-02", group=DARIA))
        self.assertEqual(rows, [])

    def test_far_future_and_stale_dates_are_excluded(self):
        target = m("2026-08-02", group=DARIA)
        self.assertEqual(cross_chat_facts(_facts_data(target_date="2027-06-01"), target), [])
        self.assertEqual(cross_chat_facts(_facts_data(target_date="2026-06-01"), target), [])

    def test_a_march_commitment_for_august_is_in_range(self):
        """Commitments here are routinely made months ahead — the horizon is
        on the CLEANING date, never on when it was said."""
        data = _facts_data(extracted_at="2026-03-30T10:00:00", target_date="2026-08-03")
        rows = cross_chat_facts(data, m("2026-08-02", group=DARIA))
        self.assertEqual(len(rows), 1)

    def test_most_recent_statement_wins(self):
        data = _facts_data()
        data["messages"].append({"id": "msg2", "group": ITZEL, "timestamp": "2026-07-21T10:00:00"})
        data["message_facts"]["msg2"] = {
            "extracted_at": "2026-07-21T10:00:00",
            "facts": [{"kind": "confirm", "target_date": "2026-08-03",
                       "target_time": "11:00", "cleaner": "Itzel", "confidence": 0.9}],
        }
        rows = cross_chat_facts(data, m("2026-08-02", group=DARIA))
        self.assertEqual(len(rows), 1, "same date+cleaner+kind collapses to one row")
        self.assertEqual(rows[0]["time"], "11:00", "the July statement supersedes March")

    def test_unclear_facts_are_dropped(self):
        data = _facts_data(kind="unclear")
        self.assertEqual(cross_chat_facts(data, m("2026-08-02", group=DARIA)), [])

    def test_missing_facts_layer_is_not_an_error(self):
        for data in ({}, {"messages": []}, {"message_facts": {}}):
            self.assertEqual(cross_chat_facts(data, m("2026-08-02", group=DARIA)), [])

    def test_row_count_is_capped(self):
        data = {"messages": [], "group_labels": {}, "message_facts": {}}
        for i in range(90):
            mid = f"m{i}"
            data["messages"].append({"id": mid, "group": ITZEL, "timestamp": "2026-07-01T10:00:00"})
            data["message_facts"][mid] = {
                "extracted_at": f"2026-07-01T10:00:{i:02d}",
                "facts": [{"kind": "confirm", "target_date": "2026-08-03",
                           "target_time": "10:00", "cleaner": f"C{i}", "confidence": 0.9}],
            }
        rows = cross_chat_facts(data, m("2026-08-02", group=DARIA))
        self.assertLessEqual(len(rows), 80)


class PromptTests(unittest.TestCase):
    def test_prompt_no_longer_claims_to_see_all_groups(self):
        """It said 'Prior messages across all groups' while the code filtered
        to one — the model was told its view was complete when it wasn't."""
        p = facts_mod._build_prompt(
            {"group": DARIA, "timestamp": "2026-08-02T09:59:00", "text": "ok Monday"},
            [], ["Itzel", "Daria"], {DARIA: "Daria"}, None,
        )
        self.assertNotIn("Prior messages across all groups", p)
        self.assertIn("THIS chat only", p)

    def test_cross_facts_render_into_the_prompt(self):
        rows = [{"date": "2026-08-03", "cleaner": "Itzel", "kind": "confirm",
                 "time": "17:00", "chat": "Itzel", "stated": "2026-03-30"}]
        p = facts_mod._build_prompt(
            {"group": DARIA, "timestamp": "2026-08-02T09:59:00", "text": "ok Monday"},
            [], ["Itzel", "Daria"], {DARIA: "Daria"}, rows,
        )
        self.assertIn("2026-08-03", p)
        self.assertIn("Itzel", p)
        self.assertIn("17:00", p)

    def test_empty_cross_facts_says_so_rather_than_rendering_nothing(self):
        """A blank section reads as 'no other chats exist'; the explicit line
        distinguishes 'checked, nothing found'."""
        self.assertIn("no established facts", facts_mod._format_cross_facts([]))

    def test_prompt_version_was_bumped(self):
        self.assertNotEqual(facts_mod.FACTS_PROMPT_VERSION, "facts-v2",
                            "prompt changed — version must move or reprocess is a no-op")




class SenderRoleTests(unittest.TestCase):
    """The role tag drives fact KIND: schedule_assertion is host-only, confirm
    is cleaner-only. Mislabel the speaker and a cleaner's acceptance is filed
    as a host assertion, which `contested_cleaner` never looks at."""

    ROLES = {
        "135712527638545@lid": "cleaner:Daria",
        "192466460373222@lid": "cleaner:Itzel",
        "16472343440@s.whatsapp.net": "host",
    }

    def test_jid_map_wins_over_the_substring_guess(self):
        self.assertEqual(
            facts_mod._sender_role("135712527638545@lid", ["Daria", "Itzel"],
                                   "135712527638545@lid", self.ROLES),
            "cleaner:Daria",
        )

    def test_the_daria_regression(self):
        """Her exported sender name is a bare phone number, so the substring
        test called her the host in her own chat."""
        label, jid = "+380 97 550 6538", "135712527638545@lid"
        self.assertEqual(
            facts_mod._sender_role(label, ["Daria", "Itzel"], jid, None), "host",
            "documents the old behaviour",
        )
        self.assertEqual(
            facts_mod._sender_role(label, ["Daria", "Itzel"], jid, self.ROLES),
            "cleaner:Daria",
        )

    def test_live_jids_also_failed_the_substring_test(self):
        """Not just backfill — no live JID contains a cleaner's name either."""
        self.assertEqual(
            facts_mod._sender_role("192466460373222@lid", ["Itzel"], None, None), "host")
        self.assertEqual(
            facts_mod._sender_role("192466460373222@lid", ["Itzel"],
                                   "192466460373222@lid", self.ROLES), "cleaner:Itzel")

    def test_host_jid_resolves_to_host(self):
        self.assertEqual(
            facts_mod._sender_role("Josh Mohan", ["Daria"],
                                   "16472343440@s.whatsapp.net", self.ROLES), "host")

    def test_falls_back_to_substring_for_unknown_senders(self):
        """Pasted transcripts whose sender is in neither list must still work."""
        self.assertEqual(
            facts_mod._sender_role("Itzel Cleaner", ["Itzel"], "backfill:itzel", self.ROLES),
            "cleaner:Itzel")

    def test_unknown_with_no_label_is_unknown_not_host(self):
        self.assertEqual(facts_mod._sender_role("", ["Daria"], "nope@lid", self.ROLES), "unknown")


class SenderRolesMapTests(unittest.TestCase):
    def test_builds_from_stored_jid_data(self):
        build = NS2["_sender_roles"]
        roles = build({
            "host_jids": ["16472343440@s.whatsapp.net"],
            "cleaner_jids": {"Daria": ["135712527638545@lid"],
                             "Itzel": ["backfill:itzel-cleaner", "192466460373222@lid"]},
        })
        self.assertEqual(roles["16472343440@s.whatsapp.net"], "host")
        self.assertEqual(roles["135712527638545@lid"], "cleaner:Daria")
        self.assertEqual(roles["192466460373222@lid"], "cleaner:Itzel")
        self.assertEqual(roles["backfill:itzel-cleaner"], "cleaner:Itzel")

    def test_missing_keys_are_not_an_error(self):
        build = NS2["_sender_roles"]
        for data in ({}, {"host_jids": None}, {"cleaner_jids": {"X": None}}):
            self.assertIsInstance(build(data), dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
