"""Unit tests for the bridge liveness watchdog.

Covers the state machine that the 2026-07-28 outage exposed: detect a stopped
container, heal it, and — critically — keep telling the operator about the
messages that were lost while it was down, rather than going quiet the moment
the process comes back.

Run: python3 scripts/test_bridge_watchdog.py
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))

import bridge_watchdog as wd


class FakeSupervisor:
    """Stands in for the Supervisor HTTP API."""

    def __init__(self, state):
        self.state = state
        self.starts = 0
        self.fail_start = False
        self.fail_info = False

    def get_state(self, slug, token, timeout=10):
        if self.fail_info:
            raise RuntimeError("info HTTP 403: forbidden")
        return self.state

    def start(self, slug, token, timeout=90):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("start HTTP 500: boom")
        self.state = "started"
        return True


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge_watchdog.json"
        self.today = datetime.now().date().isoformat()
        self._real_get = wd._supervisor_get_state
        self._real_start = wd._supervisor_start

    def tearDown(self):
        wd._supervisor_get_state = self._real_get
        wd._supervisor_start = self._real_start
        self.tmp.cleanup()

    def _wire(self, sup):
        wd._supervisor_get_state = sup.get_state
        wd._supervisor_start = sup.start

    def test_healthy_bridge_produces_no_findings(self):
        self._wire(FakeSupervisor("started"))
        state = wd.check(self.path, "slug", "token")
        self.assertEqual(state["last_state"], "started")
        self.assertIsNone(state["outage"])
        self.assertEqual(wd.findings(state, self.today), [])

    def test_stopped_bridge_is_started_and_flagged(self):
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        state = wd.check(self.path, "slug", "token", last_message_at="2026-07-28T14:08:00")
        self.assertEqual(sup.starts, 1, "a stopped bridge must be started without asking")
        self.assertIsNotNone(state["outage"])
        kinds = [f["kind"] for f in wd.findings(state, self.today)]
        self.assertIn("bridge_down", kinds)

    def test_recovery_leaves_a_blind_window_that_persists(self):
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        wd.check(self.path, "slug", "token", last_message_at="2026-07-28T14:08:00")
        # Second pass: the start took effect.
        state = wd.check(self.path, "slug", "token")
        self.assertIsNone(state["outage"], "outage closes once the container is back")
        self.assertEqual(len(state["blind_windows"]), 1)

        found = wd.findings(state, self.today)
        kinds = [f["kind"] for f in found]
        self.assertNotIn("bridge_down", kinds, "recovered bridge must stop alarming as down")
        self.assertIn("bridge_blind_window", kinds,
                      "message loss must survive the restart — a restart is not a fix")
        why = [f["why"] for f in found if f["kind"] == "bridge_blind_window"][0]
        self.assertIn("2026-07-28 14:08", why, "the window must name when we went blind")

    def test_blind_window_id_is_stable_across_nights(self):
        """A changing id would read as a brand-new problem every evening and
        could never be dismissed."""
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        wd.check(self.path, "slug", "token", last_message_at="2026-07-28T14:08:00")
        state = wd.check(self.path, "slug", "token")
        first = [f["id"] for f in wd.findings(state, self.today)]
        later = [f["id"] for f in wd.findings(wd.load_state(self.path), self.today)]
        self.assertEqual(first, later)

    def test_failed_start_is_reported_not_swallowed(self):
        sup = FakeSupervisor("stopped")
        sup.fail_start = True
        self._wire(sup)
        state = wd.check(self.path, "slug", "token")
        why = [f["why"] for f in wd.findings(state, self.today) if f["kind"] == "bridge_down"][0]
        self.assertIn("Automatic restart failed", why)

    def test_unreadable_supervisor_produces_a_finding(self):
        """A watchdog that cannot check is the failure it exists to prevent, so
        it must announce itself rather than silently no-op."""
        sup = FakeSupervisor("started")
        sup.fail_info = True
        self._wire(sup)
        state = wd.check(self.path, "slug", "token")
        kinds = [f["kind"] for f in wd.findings(state, self.today)]
        self.assertIn("bridge_watchdog_error", kinds)

    def test_missing_token_produces_a_finding(self):
        self._wire(FakeSupervisor("started"))
        state = wd.check(self.path, "slug", "")
        kinds = [f["kind"] for f in wd.findings(state, self.today)]
        self.assertIn("bridge_watchdog_error", kinds)

    def test_transient_state_does_not_trigger_a_restart(self):
        sup = FakeSupervisor("startup")
        self._wire(sup)
        wd.check(self.path, "slug", "token")
        self.assertEqual(sup.starts, 0, "mid-transition states must not be fought")

    def test_corrupt_state_file_does_not_kill_the_watchdog(self):
        self.path.write_text("{not json")
        self._wire(FakeSupervisor("started"))
        state = wd.check(self.path, "slug", "token")
        self.assertEqual(state["last_state"], "started")

    def test_repeated_outage_days_are_counted(self):
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        wd.save_state(self.path, {
            **wd._default_state(),
            "outage": {"detected_at": old, "restart_attempts": 3, "observed_state": "stopped"},
        })
        state = wd.check(self.path, "slug", "token")
        why = [f["why"] for f in wd.findings(state, self.today) if f["kind"] == "bridge_down"][0]
        self.assertIn("down for 3 day(s)", why)


class CheckLogTests(WatchdogTests):
    """Every pass is recorded, not only the eventful ones.

    The first design logged transitions only. It was wrong for one reason: an
    empty sparse log reads identically whether the bridge was solid or the
    watchdog never ran. Stability has to be evidenced by observations, not
    inferred from the absence of incidents.
    """

    def setUp(self):
        super().setUp()
        self.log = Path(self.tmp.name) / "bridge_checks.jsonl"

    def _check(self, **kw):
        return wd.check(self.path, "slug", "token", log_path=self.log, **kw)

    def test_uneventful_checks_are_still_recorded(self):
        self._wire(FakeSupervisor("started"))
        for _ in range(10):
            self._check()
        recs = wd.read_checks(self.log)
        self.assertEqual(len(recs), 10)
        self.assertTrue(all(r["action"] == "none" for r in recs))
        self.assertTrue(all(r["state"] == "started" for r in recs))

    def test_a_restart_is_recorded_with_its_action(self):
        self._wire(FakeSupervisor("stopped"))
        self._check()
        rec = wd.read_checks(self.log)[-1]
        self.assertEqual(rec["action"], "restarted")
        self.assertEqual(rec["state"], "stopped")
        self.assertEqual(rec["attempt"], 1)

    def test_transition_records_where_it_came_from(self):
        sup = FakeSupervisor("started")
        self._wire(sup)
        self._check()
        sup.state = "stopped"
        self._check()
        rec = wd.read_checks(self.log)[-1]
        self.assertEqual(rec["from_state"], "started")
        self.assertEqual(rec["state"], "stopped")

    def test_summary_reports_share_of_healthy_observations(self):
        sup = FakeSupervisor("started")
        self._wire(sup)
        for _ in range(3):
            self._check()
        sup.state = "stopped"
        sup.fail_start = True          # stay down so the state sticks
        self._check()
        s = wd.summary(wd.load_state(self.path), log_path=self.log)
        self.assertEqual(s["checks_logged"], 4)
        self.assertEqual(s["healthy_checks"], 3)
        self.assertEqual(s["healthy_pct"], 75.0)
        self.assertEqual(s["down_episodes_30d"], 1)

    def test_a_probe_failure_is_logged_on_every_pass(self):
        # Deliberate change from the sparse design: if Supervisor is unreachable
        # for six hours, every one of those checks is evidence, and collapsing
        # them to one line hides the duration.
        sup = FakeSupervisor("started")
        sup.fail_info = True
        self._wire(sup)
        for _ in range(5):
            self._check()
        recs = wd.read_checks(self.log)
        self.assertEqual(len(recs), 5)
        self.assertTrue(all(r["action"] == "probe_failed" for r in recs))
        s = wd.summary(wd.load_state(self.path), log_path=self.log)
        self.assertEqual(s["probe_failures_7d"], 5)

    def test_prune_drops_records_outside_the_trailing_window(self):
        old = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")
        recent = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        self.log.write_text(
            json.dumps({"at": old, "state": "started", "action": "none"}) + "\n"
            + json.dumps({"at": recent, "state": "started", "action": "none"}) + "\n"
        )
        kept = wd.prune_checks(self.log, days=30)
        self.assertEqual(kept, 1)
        self.assertEqual([r["at"] for r in wd.read_checks(self.log)], [recent])
        self.assertEqual(list(self.log.parent.glob("*.tmp")), [])

    def test_a_torn_line_does_not_break_the_history(self):
        self._wire(FakeSupervisor("started"))
        self._check()
        with open(self.log, "a") as f:
            f.write('{"at": "2026-08-03T10:00:00", "sta')   # killed mid-append
        self._check()
        recs = wd.read_checks(self.log)
        self.assertEqual(len(recs), 2)

    def test_summary_states_the_undercount_out_loud(self):
        self._wire(FakeSupervisor("started"))
        self._check()
        self.assertIn(">=", wd.summary(wd.load_state(self.path), log_path=self.log)["caveat"])

    def test_no_log_path_means_no_log(self):
        # The log is opt-in at the call site; the state machine must not depend
        # on it (every existing test calls check() without one).
        self._wire(FakeSupervisor("started"))
        wd.check(self.path, "slug", "token")
        self.assertFalse(self.log.exists())


class ConcurrencyTests(WatchdogTests):
    """`check()` has two callers in one process — the scheduler thread and the
    on-demand route — and the route exists to be hit *while* the timer ticks.
    Unguarded, the loser of an interleave overwrites the winner's state."""

    def test_concurrent_checks_do_not_lose_writes(self):
        import threading as _t
        log = Path(self.tmp.name) / "bridge_checks.jsonl"
        self._wire(FakeSupervisor("stopped"))
        barrier = _t.Barrier(8)

        def worker():
            barrier.wait()
            wd.check(self.path, "slug", "token", log_path=log)

        threads = [_t.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = wd.load_state(self.path)
        recs = wd.read_checks(log)
        self.assertEqual(state["checks"], 8)
        self.assertEqual(len(recs), 8, "a check was logged but its state write was lost")
        self.assertEqual(state["heals"], len([r for r in recs if r["action"] == "restarted"]))

    def test_save_state_is_atomic(self):
        wd.save_state(self.path, {**wd._default_state(), "checks": 7})
        self.assertEqual(wd.load_state(self.path)["checks"], 7)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
