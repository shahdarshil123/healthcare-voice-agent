"""Tests for clinic.json validation (app/clinic.py). Broken data must be rejected with a clear message."""

import copy
import json

import pytest

from app.clinic import CLINIC_FILE, opening_hours, validate_clinic

REAL = json.loads(CLINIC_FILE.read_text(encoding="utf-8"))


def errors_after(change):
    """Validate a copy of the real clinic.json after applying `change` to it. Returns the errors."""
    data = copy.deepcopy(REAL)
    change(data)
    return validate_clinic(data)[0]


def test_the_real_clinic_file_is_valid():
    assert validate_clinic(REAL) == ([], [])


@pytest.mark.parametrize("label, change, expected", [
    ("misspelled weekday", lambda d: d["doctors"][0]["weekly_schedule"][0].update(day="munday"),
     "weekly_schedule.0.day"),
    ("shift ends before it starts", lambda d: d["doctors"][0]["weekly_schedule"][0].update(end="06:00"),
     "ends (06:00)"),
    ("time in the wrong format", lambda d: d["doctors"][0]["weekly_schedule"][0].update(start="8am"), "start"),
    ("unknown department", lambda d: d["doctors"][2].update(department_id="cardio"), "'cardio' does not exist"),
    ("duplicate doctor id", lambda d: d["doctors"].append(dict(d["doctors"][6], name="Dr. Ravi Patel")),
     "duplicate doctor id"),
    ("shift past closing time", lambda d: d["doctors"][1]["weekly_schedule"][2].update(end="15:00"),
     "outside opening hours"),
    ("shift on a closed day", lambda d: d["doctors"][0]["weekly_schedule"].append(
        {"day": "sunday", "start": "09:00", "end": "12:00"}), "clinics are closed"),
    ("overlapping shifts", lambda d: d["doctors"][0]["weekly_schedule"].append(
        {"day": "monday", "start": "10:00", "end": "11:00"}), "overlapping shifts"),
    ("typo in a field name", lambda d: d["doctors"][0].update(departmentid="family-medicine"), "Extra inputs"),
    ("bad phone format", lambda d: d["hospital"].update(appointments_phone="503-555-0110"), "appointments_phone"),
    ("alias used by two departments", lambda d: d["departments"][2]["aliases"].append("heart doctor"),
     "used by both"),
    ("impossible appointment length", lambda d: d["departments"][0]["appointment_types"][0].update(
        duration_minutes=0), "duration_minutes"),
    ("negative fee", lambda d: d["policies"].update(no_show_fee_usd=-5), "no_show_fee_usd"),
    ("slot grid that doesn't divide an hour", lambda d: d["policies"].update(slot_interval_minutes=25),
     "doesn't divide an hour"),
])
def test_broken_data_is_rejected(label, change, expected):
    errors = errors_after(change)
    assert any(expected in error for error in errors), f"{label}: {errors}"


def test_shared_surname_is_allowed_with_a_warning():
    data = copy.deepcopy(REAL)
    data["doctors"].append(dict(data["doctors"][0], id="dr-wei-chen", name="Dr. Wei Chen"))
    errors, warnings = validate_clinic(data)
    assert errors == []
    assert any("'Chen'" in warning for warning in warnings)


def test_opening_hours_cover_every_day():
    hours = opening_hours(REAL["hospital"]["outpatient_hours"])
    assert hours["monday"] == ("08:00", "18:00") and hours["friday"] == ("08:00", "18:00")
    assert hours["saturday"] == ("09:00", "13:00")
    assert hours["sunday"] is None
