"""Unit tests for the 1.25.0 GCal push-repair reliability work.

Covers ISC-46..48 (honest classification), ISC-58 (retry predicate),
ISC-62..67 (staleness sentinel) and ISC-68..71 (split + correlated findings)
from ISA.md.

Two import strategies, both deliberate:

* `reconcile.py` is pure stdlib, so it imports directly.
* `app.py` pulls in flask / icalendar / anthropic, none of which are installed
  on a dev box and none of which the pure functions need. Rather than mock a
  dependency tree, the two pure predicates are extracted from the real source
  text via `ast` and exec'd in isolation. That tests the shipped code, not a
  copy of it — if someone renames or reshapes them, this fails loudly.

Run: python3 scripts/test_gcal_repair.py
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

import reconcile as R  # noqa: E402


# ── app.py pure-function extraction ─────────────────────────────────────────

def _load_pure_from_app(names):
    """Exec the named top-level functions out of app.py without importing it."""
    src = (APP_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            wanted[node.name] = node
    missing = set(names) - set(wanted)
    if missing:
        raise AssertionError(f"app.py is missing expected function(s): {sorted(missing)}")
    ns = {"datetime": datetime, "date": date, "timedelta": timedelta}
    module = ast.Module(body=[wanted[n] for n in names], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<app-pure>", "exec"), ns)
    return ns


_PURE = _load_pure_from_app(["_classify_push", "_should_retry_push"])
classify_push = _PURE["_classify_push"]
should_retry_push = _PURE["_should_retry_push"]


TODAY = date.today().isoformat()


def _status(ok=True, outcome="ok", last_ok_at=None, error=None, at=None):
    now = datetime.now()
    return {
        "ok": ok,
        "outcome": outcome,
        "at": (at or now).isoformat(timespec="seconds"),
        "error": error,
        "attempt": 1,
        "last_ok_at": last_ok_at.isoformat(timespec="seconds") if isinstance(last_ok_at, datetime) else last_ok_at,
        "stats": None,
    }


# ── ISC-46/47/48: honest push classification ────────────────────────────────

class ClassifyPush(unittest.TestCase):
    def test_success_is_ok(self):
        r = classify_push({"inserted": 2, "patched": 0, "deleted": 0}, None)
        self.assertTrue(r["ok"])
        self.assertEqual(r["outcome"], "ok")
        self.assertIsNone(r["error"])

    def test_skipped_is_not_success(self):
        """ISC-46 — the whole bug: {'skipped': 1} used to read as a sync."""
        r = classify_push({"skipped": 1}, None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["outcome"], "skipped")
        self.assertTrue(r["error"])

    def test_error_string_is_failure(self):
        r = classify_push(None, "Google API error: 403")
        self.assertFalse(r["ok"])
        self.assertEqual(r["outcome"], "failed")
        self.assertIn("403", r["error"])

    def test_exception_is_failure_and_wins_over_stats(self):
        r = classify_push({"inserted": 1}, None, exc=RuntimeError("socket timeout"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["outcome"], "failed")
        self.assertIn("timeout", r["error"])

    def test_no_stats_no_error_is_failure_not_success(self):
        r = classify_push(None, None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["outcome"], "failed")

    def test_three_outcomes_are_distinguishable(self):
        """ISC-47 — ok / skipped / failed must not collapse into two states."""
        outcomes = {
            classify_push({"inserted": 1}, None)["outcome"],
            classify_push({"skipped": 1}, None)["outcome"],
            classify_push(None, "boom")["outcome"],
        }
        self.assertEqual(outcomes, {"ok", "skipped", "failed"})


# ── ISC-58: retry cadence predicate ─────────────────────────────────────────

class ShouldRetryPush(unittest.TestCase):
    def test_absent_status_retries(self):
        self.assertTrue(should_retry_push(None))

    def test_failed_status_retries(self):
        self.assertTrue(should_retry_push(_status(ok=False, outcome="failed")))

    def test_skipped_status_retries(self):
        self.assertTrue(should_retry_push(_status(ok=False, outcome="skipped")))

    def test_healthy_status_does_not_retry(self):
        self.assertFalse(should_retry_push(_status(ok=True)))


# ── ISC-62..67: staleness sentinel ──────────────────────────────────────────

class PushHealth(unittest.TestCase):
    def test_fresh_success_is_quiet(self):
        """Quiet-when-clean must survive: a healthy push emits nothing."""
        st = _status(ok=True, last_ok_at=datetime.now())
        self.assertEqual(R._gcal_push_health(st, TODAY), [])

    def test_absent_status_is_stale_not_healthy(self):
        """ISC-67 — never pushed is not the same as pushed fine."""
        out = R._gcal_push_health(None, TODAY)
        kinds = {f["kind"] for f in out}
        self.assertIn("stale_push", kinds)

    def test_failed_push_emits_push_failed(self):
        st = _status(ok=False, outcome="failed", error="Google API error: 404 calendar not found",
                     last_ok_at=datetime.now())
        out = R._gcal_push_health(st, TODAY)
        kinds = {f["kind"] for f in out}
        self.assertIn("gcal_push_failed", kinds)

    def test_old_last_ok_is_stale(self):
        old = datetime.now() - timedelta(hours=R.PUSH_STALE_HOURS + 2)
        st = _status(ok=True, last_ok_at=old)
        kinds = {f["kind"] for f in R._gcal_push_health(st, TODAY)}
        self.assertIn("stale_push", kinds)

    def test_threshold_exceeds_nightly_cadence(self):
        """ISC-63 — a 24h cadence with a <=24h threshold alarms every morning."""
        self.assertGreater(R.PUSH_STALE_HOURS, 24)

    def test_findings_are_dated_today(self):
        """ISC-65 — STALE_DAYS suppression must not eat the sentinel."""
        old = datetime.now() - timedelta(days=40)
        st = _status(ok=False, outcome="failed", error="boom", last_ok_at=old)
        for f in R._gcal_push_health(st, TODAY):
            self.assertEqual(f["date"], TODAY, f"{f['kind']} not dated today")

    def test_ids_are_stable_across_runs(self):
        """ISC-66 — a per-run id would re-alarm every morning via the digest diff."""
        st = _status(ok=False, outcome="failed", error="boom")
        a = sorted(f["id"] for f in R._gcal_push_health(st, TODAY))
        b = sorted(f["id"] for f in R._gcal_push_health(st, TODAY))
        self.assertEqual(a, b)
        self.assertTrue(all(":" in i for i in a))

    def test_severity_is_needs_attention(self):
        st = _status(ok=False, outcome="failed", error="boom")
        for f in R._gcal_push_health(st, TODAY):
            self.assertEqual(f["severity"], "needs-attention")

    def test_why_names_the_write_not_the_drift(self):
        """ISC-85 — the host must be able to tell push-failure from calendar drift."""
        st = _status(ok=False, outcome="failed", error="Google API error: 404")
        failed = [f for f in R._gcal_push_health(st, TODAY) if f["kind"] == "gcal_push_failed"]
        self.assertTrue(failed)
        self.assertIn("404", failed[0]["why"])

    def test_findings_carry_the_vps_allowlist_keys(self):
        """ISC-72 — the payload builder reads these keys off every finding."""
        st = _status(ok=False, outcome="failed", error="boom")
        for f in R._gcal_push_health(st, TODAY):
            for key in ("id", "detector", "kind", "severity", "date", "cleaner", "why"):
                self.assertIn(key, f, f"{f.get('kind')} missing {key}")

    def test_no_secret_material_in_why(self):
        """ISC-73 — nothing that looks like a credential or a feed URL."""
        st = _status(ok=False, outcome="failed",
                     error="Google API error: 403 forbidden")
        for f in R._gcal_push_health(st, TODAY):
            low = f["why"].lower()
            for banned in ("private_key", "begin private key", "airbnb.com/calendar", "token="):
                self.assertNotIn(banned, low)


# ── ISC-64, ISC-68..71: correlation inside run() ────────────────────────────

def _fake_gcal_findings(*_a, **_k):
    return [
        {"id": "gcal_missing_event:stay:x", "detector": "bookings_vs_gcal",
         "kind": "gcal_missing_event", "severity": "needs-attention",
         "booking_uid": "x", "cleaner": None, "date": TODAY,
         "why": "stay event for x missing from Google Calendar", "evidence": []},
        {"id": "gcal_stale_event:clean:x", "detector": "bookings_vs_gcal",
         "kind": "gcal_stale_event", "severity": "suggest",
         "booking_uid": "x", "cleaner": None, "date": TODAY,
         "why": "Google Calendar clean for x is out of date", "evidence": []},
    ]


class Correlation(unittest.TestCase):
    def setUp(self):
        self._orig = R._bookings_vs_gcal
        R._bookings_vs_gcal = _fake_gcal_findings
        self.drift = [{"uid": "d1", "kind": "unassigned", "cleaner": None, "date": TODAY}]

    def tearDown(self):
        R._bookings_vs_gcal = self._orig

    def _run(self, status):
        return R.run({"bookings": {}}, self.drift, gcal_events={}, gcal_status=status)

    def test_healthy_push_passes_gcal_findings_through(self):
        """ISC-71 — correlation must never suppress while the push is healthy."""
        res = self._run(_status(ok=True, last_ok_at=datetime.now()))
        kinds = {f["kind"] for f in res["findings"]}
        self.assertIn("gcal_missing_event", kinds)
        self.assertIn("gcal_stale_event", kinds)
        self.assertNotIn("gcal_push_failed", kinds)

    def test_failed_push_absorbs_gcal_findings(self):
        """ISC-69 — one root cause, one alert."""
        res = self._run(_status(ok=False, outcome="failed", error="boom",
                                last_ok_at=datetime.now()))
        kinds = {f["kind"] for f in res["findings"]}
        self.assertIn("gcal_push_failed", kinds)
        self.assertNotIn("gcal_missing_event", kinds)
        self.assertNotIn("gcal_stale_event", kinds)

    def test_absorption_is_scoped_to_gcal_detector(self):
        """ISC-70 — unrelated findings still surface during a push failure."""
        res = self._run(_status(ok=False, outcome="failed", error="boom"))
        detectors = {f["detector"] for f in res["findings"]}
        self.assertIn("drift", detectors)

    def test_absorbed_count_is_stated_not_hidden(self):
        res = self._run(_status(ok=False, outcome="failed", error="boom",
                                last_ok_at=datetime.now()))
        failed = [f for f in res["findings"] if f["kind"] == "gcal_push_failed"][0]
        self.assertIn("2", failed["why"])

    def test_counts_agree_with_findings(self):
        """ISC-64 — the 1.24.2 lesson: a consumer keying off counts must not
        read a broken night as healthy."""
        for st in (None,
                   _status(ok=True, last_ok_at=datetime.now()),
                   _status(ok=False, outcome="failed", error="boom")):
            res = self._run(st)
            self.assertEqual(res["counts"]["total"], len(res["findings"]))
            per_sev = sum(res["counts"].get(s, 0)
                          for s in ("needs-attention", "suggest", "informational"))
            self.assertEqual(per_sev, len(res["findings"]))

    def test_run_without_status_is_backwards_compatible(self):
        """An omitted gcal_status must not crash callers that predate it."""
        res = R.run({"bookings": {}}, self.drift)
        self.assertIn("findings", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
