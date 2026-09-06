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
        t = datetime(2026, 9, 6, 12, 0, 0)
        state = wd.check(self.path, "slug", "token", now=t)
        self.assertEqual(state["last_state"], "started")
        self.assertIsNone(state["outage"])
        self.assertEqual(wd.findings(state, self.today, now=t), [])

    def test_stopped_bridge_is_started_and_flagged(self):
        sup = FakeSupervisor("stopped")
        self._wire(sup)
        t0 = datetime(2026, 7, 28, 14, 10, 0)
        state = wd.check(self.path, "slug", "token", now=t0,
                         last_message_at="2026-07-28T14:08:00")
        self.assertEqual(sup.starts, 1, "a stopped bridge must be started without asking")
        self.assertIsNotNone(state["outage"])
        self.assertEqual(wd.findings(state, self.today, now=t0), [],
                         "a container outage the watchdog is already healing "
                         "must not alarm inside the hour — what it cost is "
                         "recorded as a blind window, not as an alert")
        late = t0 + timedelta(minutes=61)
        sup.state = "stopped"
        state = wd.check(self.path, "slug", "token", now=late, heal=False)
        kinds = [f["kind"] for f in wd.findings(state, self.today, now=late)]
        self.assertIn("bridge_health", kinds)

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
        late = datetime.now() + timedelta(minutes=61)
        state = wd.check(self.path, "slug", "token", now=late)
        why = [f["why"] for f in wd.findings(state, self.today, now=late)
               if f["kind"] == "bridge_health"][0]
        self.assertIn("Automatic restart failed", why)

    def test_unreadable_supervisor_produces_a_finding(self):
        """A watchdog that cannot check is the failure it exists to prevent, so
        it must announce itself rather than silently no-op."""
        sup = FakeSupervisor("started")
        sup.fail_info = True
        self._wire(sup)
        state = wd.check(self.path, "slug", "token")
        kinds = [f["kind"] for f in wd.findings(state, self.today)]
        self.assertIn("bridge_health", kinds)

    def test_missing_token_produces_a_finding(self):
        self._wire(FakeSupervisor("started"))
        state = wd.check(self.path, "slug", "")
        kinds = [f["kind"] for f in wd.findings(state, self.today)]
        self.assertIn("bridge_health", kinds)

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
        why = [f["why"] for f in wd.findings(state, self.today) if f["kind"] == "bridge_health"][0]
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


class LinkStateTests(unittest.TestCase):
    """The 2026-09-05 lesson: container state and message traffic both lie.

    The container said `started` for 22 hours while the process crash-looped
    against a revoked WhatsApp registration, and message absence said nothing
    at all because the cleaner groups had genuinely been quiet for three days
    beforehand. Only the bridge knows whether WhatsApp is talking to it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge_watchdog.json"
        self.today = datetime.now().date().isoformat()
        self._real_get = wd._supervisor_get_state
        self._real_start = wd._supervisor_start
        wd._supervisor_get_state = lambda slug, token, timeout=10: "started"
        wd._supervisor_start = lambda slug, token, timeout=90: True
        self.alerts = []
        self.now = datetime(2026, 9, 6, 12, 0, 0)

    def tearDown(self):
        wd._supervisor_get_state = self._real_get
        wd._supervisor_start = self._real_start
        self.tmp.cleanup()

    def _cb(self, kind, title, message):
        self.alerts.append((kind, title, message))

    def _beat(self, connection="open", age_sec=0, now=None):
        now = now or self.now
        return {"received_at": int(now.timestamp()) - age_sec,
                "connection": connection, "bridge_version": "1.4.0"}

    def _check(self, heartbeat, now=None, **kw):
        return wd.check(self.path, "slug", "token", now=now or self.now,
                        heartbeat=heartbeat, alert_cb=self._cb, **kw)

    # ── verdicts ────────────────────────────────────────────────────────────

    def test_fresh_open_heartbeat_is_up(self):
        state = self._check(self._beat("open"))
        self.assertEqual(state["link"]["verdict"], "up")
        self.assertIsNone(state["link"]["down_since"])
        self.assertEqual(self.alerts, [])

    def test_fresh_closed_heartbeat_is_down(self):
        state = self._check(self._beat("close"))
        self.assertEqual(state["link"]["verdict"], "down")
        self.assertIsNotNone(state["link"]["down_since"])

    def test_stale_heartbeat_is_down_not_unknown(self):
        """Silence is a verdict. A deadman that answers 'unknown' is not one."""
        state = self._check(self._beat("open", age_sec=9999))
        self.assertEqual(state["link"]["verdict"], "down")
        self.assertIn("no heartbeat", state["link"]["reason"])

    def test_never_seen_is_distinct_from_was_up_now_down(self):
        state = self._check(None)
        self.assertEqual(state["link"]["verdict"], "never_seen")

    def test_negative_age_is_down_never_fresh(self):
        """A timestamp from the future is corruption or a moved clock. Reading
        it as '0 seconds old, all good' is the fail-open that makes a deadman
        useless — this fleet has been bitten by exactly that."""
        state = self._check(self._beat("open", age_sec=-3600))
        self.assertEqual(state["link"]["verdict"], "down")
        self.assertIn("future", state["link"]["reason"])

    def test_unusable_received_at_is_down(self):
        state = self._check({"connection": "open", "received_at": None})
        self.assertEqual(state["link"]["verdict"], "down")

    # ── the one-hour rule ───────────────────────────────────────────────────

    def test_no_phone_alert_before_the_threshold(self):
        self._check(self._beat("close"))
        later = self.now + timedelta(minutes=59)
        self._check(self._beat("close", now=later), now=later)
        self.assertEqual(self.alerts, [], "59 minutes must not alert")

    def test_alert_fires_once_at_one_hour_and_does_not_repeat(self):
        self._check(self._beat("close"))
        for extra in (60, 65, 120, 600):
            t = self.now + timedelta(minutes=extra)
            self._check(self._beat("close", now=t), now=t)
        kinds = [a[0] for a in self.alerts]
        self.assertEqual(kinds, ["bridge_health"],
                         "exactly one alert per episode — one that repeats "
                         "every poll is one you learn to swipe away")

    def test_recovery_closes_the_episode_and_keeps_it(self):
        self._check(self._beat("close"))
        t = self.now + timedelta(minutes=90)
        self._check(self._beat("close", now=t), now=t)
        back = t + timedelta(minutes=5)
        state = self._check(self._beat("open", now=back), now=back)
        self.assertEqual(state["link"]["verdict"], "up")
        self.assertIsNone(state["link"]["down_since"])
        self.assertIsNone(state["link"]["alerted_at"])
        self.assertEqual(len(state["link"]["episodes"]), 1)
        self.assertTrue(state["link"]["episodes"][0]["alerted"])

    def test_a_second_outage_alerts_again(self):
        """The guard is per-episode, not per-lifetime."""
        self._check(self._beat("close"))
        t = self.now + timedelta(minutes=70)
        self._check(self._beat("close", now=t), now=t)
        back = t + timedelta(minutes=5)
        self._check(self._beat("open", now=back), now=back)
        d2 = back + timedelta(minutes=10)
        self._check(self._beat("close", now=d2), now=d2)
        d3 = d2 + timedelta(minutes=61)
        self._check(self._beat("close", now=d3), now=d3)
        self.assertEqual([a[0] for a in self.alerts], ["bridge_health", "bridge_health"])

    def test_finding_is_gated_on_the_threshold_so_deploys_do_not_cry_wolf(self):
        state = self._check(None)
        # `findings()` MUST be given the same clock as `check()`. Omitting it
        # defaults to the real wall clock, so this asserted "no finding" while
        # the verdict was computing hours of downtime against a `down_since`
        # that the test had pinned to noon — green at noon, red by teatime.
        self.assertEqual(wd.findings(state, self.today, now=self.now), [],
                         "a 1.3 bridge behind a 1.42 tracker must not alarm "
                         "at zero minutes during a rolling deploy")
        t = self.now + timedelta(minutes=61)
        state = self._check(None, now=t)
        kinds = [f["kind"] for f in wd.findings(state, self.today, now=t)]
        self.assertIn("bridge_health", kinds)


class HealCapTests(unittest.TestCase):
    """263 identical restarts against a revoked registration, five minutes
    apart, is not a repair — it is a loop that made a dead bridge read as one
    under active repair."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge_watchdog.json"
        self.today = datetime.now().date().isoformat()
        self._real_get = wd._supervisor_get_state
        self._real_start = wd._supervisor_start
        self.starts = 0
        self.container = "error"
        wd._supervisor_get_state = lambda s, t, timeout=10: self.container
        def _start(s, t, timeout=90):
            self.starts += 1
            raise RuntimeError("start HTTP 400: Another job is already in progress")
        wd._supervisor_start = _start
        self.alerts = []
        self.now = datetime(2026, 9, 6, 12, 0, 0)

    def tearDown(self):
        wd._supervisor_get_state = self._real_get
        wd._supervisor_start = self._real_start
        self.tmp.cleanup()

    def _cb(self, kind, title, message):
        self.alerts.append((kind, title, message))

    def _pass(self, n):
        t = self.now + timedelta(minutes=5 * n)
        return wd.check(self.path, "slug", "token", now=t,
                        heartbeat={"received_at": int(t.timestamp()),
                                   "connection": "close"},
                        alert_cb=self._cb, max_heal_attempts=5)

    def test_restarts_stop_at_the_cap(self):
        for n in range(12):
            state = self._pass(n)
        self.assertEqual(self.starts, 5,
                         "past the cap it must stop calling Supervisor")
        self.assertEqual(state["outage"]["restart_attempts"], 5)

    def test_cap_escalates_exactly_once(self):
        for n in range(12):
            self._pass(n)
        self.assertEqual([a[0] for a in self.alerts].count("bridge_health"), 1,
                         "one fault, one alert — never one per symptom")

    def test_cap_reports_unhealable_not_restarted(self):
        for n in range(8):
            self._pass(n)
        end = self.now + timedelta(minutes=5 * 7)
        state = wd.load_state(self.path)
        self.assertIsNotNone(state["outage"]["escalated_at"])
        kinds = [f["kind"] for f in wd.findings(state, self.today, now=end)]
        self.assertIn("bridge_health", kinds)

    def test_counter_resets_after_recovery_so_a_later_outage_still_heals(self):
        """The cap must not become a permanent refusal to heal."""
        for n in range(8):
            self._pass(n)
        self.assertEqual(self.starts, 5)
        # Bridge comes back, then dies again later.
        self.container = "started"
        t = self.now + timedelta(hours=2)
        wd.check(self.path, "slug", "token", now=t,
                 heartbeat={"received_at": int(t.timestamp()), "connection": "open"},
                 alert_cb=self._cb, max_heal_attempts=5)
        self.container = "error"
        t2 = t + timedelta(minutes=5)
        wd.check(self.path, "slug", "token", now=t2,
                 heartbeat={"received_at": int(t2.timestamp()), "connection": "close"},
                 alert_cb=self._cb, max_heal_attempts=5)
        self.assertEqual(self.starts, 6,
                         "a later, genuinely restartable outage must still heal")

    def test_a_backwards_clock_cannot_buy_the_fault_more_silence(self):
        """The Pi has no RTC and steps its clock at boot. A backward step makes
        the down-duration negative, and a negative duration never reaches the
        threshold — the alert just never fires. That is the one direction a
        deadman must not fail in."""
        wd._supervisor_get_state = lambda *a, **k: "started"
        wd._supervisor_start = lambda *a, **k: True
        t0 = datetime(2026, 9, 6, 12, 0, 0)
        beat = lambda t: {"received_at": int(t.timestamp()), "connection": "close"}
        wd.check(self.path, "slug", "token", now=t0, heartbeat=beat(t0),
                 alert_cb=lambda k, ti, m: self.alerts.append((k, m)))
        # clock jumps two hours BACKWARDS
        back = t0 - timedelta(hours=2)
        state = wd.check(self.path, "slug", "token", now=back, heartbeat=beat(back),
                         alert_cb=lambda k, ti, m: self.alerts.append((k, m)))
        self.assertGreaterEqual(int(state["link"].get("down_for_min") or 0), 0,
                                "never a negative duration")
        # and the clock cannot postpone the alert indefinitely
        later = back + timedelta(minutes=61)
        wd.check(self.path, "slug", "token", now=later, heartbeat=beat(later),
                 alert_cb=lambda k, ti, m: self.alerts.append((k, m)))
        self.assertEqual(len(self.alerts), 1, "the alert still fires after the step")

    def test_pre_1_4_state_is_migrated_not_inherited(self):
        """The Pi holds the open 2026-09-05 episode. Inheriting it would either
        fire the phone instantly on ~30h of stale downtime, or find the alert
        guard already set and stay silent through a real fault."""
        wd.save_state(self.path, {
            "last_check": "2026-09-05T11:47:41", "last_state": "error",
            "outage": None, "blind_windows": [{"from": "x", "to": "y"}],
            "probe_error": None, "checks": 8648, "heals": 262,
            "link": {"verdict": "down", "down_since": "2026-09-05T11:47:41",
                     "alerted_at": "2026-09-05T11:47:41"},
        })
        wd._supervisor_get_state = lambda *a, **k: "started"
        wd._supervisor_start = lambda *a, **k: True
        t = datetime(2026, 9, 6, 12, 0, 0)
        state = wd.check(self.path, "slug", "token", now=t, heartbeat={
            "received_at": int(t.timestamp()), "connection": "open"},
            alert_cb=lambda k, ti, m: self.alerts.append((k, m)))
        self.assertEqual(self.alerts, [], "no phone alert from inherited state")
        self.assertEqual(state["link"]["verdict"], "up")
        self.assertIsNone(state["link"]["down_since"])
        self.assertEqual(len(state["blind_windows"]), 1, "the RECORD survives")
        self.assertEqual(state["heals"], 262, "counts survive")


class CollapseTests(unittest.TestCase):
    """Josh, 2026-09-06: "I want to strip it way back to a single chain of
    notifications."

    Before this, twelve bridge-health alerts existed across three channels, all
    answering one question. Five of them fired or would have fired for the
    single 2026-09-05 fault, at five different latencies. These tests are the
    enforcement — a count in prose creeps straight back."""

    REPO = Path(__file__).resolve().parent.parent

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bridge_watchdog.json"
        self.today = datetime.now().date().isoformat()
        self._g, self._s = wd._supervisor_get_state, wd._supervisor_start
        self.alerts = []
        self.now = datetime(2026, 9, 6, 12, 0, 0)

    def tearDown(self):
        wd._supervisor_get_state, wd._supervisor_start = self._g, self._s
        self.tmp.cleanup()

    def test_only_three_bridge_kinds_can_be_emitted(self):
        """Two verdicts and one record. Not twelve."""
        import re as _re
        src = ""
        for f in ("cleaning-tracker/bridge_watchdog.py", "cleaning-tracker/reconcile.py"):
            src += (self.REPO / f).read_text(encoding="utf-8")
        kinds = set(_re.findall(r'"kind": "(bridge_\w+|channel_\w+)"', src))
        self.assertEqual(
            kinds, {"bridge_health", "bridge_completeness", "bridge_blind_window"},
            "one liveness verdict, one completeness verdict, one loss record")

    def test_many_simultaneous_faults_produce_exactly_one_alert(self):
        """The whole point. A link fault AND a container fault AND a wedged
        consumer is ONE message naming all three, not three racing each other."""
        wd._supervisor_get_state = lambda *a, **k: "error"
        wd._supervisor_start = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        hb = {"received_at": int(self.now.timestamp()), "connection": "close",
              "allowlist_matched": 9, "forwarded_ok": 0}
        for n in range(20):
            t = self.now + timedelta(minutes=5 * n)
            hb["received_at"] = int(t.timestamp())
            wd.check(self.path, "slug", "token", now=t, heartbeat=hb,
                     alert_cb=lambda k, ti, m: self.alerts.append((k, m)),
                     max_heal_attempts=5)
        # One buzz for one broken bridge, however many ways it is broken.
        self.assertEqual(len(self.alerts), 1, f"expected 1 alert, got {len(self.alerts)}")
        self.assertEqual(self.alerts[0][0], "bridge_health")

        # The alert is a snapshot at onset — it says "go look". The FINDING is
        # what carries the full picture, and it regenerates every pass, so by
        # the end it must name every fault that is live.
        state = wd.load_state(self.path)
        end = self.now + timedelta(minutes=5 * 19)
        why = [f["why"] for f in wd.findings(state, self.today, now=end)
               if f["kind"] == "bridge_health"][0]
        for expected in ("No working WhatsApp connection",
                         "Restarting is not fixing it",
                         "broken consumer"):
            self.assertIn(expected, why, "the finding names every live fault")

    def test_a_wedged_consumer_is_caught_by_presence_not_absence(self):
        """Socket open, handler detached: connection reports healthy forever and
        nothing forwards. Josh ruled absence proves nothing — so this is caught
        by a RATIO between two things the bridge watched happen."""
        wd._supervisor_get_state = lambda *a, **k: "started"
        wd._supervisor_start = lambda *a, **k: True
        hb = {"received_at": int(self.now.timestamp()), "connection": "open",
              "socket_events_seen": 14, "allowlist_matched": 6, "forwarded_ok": 0}
        state = wd.check(self.path, "slug", "token", now=self.now, heartbeat=hb,
                         alert_cb=lambda k, t, m: self.alerts.append((k, m)))
        kinds = [f["kind"] for f in wd.findings(state, self.today, now=self.now)]
        self.assertIn("bridge_health", kinds)
        self.assertIn("broken consumer", self.alerts[0][1])

    def test_a_quiet_but_working_bridge_never_alarms(self):
        """The failure Josh explicitly rejected: alarming because nobody talked."""
        wd._supervisor_get_state = lambda *a, **k: "started"
        wd._supervisor_start = lambda *a, **k: True
        for n in range(30):
            t = self.now + timedelta(minutes=5 * n)
            state = wd.check(self.path, "slug", "token", now=t, heartbeat={
                "received_at": int(t.timestamp()), "connection": "open",
                "socket_events_seen": 0, "allowlist_matched": 0, "forwarded_ok": 0},
                alert_cb=lambda k, ti, m: self.alerts.append((k, m)))
        self.assertEqual(self.alerts, [], "a quiet week is not a fault")
        self.assertEqual(wd.findings(state, self.today, now=t), [])

    def test_decrypt_failures_stay_a_separate_completeness_verdict(self):
        """Socket open, one person silently muted. Daria, three months. No
        liveness signal can see this, so it must not be folded into one."""
        wd._supervisor_get_state = lambda *a, **k: "started"
        wd._supervisor_start = lambda *a, **k: True
        state = wd.check(self.path, "slug", "token", now=self.now, heartbeat={
            "received_at": int(self.now.timestamp()), "connection": "open",
            "decrypt_failures": 9})
        kinds = [f["kind"] for f in wd.findings(state, self.today, now=self.now)]
        self.assertIn("bridge_completeness", kinds)
        self.assertNotIn("bridge_health", kinds,
                         "a healthy link with a muted sender is not a link fault")

    def test_the_bridge_no_longer_writes_to_home_assistant(self):
        """It is a sensor now. Every decision moved to the tracker, and with
        the alarms went its need for any Home Assistant privilege at all."""
        js = (self.REPO / "whatsapp-bridge/index.js").read_text(encoding="utf-8")
        self.assertNotIn("postAlarm", js)
        self.assertNotIn("SUPERVISOR_TOKEN", js)
        self.assertNotIn("persistent_notification", js)
        cfg = (self.REPO / "whatsapp-bridge/config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("homeassistant_api", cfg,
                         "least privilege: it posts nothing to HA any more")

    def test_deleted_kinds_keep_their_tombstones(self):
        """A cached reconciler_last.json and a LIVE dismissal
        (bridge_blind:2026-07-28T21:08:38.000Z) still carry the old kinds."""
        sys.path.insert(0, str(self.REPO / "cleaning-tracker"))
        import reconcile
        for dead in ("bridge_down", "bridge_watchdog_error", "bridge_link_down",
                     "bridge_unhealable", "bridge_silent", "channel_silent"):
            self.assertIn(dead, reconcile._KIND_DECISION,
                          f"{dead} must resolve, not fall to the default")

    def test_the_verdict_id_is_constant(self):
        """The digest diffs finding ids between nights. An id encoding which
        fault is active would announce itself as brand new every time the
        symptom shifted, and mark the previous one resolved."""
        wd._supervisor_get_state = lambda *a, **k: "error"
        wd._supervisor_start = lambda *a, **k: True
        ids = set()
        for n in range(30):
            t = self.now + timedelta(minutes=5 * n)
            state = wd.check(self.path, "slug", "token", now=t, heartbeat={
                "received_at": int(t.timestamp()), "connection": "close"})
            ids |= {f["id"] for f in wd.findings(state, self.today, now=t)
                    if f["kind"] == "bridge_health"}
        self.assertEqual(ids, {"bridge_health"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
