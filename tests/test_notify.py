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


if __name__ == "__main__":
    unittest.main()
