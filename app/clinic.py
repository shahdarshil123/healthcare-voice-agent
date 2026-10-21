"""The hospital's facts, read from the single source of truth: data/synthetic/clinic.json.

The file is VALIDATED when it's loaded. If it's broken (a misspelled weekday, a shift that ends before
it starts, a doctor in a department that doesn't exist), the server refuses to start with a clear
message, instead of silently giving wrong answers or making wrong bookings later.
"""

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CLINIC_FILE = Path(__file__).parent.parent / "data" / "synthetic" / "clinic.json"

logger = logging.getLogger("carevoice.clinic")

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TIME = r"^([01]\d|2[0-3]):[0-5]\d$"             # 24-hour "HH:MM", e.g. "08:00" or "17:30"
PHONE = r"^\(\d{3}\) \d{3}-\d{4}$"              # e.g. "(503) 555-0110"


# ---------- Shape: what each part must look like ----------

class Shift(BaseModel):
    day: Weekday
    start: str = Field(pattern=TIME)
    end: str = Field(pattern=TIME)

    @field_validator("end")
    @classmethod
    def ends_after_it_starts(cls, end, info):
        if "start" in info.data and end <= info.data["start"]:
            raise ValueError(f"shift ends ({end}) before it starts ({info.data['start']})")
        return end


class Doctor(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field is probably a typo, e.g. "departmentid"
    id: str = Field(pattern=r"^dr-[a-z]+(-[a-z]+)+$")
    name: str = Field(pattern=r"^Dr\. [A-Z][A-Za-z'-]+( [A-Z][A-Za-z'-]+)+$")
    department_id: str
    languages: list[str] = Field(min_length=1)
    accepting_new_patients: bool
    weekly_schedule: list[Shift] = Field(min_length=1)


class AppointmentType(BaseModel):
    type: str = Field(pattern=r"^[a-z_]+$")
    duration_minutes: int = Field(ge=5, le=240)


class Department(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(pattern=r"^[a-z]+(-[a-z]+)*$")
    name: str
    aliases: list[str] = []
    location: str
    phone: str = Field(pattern=PHONE)
    description: str
    telehealth_available: bool
    referral_required: bool = False
    appointment_types: list[AppointmentType] = Field(min_length=1)


class Policies(BaseModel):
    model_config = ConfigDict(extra="allow")
    cancellation_notice_hours: int = Field(ge=1)
    late_cancellation_fee_usd: int = Field(ge=0)
    no_show_fee_usd: int = Field(ge=0)
    late_arrival_grace_minutes: int = Field(ge=0)
    max_appointments_booked_per_patient: int = Field(ge=1)
    booking_window_days: int = Field(ge=1)
    # Appointments start on this grid, e.g. 15 -> :00, :15, :30, :45. Must divide an hour evenly.
    slot_interval_minutes: int = Field(ge=5, le=60)

    @field_validator("slot_interval_minutes")
    @classmethod
    def divides_an_hour(cls, minutes):
        if 60 % minutes:
            raise ValueError(f"{minutes} doesn't divide an hour evenly (use 5, 10, 15, 20, 30, or 60)")
        return minutes


class Hospital(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    main_phone: str = Field(pattern=PHONE)
    appointments_phone: str = Field(pattern=PHONE)
    billing_phone: str = Field(pattern=PHONE)
    outpatient_hours: dict[str, str]


class Clinic(BaseModel):
    model_config = ConfigDict(extra="allow")
    hospital: Hospital
    policies: Policies
    departments: list[Department] = Field(min_length=1)
    doctors: list[Doctor] = Field(min_length=1)


# ---------- Relationships: do the parts fit together? ----------

def opening_hours(hours: dict[str, str]) -> dict[str, tuple[str, str] | None]:
    """{"monday_to_friday": "08:00-18:00", "sunday": "closed"} -> {"monday": ("08:00", "18:00"), "sunday": None}"""
    result: dict[str, tuple[str, str] | None] = {}
    for days, value in hours.items():
        if "_to_" in days:
            first, last = days.split("_to_")
            covered = WEEKDAYS[WEEKDAYS.index(first):WEEKDAYS.index(last) + 1]
        else:
            covered = [days]
        opening = None if value == "closed" else tuple(value.split("-"))
        for day in covered:
            result[day] = opening
    return result


def repeated(values: list[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def check_identities(clinic: Clinic) -> tuple[list[str], list[str]]:
    """Unique IDs and names. A shared surname is allowed, but makes "Dr. <surname>" ambiguous."""
    errors = [f"duplicate department id {i!r}" for i in repeated([d.id for d in clinic.departments])]
    errors += [f"duplicate doctor id {i!r}" for i in repeated([d.id for d in clinic.doctors])]
    errors += [f"two doctors are both called {n!r}" for n in repeated([d.name for d in clinic.doctors])]
    warnings = [f"several doctors share the surname {s!r}: 'Dr. {s}' will be ambiguous"
                for s in repeated([d.name.split()[-1] for d in clinic.doctors])]
    return errors, warnings


def check_doctor(doctor: Doctor, department_ids: set[str], hours: dict) -> list[str]:
    """The doctor's department exists, and every shift is within opening hours and doesn't overlap another."""
    errors = []
    if doctor.department_id not in department_ids:
        errors.append(f"{doctor.id}: department {doctor.department_id!r} does not exist")
    for shift in doctor.weekly_schedule:
        opening = hours.get(shift.day)
        if opening is None:
            errors.append(f"{doctor.id}: works on {shift.day}, when outpatient clinics are closed")
        elif shift.start < opening[0] or shift.end > opening[1]:
            errors.append(f"{doctor.id}: {shift.day} {shift.start}-{shift.end} is outside opening hours "
                          f"{opening[0]}-{opening[1]}")
    shifts = sorted(doctor.weekly_schedule, key=lambda s: (WEEKDAYS.index(s.day), s.start))
    for earlier, later in zip(shifts, shifts[1:]):
        if earlier.day == later.day and later.start < earlier.end:
            errors.append(f"{doctor.id}: overlapping shifts on {later.day}")
    return errors


def check_departments(clinic: Clinic) -> tuple[list[str], list[str]]:
    """No alias belongs to two departments; every department has someone to book."""
    errors, warnings, owners = [], [], {}
    for department in clinic.departments:
        for phrase in [department.name.lower(), *department.aliases]:
            if owners.get(phrase, department.id) != department.id:
                errors.append(f"alias {phrase!r} is used by both {owners[phrase]} and {department.id}")
            owners[phrase] = department.id
        if not any(doctor.department_id == department.id for doctor in clinic.doctors):
            warnings.append(f"department {department.id!r} has no doctors, so nothing in it can be booked")
    return errors, warnings


def check_relationships(clinic: Clinic) -> tuple[list[str], list[str]]:
    """Problems that need more than one item to see. Returns (errors, warnings)."""
    errors, warnings = check_identities(clinic)
    department_ids = {department.id for department in clinic.departments}
    hours = opening_hours(clinic.hospital.outpatient_hours)
    for doctor in clinic.doctors:
        errors += check_doctor(doctor, department_ids, hours)
    department_errors, department_warnings = check_departments(clinic)
    return errors + department_errors, warnings + department_warnings


def validate_clinic(data: dict) -> tuple[list[str], list[str]]:
    """Check clinic.json's contents. Returns (errors, warnings); no errors means it's safe to use."""
    try:
        clinic = Clinic.model_validate(data)
    except ValidationError as error:
        # Pydantic lists every problem with its location, e.g. "doctors.3.weekly_schedule.0.day".
        return [f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in error.errors()], []
    return check_relationships(clinic)


def load_clinic(path: Path = CLINIC_FILE) -> dict:
    """Read and validate clinic.json. Raises ValueError (and the server won't start) if it's broken."""
    data = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = validate_clinic(data)
    for warning in warnings:
        logger.warning("clinic.json: %s", warning)
    if errors:
        raise ValueError(f"{path.name} is invalid:\n  " + "\n  ".join(errors))
    return data


CLINIC = load_clinic()
