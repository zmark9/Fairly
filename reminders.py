"""
reminders.py - ready-to-send guest messages for homes with a rotating parking
gate code and a snow-plow schedule (Big Sky).

The Fairly MCP is read-only, so these messages can't be sent automatically.
Instead they go into the daily email with a link to each guest's message
thread: copy, paste, send.

Configured per home in config.json under "parking" (see CLAUDE.md):
  - gate code + plow day every `interval_days` (14) starting `schedule_start`
    (a Tuesday); the change happens at `change_time` (9:00 AM)
  - `gate_codes`: {"YYYY-MM-DD": "code"}; "" = not released yet. A scheduled day
    with no entry at all is a plow-only day (the previous code stays in effect);
    outside plow season such a day is ignored.
  - `snow_removal`: plow season dates and where to move the car

Messages produced (only for Guest / Owner's guest stays, not the owner's own):
  - arrival:   the day before check-in, with the gate code for their stay
  - change:    the day before a change/plow day, for everyone staying that night
"""

from datetime import date, timedelta

PLACEHOLDER = "[GATE CODE TBD]"


def fmt_day(d):
    """date -> 'Tue Nov 24' (works on Windows and Linux)."""
    return f"{d:%a %b} {d.day}"


def change_days(parking, until):
    """All gate-change / plow days from schedule_start up to and including `until`."""
    day = date.fromisoformat(parking["schedule_start"])
    step = timedelta(days=int(parking.get("interval_days", 14)))
    days = []
    while day <= until:
        days.append(day)
        day += step
    return days


def is_gate_change(parking, day):
    """True if the gate code changes on this scheduled day (it has a gate_codes entry)."""
    return day.isoformat() in (parking.get("gate_codes") or {})


def event_days(parking, until):
    """Scheduled days that matter: a gate-code change, a plow day, or both."""
    return [d for d in change_days(parking, until) if is_gate_change(parking, d) or in_plow_season(parking, d)]


def code_for(parking, on_day):
    """Gate code in effect on `on_day` (the most recent gate change on or before it).
    Returns (code_or_None, change_day_or_None). None code = not released yet."""
    applicable = [d for d in change_days(parking, on_day) if is_gate_change(parking, d)]
    if not applicable:
        return None, None
    start = applicable[-1]
    code = parking["gate_codes"].get(start.isoformat()) or None
    return code, start


def in_plow_season(parking, day):
    snow = parking.get("snow_removal")
    if not snow:
        return False
    return date.fromisoformat(snow["season_start"]) <= day <= date.fromisoformat(snow["season_end"])


def changes_during_stay(parking, check_in, check_out):
    """Change days a guest is affected by: they are parked overnight before the
    9 AM change, i.e. check_in < day <= check_out."""
    return [d for d in event_days(parking, check_out) if check_in < d <= check_out]


def _code_text(code):
    return code if code else PLACEHOLDER


def arrival_message(rec, parking, check_in, check_out):
    """Message for the day before check-in. Returns (text, missing_code_days)."""
    missing = []
    code, period_start = code_for(parking, check_in)
    if period_start and not code:
        missing.append(period_start)
    lot = parking.get("lot_name", "the parking lot")
    change_time = parking.get("change_time", "9:00 AM")

    lines = [
        f"Hi {rec['Guest First Name']}, looking forward to hosting you at "
        f"{rec['Property']} starting {fmt_day(check_in)}!",
        "",
        f"Parking: please use the {lot} (directions are in your check-in message). "
        f"The parking gate code for your arrival is {_code_text(code)}.",
    ]
    for day in changes_during_stay(parking, check_in, check_out):
        plow = ""
        if in_plow_season(parking, day):
            snow = parking["snow_removal"]
            plow = (f"please move your car before {snow.get('move_by', change_time)} "
                    f"({snow.get('alternatives', 'another lot')}).")
        if is_gate_change(parking, day):
            new_code, _ = code_for(parking, day)
            if not new_code:
                missing.append(day)
            note = f"On {fmt_day(day)} at {change_time} the gate code changes to {_code_text(new_code)}."
            if plow:
                note += " The lot is also plowed that morning, so " + plow
        else:
            note = f"On {fmt_day(day)} the lot is plowed in the morning, so " + plow
        lines.append(note + " I'll send a reminder the day before.")
    lines += ["", "Safe travels!"]
    return "\n".join(lines), missing


def change_message(rec, parking, day):
    """Message for the day before a change/plow day. Returns (text, missing_code_days)."""
    missing = []
    gate = is_gate_change(parking, day)
    code, _ = code_for(parking, day)
    if gate and not code:
        missing.append(day)
    lot = parking.get("lot_name", "the parking lot")
    change_time = parking.get("change_time", "9:00 AM")

    if in_plow_season(parking, day):
        snow = parking["snow_removal"]
        first = (f"Hi {rec['Guest First Name']}, a quick reminder: the {lot} is plowed tomorrow "
                 f"({fmt_day(day)}). Please move your car before {snow.get('move_by', change_time)} "
                 f"to avoid towing. {snow.get('alternatives_sentence', '')}").strip()
        lines = [first]
        if gate:
            lines += ["", f"Also, the parking gate code changes tomorrow at {change_time} "
                          f"to {_code_text(code)}."]
    else:
        lines = [f"Hi {rec['Guest First Name']}, a quick heads-up: the parking gate code for the "
                 f"{lot} changes tomorrow ({fmt_day(day)}) at {change_time} to {_code_text(code)}."]
    lines += ["", "Thank you!"]
    return "\n".join(lines), missing


def build_reminders(records, config, today):
    """Messages to send today, plus warnings for gate codes that aren't set yet."""
    homes = {h["id"]: h for h in config["homes"] if h.get("parking")}
    reminders, missing_days = [], set()
    tomorrow = today + timedelta(days=1)

    for rec in records:
        home = homes.get(rec.get("_home_id"))
        if home is None or rec["Guest Type"] == "Owner":
            continue
        parking = home["parking"]
        check_in = date.fromisoformat(rec["Check-in"])
        check_out = date.fromisoformat(rec["Check-out"])

        if check_in == tomorrow:
            text, missing = arrival_message(rec, parking, check_in, check_out)
            reminders.append(_reminder("Arrival tomorrow: parking and gate code", rec, text))
            missing_days.update(missing)

        if tomorrow in event_days(parking, tomorrow) and check_in < tomorrow <= check_out:
            plow, gate = in_plow_season(parking, tomorrow), is_gate_change(parking, tomorrow)
            kind = ("Snow removal + gate code change tomorrow" if plow and gate
                    else "Snow removal tomorrow" if plow else "Gate code change tomorrow")
            text, missing = change_message(rec, parking, tomorrow)
            reminders.append(_reminder(kind, rec, text))
            missing_days.update(missing)

    warnings = [f"Gate code for the period starting {fmt_day(d)} ({d.isoformat()}) is not set "
                f"in config.json; the message says {PLACEHOLDER}." for d in sorted(missing_days)]
    return reminders, warnings


def _reminder(kind, rec, text):
    # Shown only to the owner (the message text itself uses the first name).
    if rec.get("Guest Last Name"):
        name = f"{rec['Guest First Name']} {rec['Guest Last Name']}"
    else:
        name = rec["Guest First Name"] + (f" {rec['Last Initial']}." if rec.get("Last Initial") else "")
    return {
        "type": kind,
        "property": rec["Property"],
        "guest": name,
        "check_in": rec["Check-in"],
        "check_out": rec["Check-out"],
        "message": text,
        "thread_link": rec.get("Message Thread Link") or "",
    }
