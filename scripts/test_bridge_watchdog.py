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


class EventLogTests(WatchdogTests):
    """The event log answers 'how often does this actually need restarting'.

    Its whole value is that it stays sparse: only transitions are recorded, so
    the log reads as a list of incidents rather than a poll transcript. At a
    five-minute interval a chatty log would be 288 rows a day.
    """

    def test_steady_state_writes_no_events(self):
        self._wire(FakeSupervisor("started"))
        for _ in range(10):
            state = wd.check(self.path, "slug", "token")
        self.assertEqual(state["events"], [])
        self.assertEqual(wd.summary(state)["restarts_24h"], 0)

    def test_a_restart_is_recorded_and_counted(self):
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        state = wd.check(self.path, "slug", "token")
        kinds = [e["kind"] for e in state["events"]]
        self.assertIn("outage_opened", kinds)
        self.assertIn("restart", kinds)
        s = wd.summary(state)
        self.assertEqual(s["restarts_24h"], 1)
        self.assertEqual(s["restarts_7d"], 1)
        self.assertEqual(s["outages_logged"], 1)
        self.assertIsNotNone(s["last_restart_at"])

    def test_summary_states_the_undercount_out_loud(self):
        # A restart Supervisor's own watchdog performs between two polls is
        # invisible here. A number that quietly undercounts is worse than one
        # that admits it, so the caveat travels with the figure.
        self._wire(FakeSupervisor("started"))
        state = wd.check(self.path, "slug", "token")
        self.assertIn(">=", wd.summary(state)["caveat"])

    def test_a_persistent_probe_error_logs_once_not_every_poll(self):
        sup = FakeSupervisor("started")
        sup.fail_info = True
        self._wire(sup)
        for _ in range(6):
            state = wd.check(self.path, "slug", "token")
        errs = [e for e in state["events"] if e["kind"] == "probe_error"]
        self.assertEqual(len(errs), 1)
        # ...and recovery closes it, so the next failure is a fresh event.
        sup.fail_info = False
        state = wd.check(self.path, "slug", "token")
        self.assertEqual([e["kind"] for e in state["events"]][-1], "probe_recovered")

    def test_event_log_is_capped(self):
        state = wd._default_state()
        for i in range(wd.EVENT_CAP + 50):
            wd._append_event(state, f"2026-01-01T00:00:{i % 60:02d}", "restart", attempt=i)
        self.assertEqual(len(state["events"]), wd.EVENT_CAP)
        # The cap must drop the OLDEST, never the newest.
        self.assertEqual(state["events"][-1]["attempt"], wd.EVENT_CAP + 49)

    def test_summary_survives_a_malformed_timestamp(self):
        state = wd._default_state()
        state["events"] = [
            {"at": "not-a-timestamp", "kind": "restart"},
            {"kind": "restart"},
            {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "kind": "restart"},
        ]
        s = wd.summary(state)          # must not raise
        self.assertEqual(s["restarts_24h"], 1)
        self.assertEqual(s["restarts_logged"], 3)


class ConcurrencyTests(WatchdogTests):
    """`check()` has two callers in one process — the scheduler thread and the
    on-demand route — and the route exists to be hit *while* the timer ticks.
    Unguarded, the loser of an interleave overwrites the winner's events."""

    def test_concurrent_checks_do_not_lose_events(self):
        import threading as _t
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        # Restart attempts accumulate on the open outage; every one must land.
        barrier = _t.Barrier(8)

        def worker():
            barrier.wait()
            wd.check(self.path, "slug", "token")

        threads = [_t.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = wd.load_state(self.path)
        restarts = [e for e in state["events"] if e["kind"] == "restart"]
        self.assertEqual(state["heals"], len(restarts))
        self.assertEqual(state["checks"], 8)
        self.assertEqual(wd.summary(state)["restarts_24h"], len(restarts))

    def test_save_state_is_atomic(self):
        # A truncated file resets the entire history, so the write must never
        # be observable half-done. Proxy: no .tmp residue, file always parses.
        wd.save_state(self.path, {**wd._default_state(), "checks": 7})
        self.assertEqual(wd.load_state(self.path)["checks"], 7)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
