"""Close the notify loop from the host's own messages.

The gap this fills: when a booking is reassigned, the system raises "tell the
cleaner". The host then tells her — in a chat the bridge reads, in a message
the facts layer already extracts as a `schedule_assertion` — and the item stays
open anyway, because `ack_notified()` was only ever called by a *cleaner's*
confirmation or by a human pressing "Mark notified". The evidence was arriving
and nothing consumed it.

Two properties are deliberate:

**It is conservative about what counts as telling someone.** The failure mode
here is worse than a stale alert. A false positive means the system reports a
cleaner as informed when she is not, which ends with somebody standing outside
a locked house or a turnover silently unstaffed. So a bare mention of the date
is not enough — see `_classify` for the three shapes accepted and the one
explicitly rejected.

**It never clears silently.** Every automatic acknowledgement returns the
message that justified it: its timestamp, the chat it came from, and the quote.
An automatic action whose reasoning is invisible is indistinguishable from a
bug, and this system's whole problem has been things happening (or not) without
anyone being told why.
"""


def _fact_time(msg):
    return (msg or {}).get("timestamp") or ""


def _norm_time(t):
    """`17:00` and `17:00:00` are the same clock time; commitments store one
    shape and extracted facts the other."""
    if not t:
        return None
    return str(t)[:5]


def _classify(asserted_cleaner, asserted_time, displaced, current_cleaner, current_time):
    """What does a host assertion in `displaced`'s chat actually establish?

    - names somebody else for the date        -> she has been told it moved
    - restates the CURRENT arrangement        -> she has been told the new plan
    - restates the OLD arrangement            -> the opposite of being told;
                                                 this is the host reaffirming a
                                                 stale plan, which is a real
                                                 conflict, not an ack
    - names nobody                            -> not enough to act on
    """
    if not asserted_cleaner:
        return "insufficient"
    if asserted_cleaner != displaced:
        return "told-moved"
    same_cleaner = current_cleaner == displaced
    if same_cleaner and _norm_time(asserted_time) == _norm_time(current_time):
        return "told-current"
    return "reaffirms-stale"


def find_ack_evidence(booking, facts_records, messages_by_id, group_of_cleaner):
    """Evidence that everyone who needs telling about `booking` has been told.

    Returns {"ok": bool, "sides": {...}, "missing": [...]} where each satisfied
    side carries the message id, timestamp, chat and quote that justified it.

    `group_of_cleaner` maps a cleaner name to her chat's group id, so an
    assertion only counts when it was made *in her own chat* — a message about
    Itzel sent to Darya tells Itzel nothing.
    """
    commitment = booking.get("cleaner_commitment") or {}
    displaced = commitment.get("cleaner")
    since = commitment.get("communicated_at") or ""
    current_cleaner = booking.get("cleaner")
    current_time = booking.get("clean_time")
    target_date = booking.get("end")

    # Which humans have an outstanding claim on this date.
    needed = []
    if displaced:
        needed.append(("displaced", displaced))
    if current_cleaner and current_cleaner != displaced:
        needed.append(("assigned", current_cleaner))
    if not needed or not target_date:
        return {"ok": False, "sides": {}, "missing": ["nothing to acknowledge"]}

    sides, missing = {}, []
    for side, cleaner in needed:
        chat = group_of_cleaner.get(cleaner)
        best = None
        for msg_id, rec in (facts_records or {}).items():
            msg = (messages_by_id or {}).get(msg_id) or {}
            if chat and msg.get("group") != chat:
                continue
            ts = _fact_time(msg)
            # Strictly after the last recorded communication: a message that
            # predates the change cannot be telling anyone about it.
            if not ts or ts <= since:
                continue
            for f in rec.get("facts") or []:
                if f.get("kind") != "schedule_assertion":
                    continue
                if f.get("target_date") != target_date:
                    continue
                verdict = (
                    _classify(f.get("cleaner"), f.get("target_time"), displaced,
                              current_cleaner, current_time)
                    if side == "displaced" else
                    ("told-current" if f.get("cleaner") == cleaner else "insufficient")
                )
                if verdict in ("told-moved", "told-current"):
                    cand = {
                        "cleaner": cleaner, "verdict": verdict, "message_id": msg_id,
                        "timestamp": ts, "group": msg.get("group"),
                        "quote": (f.get("evidence") or msg.get("text") or "")[:300],
                    }
                    if best is None or ts > best["timestamp"]:
                        best = cand
                elif verdict == "reaffirms-stale":
                    # Seen and deliberately not used. Recorded so the caller can
                    # explain why an item that "looks answered" stayed open.
                    missing.append(
                        f"{cleaner}: a message on {ts[:16]} restates the OLD "
                        f"arrangement rather than the new one"
                    )
        if best:
            sides[side] = best
        else:
            missing.append(f"{cleaner}: no host message in her chat about {target_date} "
                           f"since {since[:16] or 'the commitment was made'}")

    return {"ok": len(sides) == len(needed), "sides": sides, "missing": missing}


def describe(booking_date, evidence, include_quotes=True):
    """Human sentence for an automatic acknowledgement.

    Always names the message that justified it — timestamp first, because the
    first question about an automatic change is "based on what, and when".
    """
    bits = []
    for side in ("displaced", "assigned"):
        e = evidence.get("sides", {}).get(side)
        if not e:
            continue
        what = "told it moved" if e["verdict"] == "told-moved" else "told the current plan"
        line = f"{e['cleaner']} was {what} on {e['timestamp'][:16].replace('T', ' ')}"
        if include_quotes and e.get("quote"):
            line += f' — "{e["quote"]}"'
        bits.append(line)
    return "; ".join(bits)
