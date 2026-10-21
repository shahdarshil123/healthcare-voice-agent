"""Availability lookup: when a message asks about a doctor and a day, attach the REAL free times.

READ-ONLY: this never books anything. It turns everyday wording ("Dr. Raj on Monday from 8am to 9am",
"8 30", "829 AM", "tomorrow") into dates and times, asks the booking service for free slots, and returns
verified facts for the model to word. Code works out the times; the model only explains them.

If the message doesn't name a doctor or a day, the most recent earlier messages in the conversation are
used ("Book an appointment for 8 to 8 30" after "I'd like Dr. Patel on Monday").
"""

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.appointments import (
    HOSPITAL_TIME_ZONE,
    SLOT_MINUTES,
    find_next_available,
    get_doctor_availability,
)
from app.clinic import CLINIC, WEEKDAYS
from app.departments import resolve_departments
from app.doctors import DEPARTMENT_NAMES, find_mentions, resolve_name

APPOINTMENTS_PHONE = CLINIC["hospital"]["appointments_phone"]
NOT_BOOKED = (f"This is information only: NOTHING has been booked. To book, call the appointments line at "
              f"{APPOINTMENTS_PHONE}.")
TYPES_BY_DEPARTMENT = {d["id"]: {t["type"]: t["duration_minutes"] for t in d["appointment_types"]}
                       for d in CLINIC["departments"]}

# Everyday words for appointment types.
TYPE_WORDS = {
    "new_patient": ["new patient", "first visit", "first appointment"],
    "follow_up": ["follow up", "follow-up", "followup", "check back"],
    "annual_physical": ["annual physical", "physical", "checkup", "check-up"],
    "well_child_visit": ["well child", "well-child"],
    "x_ray": ["x-ray", "xray", "x ray"],
    "ultrasound": ["ultrasound"],
    "ct_scan": ["ct scan", "ct", "cat scan"],
    "mri": ["mri"],
}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
EARLIER_MESSAGES_TO_CHECK = 4


def current_time() -> datetime:
    """Now, in hospital time. CAREVOICE_NOW (e.g. "2026-10-20T12:00") fixes it for tests and evaluations."""
    fixed = os.environ.get("CAREVOICE_NOW")
    if fixed:
        return datetime.fromisoformat(fixed).replace(tzinfo=HOSPITAL_TIME_ZONE)
    return datetime.now(HOSPITAL_TIME_ZONE)


# ---------- Understanding dates and times in everyday wording ----------

def find_day(text: str, today: date) -> date | None:  # pylint: disable=too-many-return-statements
    """The first day mentioned: "today", "tomorrow", "Monday", "next Friday", "October 30", "10/30", "2026-10-30".

    One check per way of writing a date, in order, each returning as soon as it matches.
    """
    lower = text.lower()
    if re.search(r"\bday after tomorrow\b", lower):
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b", lower):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", lower):
        return today
    if match := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", lower):
        return date(int(match[1]), int(match[2]), int(match[3]))
    months = "|".join(month[:3] + rf"(?:{month[3:]})?" for month in MONTHS)
    if match := re.search(rf"\b({months})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", lower):
        return upcoming_date(today, MONTHS.index(next(m for m in MONTHS if m.startswith(match[1][:3]))) + 1,
                             int(match[2]))
    if match := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({months})\b", lower):
        return upcoming_date(today, MONTHS.index(next(m for m in MONTHS if m.startswith(match[2][:3]))) + 1,
                             int(match[1]))
    if match := re.search(r"\b(\d{1,2})/(\d{1,2})\b", lower):  # US style: month/day
        return upcoming_date(today, int(match[1]), int(match[2]))
    if match := re.search(r"\b(next\s+)?(" + "|".join(WEEKDAYS) + r")\b", lower):
        days_ahead = (WEEKDAYS.index(match[2]) - today.weekday()) % 7
        if match[1] and days_ahead == 0:  # "next Monday" said on a Monday means a week from today
            days_ahead = 7
        return today + timedelta(days=days_ahead)
    return None


def upcoming_date(today: date, month: int, day: int) -> date | None:
    """This year's date, or next year's if it has already passed. None if it isn't a real date."""
    try:
        candidate = date(today.year, month, day)
        return candidate if candidate >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


TIME_PATTERN = re.compile(
    r"\b(?:(?P<compact>\d{3,4})\s*(?P<compact_half>am|pm|a\.m\.|p\.m\.)"     # "829 AM", "1030pm"
    # "8", "8:30", "8.30", "8 30", each optionally with am/pm. (?!\d) stops "4" being read out of "45",
    # and the last lookahead skips durations ("45 minutes", "2 hours").
    r"|(?P<hour>\d{1,2})(?!\d)(?:[:.\s](?P<minute>[0-5]\d)(?!\d))?\s*(?P<half>am|pm|a\.m\.|p\.m\.)?)"
    r"(?!\s*(?:min|hour|hr))",
    re.IGNORECASE,
)


def find_times(text: str) -> list[time]:
    """Clock times mentioned: "8am", "8:30", "8 30" (speech-to-text), "829 AM", "from 8 to 9".

    A bare number only counts after a word like "at", "from", or "to", so "45 minutes" or "P-1019"
    aren't mistaken for times. Without am/pm, 1 to 7 is taken as afternoon, because the clinics open at 08:00.
    """
    times = []
    for match in TIME_PATTERN.finditer(text):
        half = (match["compact_half"] or match["half"] or "").lower().replace(".", "")
        if match["compact"]:
            hour, minute = int(match["compact"][:-2]), int(match["compact"][-2:])
        else:
            hour, minute = int(match["hour"]), int(match["minute"] or 0)
            before = text[:match.start()]
            after = text[match.end():]
            preceded = re.search(r"\b(at|from|to|for|around|by|until|till|between)\s*$", before, re.I)
            starts_a_range = re.match(r"\s*(to|-|until|till|and)\b", after, re.I)  # the "8" in "8 to 9"
            ends_a_range = re.search(r"\d\s*-\s*$", before)                       # the "9" in "8-9"
            if re.search(r"[A-Za-z]-$", before):  # part of an ID like "P-1019", not a time
                continue
            if not (half or match["minute"] or preceded or starts_a_range or ends_a_range):
                continue
        if half == "pm" and hour < 12:
            hour += 12
        elif half == "am" and hour == 12:
            hour = 0
        elif not half and 1 <= hour <= 7:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.append(time(hour, minute))
    return times


def find_types(text: str, department_id: str) -> list[str]:
    """Appointment types mentioned that this department offers; all of its types if none are mentioned."""
    offered = TYPES_BY_DEPARTMENT[department_id]
    lower = text.lower()
    mentioned = [kind for kind, words in TYPE_WORDS.items()
                 if kind in offered and any(re.search(rf"\b{re.escape(word)}\b", lower) for word in words)]
    return mentioned or list(offered)


# ---------- Building the facts ----------

@dataclass
class Request:
    doctor_ids: list[str]
    day: date | None
    times: list[time]
    text: str  # the message(s) the request was read from, for finding appointment types


def as_ranges(slots: list[datetime]) -> str:
    """[08:00, 08:15, 08:30, 10:00] -> "08:00-08:30, 10:00" (start times, in steps of the slot grid)."""
    ranges, step = [], timedelta(minutes=SLOT_MINUTES)
    for slot in slots:
        if ranges and slot - ranges[-1][1] == step:
            ranges[-1][1] = slot
        else:
            ranges.append([slot, slot])
    return ", ".join(f"{a:%H:%M}" if a == b else f"{a:%H:%M}-{b:%H:%M}" for a, b in ranges)


def doctors_in(text: str) -> list[str]:
    """Doctors named exactly, or, failing that, every doctor in a department named."""
    doctor_ids = [match.candidates[0]["id"] for match in map(resolve_name, find_mentions(text))
                  if match.status == "resolved"]
    if doctor_ids:
        return doctor_ids
    departments = {entity.id for match in resolve_departments(text) if match.status in ("resolved", "probable")
                   for entity in match.entities}
    return [d["id"] for d in CLINIC["doctors"] if d["department_id"] in departments]


def read_request(message: str, earlier_messages: list[str], today: date) -> Request | None:
    """Doctor, day, and times from the message, filling gaps from the most recent earlier messages."""
    doctor_ids, day, times = doctors_in(message), find_day(message, today), find_times(message)
    used = [message]
    for earlier in reversed(earlier_messages[-EARLIER_MESSAGES_TO_CHECK:]):
        if doctor_ids and day:
            break
        if not doctor_ids and (found := doctors_in(earlier)):
            doctor_ids = found
            used.append(earlier)
        if not day and (found_day := find_day(earlier, today)):
            day = found_day
            used.append(earlier)
    # Only answer availability questions: a doctor plus a day or time, or a doctor and a booking word.
    asks_about_time = day or times or re.search(r"\b(book|appointment|available|availability|free|slot)",
                                                message, re.I)
    if not doctor_ids or not asks_about_time:
        return None
    return Request(doctor_ids, day, times, " ".join(used))


def describe_time(slot_time: time, day: date, free: list[datetime], minutes: int) -> str:
    start = datetime.combine(day, slot_time, HOSPITAL_TIME_ZONE)
    if slot_time.minute % SLOT_MINUTES:
        return f"{slot_time:%H:%M} is not a start time (appointments start every {SLOT_MINUTES} minutes)"
    if start in free:
        return f"{slot_time:%H:%M} IS free ({slot_time:%H:%M}-{start + timedelta(minutes=minutes):%H:%M})"
    return f"{slot_time:%H:%M} is NOT available"


def facts_for_doctor(session: Session, doctor_id: str, request: Request, now: datetime) -> list[str]:
    """One fact per appointment type the doctor offers (or the type asked for)."""
    doctor = next(d for d in CLINIC["doctors"] if d["id"] == doctor_id)
    who = f"{doctor['name']} ({DEPARTMENT_NAMES[doctor['department_id']]})"
    working_days = [shift["day"] for shift in doctor["weekly_schedule"]]
    if request.day and WEEKDAYS[request.day.weekday()] not in working_days:
        days = ", ".join(day.capitalize() for day in working_days)
        return [f"{who} does NOT work on {request.day:%A}s (works {days}), so has no times on {request.day:%A %B %d}."]
    facts = []
    for kind in find_types(request.text, doctor["department_id"]):
        minutes = TYPES_BY_DEPARTMENT[doctor["department_id"]][kind]
        label = f"{who}, {kind.replace('_', ' ')} ({minutes} minutes)"
        if kind == "new_patient" and not doctor["accepting_new_patients"]:
            facts.append(f"{who}: is NOT accepting new patients, so no new patient times.")
        elif request.day is None:
            upcoming = find_next_available(session, doctor_id=doctor_id, appointment_type=kind, now=now)
            listed = ", ".join(f"{s:%A %B %d} {s:%H:%M}" for s in upcoming) or "none in the next 14 days"
            facts.append(f"{label}: next free start times: {listed}.")
        else:
            free = get_doctor_availability(session, doctor_id=doctor_id, appointment_type=kind, day=request.day,
                                           now=now).slots
            facts.append(fact_for_day(label, request, free, minutes))
    return facts


def fact_for_day(label: str, request: Request, free: list[datetime], minutes: int) -> str:
    """Free times on the requested day, whether each requested time is free, and whether the visit fits."""
    assert request.day is not None
    when = f"{request.day:%A %B %d, %Y}"
    if not free:
        return f"{label}, {when}: NO free times."
    fact = f"{label}, {when}: free start times {as_ranges(free)}"
    if request.times:
        fact += ". Requested: " + "; ".join(describe_time(t, request.day, free, minutes) for t in request.times)
    if len(request.times) >= 2 and request.times[1] > request.times[0]:
        window = (datetime.combine(request.day, request.times[1]) -
                  datetime.combine(request.day, request.times[0])).seconds // 60
        if minutes > window:
            fact += (f". A {minutes}-minute appointment does NOT fit in the requested "
                     f"{request.times[0]:%H:%M}-{request.times[1]:%H:%M} window")
    return fact + "."


def look_up_availability(session: Session, message: str, earlier_messages: list[str] | None = None,
                         now: datetime | None = None) -> list[str]:
    """Verified free-time facts for the model, or an empty list if the message isn't about availability."""
    now = now or current_time()
    request = read_request(message, earlier_messages or [], now.astimezone(HOSPITAL_TIME_ZONE).date())
    if request is None:
        return []
    facts = [fact for doctor_id in request.doctor_ids[:3]  # at most 3 doctors, to keep the message short
             for fact in facts_for_doctor(session, doctor_id, request, now)]
    return facts + [NOT_BOOKED] if facts else []
