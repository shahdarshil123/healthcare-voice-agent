"""Runs the entity evaluation (evaluate_entities.py) as part of the normal test suite.

The evaluation is deterministic and takes well under a second, so it can guard every change:
- no wrong denials ever (saying a doctor or department doesn't exist when it does);
- no failures except the known, accepted misses listed below.

When you add harder cases to data/evaluation/entities/cases.jsonl, this test fails until the code
handles them, or until you consciously add them to KNOWN_MISSES with the reason.
"""

from evaluate_entities import evaluate, load_cases

# Cases we have decided to accept as misses, with the reason.
KNOWN_MISSES = {
    # Matching misspellings of specialties we DON'T have caused a wrong denial
    # ("cancel my appointment" -> "no Oncology department"). A miss here is harmless; a wrong denial is not.
    "dep-031",
}


def test_entity_evaluation_has_no_wrong_denials():
    result = evaluate(load_cases())
    assert result["wrong_denials"] == []


def test_entity_evaluation_has_no_unexpected_failures():
    result = evaluate(load_cases())
    unexpected = [failure for failure in result["failures"] if failure["id"] not in KNOWN_MISSES]
    assert unexpected == []


def test_known_misses_are_still_misses():
    # If a known miss starts passing, remove it from KNOWN_MISSES, so the list stays honest.
    failed_ids = {failure["id"] for failure in evaluate(load_cases())["failures"]}
    assert KNOWN_MISSES <= failed_ids
