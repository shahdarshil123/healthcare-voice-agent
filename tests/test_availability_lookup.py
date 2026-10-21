"""Tests for the availability lookup (app/availability_lookup.py): reading dates and times from everyday
wording, and the facts given to the model. The parsing tests need no database; the facts tests use the
test database (see conftest.py)."""

# pylint: disable=redefined-outer-name,unused-argument

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.availability_lookup import NOT_BOOKED, as_ranges, current_time, find_day, find_times, look_up_availability

LA = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 10, 20)  # a Tuesday
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LA)


# ---------- Days ----------

@pytest.mark.parametrize("text, expected", [
    ("on Monday", date(2026, 10, 26)),
    ("Tuesday", date(2026, 10, 20)),              # today is Tuesday
    ("next Tuesday", date(2026, 10, 27)),         # "next" means a week from today
    ("tomorrow", date(2026, 10, 21)),
    ("the day after tomorrow", date(2026, 10, 22)),
    ("today", date(2026, 10, 20)),
    ("October 30", date(2026, 10, 30)),
    ("Oct 30th", date(2026, 10, 30)),
    ("2nd of November", date(2026, 11, 2)),
    ("10/30", date(2026, 10, 30)),
    ("2026-11-04", date(2026, 11, 4)),
    ("January 5", date(2027, 1, 5)),              # already passed this year, so next year
])
def test_days_are_understood(text, expected):
    assert find_day(text, TODAY) == expected


@pytest.mark.parametrize("text", ["next week", "February 30", "sometime soon", "I'd like an appointment"])
def test_unclear_or_impossible_days_give_nothing(text):
    assert find_day(text, TODAY) is None


# ---------- Times ----------

@pytest.mark.parametrize("text, expected", [
    ("from 8am to 9am", ["08:00", "09:00"]),
    ("8 to 8 30", ["08:00", "08:30"]),            # how speech-to-text writes "eight to eight thirty"
    ("829 AM", ["08:29"]),                        # speech-to-text for "8:29 AM"
    ("at 8:30", ["08:30"]),
    ("around 9.30", ["09:30"]),
    ("11:45 a.m.", ["11:45"]),
    ("at 2", ["14:00"]),                          # 1-7 without am/pm: afternoon (clinics open at 08:00)
    ("12 pm", ["12:00"]),
    ("8-9", ["08:00", "09:00"]),
    ("a 20 minute follow up at 3", ["15:00"]),
])
def test_times_are_understood(text, expected):
    assert [t.strftime("%H:%M") for t in find_times(text)] == expected


@pytest.mark.parametrize("text", ["for 45 minutes", "I have 2 kids and 3 questions", "call (503) 555-0110",
                                  "patient P-1019", "appointment A-0002", "2 hours"])
def test_numbers_that_are_not_times_are_ignored(text):
    assert find_times(text) == []


def test_free_slots_are_shown_as_ranges():
    slots = [datetime(2026, 10, 30, h, m, tzinfo=LA) for h, m in [(8, 0), (8, 15), (8, 30), (10, 0), (10, 15)]]
    assert as_ranges(slots) == "08:00-08:30, 10:00-10:15"


def test_now_can_be_fixed_for_tests(monkeypatch):
    monkeypatch.setenv("CAREVOICE_NOW", "2026-10-20T12:00")
    assert current_time() == NOW


# ---------- Facts (test database) ----------

def facts(db, message, earlier=()):
    return look_up_availability(db, message, list(earlier), NOW)


def test_a_free_requested_time_is_confirmed_as_free(db):
    found = facts(db, "Is Dr. Raj Patel free on Monday at 8am for a new patient visit?")
    assert "Monday October 26, 2026" in found[0]
    assert "08:00 IS free (08:00-08:45)" in found[0]


def test_a_booked_time_is_reported_as_not_available(db):
    # A-0002: Dr. Okafor, Tuesday Oct 27, 14:00-14:30.
    found = facts(db, "Can I see Dr. Okafor on October 27 at 2pm for a follow up?")
    assert "14:00 is NOT available" in found[0]


def test_a_time_off_the_grid_is_explained(db):
    assert "08:29 is not a start time" in facts(db, "Dr. Raj Patel on Monday from 829 AM")[0]


def test_an_appointment_too_long_for_the_requested_window_is_flagged(db):
    found = facts(db, "Dr. Raj Patel, new patient, Monday from 8 to 8 30")
    assert "does NOT fit in the requested 08:00-08:30 window" in found[0]


def test_missing_doctor_and_day_come_from_earlier_messages(db):
    # The user's real conversation: the last message names neither the doctor nor the day.
    earlier = ["I need to book an appointment with Dr. Raj Patel.",
               "Booking a point met with Dr. Raj on Monday from 8am to 9am."]
    found = facts(db, "Book an appointment for 8 to 8 30.", earlier)
    assert "Dr. Raj Patel" in found[0] and "Monday October 26" in found[0]


def test_a_day_the_doctor_does_not_work_is_one_clear_fact(db):
    found = facts(db, "Can I see Dr. Patel on Tuesday?")
    assert found[0].startswith("Dr. Raj Patel (Orthopedics) does NOT work on Tuesdays")


def test_without_a_day_the_next_free_times_are_given(db):
    found = facts(db, "When is Dr. Okafor free for a follow-up?")
    assert "next free start times" in found[0]


def test_doctors_not_accepting_new_patients_get_no_new_patient_times(db):
    assert "NOT accepting new patients" in facts(db, "Is Dr. Lindqvist free Monday for a new patient visit?")[0]


def test_a_department_question_covers_its_doctors(db):
    found = facts(db, "Is cardiology free on Thursday at 10?")
    assert any("Dr. Elena Rivera" in f for f in found) and any("Dr. James Whitaker" in f for f in found)


def test_every_availability_answer_says_nothing_was_booked(db):
    assert facts(db, "Is Dr. Raj Patel free on Monday?")[-1] == NOT_BOOKED


def test_messages_that_are_not_about_availability_get_no_facts(db):
    assert facts(db, "What is the late cancellation fee?") == []
    assert facts(db, "Does Dr. Patel speak Hindi?") == []  # a doctor, but nothing about times
