"""Unit tests for the cleaner rename.

"Daria" was a misspelling of Darya's actual name. Her name is a join key in
five separate stores, and the detectors compare cleaner names by string
equality — so a PARTIAL rename is strictly worse than none: every booking she
has touched would report a contested cleaner against her own facts.

Run: python3 scripts/test_rename_cleaner.py
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"


def _extract(names, ns):
    tree = ast.parse((APP_DIR / "app.py").read_text(encoding="utf-8"))
    found = {n.name: n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in names}
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"not found in app.py: {sorted(missing)}")
    for n in names:
        exec(compile(ast.Module(body=[found[n]], type_ignores=[]), "app.py", "exec"), ns)
    return ns


NS = _extract(["_rename_cleaner_in_data"], {})
rename = NS["_rename_cleaner_in_data"]

DARIA_JID = "135712527638545@lid"
DARIA_GROUP = "120363410469116316@g.us"


def _data():
    return {
        "bookings": {
            "b1": {"cleaner": "Daria", "end": "2026-08-14",
                   "cleaner_commitment": {"cleaner": "Daria", "date": "2026-08-14"}},
            "b2": {"cleaner": "Itzel", "end": "2026-08-03",
                   "cleaner_commitment": {"cleaner": "Itzel", "date": "2026-08-03"}},
            "b3": {"cleaner": "Daria", "end": "2026-08-24", "notes": "Daria confirmed date"},
        },
        "cleaner_jids": {"Daria": [DARIA_JID], "Itzel": ["192466460373222@lid"]},
        "group_labels": {DARIA_GROUP: "Daria", "x@g.us": "Itzel"},
        "message_facts": {
            "m1": {"facts": [{"kind": "confirm", "cleaner": "Daria", "target_date": "2026-08-14"},
                             {"kind": "confirm", "cleaner": "Itzel", "target_date": "2026-08-03"}]},
            "m2": {"facts": [{"kind": "decline", "cleaner": "Daria", "target_date": "2026-09-01"}]},
        },
    }


class RenameTests(unittest.TestCase):
    def test_renames_every_join_key_together(self):
        d = _data()
        counts = rename(d, "Daria", "Darya")
        self.assertEqual(counts, {"bookings": 2, "commitments": 1, "cleaner_jids": 1,
                                  "group_labels": 1, "facts": 2})

    def test_no_join_key_is_left_behind(self):
        """The failure that matters: one store still saying Daria while another
        says Darya makes the reconciler see two different people."""
        d = _data()
        rename(d, "Daria", "Darya")
        blob = str(d)
        # notes are deliberately preserved, so exclude them from the sweep
        for b in d["bookings"].values():
            b.pop("notes", None)
        self.assertNotIn("Daria", str(d))
        self.assertIn("Daria", blob, "sanity: the fixture really did contain it")

    def test_the_jid_follows_the_new_name(self):
        d = _data()
        rename(d, "Daria", "Darya")
        self.assertEqual(d["cleaner_jids"]["Darya"], [DARIA_JID])
        self.assertNotIn("Daria", d["cleaner_jids"])

    def test_group_label_follows(self):
        d = _data()
        rename(d, "Daria", "Darya")
        self.assertEqual(d["group_labels"][DARIA_GROUP], "Darya")

    def test_other_cleaners_are_untouched(self):
        d = _data()
        rename(d, "Daria", "Darya")
        self.assertEqual(d["bookings"]["b2"]["cleaner"], "Itzel")
        self.assertEqual(d["cleaner_jids"]["Itzel"], ["192466460373222@lid"])

    def test_notes_are_preserved_as_written(self):
        """Notes record what was said at the time. Rewriting them to match a
        later correction turns a record into a reconstruction."""
        d = _data()
        rename(d, "Daria", "Darya")
        self.assertEqual(d["bookings"]["b3"]["notes"], "Daria confirmed date")

    def test_is_idempotent(self):
        d = _data()
        rename(d, "Daria", "Darya")
        second = rename(d, "Daria", "Darya")
        self.assertEqual(sum(second.values()), 0, "re-running must be a no-op")

    def test_half_applied_rename_converges(self):
        """If a previous attempt renamed some stores and died, running again
        must merge rather than clobber the JIDs already moved."""
        d = _data()
        d["cleaner_jids"] = {"Daria": [DARIA_JID], "Darya": ["other@lid"]}
        rename(d, "Daria", "Darya")
        self.assertEqual(sorted(d["cleaner_jids"]["Darya"]), sorted(["other@lid", DARIA_JID]))

    def test_missing_stores_do_not_raise(self):
        for d in ({}, {"bookings": {}}, {"cleaner_jids": None}, {"message_facts": {}}):
            self.assertIsInstance(rename(d, "Daria", "Darya"), dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
