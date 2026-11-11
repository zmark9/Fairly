"""Tests for sync.py. All guest data here is made up.

Run from the project folder:   py -m unittest -v        (Windows)
                               python3 -m unittest -v   (Linux/macOS)
"""

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402

OWNER_ID = "owner-1"
CONFIG = {
    "timezone": "America/Phoenix",
    "owner": {"user_id": OWNER_ID, "last_initial": "Q"},
    "homes": [
        {"id": "home-a", "name": "Desert Condo", "slug": "desert", "timezone": "America/Phoenix", "remotelock": False},
        {"id": "home-b", "name": "Mountain Cabin", "slug": "mountain", "timezone": "America/Denver", "remotelock": True},
    ],
}
TODAY = date(2026, 11, 11)


def make_detail(rid, home="home-b", check_in="2026-12-01", check_out="2026-12-05", status="CONFIRMED",
                first="Testa", initial="Z", phone="+15555550123", guest_type="GUEST", channel="AIRBNB",
                owner_due=1000.5, caretaker_due=(150, 40.25), offset="-0700"):
    """Build a fake get_reservation_by_id result."""
    messages = []
    if initial is not None:
        messages.append({"MESSAGE_BODY": f"Fairly: Reservation created - You have a confirmed "
                                         f"reservation with {first} {initial}.\n\nHome: 1 Test St"})
    funds = [
        {"ROLE": "OWNER", "USER_ID": OWNER_ID, "AMOUNT_DUE": owner_due, "DELETED_AT": None},
        {"ROLE": "OWNER", "USER_ID": OWNER_ID, "AMOUNT_DUE": 999, "DELETED_AT": "2026-10-01"},  # deleted row
        {"ROLE": None, "USER_ID": None, "AMOUNT_DUE": 60, "DELETED_AT": None},                  # insurance
    ] + [{"ROLE": "CARETAKER", "USER_ID": OWNER_ID, "AMOUNT_DUE": amt, "DELETED_AT": None} for amt in caretaker_due]
    return {
        "reservation": {
            "ID": rid, "HOME_ID": home, "STATUS": status, "CHECK_IN": check_in, "CHECK_OUT": check_out,
            "EFFECTIVE_CHECK_OUT": check_out, "CHANNEL_ID": channel, "GUEST_TYPE": guest_type,
            "GUEST_FIRST_NAME": first, "GUEST_PHONE_NUMBERS": [phone] if phone else [],
            "GUEST_NUMBER_ADULTS": 2, "GUEST_NUMBER_CHILDREN": 1, "GUEST_NUMBER_PETS": 0,
            "EXTERNAL_ID": f"CONF-{rid}", "CANCELLATION_POLICY": "AIRBNB_FIRM", "NOTES": None,
            "BOOKED_AT": "2026-11-01 03:30:00.000 +0000",
            "CHECK_IN_AT": f"{check_in} 16:00:00.000 {offset}", "CHECK_OUT_AT": f"{check_out} 10:00:00.000 {offset}",
            "MESSAGE_THREAD_LINK": f"https://example.test/messages/{rid}",
        },
        "conversation_messages": messages,
        "funds_distribution": funds,
    }


def records_for(*details):
    return sync.build_records({d["reservation"]["ID"]: d for d in details}, CONFIG, TODAY)


class HelperTests(unittest.TestCase):
    def test_format_phone(self):
        self.assertEqual(sync.format_phone("+15555550123"), "(555) 555-0123")
        self.assertEqual(sync.format_phone("+442071234567"), "+442071234567")
        self.assertEqual(sync.format_phone(""), "")

    def test_pin(self):
        self.assertEqual(sync.pin_from_phone("+15555550123"), "0123")
        self.assertEqual(sync.pin_from_phone(""), "")

    def test_policy_label(self):
        self.assertEqual(sync.policy_label("AIRBNB_FIRM"), "Airbnb Firm")
        self.assertEqual(sync.policy_label("VRBO_MODERATE"), "VRBO Moderate")
        self.assertEqual(sync.policy_label("FAIRLY_DIRECT_LONG_TERM_FIRM"), "Fairly Direct Long Term Firm")
        self.assertEqual(sync.policy_label("FAIRLY_OWNER"), "Owner")


class RecordTests(unittest.TestCase):
    def test_filters_status_and_past_stays(self):
        recs = records_for(
            make_detail("keep"),
            make_detail("cancelled", status="CANCELLED"),
            make_detail("failed", status="PAYMENT_FAILED"),
            make_detail("past", check_in="2026-11-01", check_out="2026-11-05"),
            make_detail("other-home", home="not-configured"),
        )
        self.assertEqual([r["Reservation ID"] for r in recs], ["keep"])

    def test_in_progress_stay_kept_and_flagged(self):
        rec = records_for(make_detail("now", check_in="2026-11-09", check_out="2026-11-12"))[0]
        self.assertEqual(rec["Notes"], "In progress")

    def test_checkout_today_is_kept(self):
        self.assertEqual(len(records_for(make_detail("x", check_in="2026-11-08", check_out="2026-11-11"))), 1)

    def test_fields(self):
        rec = records_for(make_detail("r1"))[0]
        self.assertEqual(rec["Property"], "Mountain Cabin")
        self.assertEqual(rec["Nights"], 4)
        self.assertEqual(rec["Last Initial"], "Z")
        self.assertEqual(rec["Phone"], "(555) 555-0123")
        self.assertEqual(rec["Channel"], "Airbnb")
        self.assertEqual(rec["Owner Payout"], "1000.50")       # deleted row ignored
        self.assertEqual(rec["Caretaker Payout"], "190.25")    # 150 + 40.25
        self.assertEqual(rec["Booked On"], "2026-10-31")       # 03:30 UTC = previous evening in Denver
        self.assertEqual(rec["Pets"], 0)

    def test_owner_stay(self):
        rec = records_for(make_detail("own", guest_type="OWNER", channel="FAIRLY_WEB", initial=None,
                                      owner_due=0, caretaker_due=()))[0]
        self.assertEqual(rec["Last Initial"], "Q")
        self.assertEqual(rec["Channel"], "Owner booking")
        self.assertEqual(rec["Guest Type"], "Owner")
        self.assertEqual(rec["Owner Payout"], "0.00")

    def test_missing_initial_and_phone(self):
        rec = records_for(make_detail("r", initial=None, phone=None))[0]
        self.assertEqual(rec["Last Initial"], "")
        self.assertEqual(rec["Phone"], "")

    def test_sorted_by_home_then_checkin(self):
        recs = records_for(
            make_detail("b2", check_in="2026-12-10", check_out="2026-12-12"),
            make_detail("a1", home="home-a", check_in="2027-01-01", check_out="2027-01-03"),
            make_detail("b1", check_in="2026-12-01", check_out="2026-12-03"),
        )
        self.assertEqual([r["Reservation ID"] for r in recs], ["a1", "b1", "b2"])


class RemoteLockTests(unittest.TestCase):
    def test_row(self):
        row = sync.remotelock_row(records_for(make_detail("r1"))[0])
        self.assertEqual(row, {"name": "Testa Z.", "email": "", "department": "Mountain Cabin",
                               "start date": "2026-12-01", "start time": "16:00",
                               "end date": "2026-12-05", "end time": "10:00", "pin": "0123"})

    def test_airbnb_missing_phone_is_not_a_phone_warning(self):
        recs = records_for(make_detail("air", home="home-a", phone=None, channel="AIRBNB"),
                           make_detail("vrbo", home="home-a", phone=None, channel="VRBO",
                                       check_in="2027-01-01", check_out="2027-01-05"))
        warnings = sync.find_warnings(recs, CONFIG)
        phone_warnings = [w for w in warnings if w.startswith("No phone number")]
        self.assertEqual(len(phone_warnings), 1)
        self.assertIn("2027-01-01", phone_warnings[0])  # only the VRBO stay

    def test_warnings(self):
        recs = records_for(
            make_detail("nophone", phone=None, channel="VRBO"),
            make_detail("p1", check_in="2027-01-01", check_out="2027-01-05", phone="+15555551111"),
            make_detail("p2", check_in="2027-01-03", check_out="2027-01-08", phone="+15555991111"),  # same last 4 as p1
        )
        warnings = sync.find_warnings(recs, CONFIG)
        self.assertTrue(any(w.startswith("No phone number") for w in warnings))
        self.assertTrue(any(w.startswith("No RemoteLock PIN") for w in warnings))
        self.assertTrue(any(w.startswith("Duplicate PIN") for w in warnings))


class DiffTests(unittest.TestCase):
    def test_added_changed_cancelled_completed(self):
        previous = [
            {"Reservation ID": "same", "Check-in": "2026-12-01", "Check-out": "2026-12-05",
             "Guest First Name": "Testa", "Last Initial": "Z", "Phone": "(555) 555-0123", "Property": "x"},
            {"Reservation ID": "moved", "Check-in": "2026-12-10", "Check-out": "2026-12-12",
             "Guest First Name": "Testa", "Last Initial": "Z", "Phone": "(555) 555-0123", "Property": "x"},
            {"Reservation ID": "gone", "Check-in": "2027-02-01", "Check-out": "2027-02-03",
             "Guest First Name": "Gone", "Last Initial": "", "Phone": "", "Property": "x"},
            {"Reservation ID": "done", "Check-in": "2026-11-01", "Check-out": "2026-11-05",
             "Guest First Name": "Done", "Last Initial": "", "Phone": "", "Property": "x"},
        ]
        current = records_for(
            make_detail("same"),
            make_detail("moved", check_in="2026-12-11", check_out="2026-12-13"),
            make_detail("new", check_in="2027-03-01", check_out="2027-03-02"),
        )
        changes = sync.diff(previous, current, TODAY)
        self.assertEqual([r["Reservation ID"] for r in changes["added"]], ["new"])
        self.assertEqual([c["record"]["Reservation ID"] for c in changes["changed"]], ["moved"])
        self.assertEqual(changes["changed"][0]["fields"], ["Check-in", "Check-out"])
        self.assertEqual([r["Reservation ID"] for r in changes["cancelled"]], ["gone"])
        self.assertEqual([r["Reservation ID"] for r in changes["completed"]], ["done"])


class EndToEndTest(unittest.TestCase):
    def test_main_writes_files_and_detects_no_change_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            raw = tmp / "raw"
            raw.mkdir()
            (raw / "r1.json").write_text(json.dumps(make_detail("r1")), encoding="utf-8")
            (raw / "a1.json").write_text(json.dumps(make_detail("a1", home="home-a")), encoding="utf-8")
            (raw / "junk.txt").write_text("not json", encoding="utf-8")
            config = tmp / "config.json"
            config.write_text(json.dumps(CONFIG), encoding="utf-8")

            args = ["--raw", str(raw), "--config", str(config), "--today", "2026-11-11"]
            sync.main(args + ["--out", str(tmp / "run1")])
            files = sorted(p.name for p in (tmp / "run1").iterdir())
            self.assertEqual(files, ["remotelock_mountain.csv", "summary.json", "summary.md",
                                     "upcoming_reservations.csv", "upcoming_reservations_desert.csv",
                                     "upcoming_reservations_mountain.csv"])
            with (tmp / "run1" / "upcoming_reservations.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertNotIn("_phone_e164", rows[0])  # internal fields stay out of the CSV

            sync.main(args + ["--out", str(tmp / "run2"), "--previous", str(tmp / "run1")])
            summary = json.loads((tmp / "run2" / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["has_changes"])


if __name__ == "__main__":
    unittest.main()
