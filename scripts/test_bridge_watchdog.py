"""Unit tests for the bridge liveness watchdog.

Covers the state machine that the 2026-07-28 outage exposed: detect a stopped
container, heal it, and — critically — keep telling the operator about the
messages that were lost while it was down, rather than going quiet the moment
the process comes back.

Run: python3 scripts/test_bridge_watchdog.py
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
