"""The digest payload archive: the replay record for ISC-358/359.

Before 1.40.0 nothing retained past VPS payloads — `reconciler_last.json` is
overwritten per run and the VPS keeps only the latest receipt — so no claim
about what the digest did on a past night could ever be checked. The archive
appends one JSON line per outgoing payload (delivered or not), retains a
trailing 30 days, rewrites through a tmp file so a torn write cannot destroy
history, and drops-but-counts corrupt lines rather than silently losing
nights.

Extraction pattern per test_attestation.py: `_archive_digest_payload` lives in
app.py (flask-importing), so pull the real source via ast and exec against
injected fakes — a rename fails loudly here instead of passing against a copy.

Run: python3 scripts/test_digest_archive.py
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"


def _extract(names, ns):
    src = (APP_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names}
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"app.py is missing expected function(s): {sorted(missing)}")
    mod = ast.Module(body=[found[n] for n in names], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<app-pure>", "exec"), ns)
    return ns


NOW = datetime(2026, 8, 21, 20, 0, 0)


def make_fn():
    ns = {
        "datetime": datetime,
        "timedelta": timedelta,
        "json": json,
        "Path": Path,
        "print": lambda *a, **k: None,
        "DIGEST_ARCHIVE_RETENTION_DAYS": 30,
    }
    _extract(["_archive_digest_payload"], ns)
    return ns["_archive_digest_payload"]


def payload(n=1):
    return {"ts": f"2026-08-2{n}T08:00:00", "heartbeat": True, "findings": []}


class DigestArchive(unittest.TestCase):
    def setUp(self):
        self.fn = make_fn()
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "digest_archive.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def read_lines(self):
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def test_two_consecutive_runs_leave_two_lines(self):
        """The ISC-358 probe, verbatim."""
        self.fn(self.path, payload(0), True, now=NOW)
        self.fn(self.path, payload(1), True, now=NOW + timedelta(days=1))
        lines = self.read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["payload"]["ts"], "2026-08-20T08:00:00")
        self.assertEqual(lines[1]["payload"]["ts"], "2026-08-21T08:00:00")

    def test_delivery_outcome_is_recorded(self):
        """A failed push is a night the measurement most needs to see."""
        self.fn(self.path, payload(), False, now=NOW)
        self.assertFalse(self.read_lines()[0]["delivered"])
        self.fn(self.path, payload(), True, now=NOW + timedelta(hours=1))
        self.assertTrue(self.read_lines()[1]["delivered"])

    def test_lines_older_than_retention_age_out(self):
        self.fn(self.path, payload(0), True, now=NOW - timedelta(days=40))
        self.fn(self.path, payload(1), True, now=NOW)
        lines = self.read_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["payload"]["ts"], "2026-08-21T08:00:00")

    def test_line_exactly_at_retention_boundary_is_kept(self):
        self.fn(self.path, payload(0), True, now=NOW - timedelta(days=30))
        self.fn(self.path, payload(1), True, now=NOW)
        self.assertEqual(len(self.read_lines()), 2)

    def test_corrupt_line_is_dropped_and_counted_not_fatal(self):
        self.fn(self.path, payload(0), True, now=NOW)
        with open(self.path, "a") as fh:
            fh.write('{"archived_at": "2026-08-21T09:00:00", "torn...\n')
        rep = self.fn(self.path, payload(1), True, now=NOW + timedelta(hours=2))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["dropped_corrupt"], 1)
        self.assertEqual(len(self.read_lines()), 2)

    def test_unterminated_final_line_cannot_corrupt_the_next_append(self):
        """The `_log_check` lesson: a torn write must cost at most one record."""
        self.fn(self.path, payload(0), True, now=NOW)
        with open(self.path, "a") as fh:
            fh.write('{"half": ')  # no newline, no close
        rep = self.fn(self.path, payload(1), True, now=NOW + timedelta(hours=2))
        self.assertEqual(rep["dropped_corrupt"], 1)
        lines = self.read_lines()  # every surviving line parses
        self.assertEqual(len(lines), 2)

    def test_never_raises_on_unwritable_path(self):
        rep = self.fn("/nonexistent-dir/nope/archive.jsonl", payload(), True, now=NOW)
        self.assertFalse(rep["ok"])

    def test_no_tmp_file_left_behind(self):
        self.fn(self.path, payload(), True, now=NOW)
        self.assertFalse(self.path.with_suffix(".jsonl.tmp").exists())

    def test_push_function_archives_in_finally(self):
        """Source-level guard: the archive call sits in a `finally` on the
        push, so neither delivery outcome can skip it."""
        src = (APP_DIR / "app.py").read_text(encoding="utf-8")
        i = src.index("def _push_digest_to_vps")
        body = src[i:src.index("def _digest_scheduler")]
        self.assertIn("finally:", body)
        self.assertIn("_archive_digest_payload(DIGEST_ARCHIVE_FILE", body)


if __name__ == "__main__":
    unittest.main()
