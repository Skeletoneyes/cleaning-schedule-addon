#!/usr/bin/env python3
"""Pull the tracker snapshot once and materialize it into flat local files
for fast analysis — no repeated API round-trips.

Usage:  python scripts/dump_snapshot.py
Output: .secrets/analysis/{messages,facts,bookings}.tsv + summary on stdout.
Read-only against the add-on; safe to run any time.
"""
import csv
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".secrets" / "analysis"


def main():
    cfg = json.loads((ROOT / ".secrets" / "urls.json").read_text())
    req = urllib.request.Request(
        cfg["ha_snapshot_url"], headers={"X-Shared-Secret": cfg["ha_shared_secret"]}
    )
    snap = json.load(urllib.request.urlopen(req, timeout=30))
    d = snap["data"]
    labels = d.get("group_labels", {})
    jid_to_cleaner = {
        jid: name for name, jids in d.get("cleaner_jids", {}).items() for jid in jids
    }
    OUT.mkdir(parents=True, exist_ok=True)

    msgs = sorted(d["messages"], key=lambda m: m.get("timestamp") or "")
    with open(OUT / "messages.tsv", "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["timestamp", "group", "who", "review_state", "text"])
        for m in msgs:
            sender = m.get("sender") or ""
            who = (
                jid_to_cleaner.get(sender)
                or m.get("sender_name_raw")
                or ("host" if "@s.whatsapp.net" in sender else sender)
            )
            w.writerow([
                (m.get("timestamp") or "")[:19],
                labels.get(m.get("group"), m.get("group")),
                who,
                m.get("review_state") or "",
                (m.get("text") or "").replace("\t", " ").replace("\n", " ⏎ "),
            ])

    facts_by_msg = d.get("message_facts", {})
    msg_index = {m["id"]: m for m in msgs}
    with open(OUT / "facts.tsv", "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow([
            "msg_timestamp", "group", "kind", "cleaner", "target_date",
            "target_time", "confidence", "tentative", "model", "evidence",
        ])
        rows = []
        for mid, rec in facts_by_msg.items():
            m = msg_index.get(mid, {})
            for fact in rec.get("facts", []):
                rows.append([
                    (m.get("timestamp") or "")[:19],
                    labels.get(m.get("group"), m.get("group")),
                    fact.get("kind"),
                    fact.get("cleaner") or "",
                    fact.get("target_date") or "",
                    fact.get("target_time") or "",
                    fact.get("confidence"),
                    fact.get("tentative"),
                    rec.get("model_version", ""),
                    (fact.get("evidence") or "").replace("\t", " ")[:200],
                ])
        rows.sort(key=lambda r: (r[4] or "", r[0]))
        w.writerows(rows)

    with open(OUT / "bookings.tsv", "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow([
            "start", "end", "status", "cleaner", "clean_time",
            "commit_cleaner", "commit_time", "commit_via", "notes", "uid",
        ])
        for uid, b in sorted(d["bookings"].items(), key=lambda x: x[1].get("end") or ""):
            cc = b.get("cleaner_commitment") or {}
            w.writerow([
                b.get("start"), b.get("end"), b.get("status"),
                b.get("cleaner") or "", b.get("clean_time") or "",
                cc.get("cleaner") or "", cc.get("clean_time") or "",
                cc.get("communicated_via") or "",
                (b.get("notes") or "").replace("\t", " ")[:120], uid,
            ])

    print(f"snapshot generated_at: {snap.get('generated_at')}")
    print(f"messages: {len(msgs)}  facts-records: {len(facts_by_msg)}  "
          f"bookings: {len(d['bookings'])}")
    for g, n in Counter(m.get("group") for m in msgs).items():
        span = [m["timestamp"][:10] for m in msgs if m.get("group") == g]
        print(f"  {labels.get(g, g):8} {n:5} msgs  {min(span)} → {max(span)}")
    models = Counter(r.get("model_version", "?") for r in facts_by_msg.values())
    print("facts by model:", dict(models))
    print(f"wrote: {OUT}/messages.tsv, facts.tsv, bookings.tsv")


if __name__ == "__main__":
    main()
