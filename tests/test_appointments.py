"""Tests for the appointment backend: PostgreSQL constraints and the booking service (app/appointments.py).

They run against a separate test database (see conftest.py), rebuilt from the migrations and the seed data at
the start of every test run. Each test's changes are undone afterwards. Needs PostgreSQL: docker compose up -d

Some tests insert rows DIRECTLY, bypassing the service, to prove the database itself enforces the rule.
"""

# pylint: disable=redefined-outer-name,unused-argument

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from app.appointments import (
    SLOT_MINUTES,
    Actor,
    AppointmentError,
    AppointmentNotFound,
    AuthenticationFailed,
    BookingLimitReached,
    DoctorNotFound,
    InvalidAppointmentType,
    InvalidTime,
    InvalidTransition,
    NotAcceptingNewPatients,
    NotAuthorized,
    OutsideSchedule,
    SlotUnavailable,
    TelehealthNotAvailable,
    authenticate,
    cancel_appointment,
    create_appointment,
    find_next_available,
    get_appointment,
    get_doctor_availability,
)
from app.database import make_session_factory
from app.models import Appointment, AppointmentType, Patient

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LA)  # the seed data's reference moment (a Tuesday, noon)


def at(text: str) -> datetime:
    """'2026-10-27 14:00' in hospital local time."""
    return datetime.fromisoformat(text).replace(tzinfo=LA)


def actor(db, patient_id: str) -> Actor:
    """Log in as a patient, the only way to get an Actor."""
    return authenticate(db, patient_id, db.get(Patient, patient_id).date_of_birth)


def book(db, patient_id: str, doctor_id: str, appointment_type: str, starts: str, *, as_patient: str | None = None,
         **options):
    return create_appointment(db, actor(db, as_patient or patient_id), patient_id=patient_id, doctor_id=doctor_id,
                              appointment_type=appointment_type, starts_at=at(starts), now=NOW, **options)


def raw_appointment(**changes) -> Appointment:
    """A valid appointment row, with some fields changed, for inserting directly (bypassing the service)."""
    values = {"id": "A-9999", "patient_id": "P-1019", "doctor_id": "dr-raj-patel", "department_id": "orthopedics",
              "appointment_type": "follow_up", "duration_minutes": 20, "starts_at": at("2026-10-30 11:00"),
              "ends_at": at("2026-10-30 11:20"), "mode": "in_person", "status": "booked",
              "booked_by_patient_id": "P-1019"}
    values.update(changes)
    return Appointment(**values)


def rejected_by(db, row) -> str:
    """Insert a row directly and return the name of the database constraint that rejected it."""
    with pytest.raises(IntegrityError) as caught:
        with db.begin_nested():
            db.add(row)
    return caught.value.orig.diag.constraint_name


# ==================== Schedule overlaps (no double booking) ====================

def test_overlapping_booking_with_the_same_doctor_is_rejected(db):
    # A-0002: Dr. Okafor, Tuesday 2026-10-27 14:00-14:30.
    with pytest.raises(SlotUnavailable, match="already has an appointment"):
        book(db, "P-1019", "dr-chidi-okafor", "new_patient", "2026-10-27 14:00")


def test_partially_overlapping_booking_is_rejected(db):
    with pytest.raises(SlotUnavailable):
        book(db, "P-1019", "dr-chidi-okafor", "follow_up", "2026-10-27 14:15")  # 14:15-14:30 overlaps


def test_back_to_back_appointments_are_allowed(db):
    appointment = book(db, "P-1019", "dr-chidi-okafor", "follow_up", "2026-10-27 14:30")  # starts as A-0002 ends
    assert appointment.status == "booked"


def test_a_cancelled_appointment_does_not_block_its_slot(db):
    # A-0007: Dr. Brooks, Thursday 2026-10-22 15:00, cancelled.
    appointment = book(db, "P-1007", "dr-samuel-brooks", "follow_up", "2026-10-22 15:00")
    assert appointment.status == "booked"


def test_a_patient_cannot_be_in_two_appointments_at_once(db):
    # A-0001: P-1001 with Dr. Rivera, Monday 2026-11-02 10:00-10:30.
    with pytest.raises(SlotUnavailable, match="You already have"):
        book(db, "P-1001", "dr-raj-patel", "follow_up", "2026-11-02 10:15")


def test_the_database_itself_rejects_a_double_booking(db):
    # Bypasses the service entirely: even buggy code couldn't double-book.
    clash = raw_appointment(doctor_id="dr-chidi-okafor", department_id="dermatology", appointment_type="follow_up",
                            duration_minutes=15, starts_at=at("2026-10-27 14:10"), ends_at=at("2026-10-27 14:25"))
    assert rejected_by(db, clash) == "no_double_booking_doctor"


def test_simultaneous_requests_for_one_slot_produce_exactly_one_booking(db_engine):
    """20 requests at the same moment, from different patients, for one slot: only one may succeed."""
    # One connection per thread (NullPool): the default pool holds 15, and 20 threads waiting for each other at
    # the barrier would otherwise wait forever for the missing 5 connections.
    engine = create_engine(db_engine.url, poolclass=NullPool)
    session_factory = make_session_factory(engine)
    # Patients with at most 1 upcoming appointment, so a patient who sends two requests can't hit the 3-appointment
    # limit if their first one wins (P-1001, with 2, made this test flaky: "booking_limit_reached" now and then).
    patients = ["P-1002", "P-1005", "P-1006", "P-1007", "P-1008", "P-1011", "P-1012", "P-1014",
                "P-1015", "P-1016", "P-1017", "P-1018", "P-1019", "P-1020"]
    requests = [patients[i % len(patients)] for i in range(20)]
    start_together = threading.Barrier(len(requests), timeout=30)  # never wait forever
    outcomes: list[str] = []

    def request(patient_id: str) -> None:
        with session_factory() as session:
            who = actor(session, patient_id)
            try:
                start_together.wait()  # release all 20 at the same instant
            except threading.BrokenBarrierError:
                outcomes.append("never_started")
                return
            try:
                create_appointment(session, who, patient_id=patient_id, doctor_id="dr-olivia-hart",
                                   appointment_type="x_ray", starts_at=at("2026-10-28 09:00"), now=NOW)
                session.commit()
                outcomes.append("booked")
            except AppointmentError as error:
                session.rollback()
                outcomes.append(error.code)

    threads = [threading.Thread(target=request, args=(patient_id,)) for patient_id in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    try:
        assert len(outcomes) == 20
        assert outcomes.count("booked") == 1
        assert set(outcomes) == {"booked", "slot_unavailable"}
    finally:  # this test commits for real, so clean up after itself
        with session_factory.begin() as session:
            session.execute(delete(Appointment).where(Appointment.doctor_id == "dr-olivia-hart",
                                                      Appointment.starts_at == at("2026-10-28 09:00")))
        engine.dispose()


# ==================== Invalid appointment durations ====================

def test_appointment_type_must_belong_to_the_doctors_department(db):
    with pytest.raises(InvalidAppointmentType, match="Dermatology offers"):
        book(db, "P-1019", "dr-chidi-okafor", "mri", "2026-10-28 11:00")  # MRI is a Radiology type


def test_the_database_rejects_a_duration_that_does_not_match_the_type(db):
    # A dermatology follow-up is 15 minutes, not 60.
    wrong = raw_appointment(doctor_id="dr-chidi-okafor", department_id="dermatology", appointment_type="follow_up",
                            duration_minutes=60, starts_at=at("2026-10-28 11:00"), ends_at=at("2026-10-28 12:00"))
    assert rejected_by(db, wrong) == "fk_type_and_duration"


def test_the_database_rejects_an_end_time_that_does_not_match_the_duration(db):
    assert rejected_by(db, raw_appointment(ends_at=at("2026-10-30 11:45"))) == "end_matches_duration"


@pytest.mark.parametrize("minutes", [0, -15, 600])
def test_the_database_rejects_unrealistic_appointment_lengths(db, minutes):
    kind = AppointmentType(department_id="orthopedics", type="odd_length", duration_minutes=minutes)
    assert rejected_by(db, kind) == "duration_is_realistic"


def test_an_appointment_must_fit_inside_the_doctors_shift(db):
    # Dr. Patel works Friday 08:00-15:00; a 20-minute follow-up at 14:45 would end at 15:05.
    with pytest.raises(OutsideSchedule, match="doesn't fit"):
        book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 14:45")
    # A 60-minute MRI at 17:30 with Dr. Hart (until 18:00) also doesn't fit.
    with pytest.raises(OutsideSchedule):
        book(db, "P-1019", "dr-olivia-hart", "mri", "2026-10-30 17:30")


def test_no_booking_on_a_day_the_doctor_does_not_work(db):
    with pytest.raises(OutsideSchedule, match="no hours"):
        book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-27 10:00")  # Patel doesn't work Tuesdays


# ==================== Nonexistent doctors ====================

def test_booking_with_a_doctor_who_does_not_exist_is_rejected(db):
    with pytest.raises(DoctorNotFound, match="dr-nobody"):
        book(db, "P-1019", "dr-nobody", "follow_up", "2026-10-30 11:00")


def test_the_database_rejects_an_appointment_with_an_unknown_doctor(db):
    assert rejected_by(db, raw_appointment(doctor_id="dr-nobody")) == "fk_doctor_in_department"


def test_the_database_rejects_a_doctor_in_the_wrong_department(db):
    # Dr. Patel exists, but works in Orthopedics, not Cardiology.
    wrong = raw_appointment(department_id="cardiology", appointment_type="follow_up", duration_minutes=30,
                            ends_at=at("2026-10-30 11:30"))
    assert rejected_by(db, wrong) == "fk_doctor_in_department"


# ==================== A patient ID alone is never enough ====================

def test_logging_in_needs_the_right_date_of_birth(db):
    with pytest.raises(AuthenticationFailed):
        authenticate(db, "P-1002", date(2000, 1, 1))


def test_unknown_and_wrong_logins_give_the_same_message(db):
    # So nobody can find out which patient IDs exist by trying them.
    with pytest.raises(AuthenticationFailed) as unknown:
        authenticate(db, "P-9999", date(2000, 1, 1))
    with pytest.raises(AuthenticationFailed) as wrong:
        authenticate(db, "P-1002", date(2000, 1, 1))
    assert str(unknown.value) == str(wrong.value)


def test_an_actor_cannot_be_created_without_logging_in():
    with pytest.raises(TypeError, match="patient ID alone proves nothing"):
        Actor(patient_id="P-1002", verified_by="just trust me")


def test_a_patient_cannot_book_for_someone_else(db):
    with pytest.raises(NotAuthorized):
        book(db, "P-1002", "dr-raj-patel", "follow_up", "2026-10-30 11:00", as_patient="P-1001")


def test_another_patients_appointment_looks_like_it_does_not_exist(db):
    # P-1001 knows the ID A-0002 (P-1002's appointment). That must not reveal or change anything.
    with pytest.raises(AppointmentNotFound):
        get_appointment(db, actor(db, "P-1001"), "A-0002", NOW)
    with pytest.raises(AppointmentNotFound):
        cancel_appointment(db, actor(db, "P-1001"), "A-0002", NOW)
    assert db.get(Appointment, "A-0002").status == "booked"


def test_a_guardian_can_manage_their_childs_appointments(db):
    # P-1005 is the guardian of P-1004 (age 7); A-0004 is P-1004's appointment.
    assert get_appointment(db, actor(db, "P-1005"), "A-0004", NOW).patient_id == "P-1004"
    appointment = book(db, "P-1004", "dr-thomas-nguyen", "follow_up", "2026-10-30 10:00", as_patient="P-1005")
    assert appointment.booked_by_patient_id == "P-1005"


def test_being_a_guardian_of_one_child_gives_no_access_to_another(db):
    # P-1012 is the guardian of P-1009, not of P-1004.
    with pytest.raises(AppointmentNotFound):
        get_appointment(db, actor(db, "P-1012"), "A-0004", NOW)


def test_guardian_access_ends_when_the_child_turns_18(db):
    # P-1013 (born 2009-01-25) is 17 now, and 18 from 2027-01-25. P-1014 is the guardian.
    child_is_17 = NOW
    child_is_18 = datetime(2027, 1, 26, 12, 0, tzinfo=LA)
    assert get_appointment(db, actor(db, "P-1014"), "A-0014", child_is_17)
    with pytest.raises(AppointmentNotFound):
        get_appointment(db, actor(db, "P-1014"), "A-0014", child_is_18)


# ==================== Other booking rules ====================

def test_no_booking_in_the_past_or_too_far_ahead(db):
    with pytest.raises(InvalidTime, match="past"):
        book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-19 10:00")
    with pytest.raises(InvalidTime, match="90 days"):
        book(db, "P-1019", "dr-raj-patel", "follow_up", "2027-02-01 10:00")


def test_telehealth_only_where_the_department_offers_it(db):
    with pytest.raises(TelehealthNotAvailable):
        book(db, "P-1019", "dr-chidi-okafor", "follow_up", "2026-10-28 11:00", mode="telehealth")
    assert book(db, "P-1019", "dr-maya-chen", "follow_up", "2026-10-28 11:00", mode="telehealth").mode == "telehealth"


def test_new_patients_cannot_book_doctors_who_are_not_accepting_them(db):
    with pytest.raises(NotAcceptingNewPatients):
        book(db, "P-1019", "dr-hannah-lindqvist", "new_patient", "2026-10-26 09:00")


def test_a_patient_can_have_at_most_three_upcoming_appointments(db):
    # P-1010 already has A-0010, A-0011, and A-0012.
    with pytest.raises(BookingLimitReached):
        book(db, "P-1010", "dr-chidi-okafor", "follow_up", "2026-10-28 11:00")


def test_the_same_request_twice_books_only_once(db):
    first = book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 11:00", idempotency_key="req-123")
    again = book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 11:00", idempotency_key="req-123")
    assert again.id == first.id
    assert len(db.scalars(select(Appointment).where(Appointment.idempotency_key == "req-123")).all()) == 1


def test_new_appointments_get_numbered_ids(db):
    assert book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 11:00").id.startswith("A-")


def test_appointments_start_on_the_slot_grid(db):
    # Slots start every 15 minutes (clinic.json: slot_interval_minutes). 08:29 isn't a start time; 08:30 is.
    assert SLOT_MINUTES == 15
    with pytest.raises(InvalidTime, match="every 15 minutes"):
        book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 08:29")
    assert book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 08:30").status == "booked"


# ==================== Availability (free slots) ====================

def slots(db, doctor_id: str, appointment_type: str, day: str) -> list[str]:
    availability = get_doctor_availability(db, doctor_id=doctor_id, appointment_type=appointment_type,
                                           day=date.fromisoformat(day), now=NOW)
    return [start.strftime("%H:%M") for start in availability.slots]


def test_free_slots_cover_the_shift_in_grid_steps(db):
    times = slots(db, "dr-raj-patel", "new_patient", "2026-11-02")  # Monday 08:00-15:00, 45 minutes
    assert times[:3] == ["08:00", "08:15", "08:30"]
    assert times[-1] == "14:15"  # 14:15 + 45 min = 15:00, the end of the shift; 14:30 wouldn't fit


def test_booked_appointments_remove_every_overlapping_slot(db):
    # A-0002: Dr. Okafor 14:00-14:30. A 30-minute appointment at 13:45, 14:00, or 14:15 would overlap it.
    times = slots(db, "dr-chidi-okafor", "new_patient", "2026-10-27")
    assert "13:30" in times and "14:30" in times
    assert not {"13:45", "14:00", "14:15"} & set(times)


def test_cancelled_appointments_free_their_slot(db):
    assert "15:00" in slots(db, "dr-samuel-brooks", "follow_up", "2026-10-22")  # A-0007 is cancelled


def test_no_slots_on_days_off_closed_days_or_outside_the_window(db):
    assert slots(db, "dr-raj-patel", "follow_up", "2026-10-27") == []    # Patel doesn't work Tuesdays
    assert slots(db, "dr-raj-patel", "follow_up", "2026-10-25") == []    # Sunday: the hospital is closed
    assert slots(db, "dr-raj-patel", "follow_up", "2026-10-19") == []    # in the past
    assert slots(db, "dr-raj-patel", "follow_up", "2027-02-01") == []    # beyond the 90-day window


def test_today_only_offers_times_after_now(db):
    times = slots(db, "dr-chidi-okafor", "follow_up", "2026-10-20")  # NOW is 12:00 today
    assert times[0] == "12:15" and all(t > "12:00" for t in times)


def test_availability_reports_unknown_doctors_and_types(db):
    with pytest.raises(DoctorNotFound):
        slots(db, "dr-nobody", "follow_up", "2026-10-30")
    with pytest.raises(InvalidAppointmentType):
        slots(db, "dr-raj-patel", "mri", "2026-10-30")


def test_every_offered_slot_can_actually_be_booked(db):
    # Availability and booking must agree: if a slot is offered, booking it must succeed.
    availability = get_doctor_availability(db, doctor_id="dr-raj-patel", appointment_type="follow_up",
                                           day=date(2026, 10, 30), now=NOW)
    me = actor(db, "P-1019")
    for start in [availability.slots[0], availability.slots[len(availability.slots) // 2], availability.slots[-1]]:
        booked = create_appointment(db, me, patient_id="P-1019", doctor_id="dr-raj-patel",
                                    appointment_type="follow_up", starts_at=start, now=NOW)
        assert booked.status == "booked"


def test_a_new_booking_disappears_from_the_free_slots(db):
    assert "11:00" in slots(db, "dr-raj-patel", "follow_up", "2026-10-30")
    book(db, "P-1019", "dr-raj-patel", "follow_up", "2026-10-30 11:00")
    times = slots(db, "dr-raj-patel", "follow_up", "2026-10-30")
    assert "11:00" not in times and "10:45" not in times and "11:15" not in times  # 20 min overlaps both sides


def test_next_available_finds_the_earliest_slots(db):
    found = find_next_available(db, doctor_id="dr-raj-patel", appointment_type="follow_up", now=NOW, limit=3)
    assert [f"{t:%a %H:%M}" for t in found] == ["Wed 08:00", "Wed 08:15", "Wed 08:30"]  # Wed Oct 21


def test_availability_never_reveals_who_is_booked(db):
    availability = get_doctor_availability(db, doctor_id="dr-chidi-okafor", appointment_type="follow_up",
                                           day=date(2026, 10, 27), now=NOW)
    assert set(vars(availability)) == {"doctor_id", "doctor_name", "appointment_type", "duration_minutes",
                                       "day", "slots"}


# ==================== Cancelling ====================

def test_cancelling_with_enough_notice_is_free(db):
    result = cancel_appointment(db, actor(db, "P-1006"), "A-0005", NOW)  # 10 days away
    assert result.fee_usd == 0
    assert result.appointment.status == "cancelled" and result.appointment.cancelled_at == NOW


def test_cancelling_late_costs_the_late_fee(db):
    result = cancel_appointment(db, actor(db, "P-1015"), "A-0015", NOW)  # 23 hours away
    assert result.fee_usd == 25


def test_an_appointment_can_only_be_cancelled_once(db):
    cancel_appointment(db, actor(db, "P-1006"), "A-0005", NOW)
    with pytest.raises(InvalidTransition):
        cancel_appointment(db, actor(db, "P-1006"), "A-0005", NOW)


def test_completed_appointments_cannot_be_cancelled(db):
    with pytest.raises(InvalidTransition, match="completed"):
        cancel_appointment(db, actor(db, "P-1008"), "A-0008", NOW)


def test_the_database_requires_a_cancellation_time_for_cancelled_appointments(db):
    assert rejected_by(db, raw_appointment(status="cancelled")) == "cancelled_at_only_when_cancelled"


# ==================== Synthetic data only ====================

@pytest.mark.parametrize("changes, constraint", [
    ({"phone": "(503) 867-5309"}, "synthetic_phone_only"),
    ({"email": "jane.doe@gmail.com"}, "synthetic_email_only"),
])
def test_the_database_rejects_real_looking_contact_details(db, changes, constraint):
    values = {"id": "P-9001", "first_name": "Test", "last_name": "Person", "date_of_birth": date(1990, 1, 1),
              "phone": "(503) 555-0199", "email": "test@example.com", "preferred_language": "English"}
    values.update(changes)
    assert rejected_by(db, Patient(**values)) == constraint


def test_all_seeded_patients_are_synthetic(db):
    patients = db.scalars(select(Patient)).all()
    assert len(patients) == 20
    assert all("555-01" in p.phone and p.email.endswith("@example.com") for p in patients)
