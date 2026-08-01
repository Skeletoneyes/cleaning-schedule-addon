"""Deterministic reconciler. Joins data.json bookings against message_facts
and the existing commitment-drift signal, emits a ranked list of findings.

All functions are pure; the caller (app.py) provides pre-computed drift
items (from review_queue) to avoid a circular import.

Findings schema:
    {
        "id":           stable identifier, safe to dedup on across re-runs
        "detector":     which detector emitted this
        "kind":         specific finding subtype
        "severity":     needs-attention | suggest | informational
        "booking_uid":  uid of the related booking (may be None)
        "cleaner":      cleaner name (may be None)
        "date":         YYYY-MM-DD
        "why":          short human-readable explanation
        "evidence":     list of message ids supporting the finding
        "quote":        optional short quote from the evidence message
    }
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

RECONCILER_VERSION = "reconciler-v1"
CONFIRM_THRESHOLD = 0.85
PUSH_STALE_HOURS = 26

_SEVERITY_RANK = {"needs-attention": 0, "suggest": 1, "informational": 2}


def run(data, drift_items, ical_events=None, gcal_events=None, today=None, silence=None,
        gcal_status=None, gcal_read_error=None):
    today = today or date.today()
    today_str = today.isoformat()
    bookings = data.get("bookings", {})
    facts_records = data.get("message_facts", {})
    messages_by_id = {m["id"]: m for m in data.get("messages", []) if m.get("id")}

    dismissed = data.get("dismissed_findings", {}) or {}

    findings = []
    findings.extend(_drift(drift_items))
    findings.extend(_channel_silence(silence, today_str))
    findings.extend(_facts_vs_bookings(bookings, facts_records, today_str))
    findings.extend(_fact_timeline(facts_records, messages_by_id, today_str))
    findings.extend(_schedule_vs_bookings(bookings, facts_records, today_str, messages_by_id))
    if ical_events is not None:
        findings.extend(_ical_vs_bookings(bookings, ical_events, today_str))
    if gcal_events is not None:
        findings.extend(_bookings_vs_gcal(data, gcal_events, today_str))

    # Injected here, inside run(), BEFORE dedup — so counts derived downstream
    # by filter_and_sort automatically include these findings. A prior design
    # injected a stale-push sentinel downstream of counts and left counts
    # disagreeing with findings; a consumer keying off counts read a bad
    # night as healthy. Never repeat that.
    push_health = _gcal_push_health(gcal_status, today_str, gcal_read_error=gcal_read_error)
    findings.extend(push_health)

    # Correlate: once we know the GCal WRITE pipe itself is broken or stale,
    # every bookings_vs_gcal finding is downstream noise explained by that
    # one root cause — drop them and fold the count into the push-health
    # finding's `why`. Only bookings_vs_gcal is downstream of the push; no
    # other detector's findings are ever dropped here.
    if push_health:
        suppressed = [f for f in findings if f.get("detector") == "bookings_vs_gcal"]
        if suppressed:
            plural = "s" if len(suppressed) != 1 else ""
            note = (
                f" {len(suppressed)} calendar-content finding{plural} are "
                "suppressed as downstream of this."
            )
            for f in push_health:
                f["why"] = f["why"] + note
            findings = [f for f in findings if f.get("detector") != "bookings_vs_gcal"]

    # Stable dedup on id — a later detector shouldn't re-emit what an earlier
    # one already claimed.
    seen = {}
    for f in findings:
        seen.setdefault(f["id"], f)
    findings = list(seen.values())

    return filter_and_sort({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": RECONCILER_VERSION,
        "findings_raw": findings,
    }, dismissed)


STALE_DAYS = 5  # findings whose date is older than this are auto-suppressed

def filter_and_sort(result, dismissed):
    """Re-apply dismissed filter + sort + count over a cached raw result.

    Used both by the fresh run() path and by the dismiss/undismiss path so a
    dismiss doesn't require re-fetching iCal / GCal. The raw pre-filter list
    lives in `findings_raw`; `findings` + `counts` are derived each call.
    """
    raw = list(result.get("findings_raw") or result.get("findings") or [])
    cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    dismissed_count = sum(1 for f in raw if f["id"] in dismissed)
    stale_count = sum(1 for f in raw if f["id"] not in dismissed and f.get("date") and f["date"] < cutoff)
    kept = [f for f in raw if f["id"] not in dismissed and not (f.get("date") and f["date"] < cutoff)]
    kept.sort(key=lambda f: (
        _SEVERITY_RANK.get(f["severity"], 99),
        f.get("date") or "",
        f["id"],
    ))
    counts = {"total": len(kept), "dismissed": dismissed_count, "stale": stale_count}
    for f in kept:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {
        "generated_at": result.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
        "version": result.get("version") or RECONCILER_VERSION,
        "findings": kept,
        "findings_raw": raw,
        "counts": counts,
    }


# ── Detector 3: commitment drift ────────────────────────────────────────────
# Already computed by review_queue; this just re-shapes into finding form so
# the Conflicts tab has one unified list. Severity is needs-attention because
# the notify queue's whole point is "tell a cleaner something changed".

def _drift(items):
    why_map = {
        "new": "newly assigned — not yet notified",
        "changed": "assignment changed since last notified",
        "cancelled": "booking cancelled — cleaner still has prior commitment",
        "unassigned": "booking needs a cleaner",
    }
    out = []
    for it in items:
        k = it["kind"]
        cleaner = it.get("cleaner")
        lead = f"{cleaner}: " if cleaner else ""
        out.append({
            "id": f"drift:{it['uid']}:{k}",
            "detector": "drift",
            "kind": f"drift_{k}",
            "severity": "needs-attention",
            "booking_uid": it["uid"],
            "cleaner": cleaner,
            "date": it.get("date"),
            "why": lead + why_map.get(k, k),
            "evidence": [],
        })
    return out


# ── Detector 8: GCal push health (pipeline, not calendar content) ──────────
# Findings about the WRITE PATH itself, as opposed to what bookings_vs_gcal
# finds by comparing calendar *content*. gcal_status is the dict persisted by
# app.py's _gcal_push() (D2/D3) — {"ok", "outcome", "at", "error", "attempt",
# "last_ok_at", "stats"}. A failed/skipped push means the calendar may be
# stale because the WRITE failed, which is a different story from "the
# calendar drifted" (bookings_vs_gcal's story) and needs to say so explicitly
# so a human doesn't chase the wrong repair.
#
# Both findings are dated today_str (NOT gcal_status["at"]) so filter_and_sort's
# STALE_DAYS suppression can never drop the very signal this exists to raise,
# and both ids are stable strings so the nightly digest diff alarms once, not
# every morning the push stays broken.

def _redact_error(msg, limit=160):
    """Make an exception string safe to put in a `why` that crosses to the VPS.

    Google's HttpError embeds the full request URL, which contains the calendar
    id. That id is not in this public repo and lives in /data/options.json with
    the other configured secrets, so it must not ride the digest payload to the
    VPS — the VPS is credential-free by design and the allowlist protects keys,
    not values (ISC-41's standing caveat, applied here deliberately). Strip any
    URL down to its scheme+host and cap the length; the status code and reason
    are what a human needs, and the full text is still in the add-on log.
    """
    if not msg:
        return "no error message recorded"
    out = re.sub(r"https?://([^/\s]+)\S*", r"https://\1/…", str(msg))
    return out[:limit] + ("…" if len(out) > limit else "")


def _gcal_push_health(gcal_status, today_str, now=None, gcal_read_error=None):
    """Pure. Findings about the push itself (never about calendar content).

    Emits at most two:
      1. "pipeline:gcal-push-failed" — when gcal_status is present and not ok.
      2. "pipeline:stale-push" — when last_ok_at is absent or older than
         PUSH_STALE_HOURS. A missing/None gcal_status counts as stale (never
         pushed is not healthy).

    `now` is injectable for tests; defaults to datetime.now().
    """
    now = now or datetime.now()
    out = []

    if gcal_read_error:
        out.append({
            "id": "pipeline:gcal-read-failed",
            "detector": "pipeline",
            "kind": "gcal_read_failed",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"could not read Google Calendar ({_redact_error(gcal_read_error)}) — "
                "calendar-content checks were skipped this run, so drift there is "
                "currently unmeasured rather than absent"
            ),
            "evidence": [],
        })

    if gcal_status is not None and not gcal_status.get("ok"):
        outcome = gcal_status.get("outcome") or "unknown"
        error = _redact_error(gcal_status.get("error"))
        out.append({
            "id": "pipeline:gcal-push-failed",
            "detector": "pipeline",
            "kind": "gcal_push_failed",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"the last Google Calendar push {outcome} ({error}) — the "
                "calendar may be out of date because the WRITE failed, not "
                "because the calendar itself drifted"
            ),
            "evidence": [],
        })

    last_ok_at = (gcal_status or {}).get("last_ok_at")
    age_desc = None
    if not last_ok_at:
        age_desc = "never" if gcal_status is None else "no successful push recorded"
    else:
        try:
            last_ok_dt = datetime.fromisoformat(last_ok_at)
            age_hours = (now - last_ok_dt).total_seconds() / 3600
            if age_hours >= PUSH_STALE_HOURS:
                age_desc = f"{age_hours:.1f}h ago"
        except (ValueError, TypeError):
            age_desc = f"unparseable timestamp {last_ok_at!r}"

    if age_desc is not None:
        out.append({
            "id": "pipeline:stale-push",
            "detector": "pipeline",
            "kind": "stale_push",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"no successful Google Calendar push in over {PUSH_STALE_HOURS}h "
                f"(last success: {age_desc}) — the calendar may be silently stale"
            ),
            "evidence": [],
        })

    return out


# ── Detector 7: channel silence (WhatsApp "going dark") ─────────────────────
# The 1.1.0 bridge health alarms catch *error bursts* (≥5 decrypt failures /
# 10 min, disconnect flapping, forward failures). They structurally cannot
# catch the failure that dropped 3 months of Daria's messages: a quiet
# per-sender/per-group mute where messages simply stop arriving with no error
# to count. This detector catches absence-of-signal instead of presence-of-
# error, keyed off the always-running tracker (not the bridge, which can't
# reliably alarm on its own death).
#
# `silence` is pre-computed in app.py (which owns message iteration + the
# clock) so this stays a pure function:
#   {
#     "enabled": bool,
#     "bridge_days": int, "dead_days": int, "min_group_msgs": int,
#     "last_any_age_days": float | None,   # age of newest message from ANY group
#     "groups": { group_jid: {"label": str, "age_days": float|None, "count": int,
#                             "last_ts": iso|None} },
#   }
# Findings are dated `today_str` (NOT the last-message date) so filter_and_sort's
# STALE_DAYS suppression can't drop the very signal this exists to surface, and
# ids are stable so a genuinely-dark channel alarms once (via the digest diff),
# not every morning.

def _channel_silence(silence, today_str):
    if not silence or not silence.get("enabled", True):
        return []

    bridge_days = silence.get("bridge_days", 7)
    dead_days = silence.get("dead_days", 14)
    min_msgs = silence.get("min_group_msgs", 10)
    last_any = silence.get("last_any_age_days")

    # 1. Whole-bridge silence — nothing from ANY group in bridge_days. Almost
    #    always the bridge is down / logged out. Emit ONE finding and stop; a
    #    dead bridge shouldn't also spam a per-group finding for every channel.
    if last_any is None or last_any >= bridge_days:
        span = "ever" if last_any is None else f"{int(last_any)} days"
        return [{
            "id": "bridge_silent",
            "detector": "channel_silence",
            "kind": "bridge_silent",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"No WhatsApp messages received from ANY group in {span} — the "
                f"bridge is likely down or logged out. Check the WhatsApp Bridge "
                f"add-on Log tab; re-pair (uninstall→install→scan QR) if needed."
            ),
            "evidence": [],
        }]

    # 2. Per-group silence — a channel that was demonstrably active
    #    (≥ min_group_msgs historical messages) has gone quiet past dead_days.
    #    This is the Daria failure mode: one group muted while others forward
    #    fine, so the bridge and everyone else look healthy.
    out = []
    for gjid, g in (silence.get("groups") or {}).items():
        if g.get("count", 0) < min_msgs:
            continue
        age = g.get("age_days")
        if age is None or age < dead_days:
            continue
        label = g.get("label") or gjid
        last_ts = (g.get("last_ts") or "")[:10] or "unknown"
        out.append({
            "id": f"channel_silent:{gjid}",
            "detector": "channel_silence",
            "kind": "channel_silent",
            "severity": "needs-attention",
            "booking_uid": None,
            "cleaner": None,
            "date": today_str,
            "why": (
                f"No messages from the '{label}' WhatsApp group in {int(age)} days "
                f"(last: {last_ts}). This channel may have gone dark — the "
                f"per-sender mute that silently dropped 3 months of Daria's "
                f"messages. Send a test message in the group and confirm it "
                f"appears in Review; re-pair the bridge if it doesn't."
            ),
            "evidence": [],
        })
    return out


# ── Detector 4: facts ⇄ bookings ────────────────────────────────────────────
# For each confirm/decline fact, look up the booking on that date and compare
# to what the cleaner said. Only future dates — past bookings are history, not
# conflict material.

def _facts_vs_bookings(bookings, facts_records, today_str):
    by_date = {}
    for uid, b in bookings.items():
        if b.get("status") == "cancelled" or b.get("type") == "custom_stay":
            continue
        d = b.get("end")
        if d:
            by_date.setdefault(d, []).append((uid, b))

    out = []
    for msg_id, rec in facts_records.items():
        for f in rec.get("facts", []):
            kind = f.get("kind")
            tgt = f.get("target_date")
            cleaner = f.get("cleaner")
            if not tgt or not cleaner or tgt < today_str:
                continue
            conf = f.get("confidence") or 0.0
            quote = f.get("evidence") or ""
            matches = by_date.get(tgt, [])

            if kind == "confirm" and conf >= CONFIRM_THRESHOLD:
                if not matches:
                    out.append({
                        "id": f"confirm_no_booking:{cleaner}:{tgt}:{msg_id}",
                        "detector": "facts_vs_bookings",
                        "kind": "confirm_no_booking",
                        "severity": "informational",
                        "booking_uid": None,
                        "cleaner": cleaner,
                        "date": tgt,
                        "why": f"{cleaner} confirmed for {tgt} but no booking exists on that date",
                        "evidence": [msg_id],
                        "quote": quote,
                    })
                    continue
                for uid, b in matches:
                    current = b.get("cleaner")
                    if current is None:
                        out.append({
                            "id": f"unrecorded_confirmation:{uid}:{cleaner}",
                            "detector": "facts_vs_bookings",
                            "kind": "unrecorded_confirmation",
                            "severity": "suggest",
                            "booking_uid": uid,
                            "cleaner": cleaner,
                            "date": tgt,
                            "why": f"{cleaner} confirmed for {tgt} but booking is unassigned",
                            "evidence": [msg_id],
                            "quote": quote,
                        })
                    elif current != cleaner:
                        out.append({
                            "id": f"contested_cleaner:{uid}:{cleaner}",
                            "detector": "facts_vs_bookings",
                            "kind": "contested_cleaner",
                            "severity": "needs-attention",
                            "booking_uid": uid,
                            "cleaner": cleaner,
                            "date": tgt,
                            "why": f"{cleaner} confirmed for {tgt} but booking is assigned to {current}",
                            "evidence": [msg_id],
                            "quote": quote,
                        })
                    # else: current == cleaner — exactly what we expect.

            elif kind == "decline":
                for uid, b in matches:
                    if b.get("cleaner") == cleaner:
                        out.append({
                            "id": f"decline_still_assigned:{uid}:{cleaner}",
                            "detector": "facts_vs_bookings",
                            "kind": "decline_still_assigned",
                            "severity": "needs-attention",
                            "booking_uid": uid,
                            "cleaner": cleaner,
                            "date": tgt,
                            "why": f"{cleaner} declined {tgt} but is still assigned to it",
                            "evidence": [msg_id],
                            "quote": quote,
                        })
    return out


# ── Detector 5: fact ⇄ fact timeline ────────────────────────────────────────
# Surface (cleaner, date) pairs where the cleaner both confirmed and declined
# at different times. Latest wins — informational only (detector 4 will also
# flag decline_still_assigned if the latest-state warrants action).

def _fact_timeline(facts_records, messages_by_id, today_str):
    events = {}
    for msg_id, rec in facts_records.items():
        ts = (messages_by_id.get(msg_id) or {}).get("timestamp", "")
        for f in rec.get("facts", []):
            kind = f.get("kind")
            if kind not in ("confirm", "decline"):
                continue
            cleaner = f.get("cleaner")
            tgt = f.get("target_date")
            if not cleaner or not tgt:
                continue
            events.setdefault((cleaner, tgt), []).append(
                (ts, kind, msg_id, f.get("evidence") or "")
            )

    out = []
    for (cleaner, tgt), evts in events.items():
        if tgt < today_str or len(evts) < 2:
            continue
        kinds = {e[1] for e in evts}
        if kinds != {"confirm", "decline"}:
            continue
        evts.sort()
        first_kind = evts[0][1]
        latest_ts, latest_kind, _, latest_quote = evts[-1]
        out.append({
            "id": f"changed_mind:{cleaner}:{tgt}",
            "detector": "fact_timeline",
            "kind": "changed_mind",
            "severity": "informational",
            "booking_uid": None,
            "cleaner": cleaner,
            "date": tgt,
            "why": (
                f"{cleaner} said {first_kind} then {latest_kind} for {tgt}; "
                f"latest is {latest_kind}"
            ),
            "evidence": [e[2] for e in evts],
            "quote": latest_quote,
        })
    return out


# ── Detector 6: host schedule_assertion ⇄ bookings ──────────────────────────
# The host (Michelle/Josh) posts a schedule saying "Itzel, May 19". Booking on
# that date should reflect that cleaner. If it's unset or assigned to someone
# else, surface as a finding — the host asserted a plan the data doesn't match.

def _schedule_vs_bookings(bookings, facts_records, today_str, messages_by_id=None):
    by_date = {}
    for uid, b in bookings.items():
        if b.get("status") == "cancelled" or b.get("type") == "custom_stay":
            continue
        d = b.get("end")
        if d:
            by_date.setdefault(d, []).append((uid, b))

    # Phase 1: collapse to one schedule_assertion per (cleaner, target_date),
    # keeping the one with the latest source-message timestamp.
    latest_assertions = {}
    for msg_id, rec in facts_records.items():
        ts = (messages_by_id or {}).get(msg_id, {}).get("timestamp", "")
        for f in rec.get("facts", []):
            if f.get("kind") != "schedule_assertion":
                continue
            tgt = f.get("target_date")
            cleaner = f.get("cleaner")
            if not tgt or not cleaner or tgt < today_str:
                continue
            key = (cleaner, tgt)
            existing = latest_assertions.get(key)
            if existing is None or ts > existing[0]:
                latest_assertions[key] = (ts, msg_id, f)

    # Phase 2: emit findings using only the latest assertion per (cleaner, date).
    out = []
    for (cleaner, tgt), (_, msg_id, f) in latest_assertions.items():
        quote = f.get("evidence") or ""
        for uid, b in by_date.get(tgt, []):
            current = b.get("cleaner")
            if current == cleaner:
                continue
            if current is None:
                out.append({
                    "id": f"schedule_unassigned:{uid}:{cleaner}",
                    "detector": "schedule_vs_bookings",
                    "kind": "schedule_unassigned",
                    "severity": "suggest",
                    "booking_uid": uid,
                    "cleaner": cleaner,
                    "date": tgt,
                    "why": f"host scheduled {cleaner} for {tgt} but booking is unassigned",
                    "evidence": [msg_id],
                    "quote": quote,
                })
            else:
                out.append({
                    "id": f"schedule_mismatch:{uid}:{cleaner}",
                    "detector": "schedule_vs_bookings",
                    "kind": "schedule_mismatch",
                    "severity": "needs-attention",
                    "booking_uid": uid,
                    "cleaner": cleaner,
                    "date": tgt,
                    "why": f"host scheduled {cleaner} for {tgt} but booking is assigned to {current}",
                    "evidence": [msg_id],
                    "quote": quote,
                })
    return out


# ── Detector 1: Airbnb iCal ⇄ bookings ──────────────────────────────────────
# Caller parses the feed and passes a list of {uid, start, end} dicts. Three
# shapes of drift: an iCal UID the local data has never seen, an active local
# airbnb booking that's gone from the feed, and matching UIDs whose dates
# disagree. All filtered to checkouts >= today — historical drift is not
# action-worthy.

def _ical_vs_bookings(bookings, ical_events, today_str):
    out = []
    ical_by_uid = {e["uid"]: e for e in ical_events if e.get("uid")}

    for uid, ev in ical_by_uid.items():
        end = ev.get("end") or ""
        if end < today_str:
            continue
        b = bookings.get(uid)
        if b is None:
            out.append({
                "id": f"ical_missing_booking:{uid}",
                "detector": "ical_vs_bookings",
                "kind": "ical_missing_booking",
                "severity": "needs-attention",
                "booking_uid": uid,
                "cleaner": None,
                "date": end,
                "why": f"Airbnb iCal has reservation {uid} ({ev.get('start')}→{end}) but local bookings do not — sync may be stale",
                "evidence": [],
            })
            continue
        if b.get("status") == "cancelled":
            out.append({
                "id": f"ical_resurrected:{uid}",
                "detector": "ical_vs_bookings",
                "kind": "ical_resurrected",
                "severity": "needs-attention",
                "booking_uid": uid,
                "cleaner": b.get("cleaner"),
                "date": end,
                "why": f"booking is cancelled locally but still present in Airbnb iCal ({ev.get('start')}→{end})",
                "evidence": [],
            })
            continue
        if b.get("start") != ev.get("start") or b.get("end") != end:
            out.append({
                "id": f"ical_date_mismatch:{uid}",
                "detector": "ical_vs_bookings",
                "kind": "ical_date_mismatch",
                "severity": "needs-attention",
                "booking_uid": uid,
                "cleaner": b.get("cleaner"),
                "date": end,
                "why": (
                    f"dates differ — local {b.get('start')}→{b.get('end')}, "
                    f"iCal {ev.get('start')}→{end}"
                ),
                "evidence": [],
            })

    for uid, b in bookings.items():
        if b.get("type", "airbnb") != "airbnb":
            continue
        if b.get("status") != "active":
            continue
        end = b.get("end") or ""
        if end < today_str:
            continue
        if uid in ical_by_uid:
            continue
        out.append({
            "id": f"booking_not_in_ical:{uid}",
            "detector": "ical_vs_bookings",
            "kind": "booking_not_in_ical",
            "severity": "needs-attention",
            "booking_uid": uid,
            "cleaner": b.get("cleaner"),
            "date": end,
            "why": f"active airbnb booking {b.get('start')}→{end} not present in current Airbnb iCal — cancelled upstream?",
            "evidence": [],
        })
    return out


# ── Detector 2: bookings ⇄ GCal ─────────────────────────────────────────────
# Caller fetches tagged GCal events via gcal._list_existing and passes them as
# {uid: event_dict} (uid = the private.uid tag, e.g. "clean:<booking_uid>").
# We rebuild the desired projection with gcal._desired_events and diff. This
# mirrors what sync_to_gcal converges to on every save — these findings mean
# either sync hasn't run successfully since the last change, or GCal is out of
# reach.

def _bookings_vs_gcal(data, gcal_events, today_str):
    try:
        from gcal import _desired_events, _events_equal
    except ImportError:
        return []

    desired = _desired_events(data)
    existing = gcal_events or {}
    out = []

    def _event_date(uid, body):
        start = body.get("start") or {}
        return start.get("date") or (start.get("dateTime") or "")[:10] or ""

    for uid, body in desired.items():
        d = _event_date(uid, body)
        if d and d < today_str:
            continue
        booking_uid = ((body.get("extendedProperties") or {}).get("private") or {}).get("booking_uid")
        kind = ((body.get("extendedProperties") or {}).get("private") or {}).get("kind")
        ex = existing.get(uid)
        if ex is None:
            out.append({
                "id": f"gcal_missing_event:{uid}",
                "detector": "bookings_vs_gcal",
                "kind": "gcal_missing_event",
                "severity": "needs-attention",
                "booking_uid": booking_uid,
                "cleaner": (data.get("bookings", {}).get(booking_uid, {}) or {}).get("cleaner"),
                "date": d or None,
                "why": f"{kind} event for {booking_uid} missing from Google Calendar — sync has not run",
                "evidence": [],
            })
            continue
        if not _events_equal(ex, body):
            out.append({
                "id": f"gcal_stale_event:{uid}",
                "detector": "bookings_vs_gcal",
                "kind": "gcal_stale_event",
                "severity": "suggest",
                "booking_uid": booking_uid,
                "cleaner": (data.get("bookings", {}).get(booking_uid, {}) or {}).get("cleaner"),
                "date": d or None,
                "why": f"Google Calendar {kind} for {booking_uid} is out of date — sync has not converged",
                "evidence": [],
            })

    for uid, ev in existing.items():
        if uid in desired:
            continue
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        booking_uid = priv.get("booking_uid")
        d = (ev.get("start") or {}).get("date") or ((ev.get("start") or {}).get("dateTime") or "")[:10] or ""
        if d and d < today_str:
            continue
        bookings = data.get("bookings", {})
        b = bookings.get(booking_uid) if booking_uid else None
        if b is not None and b.get("status") != "cancelled":
            # Sync would patch, not delete — skip.
            continue
        out.append({
            "id": f"gcal_orphan:{uid}",
            "detector": "bookings_vs_gcal",
            "kind": "gcal_orphan",
            "severity": "suggest",
            "booking_uid": booking_uid,
            "cleaner": None,
            "date": d or None,
            "why": f"Google Calendar has tagged event {uid} with no matching active local booking — sync has not cleaned up",
            "evidence": [],
        })
    return out
