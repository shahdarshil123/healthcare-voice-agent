"""Tests for the department lookup (app/departments.py). No AI model involved: fast and deterministic."""

from app.departments import look_up_departments, resolve_departments


def resolved_ids(message):
    return [entity.id for match in resolve_departments(message) for entity in match.entities if entity.id]


def statuses(message):
    return [match.status for match in resolve_departments(message)]


# ---------- Names, aliases, and specialist titles ----------

def test_official_names_resolve():
    assert resolved_ids("Where is the Cardiology department?") == ["cardiology"]


def test_everyday_words_and_titles_resolve_to_canonical_departments():
    assert resolved_ids("Where is the heart department?") == ["cardiology"]
    assert resolved_ids("Is there a children's department?") == ["pediatrics"]
    assert resolved_ids("I need a skin doctor") == ["dermatology"]
    assert resolved_ids("Where is the pediatrician?") == ["pediatrics"]
    assert resolved_ids("Where can I get a flu vaccination?") == ["family-medicine"]
    assert resolved_ids("Where is the orthopaedics department?") == ["orthopedics"]


def test_several_departments_in_one_message():
    assert resolved_ids("My child needs an x-ray") == ["pediatrics", "radiology"]


def test_services_that_are_not_departments_resolve():
    assert resolved_ids("Where is the lab?") == ["laboratory"]
    assert resolved_ids("Where is the billing department?") == ["billing"]
    assert resolved_ids("Do you have an emergency department?") == ["emergency"]


def test_whole_words_only():
    assert resolve_departments("I'd like an appointment on Friday") == []  # "ent" inside "appointment"
    assert resolve_departments("Er, can I book something") == []


def test_er_in_capitals_is_the_emergency_room():
    assert resolved_ids("Where is the ER?") == ["emergency"]


def test_heart_idiom_is_not_cardiology():
    assert resolve_departments("My heart is set on a Friday appointment") == []
    assert resolved_ids("Where is the heart department?") == ["cardiology"]


def test_misspellings_never_create_a_denial():
    # Typo-matching specialties we don't have turned "cancel" into "cancer" (no Oncology department).
    assert resolve_departments("I want to cancel my appointment") == []


# ---------- Misspellings ----------

def test_misspellings_are_probable_matches():
    matches = resolve_departments("Where is the dermatalogy department?")
    assert [(m.status, m.entities[0].id) for m in matches] == [("probable", "dermatology")]

    matches = resolve_departments("Where is the radiolog department?")
    assert [(m.status, m.entities[0].id) for m in matches] == [("probable", "radiology")]


def test_short_words_are_never_fuzzy_matched():
    assert resolve_departments("I like dogs") == []


# ---------- Specialties the hospital doesn't have ----------

def test_specialties_not_offered():
    assert statuses("Where is the neurology department?") == ["not_offered"]
    assert statuses("Do you have an eye department?") == ["not_offered"]
    assert statuses("I need a dentist") == ["not_offered"]
    assert statuses("Does Dr. Patel perform brain surgery?") == ["not_offered"]


def test_not_offered_facts_list_the_real_departments():
    fact = look_up_departments("Where is the neurology department?")[0]
    assert "has no Neurology department" in fact
    assert "Family Medicine, Cardiology, Dermatology, Orthopedics, Pediatrics, Radiology" in fact


# ---------- Things that must NOT be treated as missing (the old regression) ----------

def test_facilities_and_policies_are_never_claimed_to_be_missing():
    # Prompt v10 made the model say "Brightwater doesn't have a pharmacy". The lookup must say nothing here,
    # so the answer comes from the documents (or the "I don't have that information" reply).
    assert look_up_departments("Is there a pharmacy in the hospital?") == []
    assert look_up_departments("Can I bring my dog when I visit a patient?") == []


def test_medication_questions_get_no_department_facts():
    assert look_up_departments("Should I stop taking my blood pressure tablets?") == []


# ---------- Listing all departments ----------

def test_asking_for_departments_lists_them_all():
    facts = look_up_departments("What departments do you have?")
    assert facts == ["The departments at Brightwater Community Hospital are: Family Medicine, Cardiology, "
                     "Dermatology, Orthopedics, Pediatrics, Radiology."]
