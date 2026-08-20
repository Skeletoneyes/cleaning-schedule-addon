"""A valid-but-empty Airbnb feed must not cancel the whole forward schedule.

Found 2026-08-20. `_merge_ical_events` marked every airbnb booking absent from
the feed as cancelled with no floor on the count, so a 200 response carrying
zero `Reserved` events wiped the forward schedule — and every downstream signal
agreed it was a healthy night, because the sweep itself was the only thing that
knew. The digest even reported the vanished findings as "resolved".

The guard bails BEFORE save_data, so a suspicious feed writes nothing at all.

Run: python3 scripts/test_ical_mass_cancel.py
"""
from __future__ import annotations

import ast
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "cleaning-tracker"


def _extract(names, ns):
    src = (APP_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names}
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"symbol(s) not found in app.py: {sorted(missing)}")
    for name in names:
        exec(compile(ast.Module(body=[found[name]], type_ignores=[]), "app.py", "exec"), ns)
    return ns


TODAY = date.today()


def _d(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


class FakeEvent(dict):
    def walk(self, _):
        return self.get("_events", [])


def _vevent(uid, start, end):
    class DT:
        def __init__(self, v):
            self.v = v

        @property
        def dt(self):
            return datetime.strptime(self.v, "%Y-%m-%d").date()

    return {"SUMMARY": "Reserved", "UID": uid, "DTSTART": DT(start), "DTEND": DT(end)}


class Cal:
    def __init__(self, events):
        self._events = events

    def walk(self, _):
        return self._events


class MassCancelCase(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.status = []
        # six future active airbnb bookings — the live shape
        self.data = {"bookings": {
            f"u{i}": {"start": _d(i * 5), "end": _d(i * 5 + 3), "status": "active",
                      "type": "airbnb", "cleaner": "Itzel"}
            for i in range(1, 7)
        }, "last_sync": None}
        self.ns = {
            "date": date, "datetime": datetime,
            "load_data": lambda: self.data,
            "save_data": lambda d: self.saved.append(d),
            "_write_sync_status": lambda ok, err=None: self.status.append((ok, err)),
        }
        _extract(["SuspiciousFeed", "_merge_ical_events"], self.ns)
        # module-level constants the function closes over
        self.ns["MASS_CANCEL_MIN"] = 4
        self.ns["MASS_CANCEL_RATIO"] = 0.5
        self.merge = self.ns["_merge_ical_events"]
        self.SuspiciousFeed = self.ns["SuspiciousFeed"]

    def _uids(self):
        return [(u, b["status"]) for u, b in self.data["bookings"].items()]

    def test_empty_feed_is_refused_and_writes_nothing(self):
        with self.assertRaises(self.SuspiciousFeed):
            self.merge(Cal([]))
        self.assertEqual(self.saved, [], "a refused merge still called save_data")
        self.assertTrue(all(s == "active" for _, s in self._uids()),
                        "a refused merge mutated booking status")

    def test_error_message_names_the_counts(self):
        with self.assertRaises(self.SuspiciousFeed) as ctx:
            self.merge(Cal([]))
        msg = str(ctx.exception)
        self.assertIn("0 reservation(s)", msg)
        self.assertIn("6 of 6", msg)

    def test_one_real_cancellation_still_applies(self):
        """The guard must not break the thing it guards. Five of six returned
        means one genuine cancellation — that is a normal night."""
        events = [_vevent(f"u{i}", _d(i * 5), _d(i * 5 + 3)) for i in range(1, 6)]
        self.merge(Cal(events))
        self.assertEqual(len(self.saved), 1, "a normal sync did not save")
        self.assertEqual(self.data["bookings"]["u6"]["status"], "cancelled")
        self.assertEqual(self.data["bookings"]["u1"]["status"], "active")

    def test_three_cancellations_are_below_the_floor(self):
        events = [_vevent(f"u{i}", _d(i * 5), _d(i * 5 + 3)) for i in range(1, 4)]
        self.merge(Cal(events))
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.data["bookings"]["u6"]["status"], "cancelled")

    def test_four_of_six_trips_the_guard(self):
        events = [_vevent(f"u{i}", _d(i * 5), _d(i * 5 + 3)) for i in range(1, 3)]
        with self.assertRaises(self.SuspiciousFeed):
            self.merge(Cal(events))
        self.assertEqual(self.saved, [])

    def test_past_bookings_are_not_counted_as_cancellations(self):
        """A booking whose checkout has passed leaves the feed normally and is
        marked complete, not cancelled. It must not push the guard over."""
        self.data["bookings"] = {
            f"p{i}": {"start": _d(-30 - i), "end": _d(-20 - i), "status": "active",
                      "type": "airbnb", "cleaner": "Itzel"}
            for i in range(6)
        }
        self.data["bookings"]["f1"] = {"start": _d(5), "end": _d(8), "status": "active",
                                       "type": "airbnb", "cleaner": "Itzel"}
        self.merge(Cal([_vevent("f1", _d(5), _d(8))]))
        self.assertEqual(len(self.saved), 1, "past checkouts tripped the guard")
        self.assertTrue(all(self.data["bookings"][f"p{i}"]["status"] == "complete"
                            for i in range(6)))

    def test_manual_cleanings_are_untouched(self):
        self.data["bookings"]["manual-1"] = {"start": _d(4), "end": _d(4),
                                             "status": "active", "type": "manual_cleaning"}
        events = [_vevent(f"u{i}", _d(i * 5), _d(i * 5 + 3)) for i in range(1, 6)]
        self.merge(Cal(events))
        self.assertEqual(self.data["bookings"]["manual-1"]["status"], "active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
