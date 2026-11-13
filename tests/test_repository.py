"""T31–T35: durable attempts, independent source oracles, reuse and refresh.

All extraction replies are saved fixtures. Config changes exercise fingerprint
invalidation, not live provider behavior. The adapter-binding subcase uses only
an injected fake transport and in-memory dummy credentials; os.environ key
access and network access remain blocked throughout.
"""

import hashlib
import json
import os
import shlex
import socket
import sqlite3
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable


ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "materials" / "requests"
MOCKS = ROOT / "tests" / "mock_responses"
LEDGER = json.loads((ROOT / "tests" / "expected_facts.json").read_text(encoding="utf-8"))
CORRECTION = "Correction: replace the end date with 2026-11-07."


def playback(*names):
    return MockProvider([MockOutcome(response_path=MOCKS / f"{name}.json") for name in names])


def unavailable(diagnostic="scripted outage"):
    return MockProvider([MockOutcome(error=ProviderUnavailable(diagnostic))])


class PersistenceContract(unittest.TestCase):
    def setUp(self):
        original_getitem = type(os.environ).__getitem__

        def guarded_getitem(environ, key):
            if key == "API_KEY":
                raise AssertionError("Persistence tests must not read API_KEY from os.environ")
            return original_getitem(environ, key)

        key_guard = patch.object(type(os.environ), "__getitem__", guarded_getitem)
        key_guard.start()
        self.addCleanup(key_guard.stop)
        for name in ("socket", "create_connection", "getaddrinfo"):
            guard = patch.object(socket, name, side_effect=AssertionError("Network denied in persistence tests"))
            guard.start()
            self.addCleanup(guard.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.state = self.directory / "state.sqlite"

    def open_repo(self, path=None):
        from access_intake.repository import Repository
        repository = Repository(path or self.state)
        self.addCleanup(repository.close)
        return repository

    def settings(self):
        from access_intake.repository import ProcessingSettings
        return ProcessingSettings()

    def assert_business(self, record, case="T02", *, end=None, origin="mock"):
        expected = LEDGER[case]
        facts = {**expected["scalars"]}
        if end is not None:
            facts["end_date"] = end
        self.assertIsNone(record["error"], record)
        self.assertEqual(record["document_id"], expected["document_id"])
        self.assertEqual(record["response_source"], origin)
        self.assertEqual([record[key] for key in ("status", "owner", "next_action")], expected["route"])
        for field, value in facts.items():
            self.assertEqual(record["extraction"][field], value, (case, field))

    def assert_error(self, record):
        self.assertIsNone(record["extraction"])
        self.assertEqual((record["status"], record["owner"], record["next_action"]),
                         ("error", "IT Service Desk", "retry-extraction"))
        self.assertEqual(record["error"]["code"], "provider-unavailable")
        self.assertTrue(record["error"]["message"].strip())

    def corrected_source(self):
        path = self.directory / "IT-001.json"
        path.write_text(json.dumps({"message": LEDGER["T02"]["message"] + " " + CORRECTION}), encoding="utf-8")
        return path

    def test_T31(self):
        repository = self.open_repo()
        settings = self.settings()
        provider = playback("complete")
        extract = provider.extract

        def observe_durable_input(source):
            # Another connection sees committed input/settings before the call.
            observer = self.open_repo()
            pending = observer.progress()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["work_state"], "running")
            self.assertEqual(pending[0]["settings"], asdict(settings))
            self.assertEqual(pending[0]["source_sha256"], hashlib.sha256((REQUESTS / "IT-001.json").read_bytes()).hexdigest())
            saved = observer.saved_attempt(pending[0]["attempt_id"])
            self.assertEqual(saved["source_bytes"], (REQUESTS / "IT-001.json").read_bytes())
            self.assertEqual(saved["settings"], asdict(settings))
            self.assertEqual(observer.snapshot()["extractions"], [])
            # A provider call must not monopolize the SQLite writer lock.
            with sqlite3.connect(self.state, timeout=0.1) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.rollback()
            return extract(source)

        provider.extract = Mock(side_effect=observe_durable_input)
        first = repository.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        self.assert_business(first)
        repeated = repository.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        self.assertEqual(repeated, first)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(repository.history("IT-001.json")), 1)
        self.assertEqual(len(repository.snapshot()["extractions"]), 1)
        self.assertEqual(len(repository.snapshot()["queue"]), 1)
        repository.close()
        reopened = self.open_repo()
        no_calls = playback()
        self.assertEqual(reopened.intake([REQUESTS / "IT-001.json"], provider=no_calls, settings=settings)[0], first)
        self.assertEqual(no_calls.calls, [])
        failing = unavailable()
        failed = reopened.intake([REQUESTS / "IT-002.json"], provider=failing, settings=settings)[0]
        self.assert_error(failed)
        self.assertEqual(reopened.intake([REQUESTS / "IT-002.json"], provider=failing, settings=settings)[0], failed)
        self.assertEqual(len(failing.calls), 1)

    def test_T32(self):
        repository = self.open_repo()
        settings = self.settings()
        provider = playback("complete", "changed-end")
        original = repository.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        self.assert_business(original)
        source = self.corrected_source()
        revised = repository.intake([source], provider=provider, settings=settings)[0]
        self.assert_business(revised, end="2026-11-07")
        self.assertNotEqual(revised["source_sha256"], original["source_sha256"])
        self.assertGreater(revised["revision"], original["revision"])
        self.assertEqual(revised["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(repository.history("IT-001.json"), [original, revised])
        snapshot = repository.snapshot()
        self.assertEqual(snapshot["extractions"], [revised])
        self.assertEqual(len(snapshot["queue"]), 1)
        for key in ("attempt_id", "revision", "source_sha256", "document_id"):
            self.assertEqual(snapshot["queue"][0][key], revised[key])

        # Separate state isolates a whitespace-only source revision.
        whitespace_repo = self.open_repo(self.directory / "whitespace.sqlite")
        provider = playback("complete", "complete")
        first = whitespace_repo.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        source.write_bytes(b" \n" + (REQUESTS / "IT-001.json").read_bytes() + b"\n")
        whitespace = whitespace_repo.intake([source], provider=provider, settings=settings)[0]
        self.assert_business(whitespace)
        self.assertNotEqual(whitespace["source_sha256"], first["source_sha256"])
        self.assertNotEqual(whitespace["attempt_id"], first["attempt_id"])
        self.assertEqual(len(whitespace_repo.history("IT-001.json")), 2)
        self.assertEqual(len(provider.calls), 2)

    def test_T33(self):
        repository = self.open_repo()
        settings = self.settings()
        provider = playback("complete", "production-write")
        originals = repository.intake([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], provider=provider, settings=settings)
        repository.intake([REQUESTS / "IT-002.json"], provider=provider, settings=settings)
        current = repository.snapshot()
        self.assertEqual(current["extractions"], originals)
        self.assert_business(current["extractions"][0])
        self.assert_business(current["extractions"][1], "T03")
        self.assertEqual(len(current["queue"]), 2)
        self.assertEqual(len(provider.calls), 2)

    def test_T34(self):
        repository = self.open_repo()
        settings = self.settings()
        original = repository.intake([REQUESTS / "IT-001.json"], provider=playback("complete"), settings=settings)[0]
        marker = "dummy-diagnostic-must-not-be-persisted"
        failing = unavailable(marker)
        factory = Mock(return_value=failing)
        failed = repository.retry(original["attempt_id"], provider_factory=factory)
        self.assert_error(failed)
        factory.assert_called_once_with(settings)
        self.assertEqual(failing.calls[0].message, LEDGER["T02"]["message"])
        self.assertNotEqual(failed["attempt_id"], original["attempt_id"])
        self.assertEqual(failed["source_sha256"], original["source_sha256"])
        self.assertEqual(repository.history("IT-001.json"), [original, failed])
        self.assertEqual(repository.snapshot()["extractions"], [failed])

        revised_settings = replace(settings, model="test-revised-model")
        source = self.corrected_source()
        revised_bytes = source.read_bytes()
        revised = repository.intake([source], provider=playback("changed-end"), settings=revised_settings)[0]
        self.assert_business(revised, end="2026-11-07")
        source.write_bytes(b"the current file must not supply saved retry input")
        repository.close()
        reopened = self.open_repo()
        failing = unavailable(marker)
        factory = Mock(return_value=failing)
        latest = reopened.retry(revised["attempt_id"], provider_factory=factory)
        factory.assert_called_once_with(revised_settings)
        self.assert_error(latest)
        self.assertEqual(failing.calls[0].message, LEDGER["T02"]["message"] + " " + CORRECTION)
        self.assertEqual(reopened.saved_attempt(revised["attempt_id"])["source_bytes"], revised_bytes)
        self.assertEqual(latest["source_sha256"], revised["source_sha256"])
        self.assertEqual(reopened.history("IT-001.json")[-2:], [revised, latest])
        self.assertIn(latest["attempt_id"], latest["retry_command"])
        command = shlex.split(latest["retry_command"])
        self.assertEqual(command[command.index("--state") + 1], str(self.state))
        older_factory = Mock(return_value=playback("complete"))
        historical = reopened.retry(original["attempt_id"], provider_factory=older_factory)
        older_factory.assert_called_once_with(settings)
        self.assert_business(historical)
        self.assertEqual(reopened.snapshot()["extractions"], [latest])
        history = reopened.history("IT-001.json")
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0], original)
        self.assertNotIn(marker, json.dumps(history))
        for path in self.directory.glob("*.sqlite*"):
            self.assertNotIn(marker.encode(), path.read_bytes())

    def test_T35(self):
        repository = self.open_repo()
        settings = self.settings()
        original = repository.intake([REQUESTS / "IT-001.json"], provider=playback("complete"), settings=settings)[0]
        changes = [
            {"model": "changed-model"}, {"prompt": settings.prompt + "\nPrompt version two."},
            {"schema": {**settings.schema, "description": "schema version two"}},
            {"request_settings": {"temperature": 0.2}}, {"extraction_version": "extraction-v2"},
            {"provider": "test-only-provider-variant"},
            {"endpoint": "https://example.invalid/v2/chat/completions"},
        ]
        previous = original
        for change in changes:
            with self.subTest(setting=next(iter(change))):
                settings = replace(settings, **change)
                provider = playback("complete")
                factory = Mock(return_value=provider)
                results = repository.refresh(settings=settings, provider_factory=factory)
                self.assertEqual(len(results), 1)
                refreshed = results[0]
                self.assert_business(refreshed)
                factory.assert_called_once_with(settings)
                self.assertEqual(len(provider.calls), 1)
                self.assertNotEqual(refreshed["extraction_fingerprint"], previous["extraction_fingerprint"])
                self.assertEqual(refreshed["settings"], asdict(settings))
                forbidden = Mock(side_effect=AssertionError("Unchanged refresh called the provider"))
                self.assertEqual(repository.refresh(settings=settings, provider_factory=forbidden), [])
                previous = refreshed

        producing_settings = previous["extraction_settings"]
        forbidden = Mock(side_effect=AssertionError("Policy-only refresh called the provider"))
        settings = replace(settings, policy_version="test-version-same-office-rules")
        rerouted = repository.refresh(settings=settings, provider_factory=forbidden)[0]
        self.assert_business(rerouted)
        self.assertEqual(rerouted["extraction"], previous["extraction"])
        self.assertEqual(rerouted["extraction_fingerprint"], previous["extraction_fingerprint"])
        self.assertEqual(rerouted["extraction_settings"], producing_settings)
        self.assertEqual(rerouted["settings"], asdict(settings))
        self.assertNotEqual(rerouted["attempt_id"], previous["attempt_id"])

        from access_intake.policy import route
        def test_only_router(facts, source, document_id):
            return {**route(facts, source, document_id), "owner": "Test-only Review Desk"}

        settings = replace(settings, policy_version="test-only-routing-change")
        synthetic = repository.refresh(settings=settings, provider_factory=forbidden, router=test_only_router)[0]
        self.assertEqual(synthetic["owner"], "Test-only Review Desk")
        self.assertEqual(synthetic["extraction"], previous["extraction"])
        self.assertEqual(synthetic["extraction_settings"], producing_settings)
        self.assertEqual(repository.history("IT-001.json")[0], original)
        self.assertEqual(len(repository.history("IT-001.json")), 10)

        failed = repository.intake([REQUESTS / "IT-002.json"], provider=unavailable(), settings=settings)[0]
        self.assert_error(failed)
        repository.refresh(settings=replace(settings, policy_version="test-only-next-policy"), provider_factory=forbidden)
        current = {row["document_id"]: row for row in repository.snapshot()["extractions"]}
        self.assertEqual(current["IT-002.json"], failed, "Policy refresh cannot invent a valid extraction")
        with self.assertRaises(ValueError):
            replace(settings, request_settings={"Authorization": "must-not-be-stored"})

        # Bind recorded producing settings to the actual HTTP request, still offline.
        from access_intake.http_provider import HttpProvider, HttpReply
        from access_intake.repository import ProcessingSettings
        captured = []
        reply = json.loads((MOCKS / "http-complete.json").read_text(encoding="utf-8"))
        def fake_transport(**request):
            captured.append(request)
            return HttpReply(reply["status"], reply["headers"], json.dumps(reply["body"]).encode())

        dummy = "dummy-in-memory-T35-only"
        adapter = HttpProvider.from_environment(environment={
            "API_KEY": dummy, "MODEL_ENDPOINT": "https://example.invalid/v1/chat/completions",
            "MODEL_PROVIDER": "test-provider", "MODEL_ID": "test-model",
        }, transport=fake_transport)
        bound = ProcessingSettings.from_provider(adapter)
        http_repository = self.open_repo(self.directory / "http.sqlite")
        measured = http_repository.intake([REQUESTS / "IT-001.json"], provider=adapter, settings=bound)[0]
        self.assert_business(measured, origin="test-provider/test-model-snapshot")
        self.assertEqual(measured["settings"], asdict(bound))
        with self.assertRaises(ValueError):
            http_repository.intake([REQUESTS / "IT-001.json"], provider=adapter,
                                   settings=replace(bound, model="mismatched-model"))
        updated = replace(bound, prompt=bound.prompt + "\nSaved prompt revision.",
                          schema={**bound.schema, "description": "saved schema revision"},
                          request_settings={**bound.request_settings, "temperature": 0.2})
        http_repository.refresh(settings=updated, provider_factory=lambda saved_settings: HttpProvider.from_settings(
            saved_settings, environment={"API_KEY": dummy}, transport=fake_transport))
        body = json.loads(captured[-1]["body"])
        self.assertEqual(body["messages"][0]["content"], updated.prompt)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], updated.schema)
        self.assertEqual(body["temperature"], 0.2)
        self.assertNotIn(dummy, json.dumps(http_repository.history("IT-001.json")))
        for path in self.directory.glob("http.sqlite*"):
            self.assertNotIn(dummy.encode(), path.read_bytes())
