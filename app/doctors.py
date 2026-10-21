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

# How similar a heard name's SPELLING must be to a real name to be offered as a possible match (0 to 1).
# Measured with evaluate_entities.py: 0.65 keeps "Akafur" (0.67), "Lindkvis" (0.82), "Raman" (0.91), but
# rejects common surnames that only reach 0.60 ("Jones" vs James, "Davis" vs Aisha, "Ahmed" vs James).
MATCH_CUTOFF = 0.65
# Only the best spelling match (and names within this margin of it) are offered, not everything above
# the cutoff: "Patell" should suggest Patel (0.91), not also Bell (0.60).
TIE_MARGIN = 0.05

# "Dr." or "Doctor" (any capitalisation), followed by one or two words: "Dr. Kim", "Doctor Raj Patel".
# Whether a word really is a name is decided in find_mentions().
DOCTOR_MENTION = re.compile(r"\b(?i:dr|doctor)\.?\s+([A-Za-z][\w'-]*)(?:\s+([A-Za-z][\w'-]*))?")

# Capitalised words that follow "doctor" in normal sentences but are not names ("a doctor Monday morning").
NOT_NAMES = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "today", "tomorrow", "who", "what", "when", "where", "which", "please", "the", "i",
}

# Surnames that are also everyday English words. Without "Dr." or the full name they are too risky to match
# ("ring the bell", "Brooks Road"), so for these a bare surname is ignored.
EVERYDAY_WORD_SURNAMES = {"bell", "hart", "brooks"}

# clinic.json records no gender, so the model must not guess one. A note next to the facts works better
# than a rule in the system prompt (measured: "she" in 5/5 replies with the rule alone).
GENDER_NOTE = 'Gender is not recorded: use the doctor\'s full name, never "he" or "she".'

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


def sound_key(name: str) -> str:
    """A rough "how it sounds" key, so mishearings that are spelled very differently can still match.

    Vowels and silent letters are dropped, similar sounds merged, and doubled letters collapsed:
    "Akhofer" and "Okafor" both become "kfr", "Raman" and "Rahman" both become "rmn".
    """
    name = name.lower()
    for written, sound in (("ph", "f"), ("ck", "k"), ("q", "k"), ("c", "k"), ("z", "s"), ("x", "ks")):
        name = name.replace(written, sound)
    consonants = [letter for letter in name if letter.isalpha() and letter not in "aeiouyhw"]
    return "".join(letter for i, letter in enumerate(consonants) if i == 0 or letter != consonants[i - 1])


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

    # 3. Similar SPELLING of the last word heard, against every surname and first name: the best match only.
    single_names = [key for key in index if " " not in key and not key.startswith("dr-")]
    spelling = {name: difflib.SequenceMatcher(None, words[-1], name).ratio() for name in single_names}
    best = max(spelling.values(), default=0)
    close = [name for name, score in spelling.items() if score >= MATCH_CUTOFF and score >= best - TIE_MARGIN]

    # 4. Similar SOUND, against surnames only (first names are too short and common for this to be safe).
    heard_sound = sound_key(words[-1])
    if len(heard_sound) >= 3:
        close += [name_parts(doctor)[1] for doctor in doctors if sound_key(name_parts(doctor)[1]) == heard_sound]

    fuzzy = {doctor["id"]: doctor for name in close for doctor in index[name]}
    if fuzzy:
        return DoctorMatch(heard, "possible", list(fuzzy.values()))

    return DoctorMatch(heard, "not_found", [])


def find_mentions(message: str, doctors: list[dict] = CLINIC["doctors"]) -> list[str]:
    """Doctor names in a message: names after "Dr."/"Doctor", plus known doctors named without a title.

    With "Dr."/"Doctor": a capitalised word is taken as a name, so unknown ones can be reported ("there is
    no Dr. Kim"); a lowercase word ("doctor patel") only if it is a known doctor's name.
    Without a title: only EXACT known names, because nothing else says the word is a name: a full name
    ("raj patel"), or a surname ("Is Patel in...") unless it is also an everyday word (Bell, Hart, Brooks).
    """
    known_names = {name for doctor in doctors for name in name_parts(doctor)}

    def is_name(word: str) -> bool:
        return word.lower() not in NOT_NAMES and (word[0].isupper() or word.lower() in known_names)

    mentions = []
    for first_word, second_word in DOCTOR_MENTION.findall(message):
        # "Rivera's office" -> "Rivera"
        first_word, second_word = first_word.removesuffix("'s"), second_word.removesuffix("'s")
        if is_name(first_word):  # the word right after "Dr." must itself be a name
            mentions.append(f"{first_word} {second_word}" if second_word and is_name(second_word) else first_word)

    already_found = {word.lower() for mention in mentions for word in mention.split()}
    for doctor in doctors:
        first, last = name_parts(doctor)
        if last in already_found:
            continue
        full_name = re.search(rf"\b{first}\s+{last}\b", message, re.IGNORECASE)
        bare_surname = re.search(rf"\b{last}\b", message, re.IGNORECASE)
        if full_name:
            mentions.append(full_name.group(0))
        elif bare_surname and last not in EVERYDAY_WORD_SURNAMES:
            mentions.append(bare_surname.group(0))
    return mentions


def describe(doctor: dict) -> str:
    """One line of verified facts about a doctor, for the model to quote."""
    days = ", ".join(
        f"{shift['day'].capitalize()} {shift['start']}-{shift['end']}" for shift in doctor["weekly_schedule"]
    )
    new_patients = "Accepting new patients" if doctor["accepting_new_patients"] else "NOT accepting new patients"
    return f"{doctor['name']}, {DEPARTMENT_NAMES[doctor['department_id']]}. Works {days}. {new_patients}."


def short_name(doctor: dict) -> str:
    return f"{doctor['name']} ({DEPARTMENT_NAMES[doctor['department_id']]})"


def look_up_doctors(message: str) -> list[str]:
    """Check every doctor mentioned in the message. Returns one line of facts per mention."""
    facts = []
    for heard in find_mentions(message):
        match = resolve_name(heard)
        label = f'"Dr. {heard}"'
        if match.status == "resolved":
            facts.append(f"{label}: {describe(match.candidates[0])} {GENDER_NOTE}")
        elif match.status == "ambiguous":
            options = "; ".join(short_name(doctor) for doctor in match.candidates)
            facts.append(f"{label}: more than one doctor matches: {options}. "
                         f"Ask the user which one they mean. {GENDER_NOTE}")
        elif match.status == "possible":
            options = " or ".join(short_name(doctor) for doctor in match.candidates)
            facts.append(f"{label}: no exact match. Possibly {options}. Before saying anything else about "
                         f"this doctor, ask the user to confirm that is who they mean. {GENDER_NOTE}")
        else:
            facts.append(f"{label}: there is no doctor with this name at {CLINIC['hospital']['name']}.")
    return facts
