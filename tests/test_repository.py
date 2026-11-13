"""T31–T35: durable state, source/settings reuse, history and saved retries."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from support import OfflineTest, MOCKS, REQUESTS, message, playback
from access_intake.providers import MockOutcome, MockProvider, ProviderUnavailable


class Persistence(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.db = self.directory / "state.sqlite"

    def setup_repo(self):
        from access_intake.repository import Repository, ProcessingSettings
        repo = Repository(self.db)
        self.addCleanup(repo.close)
        return repo, ProcessingSettings()

    def corrected(self):
        path = self.directory / "IT-001.json"
        path.write_text(json.dumps({"message": message() + " Correction: replace the end date with 2026-11-07."}), encoding="utf-8")
        return path

    def assert_owner(self, record, end="2026-11-06"):
        self.assertIsNone(record["error"])
        self.assertEqual(record["extraction"]["requester"], "Maya Chen")
        self.assertEqual(record["extraction"]["end_date"], end)
        self.assertEqual(record["next_action"], "review-request")

    def test_T31(self):
        repo, settings = self.setup_repo()
        provider = playback("complete")
        extract = provider.extract

        def observe_saved_work(source):
            # A different connection sees durable input/settings before the call,
            # and can take a write lock while the provider is running.
            with sqlite3.connect(self.db, timeout=0.1) as observer:
                raw, stored_settings, state = observer.execute(
                    "SELECT source_bytes, settings_json, work_state FROM attempts"
                ).fetchone()
                self.assertEqual(raw, (REQUESTS / "IT-001.json").read_bytes())
                self.assertEqual(json.loads(stored_settings), asdict(settings))
                self.assertEqual(state, "running")
                observer.execute("BEGIN IMMEDIATE")
                observer.rollback()
            return extract(source)

        provider.extract = Mock(side_effect=observe_saved_work)
        first = repo.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        self.assert_owner(first)
        reused = repo.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        self.assertEqual(first, reused)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(repo.history("IT-001.json")), 1)
        self.assertEqual(len(repo.snapshot()["extractions"]), 1)
        self.assertEqual(len(repo.snapshot()["queue"]), 1)
        repo.close()
        from access_intake.repository import Repository
        reopened = Repository(self.db)
        self.addCleanup(reopened.close)
        no_calls = playback()
        self.assertEqual(reopened.intake([REQUESTS / "IT-001.json"], provider=no_calls, settings=settings)[0], first)
        self.assertEqual(no_calls.calls, [])
        bad_path = self.directory / "IT-002.json"
        bad_path.write_bytes((REQUESTS / "IT-002.json").read_bytes())
        failing = MockProvider([MockOutcome(error=ProviderUnavailable("outage"))])
        failed = reopened.intake([bad_path], provider=failing, settings=settings)[0]
        self.assert_error(failed, "provider-unavailable")
        again = reopened.intake([bad_path], provider=failing, settings=settings)[0]
        self.assertEqual(again, failed)
        self.assertEqual(len(failing.calls), 1)

    def test_T32(self):
        repo, settings = self.setup_repo()
        provider = playback("complete", "changed-end", "complete")
        old = repo.intake([REQUESTS / "IT-001.json"], provider=provider, settings=settings)[0]
        path = self.corrected()
        new = repo.intake([path], provider=provider, settings=settings)[0]
        self.assert_owner(new, "2026-11-07")
        self.assertNotEqual(new["source_sha256"], old["source_sha256"])
        self.assertGreater(new["revision"], old["revision"])
        self.assertEqual(new["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(repo.history("IT-001.json"), [old, new])
        snapshot = repo.snapshot()
        self.assertEqual(snapshot["extractions"], [new])
        self.assertEqual(snapshot["queue"][0]["attempt_id"], new["attempt_id"])
        self.assertEqual(snapshot["queue"][0]["source_sha256"], new["source_sha256"])
        path.write_bytes(b" \n" + (REQUESTS / "IT-001.json").read_bytes())
        whitespace = repo.intake([path], provider=provider, settings=settings)[0]
        self.assert_owner(whitespace)
        self.assertNotEqual(whitespace["source_sha256"], old["source_sha256"])
        self.assertEqual(len(repo.history("IT-001.json")), 3)

    def test_T33(self):
        repo, settings = self.setup_repo()
        provider = playback("complete", "production-write")
        originals = repo.intake([REQUESTS / "IT-001.json", REQUESTS / "IT-002.json"], provider=provider, settings=settings)
        repo.intake([REQUESTS / "IT-002.json"], provider=provider, settings=settings)
        self.assertEqual(repo.snapshot()["extractions"], originals)
        self.assertEqual(len(provider.calls), 2)

    def test_T34(self):
        repo, settings = self.setup_repo()
        old = repo.intake([REQUESTS / "IT-001.json"], provider=playback("complete"), settings=settings)[0]
        secret = "dummy-not-a-key-should-not-be-persisted"
        failing = MockProvider([MockOutcome(error=ProviderUnavailable(secret))])
        factory = Mock(return_value=failing)
        failed = repo.retry(old["attempt_id"], provider_factory=factory)
        self.assert_error(failed, "provider-unavailable")
        self.assertNotEqual(old["attempt_id"], failed["attempt_id"])
        self.assertEqual(old["source_sha256"], failed["source_sha256"])
        factory.assert_called_once_with(settings)
        self.assertEqual(failing.calls[0].message, message())
        self.assertEqual(repo.history("IT-001.json"), [old, failed])
        self.assertEqual(repo.snapshot()["extractions"], [failed])
        revised_path = self.corrected()
        revised = repo.intake([revised_path], provider=playback("changed-end"), settings=settings)[0]
        revised_path.write_bytes(b"deleted source is not consulted on retry")
        failing = MockProvider([MockOutcome(error=ProviderUnavailable(secret))])
        latest = repo.retry(revised["attempt_id"], provider_factory=lambda saved_settings: failing)
        self.assertEqual(latest["source_sha256"], revised["source_sha256"])
        self.assertEqual(failing.calls[0].message, message() + " Correction: replace the end date with 2026-11-07.")
        self.assertEqual(repo.history("IT-001.json")[-2:], [revised, latest])
        self.assertIn(latest["attempt_id"], latest["retry_command"])
        # Retrying an older revision may add history but cannot replace the latest.
        older_retry = repo.retry(old["attempt_id"], provider_factory=lambda _: playback("complete"))
        self.assert_owner(older_retry)
        self.assertEqual(repo.snapshot()["extractions"], [latest])
        self.assertNotIn(secret, json.dumps(repo.history("IT-001.json")))
        self.assertNotIn(secret.encode(), self.db.read_bytes())

    def test_T35(self):
        repo, settings = self.setup_repo()
        original = repo.intake([REQUESTS / "IT-001.json"], provider=playback("complete"), settings=settings)[0]
        changes = [
            {"model": "changed-model"}, {"prompt": settings.prompt + "\nVersion two."},
            {"schema": {**settings.schema, "description": "version two"}},
            {"request_settings": {"temperature": 0.2}}, {"extraction_version": "v2"},
            {"endpoint": "https://example.invalid/v2/chat/completions"},
        ]
        previous = original
        for change in changes:
            with self.subTest(change=next(iter(change))):
                settings = replace(settings, **change)
                provider = playback("complete")
                factory = Mock(return_value=provider)
                refreshed = repo.refresh(settings=settings, provider_factory=factory)[0]
                factory.assert_called_once_with(settings)
                self.assertEqual(len(provider.calls), 1)
                self.assertNotEqual(previous["extraction_fingerprint"], refreshed["extraction_fingerprint"])
                self.assertEqual(refreshed["settings"], asdict(settings))
                self.assert_owner(refreshed)
                unchanged = Mock(side_effect=AssertionError("unchanged refresh called provider"))
                self.assertEqual(repo.refresh(settings=settings, provider_factory=unchanged), [])
                previous = refreshed
        producing_settings = previous["extraction_settings"]
        settings = replace(settings, policy_version="test-only-policy-v2")
        from access_intake.policy import route

        def test_only_route(facts, source, document_id):
            result = route(facts, source, document_id)
            return {**result, "owner": "Test-only human review desk"}

        never = Mock(side_effect=AssertionError("policy refresh called provider"))
        rerouted = repo.refresh(settings=settings, provider_factory=never, router=test_only_route)[0]
        self.assertEqual(rerouted["extraction"], previous["extraction"])
        self.assertEqual(rerouted["owner"], "Test-only human review desk")
        self.assertEqual(rerouted["extraction_settings"], producing_settings)
        self.assertEqual(rerouted["settings"]["policy_version"], "test-only-policy-v2")
        self.assertEqual(rerouted["response_source"], "mock")
        self.assertEqual(repo.history("IT-001.json")[0], original)
        self.assertEqual(len(repo.history("IT-001.json")), 8)
        # A policy change cannot conjure facts from an extraction error.
        failed = repo.intake([REQUESTS / "IT-002.json"], provider=MockProvider([
            MockOutcome(error=ProviderUnavailable("outage"))]), settings=settings)[0]
        self.assert_error(failed, "provider-unavailable")
        repo.refresh(settings=replace(settings, policy_version="test-only-policy-v3"), provider_factory=never)
        current = {r["document_id"]: r for r in repo.snapshot()["extractions"]}
        self.assertEqual(current["IT-002.json"], failed)
        with self.assertRaises(ValueError):
            replace(settings, request_settings={"Authorization": "must-not-be-saved"})
