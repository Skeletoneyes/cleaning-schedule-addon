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

from datetime import date, datetime

RECONCILER_VERSION = "reconciler-v1"
CONFIRM_THRESHOLD = 0.85

_SEVERITY_RANK = {"needs-attention": 0, "suggest": 1, "informational": 2}


def run(data, drift_items, ical_events=None, gcal_events=None, today=None):
    today = today or date.today()
    today_str = today.isoformat()
    bookings = data.get("bookings", {})
    facts_records = data.get("message_facts", {})
    messages_by_id = {m["id"]: m for m in data.get("messages", []) if m.get("id")}

    dismissed = data.get("dismissed_findings", {}) or {}

    findings = []
    findings.extend(_drift(drift_items))
    findings.extend(_facts_vs_bookings(bookings, facts_records, today_str))
    findings.extend(_fact_timeline(facts_records, messages_by_id, today_str))
    findings.extend(_schedule_vs_bookings(bookings, facts_records, today_str))
    if ical_events is not None:
        findings.extend(_ical_vs_bookings(bookings, ical_events, today_str))
    if gcal_events is not None:
        findings.extend(_bookings_vs_gcal(data, gcal_events, today_str))

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


def filter_and_sort(result, dismissed):
    """Re-apply dismissed filter + sort + count over a cached raw result.

    Used both by the fresh run() path and by the dismiss/undismiss path so a
    dismiss doesn't require re-fetching iCal / GCal. The raw pre-filter list
    lives in `findings_raw`; `findings` + `counts` are derived each call.
    """
    raw = list(result.get("findings_raw") or result.get("findings") or [])
    dismissed_count = sum(1 for f in raw if f["id"] in dismissed)
    kept = [f for f in raw if f["id"] not in dismissed]
    kept.sort(key=lambda f: (
        _SEVERITY_RANK.get(f["severity"], 99),
        f.get("date") or "",
        f["id"],
    ))
    counts = {"total": len(kept), "dismissed": dismissed_count}
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

def _schedule_vs_bookings(bookings, facts_records, today_str):
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
            if f.get("kind") != "schedule_assertion":
                continue
            tgt = f.get("target_date")
            cleaner = f.get("cleaner")
            if not tgt or not cleaner or tgt < today_str:
                continue
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
