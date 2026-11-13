"""T23–T26: schema/policy expectations, never copied from observed output."""

import json

from support import OfflineTest, ROOT, message, saved


class Validation(OfflineTest):
    def evaluate(self, fixture):
        from access_intake.validation import evaluate_response
        return evaluate_response(saved(fixture), message())

    def test_T23(self):
        from access_intake.validation import validate_response
        complete = validate_response(saved("complete"), message())
        self.assertEqual(complete["request_id"], "IT-001")
        self.assertEqual(complete["start_date"], "2026-10-23")
        nulls = validate_response(saved("schema-null-scalars"), message())
        fields = json.loads((ROOT / "snapshot.schema.json").read_text())["required"]
        for field in fields:
            if field not in ("issues", "supporting_quotes"):
                self.assertIsNone(nulls[field])
        invalids = [
            "schema-not-object", "schema-extra-key", "schema-invalid-scalar",
            "schema-invalid-enum", "schema-invalid-array", "bad-date", "empty-scalar",
            "fabricated-quote", "null-without-issues",
        ]
        invalids += [f"schema/missing-{field}" for field in fields]
        invalids += [f"schema/invalid-{field}" for field in fields]
        invalids += ["schema/bad-request-date", "schema/non-iso-date", "schema/empty-quote"]
        for name in invalids:
            with self.subTest(fixture=name):
                self.assert_error(self.evaluate(name), "invalid-extraction")

    def test_T24(self):
        self.assert_error(self.evaluate("refusal"), "refusal")

    def test_T25(self):
        for name in ("interrupted", "incomplete-valid-json"):
            with self.subTest(fixture=name):
                self.assert_error(self.evaluate(name), "incomplete")

    def test_T26(self):
        self.assert_error(self.evaluate("malformed"), "invalid-json")
        self.assert_error(self.evaluate("missing-output"), "invalid-envelope")
        self.assert_error(self.evaluate("bad-envelope"), "invalid-envelope")
