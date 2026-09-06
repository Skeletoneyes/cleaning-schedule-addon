"""Unit tests for the attestation + liveness work (ISC-96..114).

`_build_attestation` and `_probe_bot_health` live in app.py, which imports
flask / icalendar / anthropic — none installed on a dev box and none needed by
the logic under test. Same approach as test_gcal_repair.py: extract the real
source text via `ast` and exec it against injected fakes, so a rename or a
reshape fails here loudly rather than passing against a stale copy.

Run: python3 scripts/test_attestation.py
"""
from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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


class FakeResponse:
    def __init__(self, status=200, body=None, raises=None):
        self.status_code = status
        self._body = body or {}
        self._raises = raises

    def json(self):
        if self._raises:
            raise self._raises
        return self._body


def build_ns(*, last_sync, gcal_enabled=True, push_status=None, sync_status=None):
    """A namespace mimicking app.py's module globals for the extracted funcs."""
    return {
        "datetime": datetime,
        "print": lambda *a, **k: None,
        "load_data": lambda: {"last_sync": last_sync},
        "GCAL_ENABLED": gcal_enabled,
        "_read_gcal_status": lambda: push_status,
        "_read_sync_status": lambda: sync_status,
    }


NOW = datetime.now()


# ── ISC-96/97/105: attestation derived from durable state ───────────────────

class BuildAttestation(unittest.TestCase):
    def _attest(self, reconcile_ok=True, **kw):
        ns = build_ns(**kw)
        _extract(["_build_attestation"], ns)
        return ns["_build_attestation"](reconcile_ok)

    def test_all_healthy(self):
        a = self._attest(last_sync=NOW.isoformat(), push_status={"outcome": "ok"})
        self.assertEqual(a, {"sync_ok": True, "push_outcome": "ok", "reconcile_ok": True})

    def test_tonights_failure_is_not_masked_by_yesterdays_success(self):
        """The bug gpt-5.5 caught in 1.26.0: `last_sync` is ~24h old and inside
        the freshness window, so a freshness-derived sync_ok reported true for a
        stage that had just failed — an old good fact masking a new failure,
        which is the exact pattern this feature exists to eliminate."""
        yesterday = (NOW - timedelta(hours=24)).isoformat()
        a = self._attest(
            last_sync=yesterday,
            sync_status={"ok": False, "at": NOW.isoformat(), "error": "feed 500"},
            push_status={"outcome": "ok"})
        self.assertFalse(a["sync_ok"])

    def test_per_attempt_success_reports_ok(self):
        a = self._attest(last_sync=NOW.isoformat(),
                         sync_status={"ok": True, "at": NOW.isoformat()},
                         push_status={"outcome": "ok"})
        self.assertTrue(a["sync_ok"])

    def test_stale_per_attempt_record_is_not_ok(self):
        """A sync that succeeded but 40h ago is not a healthy nightly."""
        old = (NOW - timedelta(hours=40)).isoformat()
        a = self._attest(last_sync=old,
                         sync_status={"ok": True, "at": old},
                         push_status={"outcome": "ok"})
        self.assertFalse(a["sync_ok"])

    def test_corrupt_record_fails_closed(self):
        """SD wear is the common Pi death. A corrupt record must not degrade
        into the freshness fallback and report success off a stale-but-recent
        last_sync — a lying attestation also satisfies the absence alarm, which
        makes it strictly worse than silence. (Advisor finding.)"""
        a = self._attest(last_sync=NOW.isoformat(),
                         sync_status={"unreadable": True},
                         push_status={"outcome": "ok"})
        self.assertFalse(a["sync_ok"])

    def test_absent_record_falls_back_to_freshness(self):
        """First run after upgrade: degrade to the old weaker signal rather
        than hard-failing and alarming on nothing."""
        a = self._attest(last_sync=NOW.isoformat(), sync_status=None,
                         push_status={"outcome": "ok"})
        self.assertTrue(a["sync_ok"])

    def test_stale_sync_reports_not_ok(self):
        old = (NOW - timedelta(hours=40)).isoformat()
        self.assertFalse(self._attest(last_sync=old, push_status={"outcome": "ok"})["sync_ok"])

    def test_absent_sync_reports_not_ok(self):
        self.assertFalse(self._attest(last_sync=None, push_status={"outcome": "ok"})["sync_ok"])

    def test_future_dated_sync_is_broken_not_fresh(self):
        """Same reasoning as the staleness guard: the Pi has no RTC."""
        future = (NOW + timedelta(hours=10)).isoformat()
        self.assertFalse(self._attest(last_sync=future, push_status={"outcome": "ok"})["sync_ok"])

    def test_benign_skew_still_ok(self):
        near = (NOW + timedelta(minutes=5)).isoformat()
        self.assertTrue(self._attest(last_sync=near, push_status={"outcome": "ok"})["sync_ok"])

    def test_unparseable_sync_reports_not_ok_and_does_not_raise(self):
        self.assertFalse(self._attest(last_sync="garbage", push_status={"outcome": "ok"})["sync_ok"])

    def test_gcal_disabled_is_disabled_not_a_fault(self):
        """ISC-105 — GCal switched off is a valid configuration."""
        a = self._attest(last_sync=NOW.isoformat(), gcal_enabled=False, push_status=None)
        self.assertEqual(a["push_outcome"], "disabled")

    def test_never_pushed_is_never_not_ok(self):
        a = self._attest(last_sync=NOW.isoformat(), push_status=None)
        self.assertEqual(a["push_outcome"], "never")

    def test_push_outcome_passes_through(self):
        for outcome in ("ok", "skipped", "failed", "timeout"):
            a = self._attest(last_sync=NOW.isoformat(), push_status={"outcome": outcome})
            self.assertEqual(a["push_outcome"], outcome)

    def test_reconcile_ok_is_reported_as_given(self):
        a = self._attest(reconcile_ok=False, last_sync=NOW.isoformat(), push_status={"outcome": "ok"})
        self.assertFalse(a["reconcile_ok"])

    def test_only_booleans_and_one_string_cross(self):
        """ISC-98 — the attestation must not widen the crossing allowlist."""
        a = self._attest(last_sync=NOW.isoformat(), push_status={"outcome": "ok", "error": "secret!"})
        self.assertEqual(set(a), {"sync_ok", "push_outcome", "reconcile_ok"})
        self.assertIsInstance(a["sync_ok"], bool)
        self.assertIsInstance(a["reconcile_ok"], bool)
        self.assertIsInstance(a["push_outcome"], str)
        self.assertNotIn("secret!", str(a))


# ── ISC-109/110: the inline bot-health probe ────────────────────────────────

class ProbeBotHealth(unittest.TestCase):
    def _probe(self, *, response=None, exc=None, enabled=True,
               url="https://example.com/cleaning/digest", secret="s3cret"):
        class FakeRequests:
            @staticmethod
            def get(u, headers=None, timeout=None):
                FakeRequests.last = {"url": u, "headers": headers}
                if exc:
                    raise exc
                return response
        ns = {
            "requests": FakeRequests,
            "print": lambda *a, **k: None,
            "urlsplit": urlsplit,
            "urlunsplit": urlunsplit,
            "VPS_PUSH_ENABLED": enabled,
            "VPS_PUSH_URL": url,
            "VPS_PUSH_SECRET": secret,
        }
        _extract(["_probe_bot_health"], ns)
        return ns["_probe_bot_health"](), FakeRequests

    def test_healthy_bot(self):
        (ok, detail), _ = self._probe(
            response=FakeResponse(200, {"ok": True, "uptime_s": 900, "last_digest_age_s": 120}))
        self.assertTrue(ok)
        self.assertIn("900", detail)

    def test_probes_the_health_path_not_the_digest_path(self):
        _, fake = self._probe(response=FakeResponse(200, {"ok": True}))
        self.assertTrue(fake.last["url"].endswith("/cleaning/health"))

    def test_sends_the_push_secret(self):
        _, fake = self._probe(response=FakeResponse(200, {"ok": True}))
        self.assertEqual(fake.last["headers"]["X-Push-Secret"], "s3cret")

    def test_reachable_but_unhealthy_is_caught(self):
        """ISC-110 — the case a web-surface ping structurally cannot see."""
        (ok, detail), _ = self._probe(response=FakeResponse(200, {"ok": False}))
        self.assertFalse(ok)
        self.assertIn("not ok", detail)

    def test_non_2xx_is_unhealthy(self):
        (ok, detail), _ = self._probe(response=FakeResponse(502))
        self.assertFalse(ok)
        self.assertIn("502", detail)

    def test_unauthorized_is_unhealthy(self):
        (ok, _), _ = self._probe(response=FakeResponse(401))
        self.assertFalse(ok)

    def test_connection_error_is_unhealthy_not_a_crash(self):
        (ok, detail), _ = self._probe(exc=OSError("connection refused"))
        self.assertFalse(ok)
        self.assertIn("refused", detail)

    def test_unconfigured_is_skipped_not_failed(self):
        """An unconfigured probe must not manufacture a nightly alarm."""
        (ok, detail), _ = self._probe(enabled=False)
        self.assertTrue(ok)
        self.assertIn("skipped", detail)

    def test_route_moved_still_probes_correctly(self):
        """Path swap, not substring replace — the probe survives a moved route."""
        _, fake = self._probe(url="https://example.com/api/v2/cleaning/digest",
                              response=FakeResponse(200, {"ok": True}))
        self.assertEqual(fake.last["url"], "https://example.com/api/v2/cleaning/health")

    def test_unusable_url_is_unhealthy_not_silently_skipped(self):
        """gpt-5.5's finding: the old fallback returned healthy-and-skipped on
        any unrecognised shape, so config drift could switch the monitor off
        without ever saying so — a guard that disables itself."""
        (ok, detail), _ = self._probe(url="not-a-url")
        self.assertFalse(ok)
        self.assertIn("cannot derive", detail)

    def test_malformed_json_is_unhealthy_not_a_crash(self):
        (ok, _), _ = self._probe(response=FakeResponse(200, raises=ValueError("not json")))
        self.assertFalse(ok)


# ── ISC-111/112/113: phone escalation is opt-in and non-fatal ───────────────

class PhoneEscalation(unittest.TestCase):
    def test_service_name_is_not_hardcoded_anywhere(self):
        """ISC-112 — this repo is public; a device name must not be committed."""
        src = (APP_DIR / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("mobile_app_", src)

    def test_escalation_is_opt_in_per_call(self):
        """ISC-111 — routine findings must not buzz a phone every morning."""
        src = (APP_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn("to_phone=False", src, "default must be off")

    def test_only_pipeline_failures_escalate(self):
        """The phone is reserved for 'nobody would otherwise find out'."""
        src = (APP_DIR / "app.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("to_phone=True"), 5,
                         "expected exactly: vps push failed, ical sync failed, "
                         "reconcile failed, bot unhealthy, and (added 2026-09-06) "
                         "the SINGLE bridge health verdict. That fifth one "
                         "replaced twelve bridge-health alerts across three "
                         "channels; if a sixth ever appears here, the same "
                         "sprawl is growing back and the answer is to fold it "
                         "into an existing verdict, not to raise this number.")

    def test_escalation_failure_cannot_break_the_notification(self):
        """ISC-113 — it amplifies an alert; it must not be able to unwind it."""
        src = (APP_DIR / "app.py").read_text(encoding="utf-8")
        i = src.index("def _post_phone_notification")
        body = src[i:src.index("def _post_ha_notification")]
        self.assertIn("except Exception", body)
        self.assertIn("return False", body)


class OneClock(unittest.TestCase):
    """app.py cannot be imported without the add-on's runtime deps, so these
    are source assertions — the same technique the phone-escalation tripwire
    above uses. The behaviour underneath them is unit-tested in test_clock.py.
    """

    def _src(self):
        return (APP_DIR / "app.py").read_text(encoding="utf-8")

    def test_statement_recency_is_event_time_not_processing_time(self):
        """The bug: `extracted_at` is when facts extraction RAN. A transcript
        pasted today stamps every historical line with today, so an old
        statement outranks a newer live one and overwrites it."""
        src = self._src()
        self.assertIn("said_at[m[\"id\"]] = m.get(\"timestamp\")", src,
                      "the message's own timestamp must be collected")
        self.assertIn("stated = said_at.get(msg_id)", src,
                      "recency must key off when it was SAID")
        self.assertNotIn("stated = rec.get(\"extracted_at\") or \"\"\n", src,
                         "processing time must no longer be the ordering key")

    def test_recency_compares_parsed_instants_not_raw_strings(self):
        """Until every row is migrated the store mixes `...Z` with naive local,
        and comparing those as text orders them by spelling."""
        src = self._src()
        self.assertIn("order = _ts_utc(stated)", src)
        self.assertIn("if key not in best or order > best[key][0]:", src)

    def test_transcript_timestamps_are_stored_as_utc(self):
        """A WhatsApp export carries local wall time and says nothing about it."""
        src = self._src()
        self.assertIn('"timestamp": _utc_iso(ts),', src)
        self.assertNotIn('"timestamp": ts.isoformat(timespec="seconds"),', src)

    def test_the_live_path_never_writes_a_naive_timestamp(self):
        src = self._src()
        self.assertNotIn('ts or datetime.now().isoformat(timespec="seconds")', src)
        self.assertIn('ts or _utc_iso(datetime.now(tz=timezone.utc))', src)

    def test_the_migration_exists_and_is_idempotent(self):
        src = self._src()
        self.assertIn("def _migrate_timestamps_to_utc()", src)
        self.assertIn('data.get("timestamps_utc_migrated")', src,
                      "must not re-run on every boot")
        self.assertIn("clock_mod.has_zone(raw)", src,
                      "already-zoned rows must be left alone")

    def test_zone_logic_is_not_duplicated_in_app(self):
        """One source of truth. app.py delegates; clock.py decides."""
        src = self._src()
        self.assertNotIn("ZoneInfo(", src,
                         "timezone construction belongs in clock.py")
        self.assertIn("import clock as clock_mod", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
