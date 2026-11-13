"""T29/T30: independent failures and the real HTTP adapter through fake transport.

Facts come from the frozen source ledger. Every reply is saved under
tests/mock_responses/. The sole environment-key loading subcase uses a known
dummy inside an isolated environment; no caller's real API_KEY may be read.
"""

import hashlib
import io
import json
import logging
import os
import socket
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable


ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials" / "requests"
MOCKS = ROOT / "tests" / "mock_responses"
LEDGER = json.loads((ROOT / "tests" / "expected_facts.json").read_text(encoding="utf-8"))
DUMMY = "dummy-test-only-T30-7392"


def saved(name):
    return json.loads((MOCKS / f"{name}.json").read_text(encoding="utf-8"))


def playback(name):
    return MockProvider([MockOutcome(response_path=MOCKS / f"{name}.json")])


class AdapterContract(unittest.TestCase):
    def setUp(self):
        self.allow_dummy_key = False
        self.key_reads = 0
        original_getitem = type(os.environ).__getitem__

        def guarded_getitem(environ, key):
            if key != "API_KEY":
                return original_getitem(environ, key)
            if not self.allow_dummy_key:
                raise AssertionError("Test must not read the caller's API_KEY")
            value = original_getitem(environ, key)
            self.assertEqual(value, DUMMY, "Only the isolated dummy may be loaded")
            self.key_reads += 1
            return value

        key_guard = patch.object(type(os.environ), "__getitem__", guarded_getitem)
        key_guard.start()
        self.addCleanup(key_guard.stop)
        for name in ("socket", "create_connection", "getaddrinfo"):
            network_guard = patch.object(socket, name, side_effect=AssertionError("Network denied in adapter tests"))
            network_guard.start()
            self.addCleanup(network_guard.stop)

    def assert_business(self, record, case, origin):
        expected = LEDGER[case]
        self.assertIsNone(record["error"], record)
        self.assertEqual(record["document_id"], expected["document_id"])
        self.assertEqual(record["response_source"], origin)
        self.assertEqual([record[key] for key in ("status", "owner", "next_action")], expected["route"])
        for field, value in expected["scalars"].items():
            self.assertEqual(record["extraction"][field], value, (case, field))

    def assert_error(self, record, code):
        self.assertIsNone(record["extraction"])
        self.assertEqual((record["status"], record["owner"], record["next_action"]),
                         ("error", "IT Service Desk", "retry-extraction"))
        self.assertEqual(record["error"]["code"], code)
        self.assertTrue(record["error"]["message"].strip())

    def test_T29(self):
        from access_intake.pipeline import IntakeService, process_documents, process_source

        retry_handler = Mock(wraps=process_source)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "IT-001.json"
            raw = (REQUESTS / "IT-001.json").read_bytes()
            path.write_bytes(raw)
            provider = MockProvider([
                MockOutcome(error=ProviderUnavailable("scripted outage")),
                MockOutcome(response_path=MOCKS / "production-write.json"),
            ])
            service = IntakeService(provider=provider, retry_handler=retry_handler)
            failed, peer = service.process([path, REQUESTS / "IT-002.json"])
            self.assert_error(failed, "provider-unavailable")
            self.assertEqual(failed["document_id"], "IT-001.json")
            self.assertEqual(failed["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertTrue(failed["attempt_id"])
            self.assertIn(failed["attempt_id"], failed["retry_command"])
            self.assertIn("retry --attempt", failed["retry_command"])
            self.assertIn("--state", failed["retry_command"])
            self.assert_business(peer, "T03", "mock")
            self.assertEqual([call.message for call in provider.calls], [LEDGER["T02"]["message"], LEDGER["T03"]["message"]])

            # Service retry uses saved bytes, not whatever now occupies the path.
            path.write_bytes(b"source changed after the failed call")
            retried = service.retry(failed["attempt_id"], provider=playback("complete"))
            self.assert_business(retried, "T02", "mock")
            self.assertNotEqual(retried["attempt_id"], failed["attempt_id"])
            self.assertEqual(retried["source_sha256"], failed["source_sha256"])
            retry_handler.assert_called_once()
            document = retry_handler.call_args.args[0]
            self.assertEqual(document.source_bytes, raw)
            self.assertEqual(document.message, LEDGER["T02"]["message"])

        # The existing public entry point must also isolate provider exceptions.
        provider = MockProvider([
            MockOutcome(error=ProviderUnavailable("scripted outage")),
            MockOutcome(response_path=MOCKS / "production-write.json"),
        ])
        failed, peer = process_documents([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], provider=provider)
        self.assert_error(failed, "provider-unavailable")
        self.assert_business(peer, "T03", "mock")
        self.assertEqual(self.key_reads, 0)

    def test_T30(self):
        from access_intake.http_provider import HttpProvider, HttpReply, ProviderFailure
        from access_intake.pipeline import record_response, process_documents
        from access_intake.prompt import SYSTEM_PROMPT

        source = LEDGER["T02"]["message"]
        calls = []

        def transport_for(name):
            def fake_transport(**request):
                calls.append(request)
                reply = saved(name)
                if "raw_body" in reply:
                    body = reply["raw_body"].encode("utf-8")
                elif "raw_body_hex" in reply:
                    body = bytes.fromhex(reply["raw_body_hex"])
                else:
                    body = json.dumps(reply["body"]).encode("utf-8")
                return HttpReply(reply["status"], reply["headers"], body)
            return fake_transport

        environment = {
            "API_KEY": DUMMY, "MODEL_ENDPOINT": "https://example.invalid/v1/chat/completions",
            "MODEL_ID": "test-model", "MODEL_PROVIDER": "test-provider",
            "MODEL_TIMEOUT_SECONDS": "12", "MODEL_MAX_RESPONSE_BYTES": "65536",
        }

        def load_adapter(transport, **overrides):
            # Populate a completely isolated environment before permitting reads.
            with ExitStack() as isolation:
                # patch.dict cleanup also reads its dummy value. Keep the guard
                # exception active until that isolated environment is removed.
                isolation.callback(setattr, self, "allow_dummy_key", False)
                isolation.enter_context(patch.dict(os.environ, {**environment, **overrides}, clear=True))
                self.allow_dummy_key = True
                return HttpProvider.from_environment(transport=transport)

        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        provider = load_adapter(transport_for("http-complete"))
        self.assertGreater(self.key_reads, 0)
        record = record_response("IT-001.json", "test-hash", source, provider.extract(source))
        self.assert_business(record, "T02", "test-provider/test-model-snapshot")
        request = calls[-1]
        self.assertEqual(request["endpoint"], environment["MODEL_ENDPOINT"])
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {DUMMY}")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["timeout"], 12)
        self.assertEqual(request["max_response_bytes"], 65536)
        body = json.loads(request["body"])
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"], [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": source}])
        schema = json.loads((ROOT / "snapshot.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(body["response_format"], {"type": "json_schema", "json_schema": {
            "name": "access_request", "strict": True, "schema": schema}})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertFalse(body["stream"])
        observations = [record, asdict(provider.config)]

        for fixture, code in (("http-refusal", "refusal"), ("http-refusal-with-content", "refusal"), ("http-incomplete", "incomplete")):
            with self.subTest(fixture=fixture):
                provider.transport = transport_for(fixture)
                record = record_response("IT-001.json", "test-hash", source, provider.extract(source))
                self.assert_error(record, code)
                observations.append(record)

        for fixture, code, retryable in (
            ("http-429", "http-429", True), ("http-503", "http-503", True),
            ("http-unauthorized", "http-401", False), ("http-bad-envelope", "invalid-envelope", False),
            ("http-bad-json", "invalid-envelope", False), ("http-bad-utf8", "invalid-envelope", False),
        ):
            with self.subTest(fixture=fixture):
                provider.transport = transport_for(fixture)
                with self.assertRaises(ProviderFailure) as caught:
                    provider.extract(source)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.retryable, retryable)
                if fixture == "http-429":
                    self.assertEqual(caught.exception.retry_after, 2)
                observations.append(str(caught.exception))

        for exception, code in (
            (TimeoutError(DUMMY), "provider-timeout"),
            (ProviderUnavailable(f"transport echoed {DUMMY}"), "provider-unavailable"),
            (ProviderFailure("provider-unavailable", DUMMY, retryable=True), "provider-unavailable"),
        ):
            with self.subTest(exception=type(exception).__name__):
                provider.transport = Mock(side_effect=exception)
                with self.assertRaises(ProviderFailure) as caught:
                    provider.extract(source)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                observations.append(str(caught.exception))
                failed = process_documents([REQUESTS / "IT-001.json"], provider=provider)[0]
                self.assert_error(failed, code)
                observations.append(failed)

        configured = load_adapter(transport_for("http-complete"), MODEL_ID="alternate-model",
                                  MODEL_TEMPERATURE="0.2", MODEL_MAX_COMPLETION_TOKENS="1024")
        configured.extract(source)
        alternate_body = json.loads(calls[-1]["body"])
        self.assertEqual(alternate_body["model"], "alternate-model")
        self.assertEqual(alternate_body["temperature"], 0.2)
        self.assertEqual(alternate_body["max_completion_tokens"], 1024)
        limited = load_adapter(transport_for("http-complete"), MODEL_MAX_RESPONSE_BYTES="128")
        with self.assertRaises(ProviderFailure) as caught:
            limited.extract(source)
        self.assertEqual(caught.exception.code, "response-too-large")
        self.assertFalse(caught.exception.retryable)

        evidence = provider.request_evidence(source)
        self.assertEqual(evidence["request"]["method"], "POST")
        self.assertEqual(evidence["request"]["endpoint"], environment["MODEL_ENDPOINT"])
        self.assertEqual(evidence["request"]["body"], body)
        self.assertEqual(evidence["request"]["headers"]["Authorization"], "[REDACTED]")
        self.assertTrue(evidence["response_handling"])
        artifact = json.loads((ROOT / "deliverables/request-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["request"], evidence["request"])
        self.assertEqual(artifact["observation"]["response_origin"], "fake HTTP transport")
        self.assertTrue(artifact["observation"]["configuration"])
        self.assertNotIn(DUMMY, log.getvalue() + json.dumps(observations) + json.dumps(evidence) + json.dumps(artifact))
        documentation = (ROOT / "docs/provider.md").read_text(encoding="utf-8")
        for name in (*environment, "MODEL_TEMPERATURE", "MODEL_MAX_COMPLETION_TOKENS"):
            self.assertIn(name, documentation)
        self.assertIn("https://api.openai.com/v1/chat/completions", documentation)

        # Missing explicit live configuration must fail without contacting transport.
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                HttpProvider.from_environment(transport=Mock())
