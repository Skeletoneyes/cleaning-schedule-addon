# Digest Measurement Ledger (ISC-359)

> **The 14-night record that decides the triage-relocation question (ISC-360).**
> Started 2026-08-21. Night 1 = the 2026-08-22 08:00 digest. Verdict due on or
> after **2026-09-05** (14 nights), recorded as a Decision in `ISA.md`.
>
> Protocol: after reading each morning's digest, Josh flags anything **wrong or
> already-resolved** (in chat is fine — the session on duty records it here).
> Every flagged item gets exactly one cause tag. Unflagged nights still get a
> row — a clean night is evidence, not absence. The raw payload for any night
> is replayable from `/data/digest_archive.jsonl` on the Pi (30-day retention).

## Cause tags

| Tag | Meaning | Points toward |
|-----|---------|---------------|
| `pi-defect` | A detector/reconciler bug produced a wrong finding | Fix on the Pi (neither option) |
| `projection-poverty` | The finding was right but the prose was wrong **because a withheld field was withheld** | Enriching the projection (or relocation) |
| `write-back-gap` | The item was resolved out-of-band and nothing told the Pi | W2 discipline, not architecture |
| `tooling-asymmetry` | Resolving it needed iteration/lookups no single-turn formatter can do anywhere | A query capability, not relocation |

## Council context (2026-08-21)

Baseline morning (pre-fix): 3 bullets, post-hoc causes = 2× `pi-defect`,
1× `write-back-gap`, `projection-poverty` partial on one. All shipped fixes:
W1 (1.38.0), W2 (1.38.1 + procedure), W3 (1.39.0), archive (1.40.0).
Relocation re-opens ONLY if this ledger shows repeated `projection-poverty`
failures that W3's enrichment did not absorb.

## Nights

| # | Digest date | Bullets | Flagged items (verbatim, short) | Cause tag(s) | Notes |
|---|-------------|---------|--------------------------------|--------------|-------|
| 1 | 2026-08-22 | | | | |
| 2 | 2026-08-23 | | | | |
| 3 | 2026-08-24 | | | | |
| 4 | 2026-08-25 | | | | |
| 5 | 2026-08-26 | | | | |
| 6 | 2026-08-27 | | | | |
| 7 | 2026-08-28 | | | | |
| 8 | 2026-08-29 | | | | |
| 9 | 2026-08-30 | | | | |
| 10 | 2026-08-31 | | | | |
| 11 | 2026-09-01 | | | | |
| 12 | 2026-09-02 | | | | |
| 13 | 2026-09-03 | | | | |
| 14 | 2026-09-04 | | | | |

## Verdict

_(empty until ≥14 nights; then: counts per cause tag, the recommendation, and
a pointer to the ISA Decision that records it.)_
