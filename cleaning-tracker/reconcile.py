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


def run(data, drift_items, today=None):
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

    # Stable dedup on id — a later detector shouldn't re-emit what an earlier
    # one already claimed.
    seen = {}
    for f in findings:
        seen.setdefault(f["id"], f)
    findings = list(seen.values())

    dismissed_count = sum(1 for f in findings if f["id"] in dismissed)
    findings = [f for f in findings if f["id"] not in dismissed]

    findings.sort(key=lambda f: (
        _SEVERITY_RANK.get(f["severity"], 99),
        f.get("date") or "",
        f["id"],
    ))

    counts = {"total": len(findings), "dismissed": dismissed_count}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": RECONCILER_VERSION,
        "findings": findings,
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
