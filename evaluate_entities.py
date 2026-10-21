"""Entity evaluation: how well does our code find doctors and departments in messages?

Compares app/doctors.py and app/departments.py against hand-labelled messages in
data/evaluation/entities/cases.jsonl. Fully deterministic (no AI model), so one run is enough.

Each entity is compared as a (status, ids) pair, for example ("possible", ("dr-chidi-okafor",)).
A found entity counts only if BOTH the status and the identity are right.

Usage:
    uv run python evaluate_entities.py
"""

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from app.departments import resolve_departments
from app.doctors import find_mentions, resolve_name

ROOT = Path(__file__).parent
CASES_FILE = ROOT / "data" / "evaluation" / "entities" / "cases.jsonl"
REPORTS_FOLDER = ROOT / "data" / "evaluation" / "reports"

# Statuses that tell the user something does NOT exist. Getting these wrong is the most harmful error:
# it denies a real doctor or department ("there is no Dr. Maya", "we don't have a pharmacy").
DENIALS = {"not_found", "not_offered"}


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def as_items(entities: list[dict]) -> set[tuple]:
    """Labelled entities -> {(status, (ids...)), ...}"""
    return {(entity["status"], tuple(sorted(entity["ids"]))) for entity in entities}


def predict_doctors(message: str) -> set[tuple]:
    items = set()
    for heard in find_mentions(message):
        match = resolve_name(heard)
        items.add((match.status, tuple(sorted(doctor["id"] for doctor in match.candidates))))
    return items


def predict_departments(message: str) -> set[tuple]:
    # For a specialty we don't have, the "id" is empty, so its name identifies it instead.
    return {(match.status, tuple(sorted(entity.id or entity.name for entity in match.entities)))
            for match in resolve_departments(message)}


def scores(true_positives: int, false_positives: int, false_negatives: int) -> dict:
    precision = true_positives / (true_positives + false_positives) if true_positives + false_positives else 1.0
    recall = true_positives / (true_positives + false_negatives) if true_positives + false_negatives else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
            "true_positives": true_positives, "false_positives": false_positives, "false_negatives": false_negatives}


def evaluate(cases: list[dict]) -> dict:
    counts = {"doctors": [0, 0, 0], "departments": [0, 0, 0]}  # [true pos, false pos, false neg]
    wrong_denials, failures = [], []
    by_tag = defaultdict(lambda: [0, 0])  # tag -> [cases fully correct, cases]

    for case in cases:
        correct = True
        details = {}
        for kind, predict in (("doctors", predict_doctors), ("departments", predict_departments)):
            expected, predicted = as_items(case[kind]), predict(case["message"])
            counts[kind][0] += len(predicted & expected)
            counts[kind][1] += len(predicted - expected)
            counts[kind][2] += len(expected - predicted)
            wrong_denials += [f"{case['id']}: {item}" for item in predicted - expected if item[0] in DENIALS]
            if predicted != expected:
                correct = False
                details[kind] = {"expected": sorted(expected), "got": sorted(predicted)}
        for tag in case["tags"]:
            by_tag[tag][0] += correct
            by_tag[tag][1] += 1
        if not correct:
            failures.append({"id": case["id"], "message": case["message"], **details})

    return {
        "cases": len(cases),
        "cases_fully_correct": len(cases) - len(failures),
        "doctors": scores(*counts["doctors"]),
        "departments": scores(*counts["departments"]),
        "wrong_denials": wrong_denials,
        "by_tag": {tag: {"correct": ok, "cases": total} for tag, (ok, total) in sorted(by_tag.items())},
        "failures": failures,
    }


def print_report(result: dict) -> None:
    print(f"Cases fully correct: {result['cases_fully_correct']}/{result['cases']}\n")
    print(f"{'':12}{'precision':>10}{'recall':>8}{'F1':>7}   (true pos / false pos / false neg)")
    for kind in ("doctors", "departments"):
        s = result[kind]
        print(f"{kind:12}{s['precision']:>10.3f}{s['recall']:>8.3f}{s['f1']:>7.3f}   "
              f"({s['true_positives']} / {s['false_positives']} / {s['false_negatives']})")
    print(f"\nWrong denials (said something doesn't exist when it does): {len(result['wrong_denials'])}")
    for denial in result["wrong_denials"]:
        print(f"   {denial}")
    print("\nBy tag (cases fully correct):")
    for tag, t in result["by_tag"].items():
        marker = "" if t["correct"] == t["cases"] else "   <--"
        print(f"   {tag:16}{t['correct']:>3}/{t['cases']:<3}{marker}")
    print(f"\nFailures ({len(result['failures'])}):")
    for failure in result["failures"]:
        print(f"   {failure['id']}: {failure['message']}")
        for kind in ("doctors", "departments"):
            if kind in failure:
                print(f"      {kind}: expected {failure[kind]['expected']}  got {failure[kind]['got']}")


if __name__ == "__main__":
    report = evaluate(load_cases())
    print_report(report)
    REPORTS_FOLDER.mkdir(parents=True, exist_ok=True)
    report_file = REPORTS_FOLDER / f"entities-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    report_file.write_text(json.dumps({"created_at": datetime.now(UTC).isoformat(), **report}, indent=2),
                           encoding="utf-8")
    print(f"\nReport saved: {report_file.relative_to(ROOT)}")
