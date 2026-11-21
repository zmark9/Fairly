"""Tests for notify.py helpers (no network)."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notify  # noqa: E402


class ManualRunTests(unittest.TestCase):
    def test_scheduled_hour_is_not_manual(self):
        self.assertFalse(notify.is_manual_run(datetime(2026, 11, 12, 6, 5), 6))
        self.assertFalse(notify.is_manual_run(datetime(2026, 11, 12, 6, 59), 6))

    def test_other_hours_are_manual(self):
        self.assertTrue(notify.is_manual_run(datetime(2026, 11, 12, 7, 0), 6))
        self.assertTrue(notify.is_manual_run(datetime(2026, 11, 12, 14, 30), 6))

    def test_no_scheduled_hour_means_never_manual(self):
        self.assertFalse(notify.is_manual_run(datetime(2026, 11, 12, 14, 30), None))


class AttachmentTests(unittest.TestCase):
    def test_patterns_and_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ["upcoming_reservations.csv", "remotelock_big_sky.csv", "summary.json"]:
                (Path(tmp) / name).write_text("x", encoding="utf-8")
            cfg = {"attachments": ["upcoming_reservations.csv", "remotelock_*.csv", "*.csv"]}
            names = [p.name for p in notify.find_attachments(tmp, cfg)]
            self.assertEqual(names, ["upcoming_reservations.csv", "remotelock_big_sky.csv"])


class CleaningEmailTests(unittest.TestCase):
    REPORT = {"slug": "cabin", "property": "Mountain Cabin", "changes": [
        {"kind": "New", "guest": "Testa Z.", "check_in": "2027-01-02", "check_out": "2027-01-05", "guests": "2 adults"},
        {"kind": "Cancelled", "guest": "Old <b>", "check_in": "2027-02-02", "check_out": "2027-02-05", "guests": ""}],
        "upcoming": [{"guest": "Testa Z.", "check_in": "2027-01-02", "check_out": "2027-01-05", "guests": "2 adults"}]}

    def test_cleaning_target(self):
        cleaner = {"to_email": "cleaner@example.test", "to_name": "Cleaner"}
        owner = {"to_email": "owner@example.test"}
        # Scheduled: the cleaner, only when something changed (or --force).
        self.assertEqual(notify.cleaning_target(cleaner, owner, False, True), (cleaner, ""))
        self.assertIsNone(notify.cleaning_target(cleaner, owner, False, False))
        self.assertEqual(notify.cleaning_target(cleaner, owner, False, False, force=True), (cleaner, ""))
        # Manual: always the owner, marked, never the cleaner.
        for changed in (True, False):
            to, prefix = notify.cleaning_target(cleaner, owner, True, changed)
            self.assertEqual(to, owner)
            self.assertEqual(prefix, "[Test copy, not sent to Cleaner] ")
        self.assertIsNone(notify.cleaning_target({}, owner, False, True))

    def test_subject_and_body(self):
        self.assertEqual(notify.build_cleaning_subject(self.REPORT, "2026-11-21"),
                         "Mountain Cabin reservations 11/21/26: 1 new, 1 cancelled")
        body = notify.build_cleaning_body(self.REPORT, "2026-11-21")
        self.assertIn("What changed", body)
        self.assertIn("Old &lt;b&gt;", body)   # escaped
        self.assertIn("All upcoming stays", body)
        self.assertIn("<td>1/2/27</td>", body)
        self.assertNotIn("2027-", body)

    def test_us_date(self):
        self.assertEqual(notify.us_date("2027-03-07"), "3/7/27")
        self.assertEqual(notify.us_date("dates were 2026-12-28 to 2027-01-02"), "dates were 12/28/26 to 1/2/27")
        self.assertEqual(notify.us_date("2 adults"), "2 adults")


if __name__ == "__main__":
    unittest.main()
