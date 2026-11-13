#!/usr/bin/env python3
"""
sync.py - turn raw Fairly reservation data into reservation sheets and RemoteLock files.

Input:  a folder of JSON files, each one the result of the Fairly MCP tool
        `get_reservation_by_id` (one file per reservation).
Output: (written to --out)
        upcoming_reservations.csv               all homes
        upcoming_reservations_<slug>.csv        one per home
        remotelock_<slug>.csv                   homes with "remotelock": true in config.json
        summary.json / summary.md               counts, changes since last run, warnings

Usage:
    python sync.py --raw raw --out output
    python sync.py --raw raw --out output --previous exports_wt   # also report changes
    python sync.py --raw raw --out output --today 2026-11-11      # pretend it is another day

All business rules (what to keep, how each column is built) live in this file.
See CLAUDE.md for the reasoning behind them.
"""

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    sys.exit("Python 3.9 or newer is required.")

import reminders


# ---------------------------------------------------------------------------
# Constants: labels and column layouts
# ---------------------------------------------------------------------------

# Reservation statuses that represent a real, active stay. Everything else
# (CANCELLED, PAYMENT_FAILED, INITIAL_SNAPSHOT, ...) is ignored.
KEEP_STATUSES = {"CONFIRMED", "MIDSTAY_CANCELLED"}

CHANNEL_LABELS = {
    "AIRBNB": "Airbnb",
    "VRBO": "VRBO",
    "FAIRLY_DIRECT": "Fairly Direct",
    "FAIRLY_WEB": "Owner booking",  # stays the owner entered in the Fairly app
}

GUEST_TYPE_LABELS = {
    "GUEST": "Guest",
    "OWNER": "Owner",
    "OWNER_GUEST": "Owner's guest",
}

# Order matters: FAIRLY_DIRECT_ must be checked before any shorter prefix.
POLICY_PREFIXES = [
    ("FAIRLY_DIRECT_", "Fairly Direct"),
    ("AIRBNB_", "Airbnb"),
    ("VRBO_", "VRBO"),
]

RESERVATION_COLUMNS = [
    "Reservation ID",
    "Property",
    "Check-in",
    "Check-out",
    "Nights",
    "Guest First Name",
    "Last Initial",
    "Guest Type",
    "Phone",
    "Email",
    "Channel",
    "Confirmation",
    "Adults",
    "Children",
    "Pets",
    "Booked On",
    "Cancellation Policy",
    "Owner Payout",
    "Caretaker Payout",
    "Message Thread Link",
    "Notes",
]

REMOTELOCK_COLUMNS = [
    "name",
    "email",
    "department",
    "start date",
    "start time",
    "end date",
    "end time",
    "pin",
]

# Fields that, when they change between runs, are worth telling someone about.
TRACKED_FIELDS = ["Check-in", "Check-out", "Guest First Name", "Last Initial", "Phone"]

# Matches the automatic Fairly message "...confirmed reservation with Sierra P."
CREATED_MESSAGE = re.compile(r"reservation with (?P<name>[^\n.]+)\.", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def parse_timestamp(text):
    """Parse Fairly timestamps such as '2026-11-01 16:30:46.332 +0000'."""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unrecognized timestamp: {text!r}")


def get_zone(name):
    """Return a time zone, with a helpful message if tz data is missing (Windows)."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        sys.exit(
            f"Time zone {name!r} not found. On Windows run:  py -m pip install -r requirements.txt"
        )


def digits_only(text):
    return re.sub(r"\D", "", text or "")


def format_phone(e164):
    """'+13173586728' -> '(317) 358-6728'. Non-US numbers are returned unchanged."""
    digits = digits_only(e164)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return e164 or ""


def pin_from_phone(e164):
    """Last 4 digits of the guest's phone ('' if no phone)."""
    digits = digits_only(e164)
    return digits[-4:] if len(digits) >= 4 else ""


def remotelock_pin(reservation):
    """RemoteLock PIN: Airbnb's SUGGESTED_DOOR_CODE when present, else the last 4 of the phone.

    Airbnb no longer shares real guest phones (late Oct 2026), but since 2026-10-20 it sends a
    suggested door code (the last 4 of the real phone), which Fairly exposes. It wins over the
    phone because an Airbnb phone may be a proxy number.
    """
    code = digits_only(reservation.get("SUGGESTED_DOOR_CODE"))
    if code:
        return code
    phones = reservation.get("GUEST_PHONE_NUMBERS") or []
    return pin_from_phone(phones[0]) if phones else ""


def policy_label(code):
    """'AIRBNB_FIRM' -> 'Airbnb Firm', 'FAIRLY_OWNER' -> 'Owner'."""
    if not code:
        return ""
    if code == "FAIRLY_OWNER":
        return "Owner"
    for prefix, label in POLICY_PREFIXES:
        if code.startswith(prefix):
            rest = code[len(prefix):].replace("_", " ").title()
            return f"{label} {rest}"
    return code.replace("_", " ").title()


def money(value):
    return f"{value:.2f}"


def blank_if_none(value):
    return "" if value is None else value


# ---------------------------------------------------------------------------
# Loading raw data
# ---------------------------------------------------------------------------

def _unwrap(data):
    """Some tools save results as [{"type": "text", "text": "<json>"}]. Unwrap that."""
    if isinstance(data, list) and data and isinstance(data[0], dict) and "text" in data[0]:
        try:
            return json.loads(data[0]["text"])
        except json.JSONDecodeError:
            return None
    return data


def load_details(raw_dir):
    """Read every reservation-detail JSON file under raw_dir, keyed by reservation ID."""
    details = {}
    for path in sorted(Path(raw_dir).rglob("*")):
        if not path.is_file():
            continue
        try:
            data = _unwrap(json.loads(path.read_text(encoding="utf-8-sig")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"  skipped (not JSON): {path}", file=sys.stderr)
            continue
        if isinstance(data, dict) and isinstance(data.get("reservation"), dict):
            details[data["reservation"]["ID"]] = data
    return details


# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------

def is_upcoming(reservation, today):
    """Keep active stays that have not checked out before today."""
    if reservation.get("STATUS") not in KEEP_STATUSES:
        return False
    check_out = reservation.get("EFFECTIVE_CHECK_OUT") or reservation["CHECK_OUT"]
    return date.fromisoformat(check_out) >= today


def last_initial(detail, owner):
    """Fairly only stores a first name; the last initial appears in the
    automatic 'Reservation created' message ('...reservation with Sierra P.')."""
    reservation = detail["reservation"]
    if reservation.get("GUEST_TYPE") == "OWNER":
        return owner.get("last_initial", "")

    first = (reservation.get("GUEST_FIRST_NAME") or "").strip()
    for message in detail.get("conversation_messages") or []:
        match = CREATED_MESSAGE.search(message.get("MESSAGE_BODY") or "")
        if not match:
            continue
        full_name = match.group("name").strip()          # e.g. "Sierra P"
        if first and full_name.lower().startswith(first.lower()):
            rest = full_name[len(first):].strip()
        else:
            parts = full_name.split()
            rest = parts[-1] if len(parts) > 1 else ""
        return rest[:1].upper()
    return ""


def payouts(detail):
    """Expected payouts from the live funds distribution (see Fairly KB #84).
    Owner = OWNER role rent share. Caretaker = cleaning fee + caretaker rent share."""
    owner_total = Decimal("0")
    caretaker_total = Decimal("0")
    for row in detail.get("funds_distribution") or []:
        if row.get("DELETED_AT"):
            continue
        amount = Decimal(str(row.get("AMOUNT_DUE") or 0))
        if row.get("ROLE") == "OWNER":
            owner_total += amount
        elif row.get("ROLE") == "CARETAKER":
            caretaker_total += amount
    return owner_total, caretaker_total


def build_record(detail, home, owner, today):
    """Turn one raw Fairly reservation into one row of the reservation sheet."""
    r = detail["reservation"]
    check_in = date.fromisoformat(r["CHECK_IN"])
    check_out = date.fromisoformat(r["CHECK_OUT"])

    booked = parse_timestamp(r.get("BOOKED_AT"))
    booked_local = booked.astimezone(get_zone(home["timezone"])).date().isoformat() if booked else ""

    owner_payout, caretaker_payout = payouts(detail)
    phones = r.get("GUEST_PHONE_NUMBERS") or []

    notes = [r["NOTES"]] if r.get("NOTES") else []
    if check_in < today <= check_out:
        notes.append("In progress")

    return {
        "Reservation ID": r["ID"],
        "Property": home["name"],
        "Check-in": check_in.isoformat(),
        "Check-out": check_out.isoformat(),
        "Nights": (check_out - check_in).days,
        "Guest First Name": (r.get("GUEST_FIRST_NAME") or "").strip(),
        "Last Initial": last_initial(detail, owner),
        "Guest Type": GUEST_TYPE_LABELS.get(r.get("GUEST_TYPE"), r.get("GUEST_TYPE") or ""),
        "Phone": format_phone(phones[0]) if phones else "",
        "Email": "",  # Fairly does not expose guest emails
        "Channel": CHANNEL_LABELS.get(r.get("CHANNEL_ID"), r.get("CHANNEL_ID") or ""),
        "Confirmation": r.get("EXTERNAL_ID") or "",
        "Adults": blank_if_none(r.get("GUEST_NUMBER_ADULTS")),
        "Children": blank_if_none(r.get("GUEST_NUMBER_CHILDREN")),
        "Pets": blank_if_none(r.get("GUEST_NUMBER_PETS")),
        "Booked On": booked_local,
        "Cancellation Policy": policy_label(r.get("CANCELLATION_POLICY")),
        "Owner Payout": money(owner_payout),
        "Caretaker Payout": money(caretaker_payout),
        "Message Thread Link": r.get("MESSAGE_THREAD_LINK") or "",
        "Notes": "; ".join(notes),
        # Not written to the reservation CSV; used for the RemoteLock file.
        "_pin": remotelock_pin(r),
        "_start_time": _local_time(r.get("CHECK_IN_AT"), "16:00"),
        "_end_time": _local_time(r.get("CHECK_OUT_AT"), "10:00"),
    }


def _local_time(timestamp, default):
    """'2027-03-26 16:00:00.000 -0600' -> '16:00' (already in the home's local time)."""
    parsed = parse_timestamp(timestamp) if timestamp else None
    return parsed.strftime("%H:%M") if parsed else default


def remotelock_row(record):
    name = record["Guest First Name"]
    if record["Last Initial"]:
        name = f"{name} {record['Last Initial']}."
    return {
        "name": name,
        "email": record["Email"],
        "department": record["Property"],
        "start date": record["Check-in"],
        "start time": record["_start_time"],
        "end date": record["Check-out"],
        "end time": record["_end_time"],
        "pin": record["_pin"],
    }


def build_records(details, config, today):
    """All upcoming records for configured homes, sorted by home then check-in."""
    homes = {home["id"]: home for home in config["homes"]}
    home_order = {home["id"]: index for index, home in enumerate(config["homes"])}
    records = []
    for detail in details.values():
        reservation = detail["reservation"]
        home = homes.get(reservation.get("HOME_ID"))
        if home is None or not is_upcoming(reservation, today):
            continue
        record = build_record(detail, home, config["owner"], today)
        record["_home_id"] = home["id"]
        records.append(record)
    records.sort(key=lambda rec: (home_order[rec["_home_id"]], rec["Check-in"], rec["Reservation ID"]))
    return records


# ---------------------------------------------------------------------------
# Checks and change detection
# ---------------------------------------------------------------------------

def find_warnings(records, config):
    warnings = []
    remotelock_homes = {home["id"] for home in config["homes"] if home.get("remotelock")}

    for rec in records:
        label = f"{rec['Property']} {rec['Check-in']} {rec['Guest First Name']}"
        # Since late Oct 2026 Airbnb no longer shares guest phone numbers, so a
        # missing phone is expected there and not worth a daily warning.
        if rec["Guest Type"] == "Guest" and not rec["Phone"] and rec["Channel"] != "Airbnb":
            warnings.append(f"No phone number: {label} (message the guest via {rec['Message Thread Link'] or 'Fairly'})")
        if rec["_home_id"] in remotelock_homes and not rec["_pin"]:
            warnings.append(f"No RemoteLock PIN (no phone or suggested door code): {label}")
        if rec["Guest Type"] == "Guest" and not rec["Last Initial"]:
            warnings.append(f"Last initial not found: {label}")

    # Two overlapping stays at the same RemoteLock home must not share a PIN.
    for home_id in remotelock_homes:
        stays = [rec for rec in records if rec["_home_id"] == home_id and rec["_pin"]]
        for i, a in enumerate(stays):
            for b in stays[i + 1:]:
                overlaps = a["Check-in"] < b["Check-out"] and b["Check-in"] < a["Check-out"]
                if overlaps and a["_pin"] == b["_pin"]:
                    warnings.append(
                        f"Duplicate PIN on overlapping stays: {a['Property']} "
                        f"{a['Check-in']} {a['Guest First Name']} / {b['Check-in']} {b['Guest First Name']}"
                    )
    return warnings


def read_previous(previous_dir):
    """Rows from the last run's upcoming_reservations.csv, or [] on the first run."""
    if not previous_dir:
        return []
    path = Path(previous_dir) / "upcoming_reservations.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.DictReader(handle) if row.get("Reservation ID")]


def diff(previous_rows, records, today):
    """Compare last run to this run, keyed by Reservation ID."""
    previous = {row["Reservation ID"]: row for row in previous_rows}
    current = {rec["Reservation ID"]: rec for rec in records}

    added = [current[key] for key in current if key not in previous]
    changed = []
    for key in current.keys() & previous.keys():
        fields = [f for f in TRACKED_FIELDS if str(current[key][f]) != previous[key].get(f, "")]
        if fields:
            changed.append({"record": current[key], "previous": previous[key], "fields": fields})
    removed = [previous[key] for key in previous if key not in current]
    completed = [row for row in removed if row["Check-out"] < today.isoformat()]
    cancelled = [row for row in removed if row["Check-out"] >= today.isoformat()]

    changed.sort(key=lambda item: (item["record"]["Property"], item["record"]["Check-in"]))
    return {"added": added, "changed": changed, "cancelled": cancelled, "completed": completed}


# ---------------------------------------------------------------------------
# Writing output
# ---------------------------------------------------------------------------

def write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def short(rec):
    """One-line description of a reservation for summaries."""
    name = rec["Guest First Name"] + (f" {rec['Last Initial']}." if rec.get("Last Initial") else "")
    return f"{rec['Property']}: {rec['Check-in']} to {rec['Check-out']}, {name}"


def write_outputs(out_dir, records, config, changes, warnings, today, messages=()):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.csv"):
        old.unlink()

    files = ["upcoming_reservations.csv"]
    write_csv(out_dir / "upcoming_reservations.csv", RESERVATION_COLUMNS, records)

    counts = {}
    for home in config["homes"]:
        home_records = [rec for rec in records if rec["_home_id"] == home["id"]]
        counts[home["name"]] = len(home_records)
        name = f"upcoming_reservations_{home['slug']}.csv"
        write_csv(out_dir / name, RESERVATION_COLUMNS, home_records)
        files.append(name)
        if home.get("remotelock"):
            name = f"remotelock_{home['slug']}.csv"
            write_csv(out_dir / name, REMOTELOCK_COLUMNS, [remotelock_row(rec) for rec in home_records])
            files.append(name)

    summary = {
        "run_date": today.isoformat(),
        "total": len(records),
        "per_home": counts,
        "added": [short(rec) for rec in changes["added"]],
        "changed": [f"{short(item['record'])} (changed: {', '.join(item['fields'])})" for item in changes["changed"]],
        "cancelled": [short(row) for row in changes["cancelled"]],
        "completed": [short(row) for row in changes["completed"]],
        "warnings": warnings,
        "reminders": list(messages),
        "files": files,
        "has_changes": bool(changes["added"] or changes["changed"] or changes["cancelled"]),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    return summary


def summary_markdown(summary):
    lines = [f"# Fairly sync {summary['run_date']}", ""]
    lines.append(f"{summary['total']} upcoming reservations: " + ", ".join(
        f"{home} {count}" for home, count in summary["per_home"].items()))
    for title, key in [("New", "added"), ("Changed", "changed"), ("Cancelled / removed", "cancelled"),
                       ("Completed", "completed"), ("Warnings", "warnings")]:
        if summary[key]:
            lines += ["", f"## {title}"] + [f"- {item}" for item in summary[key]]
    if summary.get("reminders"):
        lines += ["", "## Messages to send today"]
        for rem in summary["reminders"]:
            lines += ["", f"### {rem['type']}: {rem['guest']} ({rem['property']}, "
                          f"{rem['check_in']} to {rem['check_out']})",
                      f"Thread: {rem['thread_link'] or 'no Fairly thread (owner booking)'}", ""]
            lines += [f"> {line}" if line else ">" for line in rem["message"].splitlines()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", default="raw", help="folder of get_reservation_by_id JSON files")
    parser.add_argument("--out", default="output", help="folder to write CSVs and summary to")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--previous", help="folder holding the last run's CSVs (for change detection)")
    parser.add_argument("--today", help="override today's date, YYYY-MM-DD")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))  # -sig: tolerate a BOM
    today = date.fromisoformat(args.today) if args.today else datetime.now(get_zone(config["timezone"])).date()

    details = load_details(args.raw)
    if not details:
        sys.exit(f"No reservation files found in {args.raw!r}.")

    records = build_records(details, config, today)
    warnings = find_warnings(records, config)
    messages, reminder_warnings = reminders.build_reminders(records, config, today)
    warnings += reminder_warnings
    changes = diff(read_previous(args.previous), records, today)
    summary = write_outputs(args.out, records, config, changes, warnings, today, messages)

    print(summary_markdown(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
