"""Doctor lookup: check doctor names in a message against the hospital's COMPLETE list of doctors.

Document search can only find what is similar, so it can't prove that a doctor doesn't exist.
The list in clinic.json is complete, so if a name isn't on it, the hospital has no such doctor.

Matching is deliberately conservative:
- every doctor is indexed by unique ID, full name, surname, and first name, each pointing to a LIST of
  doctors, so two doctors who share a name are both kept;
- a name that matches more than one doctor is "ambiguous" (ask which one);
- a fuzzy or partial match is only "possible" (ask the user to confirm), never stated as fact.
"""

import difflib
import re
from dataclasses import dataclass

from app.clinic import CLINIC

# How similar a heard name must be to a real name to be offered as a possible match (0 to 1).
# 0.6 catches common mishearings ("Akafur" -> Okafor, "Lindkvis" -> Lindqvist) without matching
# unrelated names ("Kim", "Smith"). Fuzzy matches are only ever suggestions for the user to confirm.
MATCH_CUTOFF = 0.6

# "Dr." or "Doctor" (any capitalisation), followed by one or two capitalised words: "Dr. Kim", "Doctor Raj Patel".
DOCTOR_MENTION = re.compile(r"\b(?i:dr|doctor)\.?\s+([A-Z][\w'-]+)(?:\s+([A-Z][\w'-]+))?")

# Capitalised words that follow "doctor" in normal sentences but are not names ("a doctor Monday morning").
NOT_NAMES = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "today", "tomorrow", "who", "what", "when", "where", "which", "please", "the", "i",
}

DEPARTMENT_NAMES = {department["id"]: department["name"] for department in CLINIC["departments"]}


@dataclass
class DoctorMatch:
    """The result of looking up one doctor mentioned in a message."""

    heard: str              # the name as it appeared, e.g. "Maya" or "Raj Patel"
    status: str             # "resolved", "ambiguous", "possible", or "not_found"
    candidates: list[dict]  # matching doctor records from clinic.json (empty if not found)


def name_parts(doctor: dict) -> tuple[str, str]:
    """("maya", "chen") from "Dr. Maya Chen"."""
    words = doctor["name"].removeprefix("Dr.").split()
    return words[0].lower(), words[-1].lower()


def build_index(doctors: list[dict]) -> dict[str, list[dict]]:
    """Map every way of naming a doctor (ID, "maya chen", "chen", "maya") to the doctors it could mean."""
    index: dict[str, list[dict]] = {}
    for doctor in doctors:
        first, last = name_parts(doctor)
        # dict.fromkeys removes repeats (e.g. a first name equal to the surname) while keeping the order.
        for key in dict.fromkeys([doctor["id"], f"{first} {last}", last, first]):
            index.setdefault(key, []).append(doctor)
    return index


def get_doctor(doctor_id: str, doctors: list[dict] = CLINIC["doctors"]) -> dict | None:
    """Exact lookup by unique ID, e.g. "dr-raj-patel"."""
    return next((doctor for doctor in doctors if doctor["id"] == doctor_id), None)


def resolve_name(heard: str, doctors: list[dict] = CLINIC["doctors"]) -> DoctorMatch:
    """Work out which doctor(s) a heard name could mean."""
    index = build_index(doctors)
    words = heard.lower().split()

    # 1. Exact matches: the full name, or a single surname/first name.
    exact = index.get(" ".join(words), [])
    if exact:
        return DoctorMatch(heard, "resolved" if len(exact) == 1 else "ambiguous", exact)

    # 2. Partial match: two words where only the surname or only the first name fits ("Raj Smith").
    if len(words) == 2:
        partial = [doctor for doctor in doctors
                   if name_parts(doctor)[1] == words[1] or name_parts(doctor)[0] == words[0]]
        if partial:
            return DoctorMatch(heard, "possible", partial)

    # 3. Fuzzy match on the last word heard, against every surname and first name.
    single_names = [key for key in index if " " not in key and not key.startswith("dr-")]
    close_names = difflib.get_close_matches(words[-1], single_names, n=3, cutoff=MATCH_CUTOFF)
    fuzzy = [doctor for name in close_names for doctor in index[name]]
    if fuzzy:
        return DoctorMatch(heard, "possible", list({doctor["id"]: doctor for doctor in fuzzy}.values()))

    return DoctorMatch(heard, "not_found", [])


def find_mentions(message: str) -> list[str]:
    """The names that follow "Dr." or "Doctor" in a message, skipping words like "Monday" or "Who"."""
    mentions = []
    for first_word, second_word in DOCTOR_MENTION.findall(message):
        words = [word for word in (first_word, second_word) if word and word.lower() not in NOT_NAMES]
        if words and words[0] == first_word:  # the word right after "Dr." must itself be a name
            mentions.append(" ".join(words))
    return mentions


def describe(doctor: dict) -> str:
    """One line of verified facts about a doctor, for the model to quote."""
    days = ", ".join(
        f"{shift['day'].capitalize()} {shift['start']}-{shift['end']}" for shift in doctor["weekly_schedule"]
    )
    new_patients = "Accepting new patients" if doctor["accepting_new_patients"] else "NOT accepting new patients"
    # clinic.json records no gender, so the model must not guess one. A note next to the facts works better
    # than a rule in the system prompt (measured: "she" in 5/5 replies before, with the rule alone).
    return (f"{doctor['name']}, {DEPARTMENT_NAMES[doctor['department_id']]}. Works {days}. {new_patients}. "
            f"Gender is not recorded: call them {doctor['name']}, not \"he\" or \"she\".")


def short_name(doctor: dict) -> str:
    return f"{doctor['name']} ({DEPARTMENT_NAMES[doctor['department_id']]})"


def look_up_doctors(message: str) -> list[str]:
    """Check every doctor mentioned in the message. Returns one line of facts per mention."""
    facts = []
    for heard in find_mentions(message):
        match = resolve_name(heard)
        label = f'"Dr. {heard}"'
        if match.status == "resolved":
            facts.append(f"{label}: {describe(match.candidates[0])}")
        elif match.status == "ambiguous":
            options = "; ".join(short_name(doctor) for doctor in match.candidates)
            facts.append(f"{label}: more than one doctor matches: {options}. Ask the user which one they mean.")
        elif match.status == "possible":
            options = " or ".join(short_name(doctor) for doctor in match.candidates)
            facts.append(f"{label}: no exact match. Possibly {options}. "
                         "Ask the user to confirm which doctor they mean before giving details.")
        else:
            facts.append(f"{label}: there is no doctor with this name at {CLINIC['hospital']['name']}.")
    return facts
