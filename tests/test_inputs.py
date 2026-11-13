"""T22: source loading and document identity, without fact validation/routing.

Expected input behavior comes from planning/plan.md T22 and policy, rather
than fixture contents. Saved replies only drive the injected mock provider.
"""

import hashlib
import json
import os
import socket
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from access_intake.providers import MockOutcome, MockProvider


ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials" / "requests"
MOCKS = ROOT / "tests" / "mock_responses"


class InputContract(unittest.TestCase):
    def test_T22(self):
        original_getitem = type(os.environ).__getitem__

        def guarded_getitem(environ, key):
            if key == "API_KEY":
                raise AssertionError("T22 must not read API_KEY")
            return original_getitem(environ, key)

        with ExitStack() as guards:
            guards.enter_context(patch.object(type(os.environ), "__getitem__", guarded_getitem))
            for name in ("socket", "create_connection", "getaddrinfo"):
                guards.enter_context(patch.object(
                    socket, name, side_effect=AssertionError("T22 must not use the network")
                ))
            temporary = Path(guards.enter_context(TemporaryDirectory()))

            from access_intake.inputs import InputError, process_exports

            def provider_for(*names):
                return MockProvider([
                    MockOutcome(response_path=MOCKS / f"{name}.json") for name in names
                ])

            def write_export(relative, raw):
                path = temporary / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                return path

            def assert_loaded(result, path, expected_message):
                self.assertEqual(result.document_id, path.name)
                self.assertEqual(result.source_sha256, hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(result.message, expected_message)
                self.assertIsNone(result.error)
                self.assertEqual(result.response["response_source"], "mock")

            with self.subTest("supplied independent documents and decoded newlines"):
                paths = [REQUESTS / name for name in ("IT-001.json", "IT-005.json", "IT-016.json")]
                messages = [json.loads(path.read_text(encoding="utf-8"))["message"] for path in paths]
                provider = provider_for("complete", "withdrawal", "identifier-mismatch")
                results = process_exports(paths, provider=provider)
                self.assertEqual(len(results), 3)
                for result, path, message in zip(results, paths, messages):
                    assert_loaded(result, path, message)
                self.assertEqual([call.message for call in provider.calls], messages)
                self.assertIn("\n\n", provider.calls[1].message)
                self.assertIn("IT-061", provider.calls[2].message)
                self.assertEqual(results[2].document_id, "IT-016.json")

            with self.subTest("new escaped UTF-8 message and unknown ancillary metadata"):
                message = "IT-901, 2026-10-22: Café team needs help."
                raw = json.dumps({
                    "message": message, "export_version": "future-format",
                    "exported_at": "2099-01-01", "requester": "Metadata must not supply facts",
                }, ensure_ascii=True).encode("utf-8")
                path = write_export("IT-901.json", raw)
                provider = provider_for("input-other")
                results = process_exports([path], provider=provider)
                self.assertEqual(len(results), 1)
                assert_loaded(results[0], path, message)
                self.assertEqual([call.message for call in provider.calls], [message])
                self.assertIn(b"\\u00e9", raw)

            with self.subTest("metadata and whitespace change hashes but never source text"):
                original = REQUESTS / "IT-001.json"
                source = json.loads(original.read_text(encoding="utf-8"))
                changed_metadata = {**source, "subject": "Grant admin", "requester": "Wrong Name"}
                variants = [
                    original,
                    write_export("metadata/IT-001.json", json.dumps(changed_metadata).encode("utf-8")),
                    write_export("whitespace/IT-001.json", b" \n" + original.read_bytes() + b"\n"),
                ]
                hashes = []
                for path in variants:
                    provider = provider_for("complete")
                    results = process_exports([path], provider=provider)
                    self.assertEqual(len(results), 1)
                    assert_loaded(results[0], path, source["message"])
                    self.assertEqual([call.message for call in provider.calls], [source["message"]])
                    hashes.append(results[0].source_sha256)
                self.assertEqual(len(set(hashes)), 3)

            with self.subTest("same request ID and bytes under distinct filenames stay separate"):
                original = REQUESTS / "IT-001.json"
                copy = write_export("IT-902.json", original.read_bytes())
                provider = provider_for("complete", "complete")
                results = process_exports([original, copy], provider=provider)
                self.assertEqual([result.document_id for result in results], ["IT-001.json", "IT-902.json"])
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(provider.calls[0].message, provider.calls[1].message)
                self.assertEqual(results[0].source_sha256, results[1].source_sha256)

            invalids = [
                ("bad-encoding.json", b'\xff', "UTF-8"),
                ("bad-json.json", b'{"message":', "JSON"),
                ("missing-message.json", b'{}', "message"),
                ("numeric-message.json", b'{"message": 123}', "message"),
                ("null-message.json", b'{"message": null}', "message"),
                ("array-message.json", b'{"message": []}', "message"),
                ("non-object.json", b'[]', "object"),
            ]
            for name, raw, diagnostic in invalids:
                with self.subTest("bad input preserves identity/hash and does not block peers", name=name):
                    bad = write_export(name, raw)
                    good = REQUESTS / "IT-001.json"
                    provider = provider_for("complete")
                    results = process_exports([bad, good], provider=provider)
                    self.assertEqual(len(results), 2)
                    failed, succeeded = results
                    self.assertEqual(failed.document_id, name)
                    self.assertEqual(failed.source_sha256, hashlib.sha256(raw).hexdigest())
                    self.assertIsNone(failed.message)
                    self.assertIsNone(failed.response)
                    self.assertEqual(failed.error["code"], "input-error")
                    self.assertIn(diagnostic.lower(), failed.error["message"].lower())
                    assert_loaded(succeeded, good, json.loads(good.read_text(encoding="utf-8"))["message"])
                    self.assertEqual(len(provider.calls), 1)

            with self.subTest("duplicate basenames rejected before any provider call"):
                duplicate = write_export("duplicate/IT-001.json", b'{"message": "different"}')
                provider = provider_for("withdrawal", "complete", "complete")
                with self.assertRaisesRegex(InputError, r"(?i)duplicate.*IT-001\.json"):
                    process_exports([
                        REQUESTS / "IT-005.json", REQUESTS / "IT-001.json", duplicate,
                    ], provider=provider)
                self.assertEqual(provider.calls, [])
