"""Booking, viewing, and cancelling appointments: the business rules, on top of the database's guarantees.

Who may do what: every function takes an Actor, the person making the request. An Actor can only be created
by authenticate(), so knowing someone's patient ID is never enough. An actor may act for themselves, or for a
patient they are the guardian of while that patient is under 18.

Where rules live (see docs/appointments.md):
- PostgreSQL guarantees: no overlapping bookings (doctor or patient), duration matches the appointment type,
  the doctor exists and belongs to the department, valid status values, synthetic contact details only.
- This module checks everything else: authorization, shifts, booking window, new-patient rule,
  telehealth, the booking limit, idempotency, and cancellation fees.

These functions never commit: the caller decides when the transaction ends (all-or-nothing).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Row, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clinic import CLINIC
from app.models import Appointment, AppointmentType, Department, Doctor, DoctorShift, Patient, appointment_number

HOSPITAL_TIME_ZONE = ZoneInfo(CLINIC["hospital"]["time_zone"])
POLICIES = CLINIC["policies"]
SLOT_MINUTES = POLICIES["slot_interval_minutes"]  # appointments start on this grid: :00, :15, :30, :45
ADULT_AGE = 18


# ---------- Errors: each has a stable code, so an API can turn it into the right response ----------

class AppointmentError(Exception):
    code = "appointment_error"


class AuthenticationFailed(AppointmentError):
    code = "authentication_failed"


class NotAuthorized(AppointmentError):
    code = "not_authorized"


class DoctorNotFound(AppointmentError):
    code = "doctor_not_found"


class AppointmentNotFound(AppointmentError):
    code = "appointment_not_found"


class InvalidAppointmentType(AppointmentError):
    code = "invalid_appointment_type"


class InvalidTime(AppointmentError):
    code = "invalid_time"


class OutsideSchedule(AppointmentError):
    code = "outside_schedule"


class SlotUnavailable(AppointmentError):
    code = "slot_unavailable"


class NotAcceptingNewPatients(AppointmentError):
    code = "not_accepting_new_patients"


class TelehealthNotAvailable(AppointmentError):
    code = "telehealth_not_available"


class BookingLimitReached(AppointmentError):
    code = "booking_limit_reached"


class InvalidTransition(AppointmentError):
    code = "invalid_transition"


# ---------- Who is asking ----------

_PROOF = object()  # private: only authenticate() has it, so only authenticate() can create an Actor


@dataclass(frozen=True)
class Actor:
    """The verified person making a request. Create one with authenticate(), never directly."""

    patient_id: str
    verified_by: str
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._proof is not _PROOF:
            raise TypeError("An Actor can only be created by authenticate(): a patient ID alone proves nothing.")


def authenticate(session: Session, patient_id: str, date_of_birth: date) -> Actor:
    """Verify identity with patient ID AND date of birth. Later phases can add stronger methods."""
    patient = session.get(Patient, patient_id)
    if patient is None or patient.date_of_birth != date_of_birth:
        # The same message either way, so a caller can't discover which patient IDs exist.
        raise AuthenticationFailed("The patient ID and date of birth don't match our records.")
    return Actor(patient_id=patient.id, verified_by="patient_id+date_of_birth", _proof=_PROOF)


def age_on(birthday: date, day: date) -> int:
    return day.year - birthday.year - ((day.month, day.day) < (birthday.month, birthday.day))


def authorize(session: Session, actor: Actor, patient_id: str, now: datetime) -> Patient:
    """Return the patient if the actor may act for them: themselves, or as guardian of a minor."""
    patient = session.get(Patient, patient_id)
    if patient is not None:
        if actor.patient_id == patient.id:
            return patient
        is_minor = age_on(patient.date_of_birth, now.astimezone(HOSPITAL_TIME_ZONE).date()) < ADULT_AGE
        if patient.guardian_id == actor.patient_id and is_minor:
            return patient
    raise NotAuthorized("You can only manage your own appointments, or those of a child you are the guardian of.")


# ---------- Booking ----------

def check_within_shift(session: Session, doctor: Doctor, starts_at: datetime, duration_minutes: int) -> None:
    local_start = starts_at.astimezone(HOSPITAL_TIME_ZONE)
    local_end = local_start + timedelta(minutes=duration_minutes)
    shifts = session.scalars(select(DoctorShift).where(DoctorShift.doctor_id == doctor.id,
                                                       DoctorShift.weekday == local_start.weekday())).all()
    fits = any(local_end.date() == local_start.date()
               and shift.starts <= local_start.time() and local_end.time() <= shift.ends for shift in shifts)
    if not fits:
        hours = ", ".join(f"{s.starts:%H:%M}-{s.ends:%H:%M}" for s in shifts) or "no hours"
        raise OutsideSchedule(f"{doctor.name} works {hours} on {local_start:%A}s. A {duration_minutes}-minute "
                              f"appointment at {local_start:%H:%M} doesn't fit.")


def constraint_name(error: IntegrityError) -> str:
    """Which database rule rejected the change, e.g. "no_double_booking_doctor"."""
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", "") or ""


def create_appointment(
    session: Session,
    actor: Actor,
    *,
    patient_id: str,
    doctor_id: str,
    appointment_type: str,
    starts_at: datetime,
    now: datetime,
    mode: str = "in_person",
    idempotency_key: str | None = None,
) -> Appointment:
    """Book an appointment. Raises an AppointmentError explaining why if it isn't allowed."""
    if starts_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("starts_at and now must include a time zone")
    authorize(session, actor, patient_id, now)
    # Lock the patient's row until this transaction ends, so two simultaneous bookings for the same patient
    # take turns (otherwise both could pass the booking-limit check at once).
    session.execute(select(Patient.id).where(Patient.id == patient_id).with_for_update())

    # The same request sent again (e.g. after a network timeout) returns the first booking instead of a second.
    if idempotency_key:
        earlier = session.scalar(select(Appointment).where(Appointment.idempotency_key == idempotency_key))
        if earlier is not None:
            if earlier.patient_id != patient_id:
                raise NotAuthorized("That request key belongs to another booking.")
            return earlier

    doctor, department, kind = find_doctor_and_type(session, doctor_id, appointment_type)
    check_booking_rules(session, patient_id=patient_id, doctor=doctor, department=department, kind=kind,
                        starts_at=starts_at, mode=mode, now=now)
    return save_new_appointment(session, doctor, Appointment(
        id=f"A-{session.scalar(appointment_number.next_value()):04d}",
        patient_id=patient_id, doctor_id=doctor.id, department_id=department.id,
        appointment_type=appointment_type, duration_minutes=kind.duration_minutes,
        starts_at=starts_at, ends_at=starts_at + timedelta(minutes=kind.duration_minutes),
        mode=mode, status="booked", booked_by_patient_id=actor.patient_id, idempotency_key=idempotency_key,
    ))


def find_doctor_and_type(session: Session, doctor_id: str,
                         appointment_type: str) -> tuple[Doctor, Department, AppointmentType]:
    doctor = session.get(Doctor, doctor_id)
    if doctor is None:
        raise DoctorNotFound(f"There is no doctor with the ID {doctor_id!r}.")
    department = session.get(Department, doctor.department_id)
    assert department is not None  # guaranteed by the foreign key
    kind = session.get(AppointmentType, (department.id, appointment_type))
    if kind is None:
        valid = session.scalars(select(AppointmentType.type).where(AppointmentType.department_id == department.id))
        raise InvalidAppointmentType(f"{department.name} offers: {', '.join(valid)}. Not {appointment_type!r}.")
    return doctor, department, kind


def check_booking_rules(session: Session, *, patient_id: str, doctor: Doctor, department: Department,
                        kind: AppointmentType, starts_at: datetime, mode: str, now: datetime) -> None:
    """The rules the database can't check by itself. Raises an AppointmentError for the first one broken."""
    if starts_at <= now:
        raise InvalidTime("Appointments can't be booked in the past.")
    if starts_at > now + timedelta(days=POLICIES["booking_window_days"]):
        raise InvalidTime(f"Appointments can be booked at most {POLICIES['booking_window_days']} days ahead.")
    local = starts_at.astimezone(HOSPITAL_TIME_ZONE)
    if local.minute % SLOT_MINUTES or local.second or local.microsecond:
        raise InvalidTime(f"Appointments start every {SLOT_MINUTES} minutes (for example 08:00, 08:15, 08:30), "
                          f"not at {local:%H:%M}.")
    check_within_shift(session, doctor, starts_at, kind.duration_minutes)
    if mode == "telehealth" and not department.telehealth_available:
        raise TelehealthNotAvailable(f"{department.name} doesn't offer telehealth appointments.")
    if kind.type == "new_patient" and not doctor.accepting_new_patients:
        raise NotAcceptingNewPatients(f"{doctor.name} isn't accepting new patients.")
    limit = POLICIES["max_appointments_booked_per_patient"]
    # SQLAlchemy builds func.count at runtime, which Pylint can't see.
    # pylint: disable-next=not-callable
    upcoming = session.scalar(select(func.count()).select_from(Appointment).where(
        Appointment.patient_id == patient_id, Appointment.status == "booked", Appointment.starts_at > now))
    if upcoming >= limit:
        raise BookingLimitReached(f"A patient can have at most {limit} upcoming appointments.")


# Database rules that reject a new appointment, and what they mean for the person booking.
SLOT_CONFLICTS = {
    "no_double_booking_doctor": "{doctor} already has an appointment at that time.",
    "no_overlapping_patient_appointments": "You already have another appointment at that time.",
    "appointments_idempotency_key_key": "This booking request is already being processed.",  # sent twice at once
}


def save_new_appointment(session: Session, doctor: Doctor, appointment: Appointment) -> Appointment:
    """Insert the appointment. The database has the final say on overlaps, even for simultaneous requests."""
    # A savepoint: if the database rejects this row, only the insert is undone, not the caller's whole transaction.
    try:
        with session.begin_nested():
            session.add(appointment)
    except IntegrityError as error:
        message = SLOT_CONFLICTS.get(constraint_name(error))
        if message is None:
            raise
        raise SlotUnavailable(message.format(doctor=doctor.name)) from error
    return appointment


# ---------- Availability (read-only) ----------

@dataclass(frozen=True)
class Availability:
    """The free start times for one doctor, one appointment type, and one day."""

    doctor_id: str
    doctor_name: str
    appointment_type: str
    duration_minutes: int
    day: date
    slots: list[datetime]  # time-zone-aware start times, earliest first


def get_doctor_availability(session: Session, *, doctor_id: str, appointment_type: str, day: date,
                            now: datetime) -> Availability:
    """Free start times for an appointment with a doctor on one day (hospital local date).

    A slot is free if the whole appointment fits inside one of the doctor's shifts, starts on the
    slot grid, is in the booking window, and doesn't overlap any of the doctor's BOOKED appointments.
    These are exactly the times create_appointment() would accept for this doctor.

    Needs no Actor: it reveals only free times, never who is booked when. Read-only.
    """
    doctor, _, kind = find_doctor_and_type(session, doctor_id, appointment_type)
    day_start = datetime.combine(day, datetime.min.time(), HOSPITAL_TIME_ZONE)
    booked = session.execute(select(Appointment.starts_at, Appointment.ends_at).where(
        Appointment.doctor_id == doctor.id, Appointment.status == "booked",
        Appointment.starts_at < day_start + timedelta(days=1), Appointment.ends_at > day_start)).all()
    shifts = session.scalars(select(DoctorShift).where(DoctorShift.doctor_id == doctor.id,
                                                       DoctorShift.weekday == day.weekday())
                             .order_by(DoctorShift.starts)).all()
    window = (now, now + timedelta(days=POLICIES["booking_window_days"]))
    slots = [start for shift in shifts
             for start in free_starts_in_shift(day, shift, timedelta(minutes=kind.duration_minutes), booked, window)]
    return Availability(doctor.id, doctor.name, appointment_type, kind.duration_minutes, day, slots)


def free_starts_in_shift(day: date, shift: DoctorShift, length: timedelta, booked: Sequence[Row],
                         window: tuple[datetime, datetime]) -> list[datetime]:
    """Grid start times in one shift where the whole appointment fits and overlaps nothing booked.

    `booked` holds (starts_at, ends_at) rows of the doctor's booked appointments that day.
    """
    starts = []
    start = datetime.combine(day, shift.starts, HOSPITAL_TIME_ZONE)
    shift_end = datetime.combine(day, shift.ends, HOSPITAL_TIME_ZONE)
    while start + length <= shift_end:
        free = all(start >= taken_end or start + length <= taken_start for taken_start, taken_end in booked)
        if window[0] < start <= window[1] and free:
            starts.append(start)
        start += timedelta(minutes=SLOT_MINUTES)
    return starts


def find_next_available(session: Session, *, doctor_id: str, appointment_type: str, now: datetime,
                        limit: int = 5, days_ahead: int = 14) -> list[datetime]:
    """The next `limit` free start times with a doctor, searching up to `days_ahead` days from today."""
    today = now.astimezone(HOSPITAL_TIME_ZONE).date()
    found: list[datetime] = []
    for offset in range(days_ahead + 1):
        day_slots = get_doctor_availability(session, doctor_id=doctor_id, appointment_type=appointment_type,
                                            day=today + timedelta(days=offset), now=now).slots
        found += day_slots[:limit - len(found)]
        if len(found) == limit:
            break
    return found


# ---------- Viewing and cancelling ----------

def get_appointment(session: Session, actor: Actor, appointment_id: str, now: datetime) -> Appointment:
    """An appointment the actor may see. Someone else's is reported as not found, without confirming it exists."""
    appointment = session.get(Appointment, appointment_id)
    if appointment is None:
        raise AppointmentNotFound(f"No appointment {appointment_id!r} was found for you.")
    try:
        authorize(session, actor, appointment.patient_id, now)
    except NotAuthorized:
        raise AppointmentNotFound(f"No appointment {appointment_id!r} was found for you.") from None
    return appointment


@dataclass(frozen=True)
class Cancellation:
    appointment: Appointment
    fee_usd: int  # late cancellation fee, 0 if cancelled with enough notice


def cancel_appointment(session: Session, actor: Actor, appointment_id: str, now: datetime) -> Cancellation:
    """Cancel a booked, future appointment. Less than the notice period before it means a late fee."""
    appointment = get_appointment(session, actor, appointment_id, now)
    if appointment.status != "booked":
        raise InvalidTransition(f"Appointment {appointment_id} is {appointment.status}, so it can't be cancelled.")
    if appointment.starts_at <= now:
        raise InvalidTransition("An appointment that has already started can't be cancelled.")
    late = appointment.starts_at - now < timedelta(hours=POLICIES["cancellation_notice_hours"])
    appointment.status = "cancelled"
    appointment.cancelled_at = now
    session.flush()
    return Cancellation(appointment, POLICIES["late_cancellation_fee_usd"] if late else 0)
