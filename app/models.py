"""The appointment system's database tables, as SQLAlchemy models.

The most important rules are enforced by PostgreSQL itself (constraints), not only by Python code,
so that no bug, script, or simultaneous request can break them:
- no doctor and no patient can have two booked appointments that overlap in time;
- an appointment's length must be the length of its appointment type;
- an appointment's doctor must exist and belong to the appointment's department;
- only fictional contact details can be stored (a guardrail against real patient data).

Changing these models requires a new Alembic migration (see alembic/versions/).
"""

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Sequence,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

APPOINTMENT_STATUSES = ("booked", "cancelled", "completed", "no_show")
APPOINTMENT_MODES = ("in_person", "telehealth")


class Base(DeclarativeBase):
    pass


# New appointment numbers: A-1001, A-1002, ... (the seed data uses A-0001 to A-0015).
appointment_number = Sequence("appointment_number_seq", start=1001, metadata=Base.metadata)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    location: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20))
    telehealth_available: Mapped[bool] = mapped_column(Boolean)
    referral_required: Mapped[bool] = mapped_column(Boolean)


class AppointmentType(Base):
    __tablename__ = "appointment_types"

    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"), primary_key=True)
    type: Mapped[str] = mapped_column(String(50), primary_key=True)
    duration_minutes: Mapped[int] = mapped_column(SmallInteger)

    __table_args__ = (
        CheckConstraint("duration_minutes BETWEEN 5 AND 240", name="duration_is_realistic"),
        # Lets an appointment point at (department, type, duration) together, so its duration must match its type.
        UniqueConstraint("department_id", "type", "duration_minutes", name="uq_type_with_duration"),
    )


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(50)))
    accepting_new_patients: Mapped[bool] = mapped_column(Boolean)

    # Lets an appointment point at (doctor, department) together, so its doctor must work in its department.
    __table_args__ = (UniqueConstraint("id", "department_id", name="uq_doctor_in_department"),)


class DoctorShift(Base):
    """One weekly working period, e.g. Dr. Raj Patel, Monday 08:00-15:00."""

    __tablename__ = "doctor_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(SmallInteger)  # 0 = Monday ... 6 = Sunday, like Python's date.weekday()
    starts: Mapped[time] = mapped_column(Time)
    ends: Mapped[time] = mapped_column(Time)

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="valid_weekday"),
        CheckConstraint("ends > starts", name="shift_ends_after_it_starts"),
        UniqueConstraint("doctor_id", "weekday", "starts", name="uq_shift"),
    )


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    date_of_birth: Mapped[date] = mapped_column(Date)
    phone: Mapped[str] = mapped_column(String(20))
    email: Mapped[str] = mapped_column(String(200))
    preferred_language: Mapped[str] = mapped_column(String(50))
    insurance_plan: Mapped[str | None] = mapped_column(String(100))
    insurance_member_id: Mapped[str | None] = mapped_column(String(50))
    guardian_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"))

    __table_args__ = (
        CheckConstraint(r"id ~ '^P-[0-9]{4}$'", name="patient_id_format"),
        # SYNTHETIC-DATA GUARDRAIL: only fictional contact details can be stored. US 555-0100 to 555-0199
        # numbers are reserved for fiction, and example.com is reserved for examples. Real patient data
        # (which would have real phone numbers and emails) is rejected by the database itself.
        CheckConstraint(r"phone ~ '^\(\d{3}\) 555-01\d{2}$'", name="synthetic_phone_only"),
        CheckConstraint("email LIKE '%@example.com'", name="synthetic_email_only"),
        CheckConstraint("guardian_id <> id", name="not_own_guardian"),
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    doctor_id: Mapped[str] = mapped_column(String(50))
    department_id: Mapped[str] = mapped_column(String(50))
    appointment_type: Mapped[str] = mapped_column(String(50))
    duration_minutes: Mapped[int] = mapped_column(SmallInteger)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The time the appointment occupies, as a range. '[)' means it includes its start but not its end,
    # so a 09:00-09:30 appointment and a 09:30-10:00 one don't overlap. Computed by PostgreSQL.
    during: Mapped[Range[datetime]] = mapped_column(
        TSTZRANGE, Computed("tstzrange(starts_at, ends_at, '[)')", persisted=True)
    )
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default="booked")
    booked_by_patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    # A key the caller sends with a booking request. The same key twice means the same request was retried,
    # so the database refuses a second booking (unique), and the service returns the first one instead.
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The doctor must exist AND work in this department.
        ForeignKeyConstraint(["doctor_id", "department_id"], ["doctors.id", "doctors.department_id"],
                             name="fk_doctor_in_department"),
        # The appointment type must exist in this department AND the duration must be that type's duration.
        ForeignKeyConstraint(["department_id", "appointment_type", "duration_minutes"],
                             ["appointment_types.department_id", "appointment_types.type",
                              "appointment_types.duration_minutes"],
                             name="fk_type_and_duration"),
        CheckConstraint("ends_at = starts_at + duration_minutes * interval '1 minute'", name="end_matches_duration"),
        CheckConstraint(f"status IN {APPOINTMENT_STATUSES}", name="valid_status"),
        CheckConstraint(f"mode IN {APPOINTMENT_MODES}", name="valid_mode"),
        CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="cancelled_at_only_when_cancelled"),
        # NO DOUBLE BOOKING: two BOOKED appointments for the same doctor may not overlap in time.
        # Enforced by PostgreSQL even when two requests arrive at the same moment. Cancelled ones don't count.
        ExcludeConstraint(("doctor_id", "="), ("during", "&&"), using="gist",
                          where=text("status = 'booked'"), name="no_double_booking_doctor"),
        # A patient can't be in two appointments at once either.
        ExcludeConstraint(("patient_id", "="), ("during", "&&"), using="gist",
                          where=text("status = 'booked'"), name="no_overlapping_patient_appointments"),
        Index("ix_appointments_patient_status", "patient_id", "status"),
        Index("ix_appointments_doctor_start", "doctor_id", "starts_at"),
    )
