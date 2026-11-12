"""Tests for reminders.py (gate codes and snow-removal messages). Made-up guests."""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reminders  # noqa: E402

PARKING = {
    "lot_name": "Test Lot",
    "schedule_start": "2026-11-24",           # Tuesday
    "interval_days": 14,
    "change_time": "9:00 AM",
    "gate_codes": {"2026-11-24": "#1111", "2026-12-08": "#2222", "2026-12-22": ""},
    "snow_removal": {
        "season_start": "2026-11-24", "season_end": "2027-04-30", "move_by": "9:00 AM",
        "alternatives": "Lot B", "alternatives_sentence": "Lot B is the easiest alternative.",
    },
}
CONFIG = {"homes": [
    {"id": "cabin", "name": "Mountain Cabin", "parking": PARKING},
    {"id": "condo", "name": "Desert Condo"},
]}


def rec(check_in, check_out, home="cabin", guest_type="Guest", first="Testa"):
    return {"_home_id": home, "Property": "Mountain Cabin", "Guest Type": guest_type,
            "Guest First Name": first, "Last Initial": "Z", "Check-in": check_in,
            "Check-out": check_out, "Message Thread Link": "https://example.test/thread"}


class ScheduleTests(unittest.TestCase):
    def test_change_days(self):
        days = reminders.change_days(PARKING, date(2027, 1, 5))
        self.assertEqual(days[:3], [date(2026, 11, 24), date(2026, 12, 8), date(2026, 12, 22)])
        self.assertEqual(days[-1], date(2027, 1, 5))
        self.assertTrue(all(d.weekday() == 1 for d in days))  # all Tuesdays

    def test_code_for(self):
        self.assertEqual(reminders.code_for(PARKING, date(2026, 11, 23)), (None, None))  # before start
        self.assertEqual(reminders.code_for(PARKING, date(2026, 11, 24))[0], "#1111")
        self.assertEqual(reminders.code_for(PARKING, date(2026, 12, 7))[0], "#1111")
        self.assertEqual(reminders.code_for(PARKING, date(2026, 12, 8))[0], "#2222")
        self.assertEqual(reminders.code_for(PARKING, date(2026, 12, 25))[0], None)  # not released

    def test_changes_during_stay(self):
        # Parked overnight before the change: check_in < day <= check_out
        self.assertEqual(reminders.changes_during_stay(PARKING, date(2026, 12, 5), date(2026, 12, 8)),
                         [date(2026, 12, 8)])
        self.assertEqual(reminders.changes_during_stay(PARKING, date(2026, 12, 8), date(2026, 12, 12)), [])


class BuildTests(unittest.TestCase):
    def test_arrival_message_day_before(self):
        msgs, warnings = reminders.build_reminders([rec("2026-12-05", "2026-12-10")], CONFIG, date(2026, 12, 4))
        self.assertEqual(len(msgs), 1)
        text = msgs[0]["message"]
        self.assertIn("#1111", text)                       # code at arrival
        self.assertIn("Tue Dec 8", text)                   # change during stay
        self.assertIn("#2222", text)
        self.assertIn("plowed", text)
        self.assertEqual(warnings, [])

    def test_change_reminder_for_guests_in_house(self):
        records = [rec("2026-12-05", "2026-12-10", first="Staying"),
                   rec("2026-12-07", "2026-12-08", first="LeavesTue"),   # checkout on plow day: car still there
                   rec("2026-12-08", "2026-12-12", first="ArrivesTue"),  # arrives after the change: no reminder
                   rec("2026-12-05", "2026-12-10", guest_type="Owner", first="Owner")]
        msgs, _ = reminders.build_reminders(records, CONFIG, date(2026, 12, 7))
        names = sorted(m["guest"] for m in msgs if m["type"].startswith("Snow"))
        self.assertEqual(names, ["LeavesTue Z.", "Staying Z."])
        self.assertIn("move your car before 9:00 AM", msgs[0]["message"])

    def test_missing_code_placeholder_and_warning(self):
        msgs, warnings = reminders.build_reminders([rec("2026-12-23", "2026-12-27")], CONFIG, date(2026, 12, 22))
        self.assertIn(reminders.PLACEHOLDER, msgs[0]["message"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-12-22", warnings[0])

    def test_out_of_season_change_is_gate_only(self):
        msgs, _ = reminders.build_reminders([rec("2027-05-08", "2027-05-14")], CONFIG, date(2027, 5, 10))
        self.assertEqual(msgs[0]["type"], "Gate code change tomorrow")
        self.assertNotIn("plowed", msgs[0]["message"])

    def test_no_parking_config_no_messages(self):
        msgs, _ = reminders.build_reminders([rec("2026-12-05", "2026-12-10", home="condo")], CONFIG,
                                            date(2026, 12, 4))
        self.assertEqual(msgs, [])

    def test_quiet_day(self):
        msgs, warnings = reminders.build_reminders([rec("2026-12-15", "2026-12-18")], CONFIG, date(2026, 12, 1))
        self.assertEqual((msgs, warnings), ([], []))


if __name__ == "__main__":
    unittest.main()
