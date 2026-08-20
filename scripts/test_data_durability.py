"""`data.json` must be written atomically and must never fail open when it vanishes.

Both defects found 2026-08-20. Every sidecar in this add-on already wrote
temp-then-rename — `bridge_watchdog.save_state` even carries the docstring
explaining why — while `data.json`, the one irreplaceable file, used a plain
truncating write. And `load_data` returned an empty store for a MISSING file,
which the next `save_data` then committed as authoritative; `sync_ical` would
repopulate the bookings from the feed and the dashboard would come back looking
healthy with every commitment silently gone.

Same AST-extraction harness as test_cleaning_match.py: the real function bodies
are pulled out of app.py and exec'd against injected fakes, so a rename or
reshape fails loudly here instead of passing against a stale copy.

Run: python3 scripts/test_data_durability.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"


def _extract(names, ns):
    src = (APP_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names}
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"symbol(s) not found in app.py: {sorted(missing)}")
    for name in names:
        exec(compile(ast.Module(body=[found[name]], type_ignores=[]), "app.py", "exec"), ns)
    return ns


class DataLayerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ns = {
            "json": json, "os": os, "datetime": datetime, "Path": Path,
            "DATA_FILE": self.tmp / "data.json",
            "INIT_MARKER": self.tmp / ".data-initialized",
            "GCAL_ENABLED": False,
            "needs_notify": lambda b: False,
            "threading": None,
            "print": lambda *a, **k: None,
        }
        _extract(["DataVanished", "load_data", "save_data"], self.ns)
        self.load = self.ns["load_data"]
        self.save = self.ns["save_data"]
        self.DataVanished = self.ns["DataVanished"]
        self.DATA_FILE = self.ns["DATA_FILE"]
        self.MARKER = self.ns["INIT_MARKER"]

    # ── atomicity ────────────────────────────────────────────────────────
    def test_save_leaves_no_temp_file(self):
        self.save({"bookings": {"u1": {"end": "2026-09-10"}}})
        self.assertEqual(list(self.tmp.glob("data.json.tmp*")), [],
                         "a temp file survived a successful save")

    def test_save_round_trips(self):
        self.save({"bookings": {"u1": {"end": "2026-09-10", "cleaner": "Itzel"}}})
        self.assertEqual(self.load()["bookings"]["u1"]["cleaner"], "Itzel")

    def test_failed_serialize_does_not_destroy_existing_data(self):
        """The point of temp+rename: a write that dies mid-serialize must not
        take the previous contents with it. A truncating write would leave a
        half-file here, and `load_data` would raise on every subsequent read."""
        self.save({"bookings": {"keep": {"end": "2026-10-01"}}})
        before = self.DATA_FILE.read_text()

        class Boom:
            def __repr__(self):
                raise ValueError("boom")

        with self.assertRaises(Exception):
            self.save({"bookings": {"bad": Boom()}, "nested": {"k": [Boom()]}})
        self.assertEqual(self.DATA_FILE.read_text(), before,
                         "a failed save mutated data.json")
        self.assertEqual(list(self.tmp.glob("data.json.tmp*")), [],
                         "a failed save left its temp file behind")

    # ── fail-closed on a vanished store ──────────────────────────────────
    def test_fresh_install_still_initialises(self):
        self.assertEqual(self.load()["bookings"], {})
        self.assertFalse(self.MARKER.exists(),
                         "the marker must not appear before the first save")

    def test_marker_is_written_on_first_save(self):
        self.save({"bookings": {}})
        self.assertTrue(self.MARKER.exists())

    def test_vanished_store_raises_instead_of_returning_empty(self):
        self.save({"bookings": {"u1": {"end": "2026-09-10"}}})
        self.DATA_FILE.unlink()
        with self.assertRaises(self.DataVanished) as ctx:
            self.load()
        self.assertIn("/internal/restore", str(ctx.exception),
                      "the error must name the recovery route")

    def test_deleting_the_marker_permits_a_deliberate_fresh_start(self):
        self.save({"bookings": {"u1": {}}})
        self.DATA_FILE.unlink()
        self.MARKER.unlink()
        self.assertEqual(self.load()["bookings"], {})

    def test_corrupt_store_still_raises_loudly(self):
        """Unchanged behaviour, asserted so nobody 'helpfully' makes it lenient:
        a truncated file must raise, not degrade to empty."""
        self.save({"bookings": {"u1": {}}})
        self.DATA_FILE.write_text('{"bookings": {"u1"')
        with self.assertRaises(json.JSONDecodeError):
            self.load()


if __name__ == "__main__":
    unittest.main(verbosity=2)
