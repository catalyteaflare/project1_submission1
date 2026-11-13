"""T23–T26: planned validation gates, before routing is implemented.

Success facts below come from planning/plan.md T02 and IT-001's message.
Failure expectations come from the schema and plan's OQ-08/T23–T26,
not from running an extractor or copying the fixture under test.
"""

import json
import os
import socket
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
SCALARS = (
    "request_id", "request_date", "kind", "requester", "team", "system",
    "environment", "permission", "business_purpose", "start_date", "end_date", "manager",
)
FIELDS = (*SCALARS, "supporting_quotes", "issues")


class ValidationContract(unittest.TestCase):
    def setUp(self):
        original_getitem = type(os.environ).__getitem__

        def guard(environ, key):
            if key == "API_KEY":
                raise AssertionError("Validation tests must not read API_KEY")
            return original_getitem(environ, key)

        key_guard = patch.object(type(os.environ), "__getitem__", guard)
        key_guard.start()
        self.addCleanup(key_guard.stop)
        for name in ("socket", "create_connection", "getaddrinfo"):
            network_guard = patch.object(socket, name, side_effect=AssertionError("Network denied in validation tests"))
            network_guard.start()
            self.addCleanup(network_guard.stop)
        self.message = json.loads(
            (ROOT / "materials" / "requests" / "IT-001.json").read_text(encoding="utf-8")
        )["message"]

    def saved(self, name):
        return json.loads((MOCKS / f"{name}.json").read_text(encoding="utf-8"))

    def assert_rejected(self, fixture, code):
        from access_intake.validation import evaluate_response
        record = evaluate_response(self.saved(fixture), self.message)
        self.assertIsNone(record["extraction"])
        self.assertEqual(
            (record["status"], record["owner"], record["next_action"]),
            ("error", "IT Service Desk", "retry-extraction"),
        )
        self.assertEqual(record["error"]["code"], code)
        self.assertIsInstance(record["error"]["message"], str)
        self.assertTrue(record["error"]["message"].strip())

    def test_T23(self):
        from access_intake.validation import validate_response, evaluate_response

        expected = {
            "request_id": "IT-001", "request_date": "2026-10-22", "kind": "new",
            "requester": "Maya Chen", "team": "Analytics", "system": "Atlas",
            "environment": "staging", "permission": "read",
            "business_purpose": "checking the weekly conversion dashboard with the new event definition changes",
            "start_date": "2026-10-23", "end_date": "2026-11-06", "manager": "Priya Shah",
        }
        accepted = validate_response(self.saved("complete"), self.message)
        self.assertEqual(set(accepted), set(FIELDS))
        self.assertEqual({field: accepted[field] for field in SCALARS}, expected)
        self.assertEqual(accepted["issues"], [])
        self.assertTrue(accepted["supporting_quotes"])
        for quote in accepted["supporting_quotes"]:
            self.assertTrue(quote)
            self.assertIn(quote, self.message)
        successful = evaluate_response(self.saved("complete"), self.message)
        self.assertIsNone(successful["error"])
        self.assertEqual(successful["extraction"], accepted)

        nulls = validate_response(self.saved("schema-null-scalars"), self.message)
        self.assertEqual(set(nulls), set(FIELDS))
        for field in SCALARS:
            self.assertIsNone(nulls[field])
            self.assertIn(field, " ".join(nulls["issues"]))

        invalids = [
            "schema-not-object", "schema-extra-key", "schema-invalid-scalar",
            "schema-invalid-enum", "schema-invalid-array", "bad-date", "empty-scalar",
            "fabricated-quote", "schema/null-without-issues", "schema/empty-quote",
            "schema/non-iso-date", "schema/bad-request-date", "schema/bad-start-date",
            "schema/invalid-kind-enum", "schema/invalid-environment-enum",
            "schema/issues-not-array", "schema/quotes-not-array",
        ]
        invalids += [f"schema/missing-{field}" for field in FIELDS]
        invalids += [f"schema/invalid-{field}" for field in FIELDS]
        for fixture in invalids:
            with self.subTest(fixture=fixture):
                self.assert_rejected(fixture, "invalid-extraction")

        # Real reversed dates pass validation; step 4 will route them to clarify.
        source = json.loads((ROOT / "materials/requests/IT-014.json").read_text(encoding="utf-8"))["message"]
        reversed_dates = validate_response(self.saved("schema/reversed-valid-dates"), source)
        self.assertEqual(reversed_dates["start_date"], "2026-11-05")
        self.assertEqual(reversed_dates["end_date"], "2026-11-02")

    def test_T24(self):
        self.assert_rejected("refusal", "refusal")

    def test_T25(self):
        for fixture in ("interrupted", "incomplete-valid-json"):
            with self.subTest(fixture=fixture):
                self.assert_rejected(fixture, "incomplete")

    def test_T26(self):
        self.assert_rejected("malformed", "invalid-json")
        for fixture in ("missing-output", "schema/bad-envelope", "schema/unknown-status", "schema/non-string-output"):
            with self.subTest(fixture=fixture):
                self.assert_rejected(fixture, "invalid-envelope")
