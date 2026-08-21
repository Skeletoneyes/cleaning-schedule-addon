"""The VPS projection allowlist: what crosses, and what must never (ISC-356/357).

The payload to the VPS Telegram bot is CONSTRUCTED field-by-field, never a
pass-through of the finding — `quote` and `evidence` carry raw WhatsApp text
and stay on the Pi. 1.39.0 moved the allowlist into a pure function
(`reconcile.project_finding_for_vps`) and widened it by exactly two fields:
`booking_status` (derived at projection time from the booking; closed
vocabulary) and `absorbed` (merged finding ids). This suite is the ISC-357
regression probe: it must be re-run after ANY allowlist change, and it fails
if a raw-text field ever crosses or the key set drifts from the declared
`VPS_FINDING_FIELDS`.

Run: python3 scripts/test_projection_allowlist.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cleaning-tracker"))
import reconcile  # noqa: E402

UID = "1418fb94e984-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@airbnb.com"

BOOKINGS = {
    UID: {"end": "2026-08-21", "cleaner": "Itzel", "status": "active"},
    "cancelled-uid@airbnb.com": {"end": "2026-08-21", "cleaner": "Darya", "status": "cancelled"},
}


def finding(**over):
    f = {
        "id": f"changed_mind:Darya:2026-08-21",
        "detector": "fact_timeline",
        "kind": "changed_mind",
        "severity": "needs-attention",
        "decision": "approve",
        "booking_uid": UID,
        "cleaner": "Darya",
        "date": "2026-08-21",
        "why": "Darya said confirm then decline for 2026-08-21; latest is decline",
        # The two fields that must NEVER cross, deliberately populated:
        "quote": "raw whatsapp text that must stay on the Pi",
        "evidence": ["3AC1C8FA9047B70D6780"],
        "evidence_latest": "2026-08-18T16:22:54.000Z",
        "absorbed": ["unread:3AC1C8FA9047B70D6780"],
    }
    f.update(over)
    return f


class ProjectionAllowlist(unittest.TestCase):
    def test_key_set_is_exactly_the_declared_allowlist(self):
        out = reconcile.project_finding_for_vps(finding(), BOOKINGS)
        self.assertEqual(set(out.keys()), set(reconcile.VPS_FINDING_FIELDS))

    def test_quote_and_evidence_never_cross(self):
        """ISC-357: the raw-text fields are absent — not None, ABSENT."""
        out = reconcile.project_finding_for_vps(finding(), BOOKINGS)
        self.assertNotIn("quote", out)
        self.assertNotIn("evidence", out)
        self.assertNotIn("evidence_latest", out)
        self.assertNotIn("booking_uid", out)

    def test_no_raw_text_in_any_value(self):
        """Belt and braces: the quote string itself appears in no projected value."""
        out = reconcile.project_finding_for_vps(finding(), BOOKINGS)
        blob = str(out)
        self.assertNotIn("raw whatsapp text", blob)

    def test_booking_status_is_derived_from_the_booking(self):
        out = reconcile.project_finding_for_vps(finding(), BOOKINGS)
        self.assertEqual(out["booking_status"], "active")
        out = reconcile.project_finding_for_vps(
            finding(booking_uid="cancelled-uid@airbnb.com"), BOOKINGS)
        self.assertEqual(out["booking_status"], "cancelled")

    def test_unknown_or_absent_booking_yields_null_status(self):
        out = reconcile.project_finding_for_vps(finding(booking_uid="nope"), BOOKINGS)
        self.assertIsNone(out["booking_status"])
        out = reconcile.project_finding_for_vps(finding(booking_uid=None), BOOKINGS)
        self.assertIsNone(out["booking_status"])
        out = reconcile.project_finding_for_vps(finding(), None)
        self.assertIsNone(out["booking_status"])

    def test_absorbed_passes_through_and_empties_to_null(self):
        out = reconcile.project_finding_for_vps(finding(), BOOKINGS)
        self.assertEqual(out["absorbed"], ["unread:3AC1C8FA9047B70D6780"])
        out = reconcile.project_finding_for_vps(finding(absorbed=[]), BOOKINGS)
        self.assertIsNone(out["absorbed"])
        out = reconcile.project_finding_for_vps(finding(absorbed=None), BOOKINGS)
        self.assertIsNone(out["absorbed"])

    def test_app_py_uses_the_pure_function_not_an_inline_allowlist(self):
        """The test only guards reality if app.py actually calls this function.

        A source-level check, because importing app.py drags in flask. If this
        fails, someone re-inlined the projection — put it back through
        reconcile.project_finding_for_vps or this suite is testing nothing.
        """
        src = (Path(__file__).resolve().parent.parent
               / "cleaning-tracker" / "app.py").read_text()
        self.assertIn("project_finding_for_vps", src)
        # The old inline shape must not resurface inside the payload build.
        self.assertNotIn('"findings": [\n            {\n                "id": f.get("id")', src)


if __name__ == "__main__":
    unittest.main()
