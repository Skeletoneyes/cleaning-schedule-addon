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
CLOCK_SKEW_TOLERANCE_H = 1  # tolerate benign skew; beyond this, a future date is a broken clock

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
    findings.extend(_unread_messages(data.get("messages", []), facts_records, today_str))
    findings.extend(_channel_silence(silence, today_str))
    findings.extend(_facts_vs_bookings(bookings, facts_records, today_str, messages_by_id))
    findings.extend(_fact_timeline(facts_records, messages_by_id, today_str))
    findings.extend(_schedule_vs_bookings(bookings, facts_records, today_str, messages_by_id))
    # Inside run() and before filter_and_sort, so counts derive from findings
    # automatically — the ISC-40 lesson, applied a third time.
    findings.extend(_time_agreement(
        bookings, facts_records, today_str,
        (today + timedelta(days=TIME_HORIZON_DAYS)).isoformat(),
        messages_by_id,
    ))
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

    # One statement per cleaning, before anything is ranked.
    findings = resolve_subjects(findings, bookings)

    return filter_and_sort({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": RECONCILER_VERSION,
        "findings_raw": findings,
    }, dismissed)


# ── Subject resolution + decision-state ranking ─────────────────────────────
# Added 2026-08-20, after the digest told Josh that Sept 10 needed a cleaner on
# a night it also held, two severity tiers lower, "Itzel said decline then
# confirm for 2026-09-10; latest is confirm".
#
# Two defects, one shape. Severity was a string literal fixed at each
# detector's emit site — a property of the FUNCTION THAT SPOKE, never of the
# finding — and `_drift`, whose entire input is one booking dict and whose
# `evidence` is a hardcoded `[]`, was pinned top. Then the nightly repeat
# filter keyed on that same severity, so the only Sept 10 line that survived
# past night one was the one guaranteed to carry nothing. The digest did not
# merely mis-order once; it decayed toward noise.
#
# So: resolve every finding about one cleaning into one statement BEFORE
# ranking, then rank by what the reader must supply that the system cannot.
# The absorb-and-count primitive already existed in this file for the GCal
# case; this generalises it rather than inventing a second one.

# What the reader has to do. Lower sorts first.
DECISION_RANK = {
    "adjudicate": 0,   # two claims held, the system cannot pick — a human must
    "approve": 1,      # incomplete, but a candidate answer is held — one tap
    "investigate": 2,  # incomplete, nothing held — go find out
    "observe": 3,      # nothing to do
}

# Which decision each finding kind demands. Anything unlisted is treated as
# `investigate`, which fails toward asking rather than toward silence.
_KIND_DECISION = {
    "contested_cleaner": "adjudicate",
    # A change of mind whose LATEST statement is unambiguous is not a
    # contradiction to settle — it is an answer to accept. `_facts_vs_bookings`
    # raises the genuinely contested cases separately.
    "changed_mind": "approve",
    "time_ambiguous": "adjudicate",
    # She named two different times for one cleaning — a contradiction to
    # settle, not a gap to go and fill. The remaining unmapped kinds (calendar
    # drift, iCal mismatches) correctly take the `investigate` default.
    "time_mismatch": "adjudicate",
    "decline_still_assigned": "adjudicate",
    # Tombstone: `schedule_mismatch` is no longer emitted (see
    # _schedule_vs_bookings). The mapping stays so a cached finding or an old
    # dismissal id still resolves rather than falling to the default.
    "schedule_mismatch": "adjudicate",
    "ical_date_mismatch": "adjudicate",
    "unrecorded_confirmation": "approve",
    "schedule_unassigned": "approve",
    "undecided_message": "approve",
    "unread_message": "investigate",
    "drift_unassigned": "investigate",
    "drift_new": "investigate",
    "drift_changed": "investigate",
    "drift_cancelled": "investigate",
    "time_unagreed": "investigate",
    "confirm_no_booking": "observe",
    # Bridge/watchdog kinds. Merged into the result AFTER run() by app.py — the
    # mapping lives here so both paths agree, and so `investigate` is a stated
    # choice rather than the default nobody wrote down. All three mean "the
    # pipe may be broken, go look", which is exactly investigate.
    "bridge_down": "investigate",
    "bridge_blind_window": "investigate",
    "bridge_watchdog_error": "investigate",
    # Health kinds from _gcal_push_health and _channel_silence.
    "stale_push": "investigate",
    "gcal_push_failed": "investigate",
    "gcal_push_timeout": "investigate",
    "gcal_read_failed": "investigate",
    "bridge_silent": "investigate",
    "channel_silent": "investigate",
    "pipeline": "investigate",
}


def _decision_of(kind):
    return _KIND_DECISION.get(kind, "investigate")


# Only findings about SCHEDULE STATE — who cleans when, and whether they were
# told — resolve into one statement. Deliberately an allowlist, so a detector
# added later shows up on its own rather than silently vanishing into a merge.
#
# `bookings_vs_gcal` is excluded on purpose: a stale or missing calendar event
# is a claim about the PROJECTION, not about the cleaning. It has a different
# repair (re-push) and a different audience, and folding it into "Itzel
# confirmed Sept 10" would hide a broken calendar behind a solved booking.
MERGEABLE_DETECTORS = {
    "drift",
    "facts_vs_bookings",
    "fact_timeline",
    "schedule_vs_bookings",
    "time_agreement",
    "unread_messages",
    "ical_vs_bookings",
}


def _subject_of(f, date_to_uid):
    """What this finding is ABOUT — a booking — or None if it is about the system.

    Findings that name no booking (`changed_mind`, `confirm_no_booking`) still
    carry a date, and a date resolves to a booking whenever exactly one exists
    on it. That is the join the reconciler never made: the finding holding the
    Sept 10 answer had `booking_uid: None` and was therefore never connected to
    the finding asking the Sept 10 question.
    """
    uid = f.get("booking_uid")
    if uid:
        return uid
    # Keeping health findings out of a cleaning's group is `MERGEABLE_DETECTORS`'
    # job, and it does it by detector — which is the durable test, because a
    # health finding is health regardless of which fields happen to be null.
    #
    # A `cleaner is not None` check used to live here as a second guard for the
    # same thing. It was redundant against the allowlist and it was wrong: it
    # blocked the one mergeable detector that legitimately names no cleaner.
    # Live on 2026-08-20, Sept 10 rendered as two lines — "host scheduled Darya
    # … but assigned to Itzel" and "a message about the 2026-09-10 cleaning is
    # waiting for a decision" — when the whole point of resolution is one line
    # per cleaning. Two guards for one job, disagreeing.
    d = f.get("date")
    if d:
        return date_to_uid.get(d)
    return None


def resolve_subjects(findings, bookings):
    """Collapse findings about one booking into one statement each.

    Health findings — no booking, no cleaner — are left alone: they describe
    how much of the list to believe rather than belonging to it.
    """
    date_to_uid = {}
    for uid, b in (bookings or {}).items():
        if b.get("status") != "active" or b.get("type") == "custom_stay":
            continue
        d = b.get("end")
        if d:
            date_to_uid.setdefault(d, []).append(uid)
    date_to_uid = {d: uids[0] for d, uids in date_to_uid.items() if len(uids) == 1}

    groups, loose = {}, []
    for f in findings:
        subj = (_subject_of(f, date_to_uid)
                if f.get("detector") in MERGEABLE_DETECTORS else None)
        if subj is None:
            loose.append(f)
        else:
            groups.setdefault(subj, []).append(f)

    # Health findings carry no booking and belong to no cleaning. They describe
    # how much of the rest of the list to believe, so they are never merged —
    # but they do get an explicit decision rather than falling through to a
    # default nobody wrote down.
    out = [dict(f, decision=_decision_of(f["kind"])) for f in loose]
    for subj, members in groups.items():
        if len(members) == 1:
            out.append(dict(members[0], decision=_decision_of(members[0]["kind"])))
            continue
        # The primary is the member demanding the most of the reader; ties go to
        # the one carrying evidence, because between two findings that ask the
        # same thing the informed one is the one worth reading.
        members.sort(key=lambda f: (
            DECISION_RANK[_decision_of(f["kind"])],
            0 if f.get("evidence") else 1,
            f["id"],
        ))
        primary = dict(members[0])
        others = members[1:]
        primary["decision"] = _decision_of(primary["kind"])
        primary["absorbed"] = [f["id"] for f in others]
        # Carry the booking the group resolved to. `changed_mind` and
        # `confirm_no_booking` are emitted with `booking_uid: None` — they were
        # never joined to a booking, which is exactly why the Sept 10 answer
        # was never connected to the Sept 10 question. Without this the merged
        # finding reaches the Conflicts tab with no uid and therefore no
        # one-tap action, which is the same information arriving unusable.
        if not primary.get("booking_uid"):
            primary["booking_uid"] = subj
        # Merge what the other findings knew, so nothing is lost by being
        # absorbed — the failure this whole change exists to end.
        merged_ev = list(primary.get("evidence") or [])
        for f in others:
            for e in (f.get("evidence") or []):
                if e not in merged_ev:
                    merged_ev.append(e)
        primary["evidence"] = merged_ev
        if not primary.get("quote"):
            for f in others:
                if f.get("quote"):
                    primary["quote"] = f["quote"]
                    break
        if not primary.get("cleaner"):
            for f in others:
                if f.get("cleaner"):
                    primary["cleaner"] = f["cleaner"]
                    break
        # Severity is the MAX across the group, not the primary's own. Getting
        # this wrong is silent and expensive: the nightly repeat filter keys on
        # `needs-attention`, so a merged finding that inherited `informational`
        # from its primary would be reported once and never again — rebuilding
        # the exact decay this change exists to stop, one layer up.
        primary["severity"] = min(
            (f["severity"] for f in members),
            key=lambda sev: _SEVERITY_RANK.get(sev, 99),
        )
        # Lead with the answer, then the two most informative corroborations,
        # then a count. Concatenating all of them produced a five-clause
        # run-on — the digest is read on a phone, and a wall of joined clauses
        # fails the same way the five separate lines did.
        others.sort(key=lambda f: (0 if f.get("evidence") else 1,
                                   DECISION_RANK[_decision_of(f["kind"])]))
        shown = others[:2]
        rest = len(others) - len(shown)
        primary["why"] = (
            primary["why"]
            + "".join(f" · {f['why']}" for f in shown)
            + (f" · +{rest} more signal{'s' if rest != 1 else ''} on this cleaning" if rest else "")
        )
        out.append(primary)
    return out


STALE_DAYS = 5  # findings whose date is older than this are auto-suppressed

def filter_and_sort(result, dismissed):
    """Re-apply dismissed filter + sort + count over a cached raw result.

    Used both by the fresh run() path and by the dismiss/undismiss path so a
    dismiss doesn't require re-fetching iCal / GCal. The raw pre-filter list
    lives in `findings_raw`; `findings` + `counts` are derived each call.
    """
    raw = list(result.get("findings_raw") or result.get("findings") or [])
    cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()

    def _is_dismissed(f):
        """A merged finding is dismissed when its own id is, or when every
        finding it absorbed was already dismissed individually.

        Subject resolution (2026-08-20) means an id a human dismissed last
        month may now be absorbed into a primary with a different id. Keying
        only on the primary would make every one of those dismissals inert at
        once and resurface months of resolved noise — which is exactly how a
        channel teaches you to ignore it.
        """
        if f["id"] in dismissed:
            return True
        absorbed = f.get("absorbed") or []
        return bool(absorbed) and all(a in dismissed for a in absorbed)

    dismissed_count = sum(1 for f in raw if _is_dismissed(f))
    stale_count = sum(1 for f in raw if not _is_dismissed(f) and f.get("date") and f["date"] < cutoff)
    kept = [f for f in raw if not _is_dismissed(f) and not (f.get("date") and f["date"] < cutoff)]
    # Rank by what the reader must DO, then by proximity. Severity survives as
    # a secondary key and as the repeat-filter input, but it no longer decides
    # what Josh reads first — a detector that has never seen a message cannot
    # outrank one holding the answer just because its emit site said so.
    kept.sort(key=lambda f: (
        DECISION_RANK.get(f.get("decision") or _decision_of(f["kind"]), 2),
        f.get("date") or "",
        _SEVERITY_RANK.get(f["severity"], 99),
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


# ── Detector 10: messages the system never decided ──────────────────────────
# The ISA's first Principle reads "Fail loudly, never silently. A broken
# dependency must produce a signal a human sees, not a row stuck in pending."
# Until 2026-08-20 it was the one principle with no implementation: nine
# detectors, a 5-minute container watchdog and two mutual dead-man switches,
# and `review_state`, `pending` and `parse_error` appeared nowhere in this
# file. 75 messages carried a parse error, 60 of them un-retried rate limits,
# and 67 had been bulk-filed as `ignored` — including "Perfect I'll be there
# Friday at noon". The 2026-08-15 half of the Sept 10 failure died there.
#
# Two properties make this different from the review queue it replaces as the
# attention mechanism. It is dated to the CLEANING the message concerns, not to
# the message, so it escalates as that date approaches and `filter_and_sort`
# retires it naturally once the date passes — where `expire_stale_reviews`
# fires seven days AFTER the cleaning, which no setting can make timely. And it
# rides the digest, so it reaches the phone instead of a badge on a tab.
#
# `why` carries structured fields only. Message text must never reach it: the
# VPS payload allowlist protects keys, not values (ISC-41).

UNREAD_URGENT_DAYS = 7


def _unread_messages(messages, facts_records, today_str, horizon_days=UNREAD_URGENT_DAYS):
    urgent_before = (
        date.fromisoformat(today_str) + timedelta(days=horizon_days)
    ).isoformat()
    out = []
    for m in messages:
        state = m.get("review_state")
        undecided = state == "pending" or (m.get("parse_error") and not m.get("parsed"))
        if not undecided:
            continue
        msg_id = m.get("id")
        if not msg_id:
            continue

        # The cleaning this message is about: the soonest future date any of
        # its facts named. Falling back to the message's own day keeps a
        # message whose extraction FAILED — the case with no facts at all, and
        # the one that matters most — from being silently dropped here too.
        dates = [
            f.get("target_date")
            for f in (facts_records.get(msg_id) or {}).get("facts", [])
            if f.get("target_date") and f["target_date"] >= today_str
        ]
        subject = min(dates) if dates else (m.get("timestamp") or "")[:10]
        if not subject or subject < today_str:
            continue

        broke = bool(m.get("parse_error"))
        why = (
            f"a message from {(m.get('sender_name_raw') or 'a cleaner')} about the "
            f"{subject} cleaning was never read — extraction failed"
            if broke else
            f"a message about the {subject} cleaning is waiting for a decision"
        )
        out.append({
            "id": f"unread:{msg_id}",
            "detector": "unread_messages",
            "kind": "unread_message" if broke else "undecided_message",
            "severity": "needs-attention" if subject <= urgent_before else "suggest",
            "booking_uid": None,
            "cleaner": None,
            "date": subject,
            "why": why,
            "evidence": [msg_id],
        })
    return out


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
    def _what_changed(it):
        """Name the change, not just its existence.

        `review_item` already computes `was` (what the cleaner was last told)
        and `now` (current truth) as (cleaner, date, clean_time) tuples, and
        `_drift` discarded both — so a guest extending their stay produced
        "assignment changed since last notified" while the system held
        "Itzel was told Sept 8 1:00 PM, it is now Sept 9". Same shape as the
        Sept 10 failure: the informative half was computed and dropped.
        """
        was, now = it.get("was"), it.get("now")
        if not was or not now:
            return ""
        labels = ("cleaner", "date", "time")
        diffs = [f"{lab} {a or '—'} → {b or '—'}"
                 for lab, a, b in zip(labels, was, now) if a != b]
        return f" ({'; '.join(diffs)})" if diffs else ""

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
            "why": lead + why_map.get(k, k) + _what_changed(it),
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

    # A push that blew its budget recently is worth saying out loud even if a
    # later writer recorded ok — "it converged, eventually, after we stopped
    # waiting" is a different health state from "it converged".
    last_timeout_at = (gcal_status or {}).get("last_timeout_at")
    if last_timeout_at:
        try:
            since = (now - datetime.fromisoformat(last_timeout_at)).total_seconds() / 3600
        except (ValueError, TypeError):
            since = 0
        if -CLOCK_SKEW_TOLERANCE_H <= since < PUSH_STALE_HOURS:
            out.append({
                "id": "pipeline:gcal-push-timeout",
                "detector": "pipeline",
                "kind": "gcal_push_timeout",
                "severity": "needs-attention",
                "booking_uid": None,
                "cleaner": None,
                "date": today_str,
                "why": (
                    f"a Google Calendar push exceeded its time budget {since:.1f}h ago — "
                    "the calendar may still have converged afterwards, but the push is "
                    "running slow enough that the nightly job stopped waiting for it"
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
            if age_hours < -CLOCK_SKEW_TOLERANCE_H:
                # A future-dated success. The Pi has no RTC, so a power cut can
                # leave it writing timestamps ahead of true time; once NTP
                # corrects, a plain `age >= threshold` test reads negative and
                # suppresses staleness FOREVER. Implausible is not healthy —
                # collapse it into the same loud branch as absent. (Advisor
                # finding, 2026-08-01.)
                age_desc = f"dated {abs(age_hours):.1f}h in the future — clock is wrong"
            elif age_hours >= PUSH_STALE_HOURS:
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

# Only the cleaner can agree to a time. `schedule_assertion` is host-authored
# by the role-tagged facts prompt — the host restating a plan is not the cleaner
# accepting it, and it is the largest bucket of timed facts in the archive.
_CLEANER_TIME_KINDS = ("confirm", "time_proposal")

# How far ahead a missing time is worth mentioning. Matched to the digest's
# repeat horizon so a finding starts appearing at the same moment it starts
# repeating, rather than sitting silent and then arriving nightly out of nowhere.
TIME_HORIZON_DAYS = 21

# Same guard the write path applies in app.py. Duplicated rather than imported
# because reconcile.py is deliberately dependency-free and pure.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _time_agreement(bookings, facts_records, today_str, horizon_str,
                    messages_by_id=None):
    """Two distinct ways a cleaning time can be wrong, both previously silent.

    `time_mismatch` — the cleaner named a time and the booking says a different
    one. This is the probe that would have caught the 2026-08-10 cleaning on the
    morning of Aug 8 instead of nobody catching it at all.

    `time_unagreed` — nobody ever named a time. That reads as harmless until you
    look at `gcal.py`, which substitutes `11:00:00` when `clean_time` is None
    (three separate sites). So on the one surface the cleaners actually read,
    an absent time renders as a specific, plausible, agreed-looking hour. That
    is a fail-open on the outward-facing artifact, which is exactly the class
    this project keeps rediscovering.

    Pure: no data access, no clock — `today_str` and `horizon_str` are passed in.
    """
    out = []

    # Latest cleaner-authored time per (date, cleaner). Latest-wins mirrors
    # `_fact_timeline`'s handling of confirm-vs-decline: people change their
    # minds, and the most recent statement is the operative one.
    #
    # Ordered by the MESSAGE's send time, never by `extracted_at`. Extraction
    # time is when the model last looked at the message, so a reprocess —
    # which this system runs on demand after every prompt-version bump —
    # restamps ancient messages as the newest opinion and silently resurrects
    # a superseded time. `_fact_timeline` already learned this; the send time
    # is the only clock that describes when a human actually said something.
    # Collect the newest message per (date, cleaner) and ALL the times it
    # names, not just one. Keeping a single time here would silently collapse
    # "anytime after 11am and before 3pm" — which extracts as two facts — into
    # whichever end happened to be visited last, and then report a confident
    # mismatch against an arbitrary half of a range. The write path refuses
    # that case, but the detector must recognise it from the raw facts too:
    # the archive is full of messages that never went through the new writer.
    msgs = messages_by_id or {}
    latest = {}
    for msg_id, rec in (facts_records or {}).items():
        said_at = (msgs.get(msg_id) or {}).get("timestamp") or ""
        for f in rec.get("facts", []) or []:
            if f.get("kind") not in _CLEANER_TIME_KINDS or f.get("tentative"):
                continue
            tgt, cleaner, tm = f.get("target_date"), f.get("cleaner"), f.get("target_time")
            if not tgt or not cleaner or not tm or tgt < today_str:
                continue
            # Same HH:MM guard the write path applies. Without it a malformed
            # model output rides straight into a finding id and into prose the
            # host reads, dressed as a time somebody stated.
            if not _HHMM_RE.match(str(tm)):
                continue
            key = (tgt, cleaner)
            prev = latest.get(key)
            if prev is None or said_at > prev[0]:
                latest[key] = (said_at, {str(tm)}, msg_id)
            elif said_at == prev[0] and msg_id == prev[2]:
                prev[1].add(str(tm))

    for uid, b in (bookings or {}).items():
        if b.get("status") != "active" or b.get("type") == "custom_stay":
            continue
        d = b.get("end")
        cleaner = b.get("cleaner")
        if not d or not cleaner or d < today_str:
            continue

        booked = (b.get("clean_time") or "")[:5] or None
        said = latest.get((d, cleaner))

        # She answered, but with something we could not turn into one time —
        # "anytime after 11am and before 3pm" is a WINDOW, a perfectly normal
        # human reply, not a parse failure. Without its own finding this falls
        # through to the drift queue, whose prescribed action is "tell the
        # cleaner" — the opposite of what is needed. The right action is to ask
        # her which hour, and only a distinct finding can say so.
        # Ambiguity from either source: a note the writer left, or a raw fact
        # set that names more than one time. The second is what covers the
        # historical archive, which the writer never touched.
        note = b.get("time_note")
        if not note and said and len(said[1]) > 1:
            note = (f"names {len(said[1])} different times "
                    f"({', '.join(sorted(said[1]))})")
        if note:
            out.append({
                "id": f"time_ambiguous:{uid}",
                "detector": "time_agreement",
                "kind": "time_ambiguous",
                "severity": "needs-attention",
                "booking_uid": uid,
                "cleaner": cleaner,
                "date": d,
                "why": (f"{cleaner} answered about the {d} cleaning time but "
                        f"{note} — ask her which one; the booking still says "
                        f"{booked or 'no time'} and nobody has agreed to it"),
                "evidence": [],
            })
            continue

        one_time = next(iter(said[1])) if said and len(said[1]) == 1 else None
        if one_time and booked and one_time != booked:
            out.append({
                "id": f"time_mismatch:{uid}:{one_time}",
                "detector": "time_agreement",
                "kind": "time_mismatch",
                "severity": "needs-attention",
                "booking_uid": uid,
                "cleaner": cleaner,
                "date": d,
                # Structured fields only — never the message text (ISC-41).
                "why": (f"{cleaner} said {one_time} for the {d} cleaning but the "
                        f"booking says {booked}; the shared calendar shows {booked}"),
                "evidence": [said[2]],
            })
        elif not booked and d <= horizon_str:
            out.append({
                "id": f"time_unagreed:{uid}",
                "detector": "time_agreement",
                "kind": "time_unagreed",
                "severity": "suggest",
                "booking_uid": uid,
                "cleaner": cleaner,
                "date": d,
                # Wording changed 2026-08-20 with the gcal fix. It used to say
                # the calendar "is showing 11:00 AM as a default" — true then,
                # false the moment `_event_window` started rendering untimed
                # cleanings as all-day. A finding that describes a behaviour the
                # code no longer has is a wrong statement in the answering path.
                "why": (f"No agreed time for the {d} cleaning ({cleaner}); it "
                        f"shows on the shared calendar as all-day, so she has "
                        f"no hour to plan around"),
                "evidence": [],
            })

    return out


def _latest_by_cleaner_date(facts_records, messages_by_id, kinds, today_str):
    """The LAST thing each cleaner said about each date, by message time.

    `_schedule_vs_bookings` has done this for host assertions since 1.17.1;
    `_facts_vs_bookings` never did, so every confirm and decline a cleaner had
    ever made was live simultaneously and the oldest could outrank the newest.

    Live consequence, 2026-08-20: Itzel declined Sept 10 on May 5 and confirmed
    it on Aug 15 and again on Aug 20. The moment the booking was assigned to
    her, the May decline produced `decline_still_assigned` at needs-attention —
    "Itzel declined 2026-09-10 but is still assigned to it" — while
    `_fact_timeline`, one detector over, correctly reported "latest is confirm".
    The same contradiction the reconciler was built to catch, produced by the
    reconciler. `dismissed_findings` already carries a hand-written note about
    this shape from 2026-08-03: "Detector keyed on Itzel without checking
    current assignee."

    Ties on timestamp keep the first seen, which is stable across re-runs.
    """
    latest = {}
    for msg_id, rec in (facts_records or {}).items():
        ts = ((messages_by_id or {}).get(msg_id) or {}).get("timestamp") or ""
        for f in rec.get("facts", []):
            if f.get("kind") not in kinds:
                continue
            tgt, cleaner = f.get("target_date"), f.get("cleaner")
            if not tgt or not cleaner or tgt < today_str:
                continue
            if f.get("tentative"):
                continue
            key = (cleaner, tgt)
            prev = latest.get(key)
            if prev is None or ts > prev[0]:
                latest[key] = (ts, msg_id, f)
    return latest


def _facts_vs_bookings(bookings, facts_records, today_str, messages_by_id=None):
    by_date = {}
    for uid, b in bookings.items():
        if b.get("status") == "cancelled" or b.get("type") == "custom_stay":
            continue
        d = b.get("end")
        if d:
            by_date.setdefault(d, []).append((uid, b))

    out = []
    # One statement per (cleaner, date) — her latest. A superseded decline must
    # not outrank the confirm that replaced it. `tentative` is filtered inside:
    # a statement the speaker flagged as provisional is not a commitment, and
    # it was extracted, stored and read by exactly one detector until
    # 2026-08-20.
    for (cleaner, tgt), (_ts, msg_id, f) in _latest_by_cleaner_date(
        facts_records, messages_by_id, ("confirm", "decline"), today_str
    ).items():
        kind = f.get("kind")
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
            # Gated from 2026-08-20. `schedule_assertion` is the most inferred
            # kind in the schema — it is whatever the host said that named a
            # date — and it had the loosest gate and the loudest severity in
            # the file, which is exactly backwards. The facts vocabulary has
            # `decline` as the negative of `confirm` but NOTHING as the
            # negative of `schedule_assertion` and no kind for a question, so a
            # host sentence naming a date can only be filed as an assertion.
            # That is how "only Sept 10 you cannot do" produced a finding that
            # Darya WAS scheduled for Sept 10, and how "do we have you booked
            # for July 24?" was read as a booking (dismissed 2026-07-21).
            if f.get("tentative"):
                continue
            if (f.get("confidence") or 0.0) < CONFIRM_THRESHOLD:
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
            # DELETED 2026-08-20: `schedule_mismatch`, the finding that fired
            # when a host assertion disagreed with an assigned booking.
            #
            # It was the most-dismissed finding kind in the system — 13 of 30
            # — and every dismissal carrying a written reason condemned it.
            # Four say "redundant: contested_cleaner for same booking+cleaner
            # already dismissed". One says "mis-extraction: Josh's 'do we have
            # you booked for July 24?' QUESTION was read as a host schedule
            # assertion". None says it was real.
            #
            # The reason it cannot be fixed by tuning: it reports the HOST's
            # own words back to him. He typed the sentence; he knows what he
            # meant. And `schedule_assertion` is the only kind with no negative
            # and no interrogative in the vocabulary, so a question ("are you
            # available Sept 10?") and a negation ("only Sept 10 you cannot
            # do") both collapse into an affirmative claim that he scheduled
            # her. Both of those are live on 2026-09-10 right now, both above
            # the confidence gate.
            #
            # The real conflicts arrive from the CLEANER's side, where the
            # vocabulary does carry a negative: `_facts_vs_bookings` emits
            # `contested_cleaner` from her own confirm, and Darya's decline for
            # Sept 10 extracted correctly at 0.9. That is what those four
            # "redundant" dismissals were saying.
            #
            # `schedule_unassigned` above is kept: a host naming someone for a
            # booking with nobody on it is a proposal to accept, not a
            # contradiction to adjudicate, and it is what surfaces Oct 1 and
            # Nov 24 today.
            #
            # The alternative considered and rejected was a `polarity` field on
            # every fact — 624 records changed, a prompt bump and a full
            # reprocess, to fix an asymmetry in one kind. Josh: "the polarity
            # field feels over complex."
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
