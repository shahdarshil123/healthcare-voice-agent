"""Doctor lookup: check doctor names in a message against the hospital's COMPLETE list of doctors.

Document search can only find what is similar, so it can't prove that a doctor doesn't exist.
The list in clinic.json is complete, so if a name isn't on it, the hospital has no such doctor.
"""

import difflib
import re

from app.clinic import CLINIC

# How similar a heard name must be to a real surname to count as a match (0 = anything, 1 = identical).
# 0.6 catches common mishearings ("Akafur" -> Okafor, "Lindkvis" -> Lindqvist) without matching
# unrelated names ("Kim", "Smith").
MATCH_CUTOFF = 0.6

# "Dr." or "Doctor" (any capitalisation), followed by one or two capitalised words: "Dr. Kim", "Doctor Raj Patel".
DOCTOR_MENTION = re.compile(r"\b(?i:dr|doctor)\.?\s+([A-Z][\w'-]+)(?:\s+([A-Z][\w'-]+))?")

DOCTORS_BY_SURNAME = {doctor["name"].split()[-1].lower(): doctor for doctor in CLINIC["doctors"]}
DEPARTMENT_NAMES = {department["id"]: department["name"] for department in CLINIC["departments"]}


def find_doctor(heard_name: str) -> tuple[dict | None, bool]:
    """Return (the doctor's record or None, whether the name matched exactly)."""
    surname = heard_name.split()[-1].lower()
    if surname in DOCTORS_BY_SURNAME:
        return DOCTORS_BY_SURNAME[surname], True
    close = difflib.get_close_matches(surname, DOCTORS_BY_SURNAME, n=1, cutoff=MATCH_CUTOFF)
    return (DOCTORS_BY_SURNAME[close[0]], False) if close else (None, False)


def describe(doctor: dict) -> str:
    """One line of verified facts about a doctor, e.g. for the model to quote."""
    days = ", ".join(
        f"{shift['day'].capitalize()} {shift['start']}-{shift['end']}" for shift in doctor["weekly_schedule"]
    )
    new_patients = "accepting new patients" if doctor["accepting_new_patients"] else "NOT accepting new patients"
    return f"{doctor['name']}, {DEPARTMENT_NAMES[doctor['department_id']]}. Works {days}. {new_patients.capitalize()}."


def look_up_doctors(message: str) -> list[str]:
    """Check every doctor mentioned in the message. Returns one line of facts per mention."""
    facts = []
    for first_word, second_word in DOCTOR_MENTION.findall(message):
        heard = f"{first_word} {second_word}".strip()
        doctor, exact = find_doctor(heard)
        if doctor is None:
            facts.append(f'"Dr. {heard}": there is no doctor with this name at {CLINIC["hospital"]["name"]}.')
        elif exact:
            facts.append(f'"Dr. {heard}": {describe(doctor)}')
        else:
            facts.append(f'"Dr. {heard}": probably {describe(doctor)} (the name may have been misheard).')
    return facts
