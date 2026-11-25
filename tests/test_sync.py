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
                owner_due=1000.5, caretaker_due=(150, 40.25), offset="-0700", door_code=None, last_name=None):
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
            "GUEST_FIRST_NAME": first, "GUEST_LAST_NAME": last_name, "GUEST_PHONE_NUMBERS": [phone] if phone else [],
            "GUEST_NUMBER_ADULTS": 2, "GUEST_NUMBER_CHILDREN": 1, "GUEST_NUMBER_PETS": 0,
            "EXTERNAL_ID": f"CONF-{rid}", "CANCELLATION_POLICY": "AIRBNB_FIRM", "NOTES": None,
            "BOOKED_AT": "2026-11-01 03:30:00.000 +0000",
            "CHECK_IN_AT": f"{check_in} 16:00:00.000 {offset}", "CHECK_OUT_AT": f"{check_out} 10:00:00.000 {offset}",
            "MESSAGE_THREAD_LINK": f"https://example.test/messages/{rid}",
            "SUGGESTED_DOOR_CODE": door_code,
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

    def test_initial_from_guest_last_name(self):
        for last_name, expected in [("Parker", "P"), ("parker", "P"), ("Smith-Jones", "S"), ("van Example", "E"),
                                    ("Parker (2)", "P"), (" Qu ", "Q")]:
            rec = records_for(make_detail("r", initial=None, last_name=last_name))[0]
            self.assertEqual(rec["Last Initial"], expected, last_name)
            self.assertNotIn(last_name, rec.values())  # only the initial is kept
        # It wins over the message text; a blank one falls back to it.
        self.assertEqual(records_for(make_detail("r", initial="Z", last_name="Parker"))[0]["Last Initial"], "P")
        self.assertEqual(records_for(make_detail("r", initial="Z", last_name=" "))[0]["Last Initial"], "Z")
        # The owner's own stays keep the configured initial.
        owner = records_for(make_detail("o", guest_type="OWNER", last_name="Parker"))[0]
        self.assertEqual(owner["Last Initial"], "Q")

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

    def test_suggested_door_code_is_the_pin(self):
        no_phone = records_for(make_detail("r1", phone=None, door_code="4821"))[0]
        self.assertEqual(sync.remotelock_row(no_phone)["pin"], "4821")
        proxy_phone = records_for(make_detail("r2", phone="+15555550123", door_code="4821"))[0]
        self.assertEqual(sync.remotelock_row(proxy_phone)["pin"], "4821")  # code beats phone
        warnings = sync.find_warnings([no_phone], CONFIG)
        self.assertFalse(any(w.startswith("No RemoteLock PIN") for w in warnings))

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


class SplitToolTests(unittest.TestCase):
    """Nov 2026: Fairly split get_reservation_by_id into three tools / three files."""

    def write_split(self, folder, detail):
        rid = detail["reservation"]["ID"]
        (folder / f"{rid}.json").write_text(json.dumps({"reservation": detail["reservation"]}), encoding="utf-8")
        (folder / f"{rid}.finances.json").write_text(json.dumps(
            {"reservation_id": rid, "funds_distribution": detail["funds_distribution"], "data_definitions": []}),
            encoding="utf-8")
        (folder / f"{rid}.conversations.json").write_text(json.dumps(
            {"conversations": [{"RESERVATION_ID": rid}], "conversation_messages": detail["conversation_messages"]}),
            encoding="utf-8")

    def test_split_files_are_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_split(Path(tmp), make_detail("r1"))
            details = sync.load_details(tmp)
        self.assertEqual(list(details), ["r1"])
        self.assertNotIn("data_definitions", details["r1"])
        rec = sync.build_records(details, CONFIG, TODAY)[0]
        self.assertEqual(rec["Owner Payout"], "1000.50")
        self.assertEqual(rec["Last Initial"], "Z")
        self.assertFalse(any(w.startswith("No finance data") for w in sync.find_warnings([rec], CONFIG)))

    def test_missing_finances_warns(self):
        detail = make_detail("r1")
        del detail["funds_distribution"]
        warnings = sync.find_warnings(records_for(detail), CONFIG)
        self.assertTrue(any(w.startswith("No finance data") for w in warnings))

    def test_initial_from_guest_message_author(self):
        detail = make_detail("r1", first="Testa", initial=None)
        detail["conversation_messages"] = [
            {"MESSAGE_AUTHOR": "fairly system", "MESSAGE_BODY": "Reservation created"},
            {"MESSAGE_AUTHOR": "Testa Owner", "FAIRLY_USER_ID": OWNER_ID, "MESSAGE_BODY": "hi"},  # owner, skipped
            {"MESSAGE_AUTHOR": "Testarossa Y", "MESSAGE_BODY": "hi"},                            # not a word match
            {"MESSAGE_AUTHOR": "Testa van Example", "MESSAGE_BODY": "hello"},
        ]
        self.assertEqual(records_for(detail)[0]["Last Initial"], "E")


class CleaningTests(unittest.TestCase):
    CFG = {**CONFIG, "homes": [CONFIG["homes"][0], {**CONFIG["homes"][1], "cleaning_notify": {"to_email": "x@example.test"}}]}

    def rows(self, recs):
        return [{k: str(v) for k, v in r.items() if not k.startswith("_")} for r in recs]

    def test_new_changed_cancelled_and_initial_only_ignored(self):
        before = records_for(make_detail("keep"), make_detail("move", check_in="2027-01-01", check_out="2027-01-05"),
                             make_detail("gone", check_in="2027-02-01", check_out="2027-02-04"),
                             make_detail("init", check_in="2027-03-01", check_out="2027-03-04", initial=None),
                             make_detail("desert", home="home-a"))
        after = records_for(make_detail("keep"), make_detail("move", check_in="2027-01-02", check_out="2027-01-05"),
                            make_detail("init", check_in="2027-03-01", check_out="2027-03-04"),  # initial filled in
                            make_detail("new", check_in="2027-04-01", check_out="2027-04-03"),
                            make_detail("desert", home="home-a", check_in="2026-12-02"))   # other home
        report, = sync.cleaning_report(self.rows(before), after, self.CFG, TODAY)
        self.assertEqual(report["property"], "Mountain Cabin")
        kinds = [(c["kind"], c["check_in"]) for c in report["changes"]]
        self.assertEqual(kinds, [("New", "2027-04-01"), ("Changed", "2027-01-02"), ("Cancelled", "2027-02-01")])
        changed = report["changes"][1]
        self.assertEqual(changed["was"], "dates were 2027-01-01 to 2027-01-05")
        self.assertEqual(changed["guests"], "3 (2 adults, 1 child)")
        self.assertEqual(len(report["upcoming"]), 4)
        self.assertNotIn("phone", str(report).lower())

    def test_guest_count_change_and_first_run(self):
        before = records_for(make_detail("r1"))
        after_detail = make_detail("r1")
        after_detail["reservation"]["GUEST_NUMBER_ADULTS"] = 4
        report, = sync.cleaning_report(self.rows(before), records_for(after_detail), self.CFG, TODAY)
        self.assertEqual(report["changes"][0]["was"], "guests were 3 (2 adults, 1 child)")
        report, = sync.cleaning_report([], records_for(after_detail), self.CFG, TODAY)   # no baseline
        self.assertEqual(report["changes"], [])

    def test_renamed_home_is_not_a_change(self):
        renamed = {**self.CFG, "homes": [self.CFG["homes"][0], {**self.CFG["homes"][1], "name": "New Name"}]}
        before = self.rows(records_for(
            make_detail("keep"), make_detail("gone", check_in="2027-02-01", check_out="2027-02-04"),
            make_detail("desert", home="home-a")))
        after = sync.build_records({"keep": make_detail("keep")}, renamed, TODAY)
        report, = sync.cleaning_report(before, after, renamed, TODAY)
        self.assertEqual([(c["kind"], c["check_in"]) for c in report["changes"]], [("Cancelled", "2027-02-01")])
        # Exports from before the Home ID column: still no false "New" after a rename.
        legacy = [{k: v for k, v in row.items() if k != "Home ID"} for row in before]
        report, = sync.cleaning_report(legacy, after, renamed, TODAY)
        self.assertEqual(report["changes"], [])
        # ...and without a rename, cancellations are still found by name.
        report, = sync.cleaning_report(legacy, records_for(make_detail("keep")), self.CFG, TODAY)
        self.assertEqual([c["kind"] for c in report["changes"]], ["Cancelled"])

    def test_stay_moved_between_homes(self):
        cfg = {**CONFIG, "homes": [{**h, "cleaning_notify": {"to_email": "x@example.test"}} for h in CONFIG["homes"]]}
        before = self.rows(records_for(make_detail("r1", home="home-a")))
        desert, mountain = sync.cleaning_report(before, records_for(make_detail("r1")), cfg, TODAY)
        self.assertEqual([c["kind"] for c in desert["changes"]], ["Cancelled"])
        self.assertEqual([c["kind"] for c in mountain["changes"]], ["New"])

    def test_owner_stay_label(self):
        rec = records_for(make_detail("o", guest_type="OWNER", first="Mark"))[0]
        rec["Adults"] = rec["Children"] = ""
        row = sync._cleaning_row({k: str(v) for k, v in rec.items()})
        self.assertEqual((row["guest"], row["guests"]), ("Mark Q. (Owner)", ""))


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
            self.assertNotIn("_pin", rows[0])  # internal fields stay out of the CSV

            sync.main(args + ["--out", str(tmp / "run2"), "--previous", str(tmp / "run1")])
            summary = json.loads((tmp / "run2" / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["has_changes"])


if __name__ == "__main__":
    unittest.main()
