"""One clock for the whole tracker.

Why this module exists (2026-09-06):

Until this was written the store held timestamps on three different clocks and
the code worked around it rather than fixing it.

  1. Live messages  — UTC ISO from the WhatsApp bridge (`...Z`).
  2. Backfilled rows — naive LOCAL wall time, straight out of `strptime` on a
                       WhatsApp export, never converted. Seven to eight hours
                       adrift from every live row in the same list.
  3. Fact records   — `extracted_at`, the moment facts extraction RAN, which
                       `_cross_chat_facts` then used to decide which of two
                       statements was the more recent.

The third was the dangerous one, and it was invisible for a year because on
live traffic "when it was said" and "when we read it" track within seconds. On
a backfill they diverge by months: every statement in a transcript pasted today
gets today's `extracted_at`, so a cleaner's OLD "I can't do the 18th" outranks
her NEWER live "actually I can" and silently overwrites the correct answer.

The rule now: **event time, in UTC, everywhere.** Anything needing a calendar
day converts to local first — slicing the first ten characters off a UTC string
gets the day wrong for any evening message, and most messages are evenings.

These are pure functions with no Flask dependency, deliberately: `app.py`
cannot be imported in a test on a machine without the add-on's runtime deps,
so anything worth testing does not belong in it.
"""

from datetime import datetime, timezone
import zoneinfo

import gcal as gcal_mod

# Single source of truth, shared with the calendar projection.
LOCAL_TZ = gcal_mod.LOCAL_TZ
LOCAL_ZONE = zoneinfo.ZoneInfo(LOCAL_TZ)


def ts_utc(raw):
    """Parse any stored timestamp into an aware UTC datetime, or None.

    A naive value is read as LOCAL time, because that is what naive values in
    this store have always been: pre-migration backfill rows, and the live
    path's own `datetime.now()` fallback. Reading them as UTC instead — which
    `admin_fix_parse_errors` did — moves them seven hours into the past.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_ZONE)
    return dt.astimezone(timezone.utc)


def utc_iso(dt):
    """Canonical stored form: UTC, `Z`-suffixed, second precision.

    Z-suffixed UTC also sorts correctly as a plain string, which several call
    sites compare directly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_ZONE)
    return (dt.astimezone(timezone.utc)
              .isoformat(timespec="seconds")
              .replace("+00:00", "Z"))


def local_day(raw):
    """The LOCAL calendar day a timestamp falls on, or None.

    Not a string slice. A message sent at 9pm in Vancouver is stored as the
    NEXT day in UTC, so slicing put a chunk of every evening's traffic on the
    wrong day.
    """
    dt = ts_utc(raw)
    if dt is None:
        return None
    return dt.astimezone(LOCAL_ZONE).date()


def has_zone(raw):
    """True if the stored string already carries an offset or `Z`.

    Used by the migration to leave already-correct rows alone, so it is safe
    to run repeatedly and safe on a store that was never broken.
    """
    if not raw or not isinstance(raw, str):
        return False
    tail = raw.strip()[10:]
    return tail.endswith("Z") or "+" in tail or "-" in tail
