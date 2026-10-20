"""Tests for the doctor lookup (app/doctors.py). No AI model involved: fast and deterministic."""

from app.doctors import find_mentions, get_doctor, look_up_doctors, resolve_name

# Two doctors who share a surname: the case a surname-only index silently got wrong.
TWO_CHENS = [
    {"id": "dr-maya-chen", "name": "Dr. Maya Chen", "department_id": "family-medicine"},
    {"id": "dr-wei-chen", "name": "Dr. Wei Chen", "department_id": "cardiology"},
]


# ---------- Finding doctor names in a message ----------

def test_finds_names_after_dr_or_doctor_in_any_capitalisation():
    assert find_mentions("Can I see Dr. Kim?") == ["Kim"]
    assert find_mentions("is dr. Lindqvist free") == ["Lindqvist"]
    assert find_mentions("Book Doctor Raj Patel please") == ["Raj Patel"]
    assert find_mentions("Dr. Nguyen or Dr. Smith") == ["Nguyen", "Smith"]


def test_weekdays_and_common_words_after_doctor_are_not_names():
    # Previously these produced "there is no Dr. Monday".
    assert find_mentions("I need a doctor Monday morning") == []
    assert find_mentions("Is the doctor Friday available?") == []
    assert find_mentions("Doctor Who is my favourite show") == []
    assert find_mentions("Dr. Okafor Tuesday") == ["Okafor"]  # the name is kept, the weekday dropped


# ---------- Resolving a name to a doctor ----------

def test_full_name_resolves_exactly():
    match = resolve_name("Raj Patel")
    assert match.status == "resolved"
    assert match.candidates[0]["id"] == "dr-raj-patel"


def test_first_name_alone_resolves():
    # Previously "Dr. Maya" was reported as "no doctor with this name".
    match = resolve_name("Maya")
    assert match.status == "resolved"
    assert match.candidates[0]["name"] == "Dr. Maya Chen"


def test_unique_id_lookup():
    assert get_doctor("dr-raj-patel")["name"] == "Dr. Raj Patel"
    assert get_doctor("dr-nobody") is None


def test_shared_surname_is_ambiguous_and_keeps_both_doctors():
    match = resolve_name("Chen", TWO_CHENS)
    assert match.status == "ambiguous"
    assert {doctor["id"] for doctor in match.candidates} == {"dr-maya-chen", "dr-wei-chen"}


def test_full_name_picks_the_right_doctor_when_surnames_are_shared():
    match = resolve_name("Wei Chen", TWO_CHENS)
    assert match.status == "resolved"
    assert match.candidates[0]["id"] == "dr-wei-chen"


def test_misheard_name_is_only_a_possible_match():
    match = resolve_name("Akafur")
    assert match.status == "possible"
    assert match.candidates[0]["id"] == "dr-chidi-okafor"


def test_partial_full_name_is_only_a_possible_match():
    match = resolve_name("Raj Smith")  # the first name fits Dr. Raj Patel, the surname doesn't
    assert match.status == "possible"
    assert match.candidates[0]["id"] == "dr-raj-patel"


def test_unknown_names_are_not_found():
    assert resolve_name("Kim").status == "not_found"
    assert resolve_name("Smith").status == "not_found"


# ---------- The facts given to the model ----------

def test_facts_for_each_outcome():
    assert "there is no doctor with this name" in look_up_doctors("Book Dr. Kim")[0]
    assert "Friday 08:00-15:00" in look_up_doctors("Book Dr. Raj Patel")[0]
    assert "NOT accepting new patients" in look_up_doctors("Is Dr. Lindqvist free?")[0]

    possible = look_up_doctors("Can I see Doctor Akafur?")[0]
    assert "Possibly Dr. Chidi Okafor" in possible
    assert "confirm" in possible
    assert "Works" not in possible  # no details are given until the user confirms


def test_no_facts_when_no_doctor_is_mentioned():
    assert look_up_doctors("What are the visiting hours?") == []
