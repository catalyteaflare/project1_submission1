"""Prepare source-ledger accuracy inputs, including the authored T27 paraphrase.

Run from the repository root: python3 -m tools.prepare_accuracy_inputs
Does not generate expectations from application outputs or modify supplied files.
"""

import json
from pathlib import Path
import shutil


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / ".runtime/ledger-inputs"
    destination.mkdir(parents=True, exist_ok=True)
    for path in (root / "materials/requests").glob("*.json"):
        shutil.copy2(path, destination / path.name)
    ledger = json.loads((root / "tests/expected_facts.json").read_text(encoding="utf-8"))
    paraphrase = ledger["T27"]
    (destination / paraphrase["document_id"]).write_text(
        json.dumps({"message": paraphrase["message"]}, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Prepared 20 supplied exports and the source-authored IT-901.json in .runtime/ledger-inputs")


if __name__ == "__main__":
    main()
