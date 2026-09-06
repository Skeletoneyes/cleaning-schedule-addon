"""Unit tests for the structured statement write path.

Replaces the transcript pipeline: a human and a model read the conversation
and emit structured statements; this endpoint validates, deduplicates and
writes, and interprets nothing.

The dedup here is what the one-clock fix (2026-09-06) made possible. Live rows
carry WhatsApp's message id, backfilled rows a content hash, and the two
namespaces never met — so re-ingesting a period that had partly come through
live silently duplicated it. The blocker was never the ids; it was that the
two sides kept timestamps on different clocks, seven hours apart.

Run: python3 scripts/test_statements.py
"""

import ast
import sys
import unittest
import zoneinfo
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"
sys.path.insert(0, str(APP_DIR))
import gcal as _gcal

NS = {
    "datetime": datetime, "timezone": timezone, "date": date,
    "zoneinfo": zoneinfo, "hashlib": __import__("hashlib"),
    "LOCAL_ZONE": zoneinfo.ZoneInfo(_gcal.LOCAL_TZ),
    "STATEMENT_MATCH_TOLERANCE_SEC": 120,
}
_tree = ast.parse((APP_DIR / "app.py").read_text(encoding="utf-8"))
_want = ["_ts_utc", "_utc_iso", "_statement_id", "_norm_text", "_already_have"]
_found = {n.name: n for n in _tree.body
          if isinstance(n, ast.FunctionDef) and n.name in _want}
assert set(_found) == set(_want), f"missing from app.py: {sorted(set(_want) - set(_found))}"
for _n in _want:
    exec(compile(ast.Module(body=[_found[_n]], type_ignores=[]), "app.py", "exec"), NS)

statement_id = NS["_statement_id"]
norm_text = NS["_norm_text"]
already_have = NS["_already_have"]
ts_utc = NS["_ts_utc"]

GROUP = "test-group@g.us"


class IdentityTests(unittest.TestCase):
    def test_the_same_statement_always_gets_the_same_id(self):
        """Re-pasting a transcript must be a no-op, not a duplicate."""
        a = statement_id("2026-09-03T21:00:00Z", "Darya", "I can do the 18th")
        b = statement_id("2026-09-03T21:00:00Z", "Darya", "I can do the 18th")
        self.assertEqual(a, b)

    def test_different_words_give_different_ids(self):
        a = statement_id("2026-09-03T21:00:00Z", "Darya", "I can do the 18th")
        b = statement_id("2026-09-03T21:00:00Z", "Darya", "I can't do the 18th")
        self.assertNotEqual(a, b)

    def test_ids_are_namespaced(self):
        self.assertTrue(statement_id("2026-09-03T21:00:00Z", "D", "x").startswith("stmt-"))


class NormalisationTests(unittest.TestCase):
    def test_whitespace_and_case_do_not_defeat_a_match(self):
        """A transcript export and a live capture of the same message differ in
        exactly these ways and in no meaningful one."""
        self.assertEqual(norm_text("  I CAN do   the 18th "), norm_text("i can do the 18th"))

    def test_none_is_safe(self):
        self.assertEqual(norm_text(None), "")


class CrossNamespaceDedupTests(unittest.TestCase):
    """The point of the whole exercise."""

    def _live(self, ts, text, group=GROUP):
        return {"id": "3AFA38A3CD309AC3160E", "group": group,
                "text": text, "timestamp": ts}

    def test_a_live_capture_of_the_same_message_is_recognised(self):
        data = {"messages": [self._live("2026-09-03T21:00:00Z", "I can do the 18th")]}
        hit = already_have(data, GROUP, ts_utc("2026-09-03T21:00:30Z"), "I can do the 18th")
        self.assertEqual(hit, "3AFA38A3CD309AC3160E",
                         "same group, same words, 30s apart — one message")

    def test_a_naive_local_row_still_matches_after_migration(self):
        """Pre-migration rows were naive local. Post-migration they are UTC.
        `_ts_utc` reads either, so a mixed store still dedupes."""
        data = {"messages": [self._live("2026-09-03T14:00:00", "I can do the 18th")]}
        hit = already_have(data, GROUP, ts_utc("2026-09-03T21:00:00Z"), "I can do the 18th")
        self.assertIsNotNone(hit, "14:00 PDT and 21:00Z are the same moment")

    def test_the_same_words_far_apart_are_two_messages(self):
        """People repeat themselves. 'ok' on Tuesday is not 'ok' on Friday."""
        data = {"messages": [self._live("2026-09-03T21:00:00Z", "ok")]}
        hit = already_have(data, GROUP, ts_utc("2026-09-05T21:00:00Z"), "ok")
        self.assertIsNone(hit)

    def test_the_same_words_in_another_group_are_not_a_match(self):
        data = {"messages": [self._live("2026-09-03T21:00:00Z", "ok", group="other@g.us")]}
        self.assertIsNone(already_have(data, GROUP, ts_utc("2026-09-03T21:00:10Z"), "ok"))

    def test_an_unparseable_stored_timestamp_is_skipped_not_matched(self):
        data = {"messages": [self._live("garbage", "I can do the 18th")]}
        self.assertIsNone(already_have(data, GROUP, ts_utc("2026-09-03T21:00:00Z"),
                                       "I can do the 18th"))

    def test_an_empty_store_is_not_an_error(self):
        for data in ({}, {"messages": []}, {"messages": None}):
            self.assertIsNone(already_have(data, GROUP,
                                           ts_utc("2026-09-03T21:00:00Z"), "hi"))


class ContractTests(unittest.TestCase):
    """Source assertions — the route needs Flask, which is not installed here."""

    def _src(self):
        return (APP_DIR / "app.py").read_text(encoding="utf-8")

    def test_the_batch_is_all_or_nothing(self):
        """A half-applied backfill cannot be told from a complete one by
        looking at the data afterwards."""
        src = self._src()
        self.assertIn('"error": "validation failed, nothing written"', src)

    def test_it_refuses_a_kind_it_does_not_know(self):
        self.assertIn("facts_mod.VALID_KINDS", self._src())

    def test_text_is_mandatory_because_it_is_the_provenance(self):
        self.assertIn("text is required — it is the provenance for the fact", self._src())

    def test_it_never_writes_a_booking(self):
        """Statements go in, detectors read them, findings come out, a human
        decides. Writing bookings here would bypass the reconciler, which is
        the only second opinion on whether the reading was right."""
        fn = next(n for n in ast.parse(self._src()).body
                  if isinstance(n, ast.FunctionDef) and n.name == "ingest_statements")
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        called |= {n.func.attr for n in ast.walk(fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        # Checked as CALLS, not as words — the docstring says "does not touch
        # bookings", and a substring search would flag its own explanation.
        for forbidden in ("save_bookings", "sync_to_gcal", "assign_cleaner",
                          "ack_notified", "_record_change"):
            self.assertNotIn(forbidden, called,
                             f"the statements path must not call {forbidden}")
        self.assertIn("save_data", called, "it does write the message store")

    def test_it_is_authenticated_like_the_other_internal_routes(self):
        src = self._src()
        start = src.index("def ingest_statements()")
        self.assertIn("_require_local_or_secret()", src[start:start + 3000])


if __name__ == "__main__":
    unittest.main(verbosity=2)
