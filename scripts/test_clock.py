"""Unit tests for the one-clock rules.

The bug these exist to prevent (2026-09-06): the store held timestamps on
three clocks — UTC from the bridge, naive local from WhatsApp exports, and
`extracted_at` (processing time) used as if it were statement time. The third
meant a backfilled OLD statement outranked a NEWER live one, because a
transcript pasted today stamps every line in it with today.

Run: python3 scripts/test_clock.py
"""

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))

import clock


class ParsingTests(unittest.TestCase):
    def test_utc_and_naive_local_resolve_to_the_same_instant(self):
        """The exact pair that sat 7 hours apart in the live store."""
        live = clock.ts_utc("2026-09-02T22:21:20.000Z")
        backfill = clock.ts_utc("2026-09-02T15:21:20")
        self.assertEqual(live, backfill)

    def test_naive_is_read_as_local_not_utc(self):
        """`admin_fix_parse_errors` used to stamp naive values as UTC, moving
        every backfilled message seven hours into the past."""
        dt = clock.ts_utc("2026-09-02T15:21:20")
        self.assertEqual(dt.hour, 22, "15:21 PDT is 22:21 UTC, not 15:21 UTC")

    def test_explicit_offset_is_respected(self):
        self.assertEqual(clock.ts_utc("2026-09-02T15:21:20-07:00"),
                         clock.ts_utc("2026-09-02T22:21:20Z"))

    def test_unparseable_is_none_not_an_exception(self):
        for bad in (None, "", "not a date", 12345, "2026-13-99T99:99:99"):
            self.assertIsNone(clock.ts_utc(bad))

    def test_round_trip_is_stable(self):
        raw = "2026-09-02T22:21:20Z"
        self.assertEqual(clock.utc_iso(clock.ts_utc(raw)), raw)


class LocalDayTests(unittest.TestCase):
    def test_an_evening_message_is_not_pushed_to_the_next_day(self):
        """9pm in Vancouver is the NEXT day in UTC. Slicing the string — which
        is what `_msg_day` used to do — filed a third of every evening's
        traffic under tomorrow."""
        self.assertEqual(clock.local_day("2026-09-03T04:00:00Z").isoformat(),
                         "2026-09-02")

    def test_a_daytime_message_is_unaffected(self):
        self.assertEqual(clock.local_day("2026-09-02T22:21:20Z").isoformat(),
                         "2026-09-02")

    def test_naive_local_gives_its_own_day(self):
        self.assertEqual(clock.local_day("2026-09-02T15:21:20").isoformat(),
                         "2026-09-02")


class DaylightSavingTests(unittest.TestCase):
    """The migration shifts ~300 historical rows spanning April to September.
    A fixed offset would be wrong for half of them."""

    def test_summer_is_utc_minus_seven(self):
        dt = clock.ts_utc("2026-07-15T12:00:00")
        self.assertEqual(dt.hour, 19, "PDT is UTC-7")

    def test_winter_is_utc_minus_eight(self):
        dt = clock.ts_utc("2026-01-15T12:00:00")
        self.assertEqual(dt.hour, 20, "PST is UTC-8")

    def test_the_offset_is_not_hardcoded(self):
        summer = clock.ts_utc("2026-07-15T12:00:00")
        winter = clock.ts_utc("2026-01-15T12:00:00")
        self.assertNotEqual(summer.hour, winter.hour,
                            "a fixed offset would corrupt half the archive")


class MigrationGuardTests(unittest.TestCase):
    """`has_zone` is what makes the migration idempotent and safe to re-run."""

    def test_already_zoned_values_are_left_alone(self):
        for already in ("2026-09-02T22:21:20Z", "2026-09-02T22:21:20.000Z",
                        "2026-09-02T15:21:20-07:00", "2026-09-02T22:21:20+00:00"):
            self.assertTrue(clock.has_zone(already), already)

    def test_naive_values_are_migration_targets(self):
        for naive in ("2026-09-02T15:21:20", "2026-09-02T15:21:20.123456"):
            self.assertFalse(clock.has_zone(naive), naive)

    def test_the_date_portion_never_trips_the_offset_check(self):
        """The dashes in `2026-09-02` must not read as a UTC offset."""
        self.assertFalse(clock.has_zone("2026-09-02T15:21:20"))

    def test_migrating_twice_is_a_no_op(self):
        raw = "2026-09-02T15:21:20"
        once = clock.utc_iso(clock.ts_utc(raw))
        self.assertFalse(clock.has_zone(raw))
        self.assertTrue(clock.has_zone(once))
        twice = once if clock.has_zone(once) else clock.utc_iso(clock.ts_utc(once))
        self.assertEqual(once, twice)


class OrderingTests(unittest.TestCase):
    """The actual bug: which of two statements wins."""

    def test_an_older_backfilled_statement_loses_to_a_newer_live_one(self):
        # She said "can't do it" on Sep 3, then "actually I can" on Sep 4.
        # The Sep 3 line arrives via a transcript pasted today.
        older_said = clock.ts_utc("2026-09-03T18:00:00")
        newer_said = clock.ts_utc("2026-09-04T18:00:00Z")
        self.assertGreater(newer_said, older_said,
                           "ordering by WHEN IT WAS SAID puts the newer first")

        # Ordering by extraction time inverts it — this is what used to happen.
        older_extracted = datetime.now(tz=timezone.utc)                 # pasted today
        newer_extracted = clock.ts_utc("2026-09-04T18:05:00Z")          # read live
        self.assertGreater(older_extracted, newer_extracted,
                           "processing time makes the OLD statement win")

    def test_z_suffixed_utc_sorts_correctly_as_plain_text(self):
        """Several call sites compare the stored strings directly."""
        rows = ["2026-09-04T18:00:00Z", "2026-09-02T22:21:20Z", "2026-09-03T01:00:00Z"]
        self.assertEqual(sorted(rows), sorted(rows, key=clock.ts_utc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
