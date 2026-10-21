"""Load the synthetic data in data/synthetic/ into PostgreSQL.

Safe to run again: existing rows are updated rather than duplicated ("upsert"), and everything happens in
ONE transaction, so if any row is rejected (for example by the synthetic-data guardrail), nothing changes.

Usage (PostgreSQL running, migrations applied):
    docker compose up -d
    uv run alembic upgrade head
    uv run python seed.py
"""

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clinic import CLINIC, WEEKDAYS
from app.database import make_engine, make_session_factory
from app.models import Appointment, AppointmentType, Department, Doctor, DoctorShift, Patient

SYNTHETIC = Path(__file__).parent / "data" / "synthetic"
HOSPITAL_TIME_ZONE = ZoneInfo(CLINIC["hospital"]["time_zone"])  # times in the data are local hospital times


def upsert(session: Session, model, rows: list[dict], key: list[str]) -> None:
    """Insert rows, or update them if a row with the same key already exists."""
    if not rows:
        return
    statement = insert(model).values(rows)
    changes = {column: statement.excluded[column] for column in rows[0] if column not in key}
    session.execute(statement.on_conflict_do_update(index_elements=key, set_=changes))


def local_time(text: str) -> datetime:
    """'2026-11-02T10:00' (hospital local time) -> a time-zone-aware datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=HOSPITAL_TIME_ZONE)


def seed_clinic(session: Session) -> None:
    upsert(session, Department, [
        {"id": d["id"], "name": d["name"], "location": d["location"], "phone": d["phone"],
         "telehealth_available": d["telehealth_available"], "referral_required": d.get("referral_required", False)}
        for d in CLINIC["departments"]
    ], key=["id"])
    upsert(session, AppointmentType, [
        {"department_id": d["id"], "type": t["type"], "duration_minutes": t["duration_minutes"]}
        for d in CLINIC["departments"] for t in d["appointment_types"]
    ], key=["department_id", "type"])
    upsert(session, Doctor, [
        {"id": d["id"], "name": d["name"], "department_id": d["department_id"], "languages": d["languages"],
         "accepting_new_patients": d["accepting_new_patients"]}
        for d in CLINIC["doctors"]
    ], key=["id"])
    # Schedules are replaced as a whole, so a removed shift really disappears.
    session.execute(delete(DoctorShift))
    session.add_all(
        DoctorShift(doctor_id=d["id"], weekday=WEEKDAYS.index(s["day"]),
                    starts=time.fromisoformat(s["start"]), ends=time.fromisoformat(s["end"]))
        for d in CLINIC["doctors"] for s in d["weekly_schedule"]
    )


def seed_patients(session: Session) -> None:
    patients = json.loads((SYNTHETIC / "patients.json").read_text(encoding="utf-8"))["patients"]
    # Two passes: a guardian must exist before a child can point at them.
    upsert(session, Patient, [
        {"id": p["patient_id"], "first_name": p["first_name"], "last_name": p["last_name"],
         "date_of_birth": date.fromisoformat(p["date_of_birth"]), "phone": p["phone"], "email": p["email"],
         "preferred_language": p["preferred_language"],
         "insurance_plan": (p["insurance"] or {}).get("plan"),
         "insurance_member_id": (p["insurance"] or {}).get("member_id"),
         "guardian_id": None}
        for p in patients
    ], key=["id"])
    for p in patients:
        session.execute(update(Patient).where(Patient.id == p["patient_id"])
                        .values(guardian_id=p["guardian_patient_id"]))


def seed_appointments(session: Session) -> None:
    data = json.loads((SYNTHETIC / "appointments.json").read_text(encoding="utf-8"))
    # The seed data has no cancellation time, so cancelled appointments are treated as cancelled on the reference date.
    reference = local_time(f"{data['reference_date']}T12:00")
    rows = []
    for a in data["appointments"]:
        starts_at = local_time(a["start"])
        rows.append({
            "id": a["appointment_id"], "patient_id": a["patient_id"], "doctor_id": a["doctor_id"],
            "department_id": a["department_id"], "appointment_type": a["appointment_type"],
            "duration_minutes": a["duration_minutes"], "starts_at": starts_at,
            "ends_at": starts_at + func.make_interval(0, 0, 0, 0, 0, a["duration_minutes"]),
            "mode": a["mode"], "status": a["status"], "booked_by_patient_id": a["booked_by_patient_id"],
            "cancelled_at": reference if a["status"] == "cancelled" else None,
        })
    upsert(session, Appointment, rows, key=["id"])


def seed_database(session: Session) -> dict:
    """Load everything. The caller decides whether to commit."""
    seed_clinic(session)
    seed_patients(session)
    seed_appointments(session)
    # SQLAlchemy builds func.count at runtime, which Pylint can't see.
    # pylint: disable-next=not-callable
    counts = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
              for model in (Department, AppointmentType, Doctor, DoctorShift, Patient, Appointment)}
    return counts


if __name__ == "__main__":
    session_factory = make_session_factory(make_engine())
    with session_factory.begin() as db:  # one transaction: commits at the end, or rolls back everything on an error
        totals = seed_database(db)
    for table, count in totals.items():
        print(f"  {table:18} {count:>3} rows")
