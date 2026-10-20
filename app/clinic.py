"""The hospital's facts, read from the single source of truth: data/synthetic/clinic.json."""

import json
from pathlib import Path

CLINIC_FILE = Path(__file__).parent.parent / "data" / "synthetic" / "clinic.json"

CLINIC = json.loads(CLINIC_FILE.read_text(encoding="utf-8"))
