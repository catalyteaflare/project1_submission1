"""T29/T30 use saved replies and the real adapter through a fake transport."""

import json
import logging
import os
from dataclasses import asdict
from io import StringIO
from unittest.mock import Mock, patch

from support import OfflineTest, ROOT, REQUESTS, message, playback, saved
from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable


class Adapter(OfflineTest):
    def test_T29(self):
        from access_intake.pipeline import IntakeService, process_source
        retry_handler = Mock(wraps=process_source)
        provider = MockProvider([
            MockOutcome(error=ProviderUnavailable("scripted outage")),
            MockOutcome(response_path=ROOT / "tests/mock_responses/production-write.json"),
        ])
        service = IntakeService(provider=provider, retry_handler=retry_handler)
        failed, peer = service.process([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"])
        self.assert_error(failed, "provider-unavailable")
        self.assertEqual(failed["document_id"], "IT-001.json")
        self.assertIn(failed["attempt_id"], failed["retry_command"])
        self.assertIn("retry --attempt", failed["retry_command"])
        self.assertEqual(peer["extraction"]["requester"], "Owen Reed")
        self.assertEqual(peer["extraction"]["permission"], "write")
        self.assertEqual(peer["next_action"], "review-risk-then-owner")
        result = service.retry(failed["attempt_id"], provider=playback("complete"))
        self.assertEqual(result["extraction"]["requester"], "Maya Chen")
        self.assertEqual(result["next_action"], "review-request")
        retry_handler.assert_called_once()
        self.assertEqual(retry_handler.call_args.args[0].source_bytes, (REQUESTS / "IT-001.json").read_bytes())

    def test_T30(self):
        from access_intake.http_provider import HttpProvider, HttpReply, ProviderFailure
        from access_intake.pipeline import record_response
        from access_intake.prompt import SYSTEM_PROMPT
        from access_intake.validation import SCHEMA
        dummy = "dummy-credential-only-T30-7392"
        calls = []

        def transport_for(fixture):
            def transport(**request):
                calls.append(request)
                reply = saved(fixture)
                return HttpReply(reply["status"], reply["headers"], json.dumps(reply["body"]).encode())
            return transport

        environment = {
            "API_KEY": dummy, "MODEL_ENDPOINT": "https://example.invalid/v1/chat/completions",
            "MODEL_ID": "test-model", "MODEL_PROVIDER": "test-provider",
            "MODEL_TIMEOUT_SECONDS": "12", "MODEL_MAX_RESPONSE_BYTES": "65536",
        }
        log = StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)

        # The only key-loading subcase reads a distinctive dummy in an empty,
        # isolated environment. It never sees a caller's real environment/key.
        self.key_guard.stop()
        try:
            with patch.dict(os.environ, environment, clear=True):
                provider = HttpProvider.from_environment(transport=transport_for("http-complete"))
        finally:
            self.key_guard.start()
        result = record_response("IT-001.json", "test-hash", message(), provider.extract(message()))
        self.assertEqual(result["extraction"]["request_id"], "IT-001")
        self.assertEqual(result["next_action"], "review-request")
        self.assertEqual(result["response_source"], "test-provider/test-model")
        request = calls[-1]
        self.assertEqual(request["endpoint"], environment["MODEL_ENDPOINT"])
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {dummy}")
        self.assertEqual(request["timeout"], 12)
        self.assertEqual(request["max_response_bytes"], 65536)
        body = json.loads(request["body"])
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"], [{"role":"system", "content":SYSTEM_PROMPT}, {"role":"user", "content":message()}])
        self.assertEqual(body["response_format"], {"type":"json_schema", "json_schema":{
            "name":"access_request", "strict":True, "schema":SCHEMA}})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertFalse(body["stream"])
        self.assertNotIn(dummy, json.dumps(asdict(provider.config)))
        observations = [result, provider.request_evidence(message())]
        for fixture, code in [("http-refusal", "refusal"), ("http-incomplete", "incomplete")]:
            provider.transport = transport_for(fixture)
            record = record_response("IT-001.json", "test-hash", message(), provider.extract(message()))
            self.assert_error(record, code)
            observations.append(record)
        for fixture, retryable, code in [
            ("http-429", True, "http-429"), ("http-503", True, "http-503"),
            ("http-unauthorized", False, "http-401"), ("http-bad-envelope", False, "invalid-envelope"),
        ]:
            provider.transport = transport_for(fixture)
            with self.subTest(fixture=fixture), self.assertRaises(ProviderFailure) as caught:
                provider.extract(message())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.retryable, retryable)
            if fixture == "http-429":
                self.assertEqual(caught.exception.retry_after, 2)
            observations.append(str(caught.exception))
        for exception in (TimeoutError(dummy), ProviderUnavailable(f"transport echoed {dummy}"),
                          ProviderFailure("provider-unavailable", dummy, retryable=True)):
            provider.transport = Mock(side_effect=exception)
            with self.assertRaises(ProviderFailure) as caught:
                provider.extract(message())
            self.assertTrue(caught.exception.retryable)
            observations.append(str(caught.exception))
        # Producing settings must describe the actual injected HTTP adapter.
        from access_intake.repository import ProcessingSettings, Repository
        from dataclasses import replace
        from tempfile import TemporaryDirectory
        from pathlib import Path
        settings = ProcessingSettings.from_provider(provider)
        self.assertEqual(settings.model, "test-model")
        self.assertEqual(settings.endpoint, environment["MODEL_ENDPOINT"])
        provider.transport = transport_for("http-complete")
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite"
            with Repository(state) as repository:
                persisted = repository.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
                self.assertEqual(persisted["settings"], asdict(settings))
                self.assertNotIn(dummy, json.dumps(repository.history("IT-001.json")))
                with self.assertRaises(ValueError):
                    repository.intake([REQUESTS / "IT-001.json"], provider=provider,
                                      settings=replace(settings, model="does-not-match-adapter"))
                reconstructed = HttpProvider.from_settings(settings, environment={"API_KEY": dummy},
                                                           transport=transport_for("http-complete"))
                self.assertEqual(reconstructed.request_body(message()), provider.request_body(message()))
            self.assertNotIn(dummy.encode(), state.read_bytes())
        evidence = json.loads((ROOT / "deliverables/request-evidence.json").read_text())
        self.assertEqual(evidence["request"], provider.request_evidence(message())["request"])
        self.assertEqual(evidence["observation"]["response_origin"], "fake HTTP transport")
        self.assertNotIn(dummy, log.getvalue() + json.dumps(observations) + json.dumps(evidence))
        with self.assertRaises(ValueError):
            HttpProvider.from_environment(environment={"API_KEY": dummy}, transport=Mock())
