"""T41: offline package walkthrough, paired artifacts and independent accuracy.

The oracle is the frozen, source-authored expected_facts.json, never a run.
All replies come from tests/mock_responses/. A clean source copy excludes local
state/environments. README's executable offline walkthrough is enclosed by
<!-- offline-walkthrough:start --> and <!-- offline-walkthrough:end -->; its sh
blocks run verbatim, with network/API_KEY guards inherited by Python children.
The full-suite command is checked separately to avoid recursively running T41.

Accuracy contract: compare(records, ledger) returns scalar_matches/scalar_total,
route_matches/route_total, per_field/per_document {matches,total}, factual_errors
with source_quotes, and separate evidence_errors/issue_errors. CLI accuracy uses
--state and --expected. Export --copy-to copies one verified generation's pair.
No production implementation or published observations are authored here.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from access_intake.pipeline import process_documents
from access_intake.providers import MockOutcome, MockProvider


ROOT = Path(__file__).resolve().parents[1]
LEDGER = json.loads((ROOT / "tests/expected_facts.json").read_text(encoding="utf-8"))
MOCKS = ROOT / "tests/mock_responses"
FIELDS = tuple(LEDGER["T02"]["scalars"])


class HandoverContract(unittest.TestCase):
    def setUp(self):
        original = type(os.environ).__getitem__

        def guarded_getitem(environ, name):
            if name == "API_KEY":
                raise AssertionError("Handover tests must not read API_KEY")
            return original(environ, name)

        for guard in (
            patch.object(type(os.environ), "__getitem__", guarded_getitem),
            *(patch.object(socket, name, side_effect=AssertionError("Network denied in T41"))
              for name in ("socket", "create_connection", "getaddrinfo")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def run_offline(self, arguments, root, *, timeout=90):
        environment = {key: os.environ[key] for key in os.environ if key != "API_KEY"}
        environment["PYTHONPATH"] = os.pathsep.join((str(root / "tests/offline_guard"), str(root)))
        environment["PIP_NO_INDEX"] = "1"
        environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        completed = subprocess.run(arguments, cwd=root, env=environment, text=True,
                                   capture_output=True, timeout=timeout)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed.stdout

    def assert_pair(self, directory, sources):
        rows = [json.loads(line) for line in (directory / "extractions.jsonl").read_text().splitlines()]
        queue = json.loads((directory / "queue.json").read_text())
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(queue), 20)
        self.assertEqual({row["document_id"] for row in rows}, {item["document_id"] for item in sources})
        self.assertEqual(len({row["document_id"] for row in rows}), 20)
        by_id = {row["document_id"]: row for row in rows}
        for expected in sources:
            row = by_id[expected["document_id"]]
            self.assertEqual(row["source_sha256"], hashlib.sha256(
                (ROOT / "materials/requests" / row["document_id"]).read_bytes()).hexdigest())
            self.assertEqual(row["response_source"], "mock")
            self.assertIsNone(row["error"])
            self.assertEqual([row[key] for key in ("status", "owner", "next_action")], expected["route"])
            self.assertEqual(set(row["extraction"]), set(FIELDS) | {"issues", "supporting_quotes"})
            for field, value in expected["scalars"].items():
                self.assertEqual(row["extraction"][field], value, (row["document_id"], field))
            for quote in row["extraction"]["supporting_quotes"]:
                self.assertIn(quote, expected["message"])
        self.assertEqual({row["document_id"] for row in queue}, set(by_id))
        for item in queue:
            row = by_id[item["document_id"]]
            for key in ("document_id", "source_sha256", "attempt_id", "revision",
                        "status", "owner", "next_action", "response_source"):
                self.assertEqual(item[key], row[key], key)
        return rows

    def test_T41(self):
        """Package/setup, documented commands, exports, accuracy and evidence agree."""
        required = ("requirements.lock", "deliverables/extractions.jsonl", "deliverables/queue.json",
                    "deliverables/accuracy.md", "deliverables/verification.md")
        missing = [name for name in required if not (ROOT / name).is_file()]
        self.assertEqual(missing, [], "Step 9 handover artifacts are missing")

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("env -u API_KEY python3 -m unittest discover -s tests", readme)
        for name in ("Python", "API_KEY", "MODEL_ENDPOINT", "MODEL_ID", "MODEL_PROVIDER",
                     "mock", "docs/operator.md", "requirements.lock"):
            self.assertIn(name, readme)
        match = re.search(r"<!-- offline-walkthrough:start -->(.*?)<!-- offline-walkthrough:end -->",
                          readme, re.S)
        self.assertIsNotNone(match, "README must identify its executable offline walkthrough")
        blocks = re.findall(r"```(?:sh|bash)\n(.*?)```", match.group(1), re.S)
        self.assertTrue(blocks, "The walkthrough must contain runnable shell commands")
        script = "\n".join(blocks)
        for operation in ("intake", "queue", "progress", "history", "export", "accuracy"):
            self.assertRegex(script, rf"-m access_intake {operation}\b")
        self.assertNotIn("unittest discover", script, "Suite command belongs outside the nonrecursive walkthrough")
        for flag in ("--no-index", "--require-hashes", "requirements.lock", "--copy-to"):
            self.assertIn(flag, script)

        supplied = [case for case in LEDGER.values() if case["document_id"] != "IT-901.json"]
        self.assertEqual(len(supplied), 20)
        self.assert_pair(ROOT / "deliverables", supplied)

        # Check published expectations against the pre-existing source ledger.
        accuracy_doc = (ROOT / "deliverables/accuracy.md").read_text(encoding="utf-8")
        for case in LEDGER.values():
            self.assertIn(case["document_id"], accuracy_doc)
            for field, expected in case["scalars"].items():
                self.assertIn(field, accuracy_doc)
                if expected is not None:
                    self.assertIn(str(expected), accuracy_doc)
            for quote in case["source_quotes"]:
                self.assertIn(quote, accuracy_doc)
        for text in ("252", "null", "exact", "mock", "policy", "factual", "purpose"):
            self.assertIn(text, accuracy_doc.lower())
        verification = (ROOT / "deliverables/verification.md").read_text(encoding="utf-8")
        for text in ("command", "assert", "observ", "config", "mock", "limit", "single",
                     "interrupt", "400", "2,000", "live", "quota", "cost"):
            self.assertIn(text, verification.lower())
        for number in range(1, 42):
            self.assertIn(f"T{number:02}", verification, "Every verification case must be traceable")

        with TemporaryDirectory() as temporary:
            clean = Path(temporary) / "clean"
            clean.mkdir()
            for name in ("access_intake", "config", "docs", "materials", "tests", "tools", "deliverables"):
                shutil.copytree(ROOT / name, clean / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.sqlite*"))
            for name in ("README.md", "requirements.lock", "snapshot.schema.json"):
                shutil.copy2(ROOT / name, clean / name)
            # Outputs must be produced afresh, not inherited from the submission.
            for name in ("extractions.jsonl", "queue.json"):
                (clean / "deliverables" / name).unlink()
            self.run_offline(["bash", "-eu", "-c", script], clean)
            self.assertTrue((clean / ".venv/bin/python").is_file(), "Walkthrough must create a clean environment")
            python = str(clean / ".venv/bin/python")
            self.assertEqual(self.run_offline([python, "-c",
                "import os; assert os.environ.get('ACCESS_INTAKE_OFFLINE_TEST') == '1'; print('guarded')"],
                clean).strip(), "guarded")
            generated = self.assert_pair(clean / "deliverables", supplied)
            # Final files must be the exact bytes of one checksummed generation.
            matching_generations = []
            for manifest_path in (clean / ".runtime").rglob("manifest.json"):
                manifest = json.loads(manifest_path.read_text())
                generation = manifest_path.parent / manifest["generation"]
                if all((generation / name).read_bytes() == (clean / "deliverables" / name).read_bytes()
                       and hashlib.sha256((generation / name).read_bytes()).hexdigest() == manifest["sha256"][name]
                       for name in ("extractions.jsonl", "queue.json")):
                    matching_generations.append(generation)
            self.assertTrue(matching_generations, "Final output pair must copy one verified snapshot generation")

            # Reproduce committed evidence through the actual adapter + saved HTTP response.
            before = json.loads((clean / "deliverables/request-evidence.json").read_text())
            self.run_offline([python, "-m", "tools.record_request_evidence"], clean)
            after = json.loads((clean / "deliverables/request-evidence.json").read_text())
            self.assertEqual(before, after)
            self.assertEqual(after["request"]["headers"]["Authorization"], "[REDACTED]")
            self.assertEqual(after["observation"]["response_origin"], "fake HTTP transport")
            self.assertEqual(after["observation"]["source_sha256"], hashlib.sha256(
                (ROOT / "materials/requests/IT-001.json").read_bytes()).hexdigest())

            # Separate 21-case accuracy state: the final handover pair stays at 20.
            inputs = clean / ".runtime/accuracy-inputs"
            shutil.copytree(clean / "materials/requests", inputs)
            paraphrase = LEDGER["T27"]
            (inputs / paraphrase["document_id"]).write_text(json.dumps({"message": paraphrase["message"]}))
            state = clean / ".runtime/accuracy.sqlite"
            self.run_offline([python, "-m", "access_intake", "intake", str(inputs), "--state", str(state)], clean)
            report = json.loads(self.run_offline([python, "-m", "access_intake", "accuracy",
                "--state", str(state), "--expected", "tests/expected_facts.json"], clean))
            self.assertEqual((report["scalar_matches"], report["scalar_total"]), (252, 252))
            self.assertEqual((report["route_matches"], report["route_total"]), (21, 21))
            self.assertEqual(report["factual_errors"], [])
            self.assertEqual(report["evidence_errors"], [])
            self.assertEqual(report["issue_errors"], [])
            self.assertEqual(len(generated), 20)

        # Adversarial accounting: a passing mock replay alone cannot test the scorer.
        from access_intake.accuracy import compare

        with TemporaryDirectory() as temporary:
            records = []
            for case in LEDGER.values():
                path = Path(temporary) / case["document_id"]
                path.write_text(json.dumps({"message": case["message"]}), encoding="utf-8")
                records.extend(process_documents([path], provider=MockProvider([
                    MockOutcome(response_path=MOCKS / f"{case['fixture']}.json")])))
        baseline = compare(records, LEDGER)
        self.assertEqual((baseline["scalar_matches"], baseline["scalar_total"]), (252, 252))
        self.assertEqual((baseline["route_matches"], baseline["route_total"]), (21, 21))
        for field in FIELDS:
            self.assertEqual(baseline["per_field"][field], {"matches": 21, "total": 21})
        for case in LEDGER.values():
            self.assertEqual(baseline["per_document"][case["document_id"]], {"matches": 12, "total": 12})

        changed = copy.deepcopy(records)
        by_id = {row["document_id"]: row for row in changed}
        by_id["IT-001.json"]["extraction"]["manager"] = "Invented Manager"
        del by_id["IT-003.json"]["extraction"]["start_date"]  # Missing is not a null-to-null match.
        by_id["IT-010.json"]["extraction"]["end_date"] = "2099-01-01"  # Unsupported non-null fact.
        by_id["IT-005.json"].update(extraction=None, error={"code": "provider-unavailable"}, status="error")
        changed.remove(by_id["IT-002.json"])  # Missing record still contributes 12 nonmatches.
        by_id["IT-901.json"]["extraction"]["business_purpose"] = "Compare weekly conversion figures."
        by_id["IT-004.json"]["extraction"]["supporting_quotes"] = ["A fabricated source quote."]
        by_id["IT-009.json"]["extraction"]["issues"] = []
        by_id["IT-001.json"]["next_action"] = "clarify"
        measured = compare(changed, LEDGER)
        self.assertEqual((measured["scalar_matches"], measured["scalar_total"]), (224, 252))
        self.assertEqual((measured["route_matches"], measured["route_total"]), (18, 21))
        self.assertEqual(measured["per_document"]["IT-005.json"], {"matches": 0, "total": 12})
        self.assertEqual(measured["per_document"]["IT-002.json"], {"matches": 0, "total": 12})
        self.assertEqual(measured["per_field"]["manager"], {"matches": 18, "total": 21})
        self.assertEqual(measured["per_field"]["start_date"], {"matches": 18, "total": 21})
        self.assertEqual(len(measured["factual_errors"]), 28)
        unsupported = next(error for error in measured["factual_errors"]
                           if error["document_id"] == "IT-001.json" and error["field"] == "manager")
        self.assertEqual(unsupported["expected"], "Priya Shah")
        self.assertEqual(unsupported["observed"], "Invented Manager")
        self.assertIn("My manager is Priya Shah.", unsupported["source_quotes"])
        self.assertTrue(measured["evidence_errors"])
        self.assertTrue(measured["issue_errors"])
        self.assertEqual(compare(changed, LEDGER), measured, "Scoring must not mutate inputs/oracle")
        with self.assertRaises(ValueError):
            compare(records + [records[0]], LEDGER)  # Never silently pick one duplicate.


if __name__ == "__main__":
    unittest.main()
