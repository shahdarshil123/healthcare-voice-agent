"""Department lookup: recognise departments (and services asked about like departments) in a message,
by name, alias, or misspelling, using clinic.json as the source of truth.

Only departments and medical specialties are treated as a closed world: if someone asks for a specialty
the hospital doesn't have (for example neurology), we can say so, because the department list is complete.
Anything else (a pharmacy, a pet policy) is never claimed to be missing; that is left to the documents.
"""

import difflib
import re
from dataclasses import dataclass

from app.clinic import CLINIC

# Misspellings: how similar a word must be to a department name or alias (0 to 1).
# Department words are long and distinctive, so this can be stricter than for doctor names
# ("dermatalogy" vs "dermatology" scores 0.91).
TYPO_CUTOFF = 0.8
# Shorter words ("skin", "kids") must match exactly; fuzzy-matching short words gives false matches.
TYPO_MIN_LENGTH = 6
# When several entities are almost equally close to a misspelling, it's ambiguous.
TYPO_TIE_MARGIN = 0.05

HOSPITAL_NAME = CLINIC["hospital"]["name"]

SERVICES = [
    {
        "id": "laboratory", "name": "Laboratory",
        "aliases": ["lab", "laboratory", "blood test", "blood tests", "blood work"],
        "facts": f"Walk-in blood tests with a doctor's order. {CLINIC['laboratory']['location']}.",
    },
    {
        "id": "emergency", "name": "Emergency Department",
        "aliases": ["emergency", "emergency room", "emergency department", "a&e"],
        "facts": CLINIC["hospital"]["emergency_department"],
    },
    {
        "id": "billing", "name": "Billing office",
        "aliases": ["billing", "billing office", "bill", "bills", "payment", "payments"],
        "facts": f"Bills, payments, and insurance questions. Phone {CLINIC['hospital']['billing_phone']}.",
    },
]

# Abbreviations that are only safe to match in CAPITALS: "Where is the ER?" means the emergency room,
# but "Er, can I book..." is just a pause.
CASE_SENSITIVE_ALIASES = {"emergency": ["ER"]}

# Medical specialties Brightwater does NOT have. This is general medical vocabulary, not hospital data.
# Only EXACT phrases are used: we never claim something is missing based on a guess. Measured with
# evaluate_entities.py: matching misspellings here turned "cancel my appointment" into "no Oncology department".
SPECIALTIES_NOT_OFFERED = {
    "Neurology": ["neurology", "neurologist"],
    "Neurosurgery": ["neurosurgery", "neurosurgeon", "brain surgery", "brain surgeon"],
    "Ophthalmology (eye care)": ["ophthalmology", "ophthalmologist", "eye doctor", "eye department", "eye clinic",
                                 "optometrist"],
    "Ear, nose, and throat (ENT)": ["ent", "otolaryngology", "ear nose and throat", "ear, nose and throat"],
    "Oncology": ["oncology", "oncologist", "cancer"],
    "Psychiatry": ["psychiatry", "psychiatrist", "mental health"],
    "Obstetrics and gynecology": ["obstetrics", "gynecology", "gynaecology", "gynecologist", "obgyn", "ob-gyn",
                                  "maternity"],
    "Gastroenterology": ["gastroenterology", "gastroenterologist"],
    "Urology": ["urology", "urologist"],
    "Dentistry": ["dentist", "dentistry", "dental"],
    "Physical therapy": ["physical therapy", "physiotherapy", "physiotherapist", "physio"],
}


@dataclass
class Entity:
    """A department or service the hospital has."""

    id: str
    name: str
    facts: str


@dataclass
class DepartmentMatch:
    """One department, service, or specialty mentioned in a message."""

    heard: str              # the words in the message, e.g. "heart" or "dermatalogy"
    status: str             # "resolved", "probable" (a misspelling), "ambiguous", or "not_offered"
    entities: list[Entity]  # what it could mean (for "not_offered": the specialty, with no hospital facts)


def build_entities() -> list[tuple[Entity, list[str]]]:
    """Every department and service, with all the phrases that refer to it."""
    entities = []
    for department in CLINIC["departments"]:
        facts = f"{department['description']} {department['location']}. Phone {department['phone']}."
        phrases = [department["name"].lower(), department["id"].replace("-", " "), *department.get("aliases", [])]
        entities.append((Entity(department["id"], department["name"], facts), phrases))
    for service in SERVICES:
        entities.append((Entity(service["id"], service["name"], service["facts"]), service["aliases"]))
    return entities


ENTITIES = build_entities()
# Specialties we don't have, as entities with no ID (their name identifies them) and no hospital facts.
NOT_OFFERED = [(Entity("", specialty, ""), phrases) for specialty, phrases in SPECIALTIES_NOT_OFFERED.items()]
DEPARTMENT_LIST = ", ".join(department["name"] for department in CLINIC["departments"])


def key(entity: Entity) -> str:
    """What identifies an entity: its ID, or for a specialty we don't have, its name."""
    return entity.id or entity.name


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word match: "ent" matches "an ENT clinic" but not "appointment"."""
    return re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", text) is not None


def resolve_departments(message: str) -> list[DepartmentMatch]:
    """Find every department, service, or specialty mentioned in a message."""
    text = message.lower()
    matches: list[DepartmentMatch] = []
    found: set[str] = set()

    # 1. Exact names and aliases (and specialties we don't have). The longest phrase found is reported.
    for status, entities in (("resolved", ENTITIES), ("not_offered", NOT_OFFERED)):
        for entity, phrases in entities:
            heard = [phrase for phrase in phrases if contains_phrase(text, phrase)]
            heard += [alias for alias in CASE_SENSITIVE_ALIASES.get(entity.id, []) if contains_phrase(message, alias)]
            if heard:
                matches.append(DepartmentMatch(max(heard, key=len), status, [entity]))
                found.add(key(entity))

    # 2. Misspellings of departments and services we HAVE, for long words not already matched.
    #    (Words of specialties we don't have count as known words, so they are never "corrected" into ours.)
    known_words = {word for _, phrases in ENTITIES + NOT_OFFERED for phrase in phrases for word in phrase.split()}
    vocabulary = [(phrase, entity) for entity, phrases in ENTITIES for phrase in phrases
                  if " " not in phrase and len(phrase) >= TYPO_MIN_LENGTH]
    for word in set(re.findall(r"[a-z']+", text)):
        if len(word) < TYPO_MIN_LENGTH or word in known_words:
            continue
        scored = [(difflib.SequenceMatcher(None, word, phrase).ratio(), entity) for phrase, entity in vocabulary]
        best = max((score for score, _ in scored), default=0)
        if best < TYPO_CUTOFF:
            continue
        close = {key(entity): entity for score, entity in scored if score >= best - TYPO_TIE_MARGIN}
        if len(close) > 1:
            matches.append(DepartmentMatch(word, "ambiguous", list(close.values())))
        elif not close.keys() & found:
            entity = next(iter(close.values()))
            matches.append(DepartmentMatch(word, "probable", [entity]))
            found.add(key(entity))
    return matches


def look_up_departments(message: str) -> list[str]:
    """Verified facts about every department mentioned in the message, one line each."""
    facts = []
    if contains_phrase(message.lower(), "departments"):
        facts.append(f"The departments at {HOSPITAL_NAME} are: {DEPARTMENT_LIST}.")
    for match in resolve_departments(message):
        label = f'"{match.heard}"'
        if match.status == "resolved":
            entity = match.entities[0]
            facts.append(f"{label}: {entity.name}. {entity.facts}")
        elif match.status == "probable":
            entity = match.entities[0]
            facts.append(f"{label}: probably a misspelling of {entity.name}. {entity.facts}")
        elif match.status == "ambiguous":
            options = " or ".join(entity.name for entity in match.entities)
            facts.append(f"{label}: could mean {options}. Ask the user which one they mean.")
        else:
            specialty = match.entities[0].name
            facts.append(f"{label}: {HOSPITAL_NAME} has no {specialty} department. "
                         f"Its departments are: {DEPARTMENT_LIST}.")
    return facts
